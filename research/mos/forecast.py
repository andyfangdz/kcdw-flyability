"""Calibrated hourly KCDW wind for the next days from the latest issue time; writes var/mos/forecast.json.

Uses the issue-time table (build_issue.py): for the latest issue time already reached (the 04, 11, 16
and 22Z updates), every local hour 06-21 after it, out to 7 days, with each source's newest run
published by then under the same publication-delay rule as training. Ranges are widened by the
conformal widths per lead day (local days from the issue). Threshold probabilities come from the 23-level
quantile models (probability.py); spread and METAR-gust probabilities from their classifiers.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from probability import exceed
from train import EXCEEDANCE

ARTIFACTS = Path('var/mos/artifacts')
OUT = Path('var/mos/forecast.json')
MIN_MODEL_FEATURES = 10
TZ = 'America/New_York'


def main():
    meta = json.loads((ARTIFACTS / 'meta.json').read_text())
    conformal = json.loads(Path('var/mos/conformal.json').read_text())
    feats = meta['features']
    t = pd.read_parquet('var/mos/table_issue.parquet')
    today = pd.Timestamp.now(tz=TZ).normalize().tz_localize(None)
    # The latest issue time (04/11/16/22Z) already reached: build_issue.py chose each source's newest run published by then
    # (falling back to its previous run when late), exactly as for the training rows.
    issue = t.init[t.init <= pd.Timestamp.now(tz='UTC').tz_localize(None)].max()
    t = t[(t.init == issue) & (t.date <= today + pd.Timedelta(days=7))].copy()
    model_cols = [c for c in feats if not c.startswith(('hour_', 'doy_', 'lead_', 'slot')) and not c.endswith('_age_h')]
    rows = t[t[model_cols].notna().sum(axis=1) >= MIN_MODEL_FEATURES].sort_values(['date', 'hour']).reset_index(drop=True)
    X = rows.reindex(columns=feats).to_numpy('float32')
    out = rows[['date', 'hour', 'lead_day', 'valid', 'init']].copy()
    for target in meta.get('quantile_targets', ['sust_mean', 'gust_peak', 'metar_peak']):
        booster = xgb.XGBRegressor(); booster.load_model(ARTIFACTS / f'{target}.json')
        q = np.sort(booster.predict(X), axis=1)
        widths = conformal['targets'].get(target, {})  # a new target has no width until the weekly cross-validation
        widen = np.array([widths.get(str(int(l)), {}).get('widen_kt', 0.0) for l in rows.lead_day])
        q[:, 0] -= np.maximum(widen, 0); q[:, 2] += np.maximum(widen, 0)
        out[target] = [[round(float(max(v, 0)), 1) for v in r] for r in q]
    direction = xgb.XGBClassifier(); direction.load_model(ARTIFACTS / 'direction.json')
    probs = direction.predict_proba(X)
    angles = np.radians(np.arange(16) * 22.5)
    vec = probs @ np.c_[np.sin(angles), np.cos(angles)]
    out['dir'] = (np.degrees(np.arctan2(vec[:, 0], vec[:, 1])) % 360).round().astype(int)
    out['dir_confidence'] = np.hypot(vec[:, 0], vec[:, 1]).round(2)
    for xw, total in (('xw_sust_mean', 'sust_mean'), ('xw_gust_peak', 'gust_peak')):  # separate models: crosswind <= the wind itself
        if xw in out and total in out:
            out[xw] = [[min(a, b) for a, b in zip(x, w)] for x, w in zip(out[xw], out[total])]
    distributions = {}
    for target in meta.get('distributions', {}):  # one 23-level model per target gives every threshold, in order
        booster = xgb.XGBRegressor(); booster.load_model(ARTIFACTS / f'{target}_levels.json')
        distributions[target] = np.sort(booster.predict(X), axis=1)
    probs = [name for name, (column, _) in EXCEEDANCE.items() if column in distributions]
    for name in probs:
        column, threshold = EXCEEDANCE[name]
        out[name] = exceed(distributions[column], threshold)
    # Models from before the switch (meta 'probabilities') hold a classifier for every threshold.
    classifiers = [n for n in meta.get('classifiers', meta.get('probabilities', ['p_gust_ge20', 'p_spread_ge10', 'p_metar_gust'])) if n not in probs]
    for name in classifiers:
        clf = xgb.XGBClassifier(); clf.load_model(ARTIFACTS / f'{name}.json')
        out[name] = clf.predict_proba(X)[:, 1]
    probs += classifiers
    # Separate classifiers can rank a higher threshold above a lower one; keep each family in order.
    for low, high in (('p_gust_ge20', 'p_gust_ge25'), ('p_xw_ge10', 'p_xw_ge15'), ('p_xwgust_ge15', 'p_xwgust_ge20')):
        if low in out and high in out:
            out[high] = np.minimum(out[high], out[low])
    for name in probs:
        out[name] = out[name].round(3)
    days = {}
    for date, group in out.groupby('date'):
        days[f'{date:%Y-%m-%d}'] = [{'hour': int(r.hour), 'valid_utc': f'{r.valid:%Y-%m-%dT%H:%M:%SZ}', 'init_utc': f'{r.init:%Y-%m-%dT%H:%M:%SZ}',
                                     'lead_day': int(r.lead_day), 'sust_kt': r.sust_mean, 'gust_kt': r.gust_peak, 'metar_peak_kt': r.metar_peak,
                                     'dir_deg': int(r.dir), 'dir_confidence': round(float(r.dir_confidence), 2),
                                     **({'xw_sust_kt': r.xw_sust_mean, 'xw_gust_kt': r.xw_gust_peak} if 'xw_gust_peak' in out else {}),
                                     **{name: round(float(getattr(r, name)), 3) for name in probs}}
                                    for r in group.itertuples()]
    skill = json.loads(Path('var/mos/benchmark.json').read_text()) if Path('var/mos/benchmark.json').exists() else None
    packet = {'version': 1, 'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'station': 'KCDW',
              'trained_at': meta['trained_at'], 'training_period': [meta['data_from'], meta['data_through']],
              'quantiles': [0.1, 0.5, 0.9], 'coverage_target': 1 - conformal['alpha'], 'skill': skill, 'days': days}
    tmp = OUT.with_suffix('.tmp'); tmp.write_text(json.dumps(packet, indent=1)); tmp.replace(OUT)
    print(f'wrote {OUT}: {len(days)} dates, {len(out)} hours')


if __name__ == '__main__':
    main()
