"""Neural distributional post-processing on a GPU (CUDA on the RTX 5090 desktop, or XPU on the homeserver's Intel Arc), for the method comparison.

DRN (distributional regression network, Rasp & Lerch 2018): an MLP outputs the
mean and standard deviation of a normal distribution, trained on its closed-form
CRPS; quantiles are taken from the normal truncated at zero.
BQN (Bernstein quantile network, Bremnes 2020): an MLP outputs non-decreasing
Bernstein-polynomial coefficients, so the quantile function is monotone; trained
on pinball loss over 99 levels.
Inputs are standardized per fold (NaN -> 0) plus a missing-indicator per model,
so eras with different model coverage share one network. Each method averages 3
seeds. Runs in var/nn-venv.
"""
import math
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

from compare_common import LEVELS, OUT, TARGETS, crps, load, save

DEVICE = 'cuda' if torch.cuda.is_available() else 'xpu' if hasattr(torch, 'xpu') and torch.xpu.is_available() else 'cpu'
DEGREE = 12
TRAIN_LEVELS = torch.linspace(0.01, 0.99, 99)
SEEDS = 3
BATCH, EPOCHS, PATIENCE = 2048, 60, 6


def design(t, features, train_mask):
    X = t[features].to_numpy('float32')
    mu = np.nanmean(X[train_mask], axis=0); sd = np.nanstd(X[train_mask], axis=0) + 1e-6
    Z = np.nan_to_num((X - mu) / sd)
    groups = sorted({f.split('_')[0] for f in features})
    missing = np.stack([t[[f for f in features if f.split('_')[0] == g]].isna().all(axis=1).to_numpy('float32') for g in groups], axis=1)
    return np.concatenate([Z, missing], axis=1).astype('float32')


class Net(nn.Module):
    def __init__(self, n_in, n_out):
        super().__init__()
        self.body = nn.Sequential(nn.Linear(n_in, 256), nn.GELU(), nn.Dropout(0.1), nn.Linear(256, 128), nn.GELU(), nn.Dropout(0.1),
                                  nn.Linear(128, n_out))

    def forward(self, x):
        return self.body(x)


def drn_loss(out, y):
    mu, sigma = out[:, 0], nn.functional.softplus(out[:, 1]) + 1e-3
    z = (y - mu) / sigma
    normal = torch.distributions.Normal(0.0, 1.0)
    return (sigma * (z * (2 * normal.cdf(z) - 1) + 2 * torch.exp(normal.log_prob(z)) - 1 / math.sqrt(math.pi))).mean()


def bernstein(levels, degree=DEGREE):
    k = torch.arange(degree + 1, dtype=torch.float32)
    comb = torch.exp(torch.lgamma(torch.tensor(degree + 1.0)) - torch.lgamma(k + 1) - torch.lgamma(degree - k + 1))
    tau = levels[:, None]
    return comb * tau ** k * (1 - tau) ** (degree - k)  # [n_levels, degree+1]


def bqn_quantiles(out, basis):
    coef = torch.cumsum(torch.cat([out[:, :1], nn.functional.softplus(out[:, 1:])], dim=1), dim=1)  # non-decreasing
    return coef @ basis.T


def fit(kind, Xtr, ytr, Xva, yva, seed):
    torch.manual_seed(seed)
    n_out = 2 if kind == 'drn' else DEGREE + 1
    net = Net(Xtr.shape[1], n_out).to(DEVICE)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    basis = bernstein(TRAIN_LEVELS).to(DEVICE)
    lv = TRAIN_LEVELS.to(DEVICE)
    Xtr_t, ytr_t = torch.tensor(Xtr, device=DEVICE), torch.tensor(ytr, device=DEVICE)
    Xva_t, yva_t = torch.tensor(Xva, device=DEVICE), torch.tensor(yva, device=DEVICE)

    def loss_fn(x, y):
        out = net(x)
        if kind == 'drn':
            return drn_loss(out, y)
        q = bqn_quantiles(out, basis)
        d = y[:, None] - q
        return torch.maximum(lv * d, (lv - 1) * d).mean() * 2

    best, best_state, wait = float('inf'), None, 0
    n = len(Xtr_t)
    for epoch in range(EPOCHS):
        net.train()
        perm = torch.randperm(n, device=DEVICE)
        for i in range(0, n, BATCH):
            idx = perm[i:i + BATCH]
            opt.zero_grad(); loss = loss_fn(Xtr_t[idx], ytr_t[idx]); loss.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            val = float(sum(loss_fn(Xva_t[i:i + 8192], yva_t[i:i + 8192]) * len(Xva_t[i:i + 8192]) for i in range(0, len(Xva_t), 8192)) / len(Xva_t))
        if val < best - 1e-4:
            best, best_state, wait = val, {k: v.detach().clone() for k, v in net.state_dict().items()}, 0
        else:
            wait += 1
            if wait >= PATIENCE:
                break
    net.load_state_dict(best_state)
    return net, epoch + 1, best


def predict(kind, net, X):
    net.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), 8192):
            o = net(torch.tensor(X[i:i + 8192], device=DEVICE))
            if kind == 'drn':
                mu, sigma = o[:, 0], nn.functional.softplus(o[:, 1]) + 1e-3
                normal = torch.distributions.Normal(0.0, 1.0)
                p0 = normal.cdf(-mu / sigma)
                lv = torch.tensor(LEVELS, dtype=torch.float32, device=DEVICE)
                q = mu[:, None] + sigma[:, None] * normal.icdf((p0[:, None] + lv[None, :] * (1 - p0[:, None])).clamp(1e-6, 1 - 1e-6))
            else:
                q = bqn_quantiles(o, bernstein(torch.tensor(LEVELS, dtype=torch.float32)).to(DEVICE))
            out.append(q.cpu().numpy())
    return np.concatenate(out)


def main():
    Path(OUT).mkdir(parents=True, exist_ok=True)
    t, features, fold = load()
    months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    print(f'device {DEVICE}; {len(t)} rows, {len(features)} features', flush=True)
    for kind in ('bqn', 'drn'):
        preds = {}
        for target in TARGETS:
            y = t[target].to_numpy('float32')
            q = np.zeros((len(t), len(LEVELS)))
            for k in np.unique(fold):
                train = fold != k
                X = design(t, features, train)
                val = train & (months % 7 == 3)  # held-out months inside the training fold for early stopping
                fit_rows = train & ~val
                started = time.time()
                qs = []
                for seed in range(SEEDS):
                    net, epochs, best = fit(kind, X[fit_rows], y[fit_rows], X[val], y[val], seed)
                    qs.append(predict(kind, net, X[fold == k]))
                q[fold == k] = np.sort(np.maximum(np.mean(qs, axis=0), 0), axis=1)
                print(f'  {kind} {target} fold {k}: {epochs} epochs (last seed), val {best:.3f}, {time.time() - started:.0f} s', flush=True)
            preds[target] = q
            print(f'{kind} {target}: CRPS {crps(y, q).mean():.3f}', flush=True)
        save(kind, t, preds)


if __name__ == '__main__':
    main()
