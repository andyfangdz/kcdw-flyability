"""Does data from more airports help at KCDW? Scaling curve for a regularized MLP and the fast XGBoost (research).

Trains on the 1, 4, 11 and 22 airports nearest KCDW (all five month-interleaved folds, gust) and
scores KCDW only. The MLP uses the settings the fold-0 diagnosis favoured (diag_nn.py: lr 3e-4,
weight decay 1e-2, dropout 0.3), validation on held-out months every 200 updates with early
stopping, 3 seeds averaged. Also reports how many independent days each set contains.
Writes var/mos-multi/scaling_nn.json.
"""
import json
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

import multi_train as mt
import nn_arch as na
import nn_multi as nm
from compare_common import LEVELS, crps
from compare_nn import TRAIN_LEVELS, bernstein

SEEDS = 3
CHECK, PATIENCE, MAX_EPOCHS = 200, 6, 8


def train_mlp(X, y, s, fit, val, n_st, seed):
    torch.manual_seed(seed)
    net = nm.SiteMLP(X.shape[1], n_st).cuda()
    for m in net.body:
        if isinstance(m, nn.Dropout):
            m.p = 0.3
    opt = torch.optim.AdamW(net.parameters(), lr=3e-4, weight_decay=1e-2)
    basis, lv = bernstein(TRAIN_LEVELS).cuda(), TRAIN_LEVELS.cuda()
    fit_i, val_i = torch.tensor(np.flatnonzero(fit), device='cuda'), torch.tensor(np.flatnonzero(val), device='cuda')
    batch = 4096 if len(fit_i) > 400_000 else 1024

    def vloss():
        net.eval()
        with torch.no_grad():
            v = torch.cat([na.pinball(net(X[i], None, s[i]), y[i], basis, lv) for i in val_i.split(16384)]).mean().item()
        net.train()
        return v

    best, state, wait, step = 9e9, None, 0, 0
    for _ in range(MAX_EPOCHS):
        perm = fit_i[torch.randperm(len(fit_i), device='cuda')]
        for i in range(0, len(perm), batch):
            idx = perm[i:i + batch]
            opt.zero_grad()
            na.pinball(net(X[idx], None, s[idx]), y[idx], basis, lv).mean().backward()
            opt.step(); step += 1
            if step % CHECK == 0:
                v = vloss()
                if v < best - 1e-4:
                    best, state, wait = v, {k: x.detach().clone() for k, x in net.state_dict().items()}, 0
                else:
                    wait += 1
                if wait >= PATIENCE:
                    break
        if wait >= PATIENCE:
            break
    if state is None:
        state = net.state_dict()
    net.load_state_dict(state); net.eval()
    return net, step


def main():
    full = mt.load()
    out = []
    xgb22 = pd.read_parquet('var/mos-multi/oof_xgb_site_fast.parquet').query("station == 'CDW'")
    for n in (1, 4, 11, 22):
        started = time.time()
        t = full[full.station.isin(nm.NEAREST[:n])].reset_index(drop=True)
        cdw = (t.station == 'CDW').to_numpy()
        months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
        stations = sorted(t.station.unique())
        s = torch.tensor(t.station.map({x: i for i, x in enumerate(stations)}).to_numpy('int64'), device='cuda')
        y_np = t.gust_peak.to_numpy('float32')
        y = torch.tensor(y_np, device='cuda')
        q = np.zeros((len(t), len(LEVELS)), 'float32')
        steps = []
        for k in range(mt.FOLDS):
            train = (t.fold != k).to_numpy()
            mt.add_history(t, train)
            X_np, _ = nm.design(t, mt.features(t, 'site'), train)
            X = torch.tensor(X_np, device='cuda'); del X_np
            fit, val, test = train & (months % 7 != 3), train & (months % 7 == 3), np.flatnonzero(~train)
            qs = []
            for seed in range(SEEDS):
                net, step = train_mlp(X, y, s, fit, val, len(stations), seed)
                steps.append(step)
                basis = bernstein(torch.tensor(LEVELS, dtype=torch.float32)).cuda()
                with torch.no_grad():
                    ti = torch.tensor(test, device='cuda')
                    qs.append(torch.cat([na.bqn_quantiles(net(X[i], None, s[i]), basis) for i in ti.split(16384)]).cpu().numpy())
            q[test] = np.mean(qs, axis=0)
            del X; torch.cuda.empty_cache()
        q = np.sort(np.maximum(q, 0), axis=1)
        aft = t.hour.between(13, 16).to_numpy()
        c = crps(y_np, q)
        row = {'stations': n, 'station_hours': int(len(t)), 'independent_days': int(t.date.nunique()),
               'mlp_kcdw': round(float(c[cdw].mean()), 4), 'mlp_kcdw_1_4pm': round(float(c[cdw & aft].mean()), 4), 'mlp_median_updates': int(np.median(steps))}
        if n == 22:
            cols = [f'gust_peak_q{int(round(a * 100)):02d}' for a in LEVELS]
            cx = crps(xgb22.gust_peak.to_numpy(), xgb22[cols].to_numpy())
            xa = pd.DatetimeIndex(xgb22.valid).tz_localize('UTC').tz_convert('America/New_York').hour.isin(range(13, 17))
        else:
            _, qx = mt.run_xgb(t, 'site', 'gust_peak', fast=True)
            cx = crps(y_np, qx)[cdw]
            xa = aft[cdw]
        row['xgb_kcdw'], row['xgb_kcdw_1_4pm'] = round(float(cx.mean()), 4), round(float(cx[xa].mean()), 4)
        row['minutes'] = round((time.time() - started) / 60, 1)
        out.append(row)
        print(row, flush=True)
        json.dump(out, open('var/mos-multi/scaling_nn.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
