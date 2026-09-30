"""Do neural nets gain more than trees from 22 stations' data? (research)

Part A, architectures on all 22 stations (site-aware, Open-Meteo excluded), each with a
learned station embedding, the same BQN head and early stopping as nn_arch.py, one seed:
  mlp      MLP 512-256 on standardized inputs, per-model missing flags and the station embedding
  resmlp   pre-norm residual MLP (width 512, 4 blocks), station embedding at the input
  sources  one token per forecast model, a time token and a site token (site inputs plus the
           station embedding); attention across the models present
  ftt      FT-Transformer over every input plus a station token
Part B, scaling: the sources net and the fast XGBoost on the 1, 4, 11 and 22 stations
nearest KCDW (gust), scored at KCDW.
Out-of-fold quantiles: var/mos-multi/oof_nn_<arch>.parquet; summary var/mos-multi/nn_multi.json.
Usage: var/nn-venv/bin/python research/mos/nn_multi.py [--archs ...] [--scaling]
"""
import argparse
import gc
import json
import math
import os
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

import multi_train as mt
import nn_arch as na
from compare_common import LEVELS, crps

ROOT = 'var/mos-multi'
NEAREST = ['CDW', 'TEB', 'EWR', 'LGA', 'SMQ', 'FWN', 'JFK', 'HPN', 'MGJ', 'FRG', 'TTN', 'DXR', 'POU', 'ISP', 'ABE', 'BDR', 'PNE', 'HVN', 'PHL',
           'RDG', 'ILG', 'MIV']
SOURCES = ['gfs', 'gefs', 'ifs', 'ifshres', 'aifs', 'hrrr', 'wn2', 'wn3', 'ukmo']
SITE = ('stn_', 'upwind_', 'hist_', 'consensus_')
TIME = ['hour_sin', 'hour_cos', 'doy_sin', 'doy_cos', 'lead_h']
EMB = 16


def prefix(f):
    return 'ifshres' if f.startswith('ifshres_') else f.split('_')[0]


def design(t, cols, train):
    """Standardized inputs with per-model missing flags, filled in place; per-input missing mask as uint8."""
    groups = sorted({prefix(c) for c in cols})
    X = np.empty((len(t), len(cols) + len(groups)), dtype='float32')
    M = np.empty((len(t), len(cols) + len(groups)), dtype='uint8')
    for j, c in enumerate(cols):
        col = t[c].to_numpy('float32')
        miss = np.isnan(col)
        mu, sd = np.nanmean(col[train]), np.nanstd(col[train]) + 1e-6
        X[:, j] = np.where(miss, 0, (col - mu) / sd)
        M[:, j] = miss
    for g, name in enumerate(groups):
        idx = [j for j, c in enumerate(cols) if prefix(c) == name]
        flag = M[:, idx].min(axis=1)
        X[:, len(cols) + g], M[:, len(cols) + g] = flag, flag
    return X, M


class SiteMLP(nn.Module):
    def __init__(self, n_in, n_st, **_):
        super().__init__()
        self.emb = nn.Embedding(n_st, EMB)
        self.body = nn.Sequential(nn.Linear(n_in + EMB, 512), nn.GELU(), nn.Dropout(0.1), nn.Linear(512, 256), nn.GELU(), nn.Dropout(0.1))
        self.head = na.Head(256)

    def forward(self, x, m, s):
        return self.head(self.body(torch.cat([x, self.emb(s)], dim=1)))


class SiteResMLP(na.ResMLP):
    def __init__(self, n_in, n_st, **_):
        super().__init__(n_in + EMB)
        self.emb = nn.Embedding(n_st, EMB)

    def forward(self, x, m, s):
        return super().forward(torch.cat([x, self.emb(s)], dim=1), m)


class SiteSources(nn.Module):
    def __init__(self, n_in, n_st, groups, site, d=96, layers=3, heads=4, **_):
        super().__init__()
        self.groups, self.site = groups, site
        self.enc_in = nn.ModuleList(nn.Sequential(nn.Linear(len(g), d), nn.GELU(), nn.Linear(d, d)) for g in groups)
        self.site_in = nn.Sequential(nn.Linear(len(site) + EMB, d), nn.GELU(), nn.Linear(d, d))
        self.emb = nn.Embedding(n_st, EMB)
        self.kind = nn.Parameter(torch.randn(len(groups) + 1, d) * 0.1)
        layer = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.1, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers)
        self.norm = nn.LayerNorm(d)
        self.head = na.Head(d)

    def forward(self, x, m, s):
        toks, absent = [], []
        for i, (g, f) in enumerate(zip(self.groups, self.enc_in)):
            toks.append(f(x[:, g]) + self.kind[i])
            absent.append(m[:, g].amin(dim=1) > 0)
        toks.append(self.site_in(torch.cat([x[:, self.site], self.emb(s)], dim=1)) + self.kind[-1])
        absent.append(torch.zeros(len(x), dtype=torch.bool, device=x.device))
        pad = torch.stack(absent, dim=1)
        pad[:, -2] = False  # time token (last model-group slot) always present
        h = self.enc(torch.stack(toks, dim=1), src_key_padding_mask=pad)
        keep = (~pad).float()[..., None]
        return self.head(self.norm((h * keep).sum(1) / keep.sum(1)))


class SiteFTT(nn.Module):
    def __init__(self, n_in, n_st, n_feat, d=32, layers=2, heads=4, **_):
        super().__init__()
        self.n_feat = n_feat
        self.w = nn.Parameter(torch.randn(n_feat, d) * 0.1)
        self.b = nn.Parameter(torch.zeros(n_feat, d))
        self.missing = nn.Parameter(torch.randn(n_feat, d) * 0.1)
        self.emb = nn.Embedding(n_st, d)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        layer = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.1, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers)
        self.norm = nn.LayerNorm(d)
        self.head = na.Head(d)

    def forward(self, x, m, s):
        xf, mf = x[:, :self.n_feat, None], m[:, :self.n_feat, None].float()
        tok = (1 - mf) * (xf * self.w + self.b) + mf * self.missing
        h = self.enc(torch.cat([self.cls.expand(len(x), -1, -1), self.emb(s)[:, None], tok], dim=1))
        return self.head(self.norm(h[:, 0]))


def run_net(t, arch, target, epochs_cap=None):
    stations = sorted(t.station.unique())
    s_idx = t.station.map({s: i for i, s in enumerate(stations)}).to_numpy('int64')
    months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    y = t[target].to_numpy('float32')
    q = np.zeros((len(t), len(LEVELS)), 'float32')
    old_epochs = na.EPOCHS
    if epochs_cap:
        na.EPOCHS = epochs_cap
    for k in range(mt.FOLDS):
        started = time.time()
        train = (t.fold != k).to_numpy()
        mt.add_history(t, train)
        cols = mt.features(t, 'site')
        X, M = design(t, cols, train)
        groups = [[j for j, c in enumerate(cols) if prefix(c) == g] for g in SOURCES]
        groups = [g for g in groups if g] + [[cols.index(c) for c in TIME if c in cols]]
        site = [j for j, c in enumerate(cols) if c.startswith(SITE)]
        make = {'mlp': lambda: SiteMLP(X.shape[1], len(stations)), 'resmlp': lambda: SiteResMLP(X.shape[1], len(stations)),
                'sources': lambda: SiteSources(X.shape[1], len(stations), groups, site),
                'ftt': lambda: SiteFTT(X.shape[1], len(stations), len(cols))}[arch]
        fit, val = train & (months % 7 != 3), train & (months % 7 == 3)
        net, epochs, best = na.train_net(make, (X, M, s_idx), y, fit, val, 0, 2048 if arch == 'ftt' else 4096)
        test = np.flatnonzero(~train)
        q[test] = na.predict(net, (X, M, s_idx), test)
        del net, X, M; gc.collect(); torch.cuda.empty_cache()
        print(f'    {arch} {target} fold {k}: {epochs} epochs, val {best:.3f}, {time.time() - started:.0f} s', flush=True)
    na.EPOCHS = old_epochs
    return np.sort(np.maximum(q, 0), axis=1)


def summarize(t, q, target):
    y = t[target].to_numpy()
    c = crps(y, q)
    cdw = (t.station == 'CDW').to_numpy()
    aft = t.hour.between(13, 16).to_numpy()
    return {'crps_all': round(float(c.mean()), 4), 'crps_kcdw': round(float(c[cdw].mean()), 4), 'crps_kcdw_1_4pm': round(float(c[cdw & aft].mean()), 4),
            'mae_kcdw': round(float(np.abs(q[cdw, 9] - y[cdw]).mean()), 3), 'cover_all': round(float(np.mean((y >= q[:, 1]) & (y <= q[:, 17]))), 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archs', nargs='*', default=['mlp', 'sources', 'resmlp', 'ftt'])
    parser.add_argument('--scaling', action='store_true')
    args = parser.parse_args()
    na.PATIENCE = 5
    path = f'{ROOT}/nn_multi.json'
    results = json.load(open(path)) if os.path.exists(path) else {}
    t = mt.load()
    print(f'{len(t)} station-hours, {t.station.nunique()} stations', flush=True)
    for arch in args.archs:
        started = time.time()
        out = t[['station', 'valid', 'lead_day', 'hour', 'date']].copy()
        res = {}
        for target in ('gust_peak', 'sust_mean'):
            q = run_net(t, arch, target, epochs_cap=25 if arch == 'ftt' else None)
            for j, a in enumerate(LEVELS):
                out[f'{target}_q{int(round(a * 100)):02d}'] = q[:, j]
            out[target] = t[target].to_numpy('float32')
            res[target] = summarize(t, q, target)
            print(f'{arch} {target}: {res[target]}', flush=True)
        res['minutes'] = round((time.time() - started) / 60, 1)
        results.setdefault('architectures', {})[arch] = res
        out.to_parquet(f'{ROOT}/oof_nn_{arch}.parquet', index=False)
        json.dump(results, open(path, 'w'), indent=1)
    if args.scaling:
        for n in (1, 4, 11, 22):
            sub = t[t.station.isin(NEAREST[:n])].reset_index(drop=True)
            row = {'stations': n, 'station_hours': int(len(sub))}
            _, qx = mt.run_xgb(sub, 'site', 'gust_peak', fast=True)
            row['xgb'] = summarize(sub, qx, 'gust_peak')
            qn = run_net(sub, 'sources', 'gust_peak')
            row['sources'] = summarize(sub, qn, 'gust_peak')
            print('scaling', row, flush=True)
            results.setdefault('scaling', []).append(row)
            json.dump(results, open(path, 'w'), indent=1)


if __name__ == '__main__':
    main()
