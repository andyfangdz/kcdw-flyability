"""ICON, ECMWF HRES and GEM point forecasts at KCDW from Open-Meteo's previous-runs archive.

``previous_dayN`` holds, for each valid hour, the value from the run issued N days
earlier. Archives start in early 2024. Writes var/mos/models/om_<model>.parquet
with one row per (valid hour, lead_day).
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

MODELS = {'icon': 'icon_global', 'hres': 'ecmwf_ifs025', 'gem': 'gem_global'}
VARIABLES = ('wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m', 'temperature_2m', 'dew_point_2m', 'cloud_cover')
OUT = Path('var/mos/models')
URL = 'https://previous-runs-api.open-meteo.com/v1/forecast'


def fetch(model, day, start='2024-01-01'):
    params = dict(latitude=40.8752, longitude=-74.2814, models=model, wind_speed_unit='kn', timezone='UTC',
                  start_date=start, end_date=date.today().isoformat(),
                  hourly=','.join(f'{v}_previous_day{day}' for v in VARIABLES))
    for attempt in range(4):
        try:
            data = json.load(urllib.request.urlopen(URL + '?' + urllib.parse.urlencode(params), timeout=180))
            break
        except Exception:
            time.sleep(20 * (attempt + 1))
    else:
        raise RuntimeError(f'{model} day {day} failed')
    hourly = data['hourly']
    frame = pd.DataFrame({v: hourly[f'{v}_previous_day{day}'] for v in VARIABLES}, dtype='float32')
    frame.insert(0, 'lead_day', day)
    frame.insert(0, 'valid', pd.to_datetime(hourly['time']))
    return frame.dropna(subset=['wind_speed_10m'])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for key, model in MODELS.items():
        frames = []
        for day in range(1, 8):
            frames.append(fetch(model, day))
            time.sleep(3)
        table = pd.concat(frames, ignore_index=True)
        table.to_parquet(OUT / f'om_{key}.parquet', index=False)
        print(f'{key}: {len(table)} rows, {table.valid.min()} .. {table.valid.max()}', flush=True)


if __name__ == '__main__':
    main()
