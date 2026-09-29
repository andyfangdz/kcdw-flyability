"""Calibrated hourly KCDW wind for the next days, from the newest runs; writes var/mos/forecast.json.

For each local date from today to today+7 and each local hour 06-21, uses the
training-table row with the shortest lead whose 00Z runs are already extracted
(at least MIN_MODEL_FEATURES present, the same row construction as training).
ICON, ECMWF HRES and GEM come from a past-only archive in training, so for
future hours their values are filled from that same 00Z run via Open-Meteo's
single-runs API. Ranges are widened by the conformal widths per lead day.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

ARTIFACTS = Path('var/mos/artifacts')
OUT = Path('var/mos/forecast.json')
MIN_MODEL_FEATURES = 10
OPEN_METEO = {'icon': 'icon_global', 'hres': 'ecmwf_ifs025', 'gem': 'gem_global'}
OM_VARS = ('wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m', 'temperature_2m', 'dew_point_2m', 'cloud_cover')
TZ = 'America/New_York'


def single_run(model, init):
    params = dict(latitude=40.8752, longitude=-74.2814, models=model, hourly=','.join(OM_VARS), wind_speed_unit='kn',
                  timezone='UTC', forecast_days=9, run=init.strftime('%Y-%m-%dT%H:%M'))
    url = 'https://single-runs-api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(params)
    data = json.load(urllib.request.urlopen(url, timeout=60))
    hourly = data['hourly']
    frame = pd.DataFrame({v: hourly[v] for v in OM_VARS}, index=pd.to_datetime(hourly['time']), dtype='float32')
    return frame


def fill_open_meteo(rows):
    cache = {}
    for i, row in rows.iterrows():
        for key, model in OPEN_METEO.items():
            if f'{key}_wind_speed_10m' in rows and not np.isnan(row[f'{key}_wind_speed_10m']):
                continue
            if (model, row.init) not in cache:
                try:
                    cache[(model, row.init)] = single_run(model, row.init)
                except Exception:
                    cache[(model, row.init)] = None
            frame = cache[(model, row.init)]
            if frame is None or row.valid not in frame.index:
                continue
            values = frame.loc[row.valid]
            for var in OM_VARS:
                rows.at[i, f'{key}_{var}'] = values[var]
            rad = np.radians(values['wind_direction_10m'])
            rows.at[i, f'{key}_dir_sin'], rows.at[i, f'{key}_dir_cos'] = np.sin(rad), np.cos(rad)
    return rows


def main():
    meta = json.loads((ARTIFACTS / 'meta.json').read_text())
    conformal = json.loads(Path('var/mos/conformal.json').read_text())
    feats = meta['features']
    t = pd.read_parquet('var/mos/table.parquet')
    today = pd.Timestamp.now(tz=TZ).normalize().tz_localize(None)
    t = t[(t.date >= today) & (t.date <= today + pd.Timedelta(days=7))].copy()
    model_cols = [c for c in feats if c.split('_')[0] in ('gfs', 'gefs', 'ifs', 'aifs', 'hrrr', 'wn2', 'wn3')]
    t['present'] = t[model_cols].notna().sum(axis=1)
    rows = t[t.present >= MIN_MODEL_FEATURES].sort_values(['date', 'hour', 'lead_day']).groupby(['date', 'hour']).head(1).reset_index(drop=True)
    rows = fill_open_meteo(rows)
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
