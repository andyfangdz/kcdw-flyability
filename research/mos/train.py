"""Cross-validated multi-model calibration for KCDW hourly wind (research).

Folds interleave calendar months (month index mod K), so each fold's test hours
are whole months never seen in training. Compares, on identical test hours:
  raw       the best available raw model value (ECMWF ENS mean, else GEFS, else GFS)
  linear    ordinary least squares on a few raw predictors (bias correction)
  xgboost   gradient-boosted trees on every model feature (NaN where a model is absent)
Quantile targets use XGBoost's multi-quantile objective (p10/p50/p90).
Usage: var/mos-venv/bin/python research/mos/train.py [--folds 5] [--since 2021-06-01]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

QUANTILES = np.array([0.1, 0.5, 0.9])
ID = ['date', 'hour', 'lead_day', 'valid', 'init']
TARGETS = ['sust_mean', 'sust_max', 'gust_peak', 'u_obs', 'v_obs', 'spread', 'metar_sknt', 'metar_drct', 'metar_gust',
           'metar_peak', 'metar_gust_reported', 'xw_sust_mean', 'xw_gust_peak']
AFTERNOON = range(13, 17)


def raw_value(t, kind):
    """First available raw model value, in preference order."""
    order = {'sust': ['ifs_ens_speed_10m_mean', 'gefs_speed_10m_mean', 'gfs_10m_spd', 'hrrr_10m_spd'],
             'gust': ['ifs_ens_wind_gust_10m_mean', 'gefs_wind_gust_surface_mean', 'hrrr_wind_gust_surface']}[kind]
    out = pd.Series(np.nan, index=t.index)
    for col in order:
        if col in t:
            out = out.fillna(t[col])
    return out.to_numpy()


def pinball(y, q, alpha):
    d = y - q
    return np.mean(np.maximum(alpha * d, (alpha - 1) * d))


def quantile_model(params):
    return xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=QUANTILES, tree_method='hist', **params)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--folds', type=int, default=5)
    parser.add_argument('--since', default='2021-06-01')
    parser.add_argument('--trees', type=int, default=500)
    args = parser.parse_args()
    t = pd.read_parquet('var/mos/table_issue.parquet')
    t = t[(t.date >= args.since) & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    features = [c for c in t.columns if c not in ID + TARGETS and t[c].notna().mean() > 0.02]
    X = t[features].to_numpy('float32')
    month = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    fold = month % args.folds
    params = dict(n_estimators=args.trees, learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8,
                  colsample_bytree=0.6, reg_lambda=5.0)
    print(f'{len(t)} rows, {len(features)} features, {t.date.min():%Y-%m-%d}..{t.date.max():%Y-%m-%d}, {args.folds} month-interleaved folds')
    results = {}
    preds = {}
    for target in ('sust_mean', 'gust_peak', 'metar_peak'):
        y = t[target].to_numpy('float32')
        ok = ~np.isnan(y)
        q = np.full((len(t), 3), np.nan)
        lin = np.full(len(t), np.nan)
        raw = raw_value(t, 'sust' if target == 'sust_mean' else 'gust')
        for k in range(args.folds):
            tr, te = ok & (fold != k), fold == k
            q[te] = quantile_model(params).fit(X[tr], y[tr]).predict(X[te])
            base = np.c_[np.ones(len(t)), np.nan_to_num(raw, nan=np.nanmean(raw)), t.hour_sin, t.hour_cos, t.doy_sin, t.doy_cos, t.lead_h]
            coef, *_ = np.linalg.lstsq(base[tr & ~np.isnan(raw)], y[tr & ~np.isnan(raw)], rcond=None)
            lin[te] = base[te] @ coef
        q.sort(axis=1)
        preds[target] = q
        for scope, mask in (('all hours', ok & ~np.isnan(raw)), ('1-4 pm', ok & ~np.isnan(raw) & t.hour.isin(AFTERNOON).to_numpy())):
            yy = y[mask]
            results[(target, scope)] = {
                'n': int(mask.sum()),
                'MAE raw': float(np.mean(np.abs(raw[mask] - yy))), 'bias raw': float(np.mean(raw[mask] - yy)),
                'MAE linear': float(np.mean(np.abs(lin[mask] - yy))),
                'MAE xgb p50': float(np.mean(np.abs(q[mask, 1] - yy))), 'bias xgb p50': float(np.mean(q[mask, 1] - yy)),
                'pinball xgb': float(np.mean([pinball(yy, q[mask, i], a) for i, a in enumerate(QUANTILES)])),
                'p10-p90 coverage': float(np.mean((yy >= q[mask, 0]) & (yy <= q[mask, 2]))),
            }
    # Direction, scored where the observed wind is at least 5 kt. Three methods on identical hours:
    # u/v regression (baseline), unit-vector regression on windy hours, and a 16-sector classifier.
    obs_dir = (np.degrees(np.arctan2(-t.u_obs, -t.v_obs)) % 360).to_numpy()
    windy_obs = (t.sust_mean >= 3).to_numpy() & ~np.isnan(obs_dir)
    err = lambda a, b: np.abs((a - b + 180) % 360 - 180)
    methods = {}
    uv = {}
    for comp in ('u_obs', 'v_obs'):
        y = t[comp].to_numpy('float32'); p = np.full(len(t), np.nan)
        for k in range(args.folds):
            tr, te = (fold != k) & ~np.isnan(y), fold == k
            p[te] = xgb.XGBRegressor(tree_method='hist', **params).fit(X[tr], y[tr]).predict(X[te])
        uv[comp] = p
    methods['xgb u/v'] = np.degrees(np.arctan2(-uv['u_obs'], -uv['v_obs'])) % 360
    unit = {}
    for name, y in (('sin', np.sin(np.radians(obs_dir))), ('cos', np.cos(np.radians(obs_dir)))):
        p = np.full(len(t), np.nan)
        for k in range(args.folds):
            tr, te = (fold != k) & windy_obs, fold == k
            p[te] = xgb.XGBRegressor(tree_method='hist', **params).fit(X[tr], y[tr]).predict(X[te])
        unit[name] = p
    methods['xgb unit vector'] = np.degrees(np.arctan2(unit['sin'], unit['cos'])) % 360
    sector = np.round(np.nan_to_num(obs_dir) / 22.5).astype(int) % 16
    probs = np.full((len(t), 16), np.nan)
    for k in range(args.folds):
        tr, te = (fold != k) & windy_obs, fold == k
        model = xgb.XGBClassifier(tree_method='hist', objective='multi:softprob', num_class=16, **dict(params, n_estimators=150))
        probs[te] = model.fit(X[tr], sector[tr]).predict_proba(X[te])
    vec = probs @ np.c_[np.sin(np.radians(np.arange(16) * 22.5)), np.cos(np.radians(np.arange(16) * 22.5))]
    methods['xgb 16-sector (probability-weighted)'] = np.degrees(np.arctan2(vec[:, 0], vec[:, 1])) % 360
    methods['raw GFS'] = (np.degrees(np.arctan2(-t.gfs_wind_u_10m, -t.gfs_wind_v_10m)) % 360).to_numpy()
    if 'ifs_ens_wind_u_10m_mean' in t:
        methods['raw ECMWF ENS mean'] = (np.degrees(np.arctan2(-t.ifs_ens_wind_u_10m_mean, -t.ifs_ens_wind_v_10m_mean)) % 360).to_numpy()
    windy = (t.sust_mean >= 5).to_numpy() & ~np.isnan(obs_dir) & ~np.isnan(methods['raw GFS'])
    entry = {'n': int(windy.sum())}
    for name, d in methods.items():
        ok = windy & ~np.isnan(d)
        entry[f'MAE {name}'] = float(np.mean(err(d[ok], obs_dir[ok])))
        entry[f'<=30 deg {name}'] = float(np.mean(err(d[ok], obs_dir[ok]) <= 30))
    results[('direction', 'sust >= 5 kt')] = entry
    # Threshold probabilities: dedicated classifiers vs raw thresholds, Brier on the same hours.
    for name, y in (('1-min gust >= 20', (t.gust_peak >= 20).astype(float).to_numpy()),
                    ('spread >= 10', (t.spread >= 10).astype(float).to_numpy()),
                    ('METAR gust reported', t.metar_gust_reported.to_numpy('float32'))):
        ok = ~np.isnan(y)
        p = np.full(len(t), np.nan)
        for k in range(args.folds):
            tr, te = ok & (fold != k), fold == k
            p[te] = xgb.XGBClassifier(tree_method='hist', eval_metric='logloss', **params).fit(X[tr], y[tr]).predict_proba(X[te])[:, 1]
        clim = np.array([y[ok & (fold != k)].mean() for k in fold])
        mask = ok & ~np.isnan(p)
        b, b0 = np.mean((p[mask] - y[mask]) ** 2), np.mean((clim[mask] - y[mask]) ** 2)
        entry = {'n': int(mask.sum()), 'base rate': float(y[mask].mean()), 'Brier skill xgb': float(1 - b / b0)}
        if name.startswith('1-min'):
            rawg = raw_value(t, 'gust')
            m2 = mask & ~np.isnan(rawg)
            entry['Brier skill raw gust>=20'] = float(1 - np.mean(((rawg[m2] >= 20) - y[m2]) ** 2) / np.mean((clim[m2] - y[m2]) ** 2))
        results[(name, 'all hours')] = entry
    for (target, scope), r in results.items():
        print(f'\n{target} [{scope}]')
        print('  ' + ', '.join(f'{k} {v:.3f}' if isinstance(v, float) else f'{k} {v}' for k, v in r.items()))
    oof = t[['date', 'hour', 'lead_day', 'valid', 'init']].copy()
    for target, q in preds.items():
        for i, a in enumerate(QUANTILES):
            oof[f'{target}_p{int(a * 100)}'] = q[:, i]
    for name, d in methods.items():
        oof['dir_' + name.replace(' ', '_').replace('/', '')] = d
    oof.to_parquet('var/mos/oof.parquet', index=False)
    Path('var/mos/cv-results.json').write_text(json.dumps({f'{a} | {b}': r for (a, b), r in results.items()}, indent=1))


if __name__ == '__main__':
    main()
