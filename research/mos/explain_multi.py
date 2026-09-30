"""What the 22-station site-aware gust model learned (research).

Trains the median (p50) true 1-hour peak-gust model with the fast multi-station settings
(depth 9, 64 bins, eta 0.1, early stopping on held-out months) on folds 1-4, without the
Open-Meteo ICON/GEM inputs, then:
  - scores fold 0 per station against raw ECMWF ENS and GEFS (held-out skill);
  - attributes a station-balanced fold-0 sample with exact TreeSHAP (pred_contribs);
  - summarizes the site effect: the summed SHAP of the station identity, exposure and
    history inputs, per station and per consensus wind sector.
The production forecast model uses vector-leaf trees, which XGBoost cannot attribute,
so this one-output model with the same settings stands in for its median.
Writes var/mos-multi/explain_multi.json. Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import os
import time

import numpy as np
import pandas as pd
import xgboost as xgb

import multi_train as mt

SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
MODELS = {'gfs': 'GFS', 'gefs': 'GEFS ensemble', 'ifs': 'ECMWF ensemble', 'ifshres': 'ECMWF HRES', 'aifs': 'AIFS (single + ensemble)', 'hrrr': 'HRRR',
          'wn2': 'WeatherNext 2', 'wn3': 'WeatherNext 3', 'ukmo': 'Met Office UKMO'}
TIME = {'hour_sin', 'hour_cos', 'doy_sin', 'doy_cos', 'lead_h'}


def family(name):
    if name == 'station':
        return 'Site: station identity'
    if name.startswith(('stn_', 'upwind_')) or name.startswith('consensus_'):
        return 'Site: exposure and location'
    if name.startswith('hist_'):
        return 'Site: station history'
    if name in TIME:
        return 'Time of day, season, lead'
    return MODELS.get(name.split('_')[0], 'Other')


def main():
    started = time.time()
    t = mt.load()
    t = t.drop(columns=[c for c in t.columns if c.startswith(('icon_', 'gem_'))])
    train = (t.fold != 0).to_numpy()
    mt.add_history(t, train)
    cols = mt.features(t, 'site')
    stations = sorted(t.station.unique())
    codes = t.station.map({s: i for i, s in enumerate(stations)}).to_numpy('float32')
    names = cols + ['station']
    types = ['q'] * len(cols) + ['c']
    y = t.gust_peak.to_numpy('float32')
    months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    val = train & (months % 7 == 3)

    def dmatrix(rows, ref=None, label=True):
        X = np.concatenate([mt.matrix(t, cols, rows), codes[rows, None]], axis=1)
        if not label:
            return xgb.DMatrix(X, feature_names=names, feature_types=types, enable_categorical=True)
        return xgb.QuantileDMatrix(X, y[rows], feature_names=names, feature_types=types, enable_categorical=True, max_bin=64, ref=ref)

    dtrain = dmatrix(train & ~val)
    dval = dmatrix(val, ref=dtrain)
    params = {'objective': 'reg:quantileerror', 'quantile_alpha': 0.5, 'tree_method': 'hist', 'device': os.environ.get('MOS_DEVICE', 'cpu'),
              'max_cat_to_onehot': 1, 'eta': 0.1, 'max_depth': 9, 'max_bin': 64, 'min_child_weight': 100, 'subsample': 0.8,
              'colsample_bytree': 0.6, 'lambda': 5.0, 'seed': 0}
    booster = xgb.train(params, dtrain, num_boost_round=600, evals=[(dval, 'val')], early_stopping_rounds=30, verbose_eval=False)
    booster = booster[:booster.best_iteration + 1]
    rounds = booster.num_boosted_rounds()
    del dtrain, dval
    print(f'trained {rounds} rounds ({time.time() - started:.0f} s)', flush=True)

    test = ~train
    tt = t[test].reset_index(drop=True)
    pred = booster.predict(dmatrix(test, label=False))
    skill = []
    for station, g in tt.assign(pred=pred).groupby('station'):
        row = {'station': station, 'hours': int(len(g)), 'calibrated': round(float(np.abs(g.pred - g.gust_peak).mean()), 2)}
        for label, col in (('ecmwf_ens', 'ifs_ens_wind_gust_10m_mean'), ('gefs', 'gefs_wind_gust_surface_mean')):
            ok = g[col].notna()
            row[label] = round(float(np.abs(g.loc[ok, col] - g.loc[ok, 'gust_peak']).mean()), 2)
            row[f'calibrated_on_{label}_hours'] = round(float(np.abs(g.loc[ok, 'pred'] - g.loc[ok, 'gust_peak']).mean()), 2)
        row['bias'] = round(float((g.pred - g.gust_peak).mean()), 2)
        skill.append(row)

    rng = np.random.default_rng(0)
    idx = np.concatenate([rng.choice(np.flatnonzero(tt.station == s), min(2500, int((tt.station == s).sum())), replace=False) for s in stations])
    sample = tt.iloc[idx].reset_index(drop=True)
    rows_mask = np.zeros(len(t), bool)
    rows_mask[np.flatnonzero(test)[idx]] = True
    order_back = np.argsort(np.argsort(np.flatnonzero(test)[idx]))  # matrix() returns rows in table order
    Xs = np.concatenate([mt.matrix(t, cols, rows_mask), codes[rows_mask, None]], axis=1)[order_back]
    contrib = booster.predict(xgb.DMatrix(Xs, feature_names=names, feature_types=types, enable_categorical=True), pred_contribs=True)
    shap, base = contrib[:, :-1], float(contrib[0, -1])
    print(f'SHAP on {len(sample)} hours ({time.time() - started:.0f} s)', flush=True)

    mean_abs = np.abs(shap).mean(axis=0)
    order = np.argsort(-mean_abs)
    fam = {}
    for i, f in enumerate(names):
        fam[family(f)] = fam.get(family(f), 0.0) + float(mean_abs[i])
    families = sorted(({'family': k, 'mean_abs_kt': round(v, 3)} for k, v in fam.items()), key=lambda r: -r['mean_abs_kt'])
    top = [{'feature': names[i], 'family': family(names[i]), 'mean_abs_kt': round(float(mean_abs[i]), 3)} for i in order[:25]]

    site_idx = [i for i, f in enumerate(names) if family(f).startswith('Site')]
    site_push = shap[:, site_idx].sum(axis=1)
    sector = sample.sector.to_numpy()
    per_station = []
    for s in stations:
        m = (sample.station == s).to_numpy()
        by_sector = {SECTORS[k]: round(float(site_push[m & (sector == k)].mean()), 2) for k in range(8) if (m & (sector == k)).sum() >= 40}
        per_station.append({'station': s, 'site_push_kt': round(float(site_push[m].mean()), 2), 'by_sector': by_sector,
                            'observed_mean_kt': round(float(sample.gust_peak[m].mean()), 2), 'prediction_mean_kt': round(float(base + shap[m].sum(axis=1).mean()), 2)})

    def dependence(name, bins=12):
        j = names.index(name)
        x = sample[name].to_numpy() if name in sample else codes[rows_mask][order_back]
        ok = ~np.isnan(x)
        edges = np.unique(np.quantile(x[ok], np.linspace(0, 1, bins + 1)))
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = ok & (x >= lo) & (x <= hi)
            if m.sum() >= 30:
                out.append({'x': round(float(np.median(x[m])), 3), 'shap': round(float(np.mean(shap[m, j])), 3),
                            'p10': round(float(np.quantile(shap[m, j], 0.1)), 3), 'p90': round(float(np.quantile(shap[m, j], 0.9)), 3)})
        return out

    wanted = [names[i] for i in order[:8] if names[i] != 'station']
    for extra in ('upwind_near_tree', 'upwind_far_water', 'stn_sea_km', 'hist_gust'):
        if extra in names and extra not in wanted:
            wanted.append(extra)
    dep = {n: dependence(n) for n in wanted}

    swarm_idx = rng.choice(len(sample), 4000, replace=False)
    beeswarm = []
    for i in order[:15]:
        x = sample[names[i]].to_numpy() if names[i] in sample else codes[rows_mask][order_back]
        rank = pd.Series(x).rank(pct=True).to_numpy()
        beeswarm.append({'feature': names[i], 'shap': [round(float(v), 3) for v in shap[swarm_idx, i]],
                         'rank': [None if np.isnan(r) else round(float(r), 3) for r in rank[swarm_idx]]})

    trees = booster.trees_to_dataframe()
    tree0 = [{k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in r.items()}
             for r in trees[trees.Tree == 0][['ID', 'Feature', 'Split', 'Yes', 'No', 'Gain', 'Cover', 'Category']].to_dict(orient='records')]
    out = {'features': len(names), 'rounds': rounds, 'train_hours': int(train.sum()), 'test_hours': int(test.sum()), 'sample': int(len(sample)),
           'stations': stations, 'base_kt': round(base, 2), 'families': families, 'top_features': top, 'per_station': per_station,
           'skill': skill, 'dependence': dep, 'beeswarm': beeswarm, 'tree0': tree0}
    with open('var/mos-multi/explain_multi.json', 'w') as fh:
        json.dump(out, fh, indent=1, default=str)
    print('families:', [(r['family'], r['mean_abs_kt']) for r in families[:8]], flush=True)
    print('site push:', [(r['station'], r['site_push_kt']) for r in sorted(per_station, key=lambda r: r['site_push_kt'])], flush=True)


if __name__ == '__main__':
    main()
