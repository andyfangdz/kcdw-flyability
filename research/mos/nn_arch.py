"""Neural architecture comparison for the KCDW calibration (research).

Every network ends in the same Bernstein quantile (BQN) head, trained on pinball loss over
99 levels with early stopping on held-out months inside each training fold, 3 seeds averaged,
on the method-comparison folds (compare_common), so only the architecture differs:
  mlp      the current 256-128 MLP on standardized inputs plus per-model missing flags
  resmlp   pre-norm residual MLP, width 512, 4 blocks
  plr      periodic (sin/cos with learned frequencies) embedding of every input, then an MLP
  ftt      FT-Transformer: one token per input (value x weight + bias, or a learned
           missing token), a CLS token, 3 attention layers
  sources  one token per forecast model (its inputs through a per-model linear map) plus a
           time token; attention across models with unavailable models masked out
  dayseq   each (date, lead day) as a sequence of its 16 hours: a per-hour MLP encoder, then
           a transformer along the hours, so neighbouring hours inform each other
Writes out-of-fold quantiles to var/mos/compare/nn_<name>.parquet and var/mos/nn_arch.json.
Usage: var/nn-venv/bin/python research/mos/nn_arch.py [--archs mlp ftt ...]
"""
import argparse
import json
import math
import os
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

from compare_common import LEVELS, TARGETS, crps, load, save
from compare_nn import DEGREE, TRAIN_LEVELS, bernstein, bqn_quantiles

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
SEEDS = 3
EPOCHS, PATIENCE = 80, 8
SOURCES = ['gfs', 'gefs', 'ifs', 'aifs', 'hrrr', 'wn2', 'wn3']
TIME = ['hour_sin', 'hour_cos', 'doy_sin', 'doy_cos', 'lead_h']


def standardize(t, features, train):
    X = t[features].to_numpy('float32')
    mu, sd = np.nanmean(X[train], axis=0), np.nanstd(X[train], axis=0) + 1e-6
    M = np.isnan(X).astype('float32')
    return np.nan_to_num((X - mu) / sd).astype('float32'), M


def group_flags(M, features):
    groups = sorted({f.split('_')[0] for f in features})
    return np.stack([M[:, [i for i, f in enumerate(features) if f.split('_')[0] == g]].min(axis=1) for g in groups], axis=1)


class Head(nn.Module):
    def __init__(self, d):
        super().__init__()
        self.out = nn.Linear(d, DEGREE + 1)

    def forward(self, h):
        return self.out(h)


class MLP(nn.Module):
    def __init__(self, n_in, **_):
        super().__init__()
        self.body = nn.Sequential(nn.Linear(n_in, 256), nn.GELU(), nn.Dropout(0.1), nn.Linear(256, 128), nn.GELU(), nn.Dropout(0.1))
        self.head = Head(128)

    def forward(self, x, m):
        return self.head(self.body(x))


class ResMLP(nn.Module):
    def __init__(self, n_in, width=512, blocks=4, **_):
        super().__init__()
        self.inp = nn.Linear(n_in, width)
        self.blocks = nn.ModuleList(nn.Sequential(nn.LayerNorm(width), nn.Linear(width, 2 * width), nn.GELU(), nn.Dropout(0.15), nn.Linear(2 * width, width))
                                    for _ in range(blocks))
        self.norm = nn.LayerNorm(width)
        self.head = Head(width)

    def forward(self, x, m):
        h = self.inp(x)
        for block in self.blocks:
            h = h + block(h)
        return self.head(self.norm(h))


class PLR(nn.Module):
    """Periodic embeddings (Gorishniy et al. 2022): per-feature learned frequencies, sin/cos, linear, ReLU."""

    def __init__(self, n_in, n_feat, k=12, d=8, sigma=0.5, **_):
        super().__init__()
        self.n_feat = n_feat
        self.freq = nn.Parameter(torch.randn(n_feat, k) * sigma)
        self.w = nn.Parameter(torch.randn(n_feat, 2 * k, d) / math.sqrt(2 * k))
        self.b = nn.Parameter(torch.zeros(n_feat, d))
        n_rest = n_in - n_feat
        self.mlp = nn.Sequential(nn.Linear(n_feat * d + n_rest, 512), nn.GELU(), nn.Dropout(0.15), nn.Linear(512, 256), nn.GELU(), nn.Dropout(0.1))
        self.head = Head(256)

    def forward(self, x, m):
        v = x[:, :self.n_feat, None] * self.freq[None] * 2 * math.pi
        e = torch.relu(torch.einsum('bfk,fkd->bfd', torch.cat([torch.sin(v), torch.cos(v)], dim=-1), self.w) + self.b)
        return self.head(self.mlp(torch.cat([e.flatten(1), x[:, self.n_feat:]], dim=1)))


class FTT(nn.Module):
    def __init__(self, n_in, n_feat, d=32, layers=3, heads=4, **_):
        super().__init__()
        self.n_feat = n_feat
        self.w = nn.Parameter(torch.randn(n_feat, d) * 0.1)
        self.b = nn.Parameter(torch.zeros(n_feat, d))
        self.missing = nn.Parameter(torch.randn(n_feat, d) * 0.1)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        layer = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.1, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers)
        self.norm = nn.LayerNorm(d)
        self.head = Head(d)

    def forward(self, x, m):
        xf, mf = x[:, :self.n_feat, None], m[:, :self.n_feat, None]
        tok = (1 - mf) * (xf * self.w + self.b) + mf * self.missing
        h = self.enc(torch.cat([self.cls.expand(len(x), -1, -1), tok], dim=1))
        return self.head(self.norm(h[:, 0]))


class Sources(nn.Module):
    """One token per forecast model plus a time token; attention across the models that exist for that hour."""

    def __init__(self, n_in, groups, d=96, layers=3, heads=4, **_):
        super().__init__()
        self.groups = groups  # list of index lists into x, one per token (time token last)
        self.enc_in = nn.ModuleList(nn.Sequential(nn.Linear(len(g), d), nn.GELU(), nn.Linear(d, d)) for g in groups)
        self.kind = nn.Parameter(torch.randn(len(groups), d) * 0.1)
        layer = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.1, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers)
        self.norm = nn.LayerNorm(d)
        self.head = Head(d)

    def forward(self, x, m):
        toks, absent = [], []
        for i, (g, f) in enumerate(zip(self.groups, self.enc_in)):
            toks.append(f(x[:, g]) + self.kind[i])
            absent.append(m[:, g].min(dim=1).values > 0.5)
        tok = torch.stack(toks, dim=1)
        pad = torch.stack(absent, dim=1)
        pad[:, -1] = False  # the time token is always present
        h = self.enc(tok, src_key_padding_mask=pad)
        keep = (~pad).float()[..., None]
        return self.head(self.norm((h * keep).sum(1) / keep.sum(1)))


class DaySeq(nn.Module):
    """Input [batch, 16 hours, features]; per-hour encoder then a transformer along the hours."""

    def __init__(self, n_in, d=192, layers=3, heads=4, **_):
        super().__init__()
        self.encode = nn.Sequential(nn.Linear(n_in, 384), nn.GELU(), nn.Dropout(0.1), nn.Linear(384, d))
        self.pos = nn.Parameter(torch.randn(16, d) * 0.1)
        layer = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.1, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers)
        self.norm = nn.LayerNorm(d)
        self.head = Head(d)

    def forward(self, x, present):
        h = self.enc(self.encode(x) + self.pos, src_key_padding_mask=~present)
        return self.head(self.norm(h))


def pinball(out, y, basis, lv):
    q = bqn_quantiles(out, basis)
    d = y[:, None] - q
    return torch.maximum(lv * d, (lv - 1) * d).mean(dim=1) * 2


def train_net(make, arrays, y, fit, val, seed, batch):
    """arrays: tensors indexed on the first axis (rows or day-groups). y: [n] or [n, 16] with NaN for missing labels."""
    torch.manual_seed(seed); np.random.seed(seed)
    net = make().to(DEVICE)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, EPOCHS)
    basis, lv = bernstein(TRAIN_LEVELS).to(DEVICE), TRAIN_LEVELS.to(DEVICE)
    dev = [torch.tensor(a, device=DEVICE) for a in arrays]
    yt = torch.tensor(y, device=DEVICE)
    fit_idx, val_idx = torch.tensor(np.flatnonzero(fit), device=DEVICE), torch.tensor(np.flatnonzero(val), device=DEVICE)

    def loss_on(idx):
        out = net(*[a[idx] for a in dev])
        target = yt[idx]
        if target.dim() == 2:  # day sequences: flatten hours, drop missing labels
            ok = ~torch.isnan(target)
            return pinball(out[ok], target[ok], basis, lv)
        return pinball(out, target, basis, lv)

    best, best_state, wait = float('inf'), None, 0
    for epoch in range(EPOCHS):
        net.train()
        perm = fit_idx[torch.randperm(len(fit_idx), device=DEVICE)]
        for i in range(0, len(perm), batch):
            opt.zero_grad()
            with torch.autocast('cuda', dtype=torch.bfloat16, enabled=DEVICE == 'cuda'):
                loss = loss_on(perm[i:i + batch]).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
        sched.step()
        net.eval()
        with torch.no_grad(), torch.autocast('cuda', dtype=torch.bfloat16, enabled=DEVICE == 'cuda'):
            v = torch.cat([loss_on(val_idx[i:i + 8192]).float() for i in range(0, len(val_idx), 8192)]).mean().item()
        if v < best - 1e-4:
            best, best_state, wait = v, {k: x.detach().clone() for k, x in net.state_dict().items()}, 0
        else:
            wait += 1
            if wait >= PATIENCE:
                break
    net.load_state_dict(best_state)
    net.eval()
    return net, epoch + 1, best


def predict(net, arrays, idx):
    basis = bernstein(torch.tensor(LEVELS, dtype=torch.float32)).to(DEVICE)
    dev = [torch.tensor(a[idx], device=DEVICE) for a in arrays]
    out = []
    with torch.no_grad():
        for i in range(0, len(idx), 4096):
            o = net(*[a[i:i + 4096] for a in dev]).float()
            q = bqn_quantiles(o.reshape(-1, o.shape[-1]), basis).reshape(*o.shape[:-1], len(LEVELS))
            out.append(q.cpu().numpy())
    return np.concatenate(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archs', nargs='+', default=['mlp', 'resmlp', 'plr', 'ftt', 'sources', 'dayseq'])
    args = parser.parse_args()
    t, features, fold = load()
    t = t.reset_index(drop=True)
    months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    afternoon = t.hour.between(13, 16).to_numpy()
    n_feat = len(features)
    token_groups = [[i for i, f in enumerate(features) if f.split('_')[0] == s] for s in SOURCES]
    token_groups = [g for g in token_groups if g] + [[features.index(f) for f in TIME if f in features]]
    # Day sequences: one group per (date, lead day), hour slot = hour - 6.
    gid, keys = pd.factorize(pd.MultiIndex.from_arrays([t.date, t.lead_day]))
    slot = (t.hour - 6).to_numpy()
    n_groups = len(keys)
    g_fold = np.zeros(n_groups, int); g_fold[gid] = fold
    g_month = np.zeros(n_groups, int); g_month[gid] = months
    results = json.load(open('var/mos/nn_arch.json')) if os.path.exists('var/mos/nn_arch.json') else {}
    for arch in args.archs:
        started = time.time()
        preds = {}
        for target in TARGETS:
            y = t[target].to_numpy('float32')
            q = np.zeros((len(t), len(LEVELS)), 'float32')
            for k in np.unique(fold):
                train = fold != k
                X, M = standardize(t, features, train)
                flags = group_flags(M, features)
                xin = np.concatenate([X, flags], axis=1)
                min_ = np.concatenate([M, flags], axis=1)
                if arch == 'dayseq':
                    Xg = np.zeros((n_groups, 16, xin.shape[1]), 'float32'); Xg[gid, slot] = xin
                    present = np.zeros((n_groups, 16), bool); present[gid, slot] = True
                    yg = np.full((n_groups, 16), np.nan, 'float32'); yg[gid, slot] = y
                    g_train = g_fold != k
                    fit, val = g_train & (g_month % 7 != 3), g_train & (g_month % 7 == 3)
                    arrays, target_y, test_idx, batch = (Xg, present), yg, np.flatnonzero(g_fold == k), 256
                    make = lambda: DaySeq(xin.shape[1])
                else:
                    fit, val = train & (months % 7 != 3), train & (months % 7 == 3)
                    arrays, target_y, test_idx, batch = (xin, min_), y, np.flatnonzero(fold == k), 1024
                    make = {'mlp': lambda: MLP(xin.shape[1]), 'resmlp': lambda: ResMLP(xin.shape[1]), 'plr': lambda: PLR(xin.shape[1], n_feat),
                            'ftt': lambda: FTT(xin.shape[1], n_feat), 'sources': lambda: Sources(xin.shape[1], token_groups)}[arch]
                qs = []
                for seed in range(SEEDS):
                    net, epochs, best = train_net(make, arrays, target_y, fit, val, seed, batch)
                    qs.append(predict(net, arrays, test_idx))
                    del net
                qk = np.mean(qs, axis=0)
                if arch == 'dayseq':
                    rows = np.flatnonzero(fold == k)
                    lookup = {g: i for i, g in enumerate(test_idx)}
                    qk = qk[[lookup[g] for g in gid[rows]], slot[rows]]
                    q[rows] = qk
                else:
                    q[fold == k] = qk
                print(f'  {arch} {target} fold {k}: {epochs} epochs (last seed), val {best:.3f}, {time.time() - started:.0f} s', flush=True)
            q = np.sort(np.maximum(q, 0), axis=1)
            preds[target] = q
            c = crps(y, q)
            results.setdefault(arch, {})[target] = {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[afternoon].mean()), 4),
                                                   'mae_p50': round(float(np.abs(q[:, 9] - y).mean()), 3),
                                                   'cover_10_90': round(float(np.mean((y >= q[:, 1]) & (y <= q[:, 17]))), 3)}
            print(f'{arch} {target}: {results[arch][target]}', flush=True)
        results[arch]['minutes'] = round((time.time() - started) / 60, 1)
        save(f'nn_{arch}', t, preds)
        json.dump(results, open('var/mos/nn_arch.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
