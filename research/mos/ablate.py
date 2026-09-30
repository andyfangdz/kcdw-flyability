"""Leave-one-model-out ablation of the calibrated gust model (research).

Retrains the 19-quantile XGBoost gust model on the method-comparison folds with
one input family removed at a time (and with every model gust field removed), and
scores each against the full model on identical hours. Differences are paired per
day, so the standard error reflects day-to-day correlation. Writes
var/mos/ablation.json. Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import multiprocessing
import os
import time

import numpy as np
import pandas as pd
import xgboost as xgb

from compare_common import LEVELS, crps, exceedance, load

DEVICE = os.environ.get('MOS_DEVICE', 'cpu')
TARGET = 'gust_peak'
WORKERS = int(os.environ.get('ABLATE_WORKERS', '4'))
FAMILIES = {'GEFS': 'gefs_', 'WeatherNext 2': 'wn2_', 'GFS': 'gfs_', 'ECMWF ensemble': 'ifs_', 'AIFS (single + ensemble)': 'aifs_',
            'ECMWF HRES': 'hres_', 'HRRR': 'hrrr_', 'WeatherNext 3': 'wn3_', 'ICON': 'icon_', 'GEM': 'gem_'}


def variants(features):
    out = {'all inputs': features,
           'no model gust fields': [f for f in features if 'gust' not in f],
           'no ECMWF ensemble gust': [f for f in features if not f.startswith('ifs_ens_wind_gust')]}
    for name, prefix in FAMILIES.items():
        out[f'no {name}'] = [f for f in features if not f.startswith(prefix)]
    out['only GEFS + WN2 + GFS + ECMWF ens'] = [f for f in features if f.startswith(('gefs_', 'wn2_', 'gfs_', 'ifs_', 'hour_', 'doy_', 'lead_'))]
    return out


def fit_predict(t, cols, fold):
    X, y = t[cols].to_numpy('float32'), t[TARGET].to_numpy('float32')
    q = np.full((len(t), len(LEVELS)), np.nan)
    for k in np.unique(fold):
        tr, te = fold != k, fold == k
        model = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=LEVELS, tree_method='hist', device=DEVICE, n_estimators=500,
                                 learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0,
                                 random_state=0)
        model.fit(X[tr], y[tr])
        q[te] = np.sort(model.predict(X[te]), axis=1)  # predicts on the training device (host data is copied over)
    return np.maximum(q, 0)


STATE = {}


def run(item):
    name, cols = item
    started = time.time()
    t, fold = STATE['t'], STATE['fold']
    q = fit_predict(t, cols, fold)
    print(f'  {name}: trained ({time.time() - started:.0f} s)', flush=True)
    return name, len(cols), crps(t[TARGET].to_numpy(), q), exceedance(q, 20.0), np.abs(q[:, 9] - t[TARGET].to_numpy())


def main():
    t, features, fold = load()
    STATE.update(t=t, fold=fold)
    y = t[TARGET].to_numpy()
    afternoon = t.hour.between(13, 16).to_numpy()
    recent = t['ifs_ens_wind_gust_10m_mean'].notna().to_numpy()  # hours where the ECMWF ensemble exists (2024 onward)
    windy = (y >= 20)
    days = t.date.to_numpy()
    ctx = multiprocessing.get_context('fork')
    with ctx.Pool(WORKERS) as pool:  # the GPU is underused by one small-tree quantile fit at a time
        fitted = pool.map(run, list(variants(features).items()), chunksize=1)
    results, base = [], None
    for name, n_inputs, c, p20, abs_err in fitted:
        brier = (p20 - windy) ** 2
        row = {'variant': name, 'inputs': n_inputs, 'crps': float(c.mean()), 'crps_1_4pm': float(c[afternoon].mean()),
               'crps_ecmwf_era': float(c[recent].mean()), 'brier_20kt': float(brier.mean()), 'mae_p50': float(abs_err.mean())}
        if base is None:
            base = {'c': c, 'brier': brier}
        else:
            for key, now, ref in (('crps', c, base['c']), ('brier_20kt', brier, base['brier'])):
                daily = pd.Series(now - ref).groupby(days).mean()
                row[f'{key}_change_pct'] = float(100 * (now - ref).mean() / ref.mean())
                row[f'{key}_change_se_pct'] = float(100 * daily.std() / np.sqrt(len(daily)) / ref.mean())
            d = c - base['c']
            row['crps_ecmwf_era_change_pct'] = float(100 * d[recent].mean() / base['c'][recent].mean())
            row['crps_1_4pm_change_pct'] = float(100 * d[afternoon].mean() / base['c'][afternoon].mean())
        results.append(row)
        extra = f"  {row['crps_change_pct']:+.2f}% ± {row['crps_change_se_pct']:.2f}" if 'crps_change_pct' in row else ''
        print(f"{name:40s} {n_inputs:4d} inputs  CRPS {row['crps']:.4f}{extra}", flush=True)
    out = {'target': TARGET, 'hours': int(len(t)), 'ecmwf_era_hours': int(recent.sum()), 'results': results}
    with open('var/mos/ablation.json', 'w') as fh:
        json.dump(out, fh, indent=1)

if __name__ == '__main__':
    main()
