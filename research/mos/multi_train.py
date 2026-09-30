"""Multi-station training and the local-vs-pooled comparison (research).

Variants, all scored on identical KCDW hours with month-interleaved folds shared by
every station (a test month is unseen at every station):
  local    KCDW rows only, same features as pooled
  pooled   all stations, no site information (station descriptors removed)
  site     all stations with site information: categorical station (XGBoost) or a
           learned embedding (BQN), directional exposure, and station history
Station history is fold-aware: per station and consensus-direction sector, the
median observed spread, gust and sustained wind from the fold's training months only.
Methods: BQN (GPU) for every variant; XGBoost 19 quantiles for local and site
(--xgb). Writes var/mos-multi/oof_<method>_<variant>.parquet and per-station scores.
Usage: var/nn-venv/bin/python research/mos/multi_train.py --method bqn
       var/mos-venv/bin/python research/mos/multi_train.py --method xgb
"""
import argparse
import os
import glob
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('var/mos-multi')
ID = ['station', 'date', 'hour', 'lead_day', 'valid', 'init']
OBS = ['sust_mean', 'sust_max', 'gust_peak', 'u_obs', 'v_obs', 'spread', 'metar_sknt', 'metar_drct', 'metar_gust', 'metar_peak',
       'metar_gust_reported']
SITE_PREFIXES = ('stn_', 'upwind_', 'hist_')
LEVELS = np.round(np.arange(0.05, 0.96, 0.05), 2)
FOLDS = 5


def load():
    frames = []
    for path in sorted(glob.glob(str(ROOT / 'table' / '*.parquet'))):
        t = pd.read_parquet(path)
        t = t[(t.date >= '2021-06-01') & t.gust_peak.notna() & t.sust_mean.notna()]
        frames.append(t)
    t = pd.concat(frames, ignore_index=True)
    t = t.drop(columns=[c for c in t.columns if c.startswith(('icon_', 'gem_'))])  # Open-Meteo look-ahead (leakage_audit.py); tables built before its removal
    t['fold'] = ((t.date.dt.year * 12 + t.date.dt.month) % FOLDS).astype('int8')
    t['sector'] = (np.round((np.degrees(np.arctan2(t.consensus_dir_sin, t.consensus_dir_cos)) % 360) / 45) % 8).fillna(-1).astype('int8')
    return t


def add_history(t, train_mask):
    """Per-station, per-sector medians from training rows only (NaN where unseen)."""
    base = t[train_mask & (t.sector >= 0)]
    med = base.groupby(['station', 'sector']).agg(hist_spread=('spread', 'median'), hist_gust=('gust_peak', 'median'),
                                                   hist_sust=('sust_mean', 'median'), hist_n=('spread', 'size'))
    med = med[med.hist_n >= 50].drop(columns='hist_n')
    joined = t[['station', 'sector']].join(med, on=['station', 'sector'])
    for c in med.columns:
        t[c] = joined[c].to_numpy('float32')


def features(t, variant):
    cols = [c for c in t.columns if c not in ID + OBS + ['fold', 'sector'] and t[c].notna().mean() > 0.01]
    if variant == 'pooled':
        cols = [c for c in cols if not c.startswith(SITE_PREFIXES) and c not in ('stn_lat', 'stn_lon', 'stn_elev', 'stn_code')]
    return cols


def pinball_crps(y, q):
    d = y[:, None] - q
    return 2 * np.maximum(LEVELS * d, (LEVELS - 1) * d).mean(axis=1)


def matrix(t, cols, rows=None):
    """Float32 feature matrix filled column by column (no intermediate DataFrame copy of the 4M-row table)."""
    n = len(t) if rows is None else int(rows.sum())
    out = np.empty((n, len(cols)), dtype='float32')
    for j, c in enumerate(cols):
        col = t[c].to_numpy('float32')
        out[:, j] = col if rows is None else col[rows]
    return out


def design_lean(t, cols, train, stations=None):
    """Standardized inputs, per-model missing flags and an optional station one-hot, as compare_nn.design, built in place."""
    groups = sorted({c.split('_')[0] for c in cols})
    extra = 0 if stations is None else stations.max() + 1
    X = np.empty((len(t), len(cols) + len(groups) + extra), dtype='float32')
    for j, c in enumerate(cols):
        col = t[c].to_numpy('float32')
        mu, sd = np.nanmean(col[train]), np.nanstd(col[train]) + 1e-6
        X[:, j] = np.nan_to_num((col - mu) / sd)
    for g, name in enumerate(groups):
        members = [c for c in cols if c.split('_')[0] == name]
        X[:, len(cols) + g] = t[members].isna().all(axis=1).to_numpy('float32')
    if stations is not None:
        X[:, len(cols) + len(groups):] = 0
        X[np.arange(len(t)), len(cols) + len(groups) + stations] = 1  # the first layer acts as a station embedding
    return X


def run_bqn(t, variant, target):
    import gc
    import compare_nn as cn
    cn.SEEDS = int(os.environ.get('MOS_SEEDS', cn.SEEDS))  # one seed keeps the 4M-row comparison to hours, identically for every variant
    cn.BATCH = int(os.environ.get('MOS_BATCH', cn.BATCH))
    sub = t if variant != 'local' else t[t.station == 'CDW'].copy()
    q = np.full((len(sub), len(LEVELS)), np.nan)
    stations = sorted(t.station.unique())
    station_idx = sub.station.map({s: i for i, s in enumerate(stations)}).to_numpy()
    months = (sub.date.dt.year * 12 + sub.date.dt.month).to_numpy()
    y = sub[target].to_numpy('float32')
    for k in range(FOLDS):
        train = (sub.fold != k).to_numpy()
        if variant != 'pooled':
            add_history(sub, train)
        X = design_lean(sub, features(sub, variant), train, station_idx if variant == 'site' else None)
        val = train & (months % 7 == 3)
        fit = train & ~val
        qs = []
        for seed in range(cn.SEEDS):
            net, epochs, best = cn.fit('bqn', X[fit], y[fit], X[val], y[val], seed)
            qs.append(cn.predict('bqn', net, X[~train]))
            del net
        q[~train] = np.sort(np.maximum(np.mean(qs, axis=0), 0), axis=1)
        del X; gc.collect()
        print(f'    bqn {variant} {target} fold {k}: {epochs} epochs, val {best:.3f}', flush=True)
    return sub, q


def run_xgb(t, variant, target, fast=False):
    import gc
    import xgboost as xgb
    sub = t if variant != 'local' else t[t.station == 'CDW'].copy()
    codes = pd.Categorical(sub.station).codes.astype('float32')
    q = np.full((len(sub), len(LEVELS)), np.nan)
    y = sub[target].to_numpy('float32')
    params = {'objective': 'reg:quantileerror', 'quantile_alpha': LEVELS, 'tree_method': 'hist', 'device': os.environ.get('MOS_DEVICE', 'cpu'),
              'max_cat_to_onehot': 1, 'eta': 0.05, 'max_depth': 7, 'min_child_weight': 100, 'subsample': 0.8, 'colsample_bytree': 0.6,
              'lambda': 5.0, 'seed': 0}
    if fast:  # one vector-leaf tree per round for all 19 quantiles, deeper, coarser bins; rounds by early stopping (bench_xgb.py)
        params.update({'multi_strategy': 'multi_output_tree', 'eta': 0.1, 'max_depth': 9, 'max_bin': 64})
    months = (sub.date.dt.year * 12 + sub.date.dt.month).to_numpy()
    for k in range(FOLDS):
        train = (sub.fold != k).to_numpy()
        add_history(sub, train)
        cols = features(sub, variant)
        types = ['q'] * len(cols) + (['c'] if variant == 'site' else [])

        def dmatrix(rows, ref=None):
            X = matrix(sub, cols, rows)
            if variant == 'site':
                X = np.concatenate([X, codes[rows, None]], axis=1)
            return xgb.QuantileDMatrix(X, y[rows], feature_types=types, enable_categorical=True, max_bin=params.get('max_bin', 256), ref=ref)

        if fast:
            val = train & (months % 7 == 3)  # held-out months inside the training fold, as for BQN
            dtrain = dmatrix(train & ~val)
            dval = dmatrix(val, ref=dtrain)
            gc.collect()
            booster = xgb.train(params, dtrain, num_boost_round=600, evals=[(dval, 'val')], early_stopping_rounds=30, verbose_eval=False)
            booster = booster[:booster.best_iteration + 1]
            del dval
        else:
            dtrain = dmatrix(train)
            gc.collect()
            booster = xgb.train(params, dtrain, num_boost_round=600)
        del dtrain; gc.collect()
        X = matrix(sub, cols, ~train)
        if variant == 'site':
            X = np.concatenate([X, codes[~train, None]], axis=1)
        q[~train] = np.sort(booster.predict(xgb.DMatrix(X, feature_types=types, enable_categorical=True)), axis=1)
        rounds = f', {booster.num_boosted_rounds()} rounds' if fast else ''
        del X, booster; gc.collect()
        print(f'    xgb {variant} {target} fold {k} done{rounds}', flush=True)
    return sub, q


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--method', choices=['bqn', 'xgb'], required=True)
    parser.add_argument('--variants', nargs='+', default=None)
    parser.add_argument('--fast', action='store_true', help='XGBoost only: vector-leaf depth-9 setup with early stopping')
    args = parser.parse_args()
    variants = args.variants or ['local', 'pooled', 'site']
    started = time.time()
    t = load()
    print(f'{len(t)} labelled station-hours, {t.station.nunique()} stations, {time.time() - started:.0f} s to load', flush=True)
    for variant in variants:
        out = None
        for target in ('gust_peak', 'sust_mean'):
            sub, q = run_bqn(t, variant, target) if args.method == 'bqn' else run_xgb(t, variant, target, fast=args.fast)
            if out is None:
                out = sub[['station', 'valid', 'lead_day', 'hour', 'date']].copy()
            names = [f'{target}_q{int(round(a * 100)):02d}' for a in LEVELS]
            for j, name in enumerate(names):
                out[name] = q[:, j].astype('float32')
            out[target] = sub[target].to_numpy('float32')
        out.to_parquet(ROOT / f"oof_{args.method}_{variant}{'_fast' if args.fast else ''}.parquet", index=False)
        print(f'  {args.method} {variant} saved ({time.time() - started:.0f} s)', flush=True)


if __name__ == '__main__':
    main()
