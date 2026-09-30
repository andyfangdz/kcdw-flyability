"""What the calibrated gust model learned: SHAP attributions, dependence and one tree (research).

Trains the median (p50) true 1-hour peak-gust model with the production settings on the
production issue-time table (every update's rows, each model's newest published run),
then uses XGBoost's exact TreeSHAP (pred_contribs) on a random sample of hours to
attribute each prediction to its inputs. Writes var/mos/explain.json.
Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb

from model import SINCE, features

DEVICE = os.environ.get('MOS_DEVICE', 'cpu')
FAMILIES = {'gfs': 'GFS', 'gefs': 'GEFS ensemble', 'ifs': 'ECMWF ensemble', 'aifs': 'AIFS (single + ensemble)', 'hrrr': 'HRRR', 'wn2': 'WeatherNext 2',
            'wn3': 'WeatherNext 3', 'icon': 'ICON', 'hres': 'ECMWF HRES', 'gem': 'GEM'}
TIME = {'hour_sin', 'hour_cos', 'doy_sin', 'doy_cos', 'lead_h', 'slot'}


def family(name):
    if name in TIME:
        return 'Time of day, season, lead, update time'
    return FAMILIES.get(name.split('_')[0], 'Other')


def main():
    t = pd.read_parquet('var/mos/table_issue.parquet')  # the production issue-time table
    feats = features(t)
    train = t[(t.date >= SINCE) & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    X, y = train[feats].to_numpy('float32'), train.gust_peak.to_numpy('float32')
    model = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=0.5, tree_method='hist', device=DEVICE, n_estimators=500,
                             learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0)
    model.fit(X, y)
    booster = model.get_booster()
    rng = np.random.default_rng(0)
    idx = rng.choice(len(train), 40000, replace=False)
    sample = train.iloc[idx].reset_index(drop=True)
    contrib = booster.predict(xgb.DMatrix(sample[feats].to_numpy('float32'), feature_names=feats), pred_contribs=True)
    shap, base = contrib[:, :-1], float(contrib[0, -1])
    mean_abs = np.abs(shap).mean(axis=0)
    order = np.argsort(-mean_abs)
    top = [{'feature': feats[i], 'family': family(feats[i]), 'mean_abs_kt': round(float(mean_abs[i]), 3)} for i in order[:25]]
    fam = {}
    for i, f in enumerate(feats):
        fam[family(f)] = fam.get(family(f), 0.0) + float(mean_abs[i])
    families = sorted(({'family': k, 'mean_abs_kt': round(v, 3)} for k, v in fam.items()), key=lambda r: -r['mean_abs_kt'])

    def dependence(name, bins=12):
        j = feats.index(name)
        x = sample[name].to_numpy()
        ok = ~np.isnan(x)
        edges = np.unique(np.quantile(x[ok], np.linspace(0, 1, bins + 1)))
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = ok & (x >= lo) & (x <= hi)
            if m.sum() >= 30:
                out.append({'x': round(float(np.median(x[m])), 3), 'shap': round(float(np.mean(shap[m, j])), 3),
                            'p10': round(float(np.quantile(shap[m, j], 0.1)), 3), 'p90': round(float(np.quantile(shap[m, j], 0.9)), 3), 'n': int(m.sum())})
        return out

    wanted = [feats[i] for i in order[:6]] + [f for f in ('ifs_ens_wind_gust_10m_mean', 'gfs_dt_80m') if f in feats]
    dep = {name: dependence(name) for name in wanted}
    # Interaction: how the ECMWF-ensemble gust input's effect changes with low-level stability (GFS 2 m minus 80 m).
    inter = []
    g, s = 'ifs_ens_wind_gust_10m_mean', 'gfs_dt_80m'
    if g in feats and s in feats:
        stab = sample[s].to_numpy()
        terciles = np.nanquantile(stab, [1 / 3, 2 / 3])
        for label, m in (('stable (surface cooler than 80 m)', stab <= terciles[0]), ('neutral', (stab > terciles[0]) & (stab <= terciles[1])),
                         ('unstable (surface warmer than 80 m)', stab > terciles[1])):
            sub = sample[m & ~np.isnan(stab)]
            shap_g = shap[m & ~np.isnan(stab), feats.index(g)]
            x = sub[g].to_numpy()
            pts = []
            for lo, hi in ((0, 8), (8, 12), (12, 16), (16, 20), (20, 25), (25, 40)):
                mm = (x >= lo) & (x < hi)
                if mm.sum() >= 30:
                    pts.append({'x': (lo + hi) / 2, 'shap': round(float(shap_g[mm].mean()), 3), 'n': int(mm.sum())})
            inter.append({'group': label, 'points': pts})
    # Afternoon versus night: where the model adds gust relative to its average.
    hours = sample.hour.to_numpy()
    by_hour = [{'hour': int(h), 'prediction_kt': round(float(base + shap[hours == h].sum(axis=1).mean()), 2),
                'observed_kt': round(float(sample.gust_peak[hours == h].mean()), 2)} for h in sorted(set(hours))]
    trees = booster.trees_to_dataframe()
    first = trees[(trees.Tree == 0)]
    name = lambda f: feats[int(f[1:])] if isinstance(f, str) and f.startswith('f') and f[1:].isdigit() else f
    tree0 = [{k: (None if (isinstance(v, float) and np.isnan(v)) else (name(v) if k == 'Feature' else v)) for k, v in r.items()}
             for r in first[['ID', 'Feature', 'Split', 'Yes', 'No', 'Gain', 'Cover']].to_dict(orient='records')]
    # Beeswarm: per-hour SHAP for the top inputs, coloured by the input's percentile rank (NaN = model missing that hour).
    swarm_idx = rng.choice(len(sample), 3000, replace=False)
    beeswarm = []
    for i in order[:15]:
        x = sample[feats[i]].to_numpy()[swarm_idx]
        rank = pd.Series(sample[feats[i]].to_numpy()).rank(pct=True).to_numpy()[swarm_idx]
        beeswarm.append({'feature': feats[i], 'shap': [round(float(v), 3) for v in shap[swarm_idx, i]],
                         'rank': [None if np.isnan(r) else round(float(r), 3) for r in rank],
                         'value': [None if np.isnan(v) else round(float(v), 2) for v in x]})
    out = {'features': len(feats), 'trees': 500, 'rows': int(len(train)), 'sample': int(len(sample)), 'base_kt': round(base, 2),
           'top_features': top, 'families': families, 'dependence': dep, 'interaction': inter, 'by_hour': by_hour, 'tree0': tree0,
           'beeswarm': beeswarm}
    with open('var/mos/explain.json', 'w') as fh:
        json.dump(out, fh, indent=1, default=float)
    print('base', round(base, 2))
    for r in families:
        print(f"  {r['family']:28s} {r['mean_abs_kt']:.2f} kt")
    for r in top[:15]:
        print(f"  {r['feature']:50s} {r['mean_abs_kt']:.2f}")


if __name__ == '__main__':
    main()
