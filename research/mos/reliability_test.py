"""Out-of-sample reliability of wind-threshold probabilities at KCDW (research).

Targets: the hour's 1-minute peak gust (10-30 kt), mean sustained wind (5-20 kt), and the runway
04/22 crosswind, sustained (5-15 kt) and peak gust (10-25 kt). Production issue-time table
(build_issue.py), production inputs and tree settings, one quantile model per target. Probabilities from:
  quantiles    a 23-level quantile model (1st-99th percentile), P(gust >= T) from its interpolated
               distribution, with an exponential tail beyond the 1st/99th fitted to the 95-99 spread
  classifiers  one binary classifier per threshold (as production does)
  recalibrated the quantile probabilities through an isotonic map fitted on held-out forecasts only: in
               cross-validation on the other folds' out-of-fold forecasts; for the forward year on training
               months held out from a separately trained model (one month in seven)
  raw ensembles  ECMWF (2024 onward) and GEFS (2020 onward): mean and spread as a normal distribution;
               crosswinds use the ensemble-mean wind direction, so those baselines are approximate
  NBM          no probabilities in our archive: as a yes/no forecast, and dressed with its own past errors
               (training rows, by lead day); paired on valid hour and lead day, same-day rows on lead day 1
Skill is also compared on identical hours where every method exists (NBM's archive starts 2024-11).
  climatology  the training months' frequency by calendar month and local hour
Out of sample two ways: month-interleaved cross-validation (2021-06 onward; enough strong-wind days
for 25-30 kt) and the forward year (trained before 2025-10 with an 8-day embargo). Scored at the
11Z update (7 a.m. EDT) so each hour counts once. Writes var/mos/reliability.json (per target;
--targets runs a subset and merges).
Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.stats import norm
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

from forward_test import SPLIT
from model import SINCE, features

LEVELS = np.array([0.01, 0.02] + list(np.round(np.arange(0.05, 0.96, 0.05), 2)) + [0.98, 0.99])
BINS = np.array([0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 1.0001])
DEVICE = os.environ.get('MOS_DEVICE', 'cpu')
COMMON = dict(tree_method='hist', device=DEVICE, n_estimators=500, learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8,
              colsample_bytree=0.6, reg_lambda=5.0, random_state=0)


def exceed(q, threshold):
    """P(y >= T) from sorted quantiles at LEVELS; linear inside, exponential tails outside the 1st/99th."""
    p = np.empty(len(q))
    for i, row in enumerate(q):
        if threshold <= row[0]:
            p[i] = 1 - LEVELS[0] * max(threshold, 0) / max(row[0], 1e-6)
        elif threshold >= row[-1]:
            scale = max((row[-1] - row[-5]) / np.log(5), 0.3)  # 95th-99th spread sets the tail decay
            p[i] = (1 - LEVELS[-1]) * np.exp(-(threshold - row[-1]) / scale)
        else:
            p[i] = 1 - np.interp(threshold, row, LEVELS)
    return np.clip(p, 0, 1)


def fit_quantiles(X, y, train, test):
    m = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=LEVELS, **COMMON)
    m.fit(X[train], y[train])
    return np.sort(m.predict(X[test]), axis=1)


def fit_classifier(X, y, train, test, threshold):
    m = xgb.XGBClassifier(objective='binary:logistic', eval_metric='logloss', **COMMON)
    m.fit(X[train], (y[train] >= threshold).astype(int))
    return m.predict_proba(X[test])[:, 1]


def climatology(t, train, test, threshold, target):
    key = t.date.dt.month.astype(str) + '-' + t.hour.astype(str)
    hit = t[target] >= threshold
    freq = hit[train].groupby(key[train]).mean()
    return key[test].map(freq).fillna(hit[train].mean()).to_numpy()


KNOTS = 3600 / 1852
RUNWAY_DEG = 30


ENSEMBLES = {'raw ECMWF ensemble': {'gust': ('ifs_ens_wind_gust_10m_mean', 'ifs_ens_wind_gust_10m_std'),
                                     'speed': ('ifs_ens_speed_10m_mean', 'ifs_ens_speed_10m_std'), 'uv': ('ifs_ens_wind_u_10m_mean', 'ifs_ens_wind_v_10m_mean')},
             'raw GEFS ensemble': {'gust': ('gefs_wind_gust_surface_mean', 'gefs_wind_gust_surface_std'),
                                   'speed': ('gefs_speed_10m_mean', 'gefs_speed_10m_std'), 'uv': ('gefs_wind_u_10m_mean', 'gefs_wind_v_10m_mean')}}


def across_deg(direction):
    return np.abs(np.sin(np.radians(direction - RUNWAY_DEG)))


def raw_ensemble(t, test, threshold, target, name):
    """The ensemble's mean and spread as a normal; crosswinds use its mean wind direction (approximate)."""
    e = ENSEMBLES[name]
    mean_col, sd_col = e['gust'] if target in ('gust_peak', 'xw_gust_peak') else e['speed']
    mu, sd = t[mean_col].to_numpy()[test], t[sd_col].to_numpy()[test]
    if target.startswith('xw_'):
        u, v = t[e['uv'][0]].to_numpy()[test], t[e['uv'][1]].to_numpy()[test]
        a = across_deg(np.degrees(np.arctan2(-u, -v)))
        mu, sd = mu * a, sd * a
    return 1 - norm.cdf((threshold - mu) / np.maximum(sd, 0.5))


def nbm_values(t):
    """NBM's single-value forecast for every row and target, paired on (valid hour, lead day); same-day rows use lead day 1."""
    nbm = pd.read_parquet('var/mos/benchmark_nbm.parquet').drop_duplicates(['valid', 'lead_day'])
    key = pd.DataFrame({'valid': t.valid, 'lead_day': np.maximum(t.lead_day.to_numpy(), 1)})
    j = key.merge(nbm[['valid', 'lead_day', 'wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m']], on=['valid', 'lead_day'], how='left')
    a = across_deg(j.wind_direction_10m.to_numpy())
    return {'gust_peak': j.wind_gusts_10m.to_numpy(), 'sust_mean': j.wind_speed_10m.to_numpy(),
            'xw_sust_mean': j.wind_speed_10m.to_numpy() * a, 'xw_gust_peak': j.wind_gusts_10m.to_numpy() * a, 'lead': key.lead_day.to_numpy()}


def nbm_dressed(nbm, y, train, test, threshold, target):
    """P(NBM + one of its past errors >= threshold): NBM's errors in the training rows, by lead day."""
    v = nbm[target]
    has_train, out = train & ~np.isnan(v) & ~np.isnan(y), np.full(int(test.sum()), np.nan)
    vt, lt = v[test], nbm['lead'][test]
    pooled = np.sort(y[has_train] - v[has_train])
    for lead in np.unique(lt):
        res = np.sort((y - v)[has_train & (nbm['lead'] == lead)])
        res = res if len(res) >= 200 else pooled
        m = (lt == lead) & ~np.isnan(vt)
        if len(res) and m.any():
            out[m] = 1 - np.searchsorted(res, threshold - vt[m], side='left') / len(res)
    return out


SPECS = {'gust_peak': {'thresholds': (10, 15, 20, 25, 30), 'classifier': True},
         'sust_mean': {'thresholds': (5, 10, 15, 20), 'classifier': True},
         'xw_sust_mean': {'thresholds': (5, 10, 15), 'classifier': True},
         'xw_gust_peak': {'thresholds': (10, 15, 20, 25), 'classifier': True}}
RECAL = 'quantiles, recalibrated'


def isotonic(p_fit, o_fit, p_apply):
    """Monotone map from forecast probability to observed frequency, fitted on held-out forecasts only."""
    ok = ~np.isnan(p_fit)
    iso = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(p_fit[ok], o_fit[ok])
    out = np.full(len(p_apply), np.nan)
    good = ~np.isnan(p_apply)
    out[good] = iso.predict(p_apply[good])
    return out


def wilson(k, n, z=1.645):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


def score(p, o, days, clim):
    ok = ~np.isnan(p)
    p, o, days, clim = p[ok], o[ok], days[ok], clim[ok]
    brier, brier_clim = float(np.mean((p - o) ** 2)), float(np.mean((clim - o) ** 2))
    curve, rel, res = [], 0.0, 0.0
    base = o.mean()
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        m = (p >= lo) & (p < hi)
        n = int(m.sum())
        if n == 0:
            continue
        f, freq = float(p[m].mean()), float(o[m].mean())
        n_days = int(pd.Series(days[m]).nunique())  # windy hours cluster in days: interval uses distinct days
        lo_ci, hi_ci = wilson(freq * n_days, n_days)
        curve.append({'bin': [float(lo), float(min(hi, 1))], 'forecast': round(f, 4), 'observed': round(freq, 4), 'hours': n, 'days': n_days,
                      'ci90': [round(float(lo_ci), 4), round(float(hi_ci), 4)]})
        rel += n * (f - freq) ** 2
        res += n * (freq - base) ** 2
    auc = float(roc_auc_score(o, p)) if 0 < o.sum() < len(o) else float('nan')
    return {'hours': int(len(o)), 'events': int(o.sum()), 'base_rate': round(float(base), 4), 'brier': round(brier, 5),
            'bss_vs_climatology': round(1 - brier / brier_clim, 4) if brier_clim > 0 else None,
            'reliability': round(rel / len(o), 5), 'resolution': round(res / len(o), 5), 'roc_auc': round(auc, 4), 'curve': curve}


def probabilities(t, X, y, train, test, target, nbm):
    spec = SPECS[target]
    q = fit_quantiles(X, y, train, test)
    out = {}
    for thr in spec['thresholds']:
        out[f'quantiles_{thr}'] = exceed(q, thr)
        if spec['classifier']:
            out[f'classifier_{thr}'] = fit_classifier(X, y, train, test, thr)
        out[f'climatology_{thr}'] = climatology(t, train, test, thr, target)
        out[f'nbm_dressed_{thr}'] = nbm_dressed(nbm, y, train, test, thr, target)
    return out


def scored(t, y, idx, probs, target, nbm):
    days = t.date.to_numpy()[idx]
    out = {}
    for thr in SPECS[target]['thresholds']:
        o = (y[idx] >= thr).astype(float)
        clim = probs[f'climatology_{thr}']
        v = nbm[target][idx]
        methods = {'quantiles': probs[f'quantiles_{thr}']}
        if f'recal_{thr}' in probs:
            methods[RECAL] = probs[f'recal_{thr}']
        if f'classifier_{thr}' in probs:
            methods['classifier'] = probs[f'classifier_{thr}']
        for name in ENSEMBLES:
            methods[name] = raw_ensemble(t, idx, thr, target, name)
        methods['NBM (yes/no)'] = np.where(np.isnan(v), np.nan, (v >= thr).astype(float))
        methods['NBM + its past errors'] = probs[f'nbm_dressed_{thr}']
        methods['climatology'] = clim
        out[str(thr)] = {name: score(p, o, days, clim) for name, p in methods.items()}
        # Identical hours: where every method has a forecast (NBM's archive from 2024-11, ECMWF's from 2024).
        common = np.all([~np.isnan(p) for p in methods.values()], axis=0)
        bc = float(np.mean((clim[common] - o[common]) ** 2))
        out[str(thr)]['common'] = {'hours': int(common.sum()), 'events': int(o[common].sum()),
                                   'bss': {name: round(1 - float(np.mean((p[common] - o[common]) ** 2)) / bc, 4) if bc > 0 else None
                                           for name, p in methods.items() if name != 'climatology'}}
        r = out[str(thr)]
        print(f'  {target} >= {thr} kt: base {r["quantiles"]["base_rate"]:.3f} ({r["quantiles"]["events"]} events); same {r["common"]["hours"]} hours BSS: '
              + ', '.join(f'{k} {v}' for k, v in r['common']['bss'].items()), flush=True)
    return out


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--targets', nargs='+', default=list(SPECS))
    args = parser.parse_args()
    t = pd.read_parquet('var/mos/table_issue.parquet')
    t = t[(t.date >= SINCE) & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    feats = features(t)
    X = t[feats].to_numpy('float32')
    morning = (t.init.dt.hour == 11).to_numpy()
    nbm = nbm_values(t)
    fold = ((t.date.dt.year * 12 + t.date.dt.month) % 5).to_numpy()
    path = 'var/mos/reliability.json'
    result = json.load(open(path)) if os.path.exists(path) else {}
    if 'targets' not in result:  # an earlier gust-only file
        result = {'issue': '11Z (7 a.m. EDT)', 'targets': {'gust_peak': {'cv': result['cv'], 'forward': result['forward']}} if 'cv' in result else {}}
    for target in args.targets:
        y = t[target].to_numpy('float32')
        labelled = ~np.isnan(y)
        pooled, idx = {}, []
        for k in range(5):
            test = morning & (fold == k) & labelled
            part = probabilities(t, X, y, (fold != k) & labelled, test, target, nbm)
            idx.append(np.flatnonzero(test))
            for key, v in part.items():
                pooled.setdefault(key, []).append(v)
        idx = np.concatenate(idx)
        merged = {k: np.concatenate(v) for k, v in pooled.items()}
        fold_of = fold[idx]
        for thr in SPECS[target]['thresholds']:
            pq, o = merged[f'quantiles_{thr}'], (y[idx] >= thr).astype(float)
            merged[f'recal_{thr}'] = np.full(len(idx), np.nan)
            for k in range(5):
                m = fold_of == k
                merged[f'recal_{thr}'][m] = isotonic(pq[~m], o[~m], pq[m])
        print(f'{target}: cross-validation', flush=True)
        cv = scored(t, y, idx, merged, target, nbm)
        train = (t.init < SPLIT - pd.Timedelta(days=8)).to_numpy() & labelled
        test = morning & (t.init >= SPLIT).to_numpy() & labelled
        print(f'{target}: forward year', flush=True)
        probs = probabilities(t, X, y, train, test, target, nbm)
        months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
        proper, calib = train & (months % 7 != 3), train & (months % 7 == 3) & morning
        both = calib | test
        q = fit_quantiles(X, y, proper, both)  # trained without the calibration months
        order = np.flatnonzero(both)
        in_calib = calib[order]
        for thr in SPECS[target]['thresholds']:
            p = exceed(q, thr)
            probs[f'recal_{thr}'] = isotonic(p[in_calib], (y[order][in_calib] >= thr).astype(float), p[~in_calib])
        forward = scored(t, y, np.flatnonzero(test), probs, target, nbm)
        result['targets'][target] = {'cv': cv, 'forward': forward}
        json.dump(result, open(path, 'w'), indent=1)


if __name__ == '__main__':
    main()
