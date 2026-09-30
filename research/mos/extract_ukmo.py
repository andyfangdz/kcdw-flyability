"""Met Office global deterministic 10 km at every station, from the Met Office's AWS open data (research).

Bucket met-office-atmospheric-model-data/global-deterministic-10km holds one
NetCDF4 file per run, variable and step from 2024-09-27 (hourly steps to 54 h,
3-hourly to 144 h, 6-hourly to 168 h). Files are gzip-chunked in 128x128 tiles, so
all stations are read from one or two tiles with HTTP range requests. Only 00Z
runs and steps valid 09Z-02Z (the 6 a.m.-9 p.m. Eastern window plus neighbours
for interpolation) are read. Writes <root>/ukmo/YYYY-MM.parquet (station column).
Usage: var/mos-venv/bin/python research/mos/extract_ukmo.py [--root var/mos-multi/models] [--start YYYY-MM] [--threads 32]
"""
import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

BUCKET = 'https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/global-deterministic-10km'
STEPS = [s for s in list(range(1, 55)) + list(range(57, 145, 3)) + list(range(150, 169, 6)) if s % 24 >= 9 or s % 24 <= 2]
VARIABLES = {'wind_speed_at_10m': ('wind_speed', 'speed_10m', 3600 / 1852), 'wind_direction_at_10m': ('wind_from_direction', 'dir_10m', 1),
             'wind_gust_at_10m': ('wind_speed_of_gust', 'gust_10m', 3600 / 1852),
             'temperature_at_screen_level': ('air_temperature', 'temperature_2m', 1),
             'temperature_of_dew_point_at_screen_level': ('dew_point_temperature', 'dew_point_2m', 1)}
STATIONS = json.loads(Path(__file__).with_name('stations.json').read_text())
FIRST_RUN = pd.Timestamp('2024-09-28')


def read(url, var, index):
    import fsspec
    import h5py
    fs = fsspec.filesystem('https', block_size=64 * 1024)
    with fs.open(url) as f, h5py.File(f, 'r') as h:
        name = var if var in h else next(k for k in h if h[k].ndim == 2)
        if index.get('grid') is None:
            lat, lon = h['latitude'][:], h['longitude'][:]
            iy = [int(np.abs(lat - s['lat']).argmin()) for s in STATIONS.values()]
            ix = [int(np.abs(((lon - s['lon']) + 180) % 360 - 180).argmin()) for s in STATIONS.values()]
            index['grid'] = (iy, ix)
        iy, ix = index['grid']
        y0, x0 = min(iy), min(ix)
        block = h[name][y0:max(iy) + 1, x0:max(ix) + 1]
        return [float(block[a - y0, b - x0]) for a, b in zip(iy, ix)]


def fetch(job, index):
    init, step, file_var = job
    nc_var = VARIABLES[file_var][0]
    valid = init + pd.Timedelta(hours=step)
    url = f'{BUCKET}/{init:%Y%m%dT%H%MZ}/{valid:%Y%m%dT%H%MZ}-PT{step:04d}H00M-{file_var}.nc'
    for attempt in range(3):
        try:
            return job, read(url, nc_var, index)
        except FileNotFoundError:
            return job, None
        except Exception:
            time.sleep(2 * (attempt + 1))
    return job, None


_INDEX = {'grid': None}


def fetch_job(job):
    """Process-pool entry: h5py serializes threads, so parallel reads need processes."""
    return fetch(job, _INDEX)


def assemble(results):
    rows = {}
    for (init, step, file_var), values in results:
        if values is None:
            continue
        _, col, scale = VARIABLES[file_var]
        for station, v in zip(STATIONS, values):
            rows.setdefault((station, init, step), {})[col] = v * scale - (273.15 if col in ('temperature_2m', 'dew_point_2m') else 0)
    return [{'station': s, 'init': i, 'lead_h': st, **cols} for (s, i, st), cols in rows.items()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='var/mos-multi/models')
    parser.add_argument('--start', default=None)
    parser.add_argument('--threads', type=int, default=48)
    args = parser.parse_args()
    out = Path(args.root) / 'ukmo'
    out.mkdir(parents=True, exist_ok=True)
    inits = pd.date_range(max(FIRST_RUN, pd.Timestamp(args.start + '-01')) if args.start else FIRST_RUN,
                          pd.Timestamp.utcnow().tz_localize(None).normalize(), freq='D')
    current = pd.Timestamp.utcnow().strftime('%Y-%m')
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out / f'{month}.parquet'
        if path.exists() and month != current:
            continue
        started = time.time()
        # Parallel within a run (steps x variables) and across runs.
        jobs = [(pd.Timestamp(i), step, v) for i in group.values for step in STEPS for v in VARIABLES]
        with ProcessPoolExecutor(max_workers=args.threads) as pool:
            records = assemble(pool.map(fetch_job, jobs, chunksize=8))
        if records:
            frame = pd.DataFrame(records)
            rad = np.radians(frame.dir_10m)
            speed = frame.speed_10m * 1852 / 3600  # back to m/s for u/v, matching the other archives
            frame['wind_u_10m'], frame['wind_v_10m'] = -speed * np.sin(rad), -speed * np.cos(rad)
            frame.drop(columns=['dir_10m']).to_parquet(path, index=False)
        runs = frame.init.nunique() if records else 0
        print(f'  {month}: {runs}/{len(group)} runs, {len(records)} rows, {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    main()
