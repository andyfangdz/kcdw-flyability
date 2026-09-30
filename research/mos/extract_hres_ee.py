"""ECMWF IFS HRES (deterministic) at every station from Earth Engine's ECMWF/NRT_FORECAST/IFS/OPER (research).

The archive starts late 2024. Earth Engine georeferences this collection one
0.25-degree row north of the GRIB grid (verified in kcdw/ecmwf_ee_worker.py
against native GRIB and the land-sea mask), so each station is sampled
ROW_SHIFT degrees north. One server-side query per 00Z run returns all stations
and all steps. Writes <root>/hres/YYYY-MM.parquet (station column).
Usage: var/mos-venv/bin/python research/mos/extract_hres_ee.py [--root var/mos-multi/models] [--start YYYY-MM]
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

COLLECTION = 'ECMWF/NRT_FORECAST/IFS/OPER'
ROW_SHIFT = 0.25
STEPS = [s for s in list(range(3, 145, 3)) + list(range(150, 199, 6)) if s % 24 >= 9 or s % 24 <= 2]
# Source band (regex allowed) -> column. The gust band is "last_3h" at 3-hourly steps and
# "since_last_post_processing" at 6-hourly steps, so it is matched by pattern.
BANDS = {'u_component_of_wind_10m_sfc': 'wind_u_10m', 'v_component_of_wind_10m_sfc': 'wind_v_10m',
         'u_component_of_wind_100m_sfc': 'wind_u_100m', 'v_component_of_wind_100m_sfc': 'wind_v_100m',
         'temperature_2m_sfc': 'temperature_2m', 'dewpoint_temperature_2m_sfc': 'dew_point_2m',
         'max_10m_wind_gust_.*': 'gust_window', 'mean_sea_level_pressure_sfc': 'mslp',
         'temperature_pl925': 'temperature_925hpa', 'temperature_pl850': 'temperature_850hpa',
         'u_component_of_wind_pl925': 'wind_u_925hpa', 'v_component_of_wind_pl925': 'wind_v_925hpa',
         'u_component_of_wind_pl850': 'wind_u_850hpa', 'v_component_of_wind_pl850': 'wind_v_850hpa',
         'most_unstable_convective_available_potential_energy_sfc': 'cape'}
STATIONS = json.loads(Path(__file__).with_name('stations.json').read_text())
KNOTS = 3600 / 1852


def init_ee():
    import ee
    import google.auth
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform', 'https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials=credentials, project='aviation-486817')
    return ee


def run(ee, init):
    points = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([s['lon'], s['lat'] + ROW_SHIFT]), {'station': k}) for k, s in STATIONS.items()])
    images = (ee.ImageCollection(COLLECTION).filter(ee.Filter.eq('creation_year', init.year)).filter(ee.Filter.eq('creation_month', init.month))
              .filter(ee.Filter.eq('creation_day', init.day)).filter(ee.Filter.eq('creation_hour', 0))
              .filter(ee.Filter.inList('forecast_hours', STEPS)).select(list(BANDS), list(BANDS.values())))

    def sample(img):
        return img.reduceRegions(collection=points, reducer=ee.Reducer.first(), crs=img.select(0).projection()).map(
            lambda f: f.set('lead_h', img.get('forecast_hours')))

    features = images.map(sample).flatten().getInfo()['features']
    rows = []
    for f in features:
        p = f['properties']
        if all(p.get(c) is None for c in BANDS.values()):
            continue
        row = {'station': p['station'], 'init': init, 'lead_h': int(p['lead_h'])}
        for col in BANDS.values():
            v = p.get(col)
            row[col] = np.nan if v is None else float(v)
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='var/mos-multi/models')
    parser.add_argument('--start', default='2024-11')
    args = parser.parse_args()
    ee = init_ee()
    out = Path(args.root) / 'hres'
    out.mkdir(parents=True, exist_ok=True)
    inits = pd.date_range(args.start + '-01', pd.Timestamp.utcnow().tz_localize(None).normalize(), freq='D')
    current = pd.Timestamp.utcnow().strftime('%Y-%m')
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out / f'{month}.parquet'
        if path.exists() and month != current:
            continue
        started = time.time()

        def one(init):
            for attempt in range(4):
                try:
                    return run(ee, pd.Timestamp(init))
                except Exception as exc:
                    time.sleep(10 * (attempt + 1))
            print(f'  {pd.Timestamp(init):%Y-%m-%d} failed', flush=True)
            return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            records = [r for rows in pool.map(one, group.values) for r in rows]
        if records:
            frame = pd.DataFrame(records)
            frame['gust_window'] *= KNOTS
            for col in ('temperature_2m', 'dew_point_2m', 'temperature_925hpa', 'temperature_850hpa'):
                frame[col] -= 273.15
            frame['mslp'] /= 100
            frame.to_parquet(path, index=False)
        print(f'  {month}: {len({r["init"] for r in records})}/{len(group)} runs, {len(records)} rows, {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    main()
