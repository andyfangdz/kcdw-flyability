"""Calibrated hourly KCDW wind for the next days from the latest issue time; writes var/mos/forecast.json.

Uses the issue-time table (build_issue.py): for the latest issue time already reached (the 04, 11, 16
and 22Z updates), every local hour 06-21 after it, out to 7 days, with each source's newest run
published by then under the same publication-delay rule as training. Ranges are widened by the
conformal widths per lead day (local days from the issue).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

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
    for target in ('sust_mean', 'gust_peak', 'metar_peak'):
        booster = xgb.XGBRegressor(); booster.load_model(ARTIFACTS / f'{target}.json')
        q = np.sort(booster.predict(X), axis=1)
        widen = np.array([conformal['targets'][target][str(int(l))]['widen_kt'] for l in rows.lead_day])
        q[:, 0] -= np.maximum(widen, 0); q[:, 2] += np.maximum(widen, 0)
        out[target] = [[round(float(max(v, 0)), 1) for v in r] for r in q]
    direction = xgb.XGBClassifier(); direction.load_model(ARTIFACTS / 'direction.json')
    probs = direction.predict_proba(X)
    angles = np.radians(np.arange(16) * 22.5)
    vec = probs @ np.c_[np.sin(angles), np.cos(angles)]
    out['dir'] = (np.degrees(np.arctan2(vec[:, 0], vec[:, 1])) % 360).round().astype(int)
    out['dir_confidence'] = np.hypot(vec[:, 0], vec[:, 1]).round(2)
    for name in ('p_gust_ge20', 'p_spread_ge10', 'p_metar_gust'):
        clf = xgb.XGBClassifier(); clf.load_model(ARTIFACTS / f'{name}.json')
        out[name] = clf.predict_proba(X)[:, 1].round(3)
    days = {}
    for date, group in out.groupby('date'):
        days[f'{date:%Y-%m-%d}'] = [{'hour': int(r.hour), 'valid_utc': f'{r.valid:%Y-%m-%dT%H:%M:%SZ}', 'init_utc': f'{r.init:%Y-%m-%dT%H:%M:%SZ}',
                                     'lead_day': int(r.lead_day), 'sust_kt': r.sust_mean, 'gust_kt': r.gust_peak, 'metar_peak_kt': r.metar_peak,
                                     'dir_deg': int(r.dir), 'dir_confidence': round(float(r.dir_confidence), 2), 'p_gust_ge20': round(float(r.p_gust_ge20), 3),
                                     'p_spread_ge10': round(float(r.p_spread_ge10), 3), 'p_metar_gust': round(float(r.p_metar_gust), 3)}
                                    for r in group.itertuples()]
    skill = json.loads(Path('var/mos/benchmark.json').read_text()) if Path('var/mos/benchmark.json').exists() else None
    packet = {'version': 1, 'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'station': 'KCDW',
              'trained_at': meta['trained_at'], 'training_period': [meta['data_from'], meta['data_through']],
              'quantiles': [0.1, 0.5, 0.9], 'coverage_target': 1 - conformal['alpha'], 'skill': skill, 'days': days}
    tmp = OUT.with_suffix('.tmp'); tmp.write_text(json.dumps(packet, indent=1)); tmp.replace(OUT)
    print(f'wrote {OUT}: {len(days)} dates, {len(out)} hours')


if __name__ == '__main__':
    main()
