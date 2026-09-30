"""NOAA's AI global model at every station: GraphCast-GFS (2024-02 to 2026-05), then AIGFS (research).

Both are GraphCast-type models run by NOAA on GFS initial conditions and published as 0.25-degree
GRIB2 on AWS (noaa-nws-graphcastgfs-pds): GraphCast-GFS until 2026-05-05 and its operational
successor AIGFS from 2026-04-16. AIGFS is used whenever it exists; `era` records which (0 GraphCast-GFS,
1 AIGFS) so the calibration can tell them apart. For each 00Z run, steps 6-102 h (6-hourly: lead
days 0-3), reads the .idx and range-fetches 10 m wind, 2 m temperature, mean sea-level pressure and
925/850 hPa wind and temperature. Winds are earth-relative on the lat/lon grid.
Writes <root>/aigfs/YYYY-MM.parquet (station column). Incremental like extract_rrfs.py.
Usage: python research/mos/extract_aigfs.py [--root var/mos-multi/models] [--start YYYY-MM] [--procs 16]
"""
import argparse
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from extract_rrfs import KNOTS, STATIONS, decode, get

BUCKET = 'https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com'
FIRST_RUN = pd.Timestamp('2024-02-05')
AIGFS_FROM = pd.Timestamp('2026-04-16')
STEPS = list(range(6, 103, 6))
WANTED = {('UGRD', '10 m above ground'): 'u10', ('VGRD', '10 m above ground'): 'v10', ('TMP', '2 m above ground'): 't2m',
          ('PRMSL', 'mean sea level'): 'mslp', ('UGRD', '925 mb'): 'u925', ('VGRD', '925 mb'): 'v925', ('TMP', '925 mb'): 't925',
          ('UGRD', '850 mb'): 'u850', ('VGRD', '850 mb'): 'v850', ('TMP', '850 mb'): 't850'}


def urls(init, step):
    if init >= AIGFS_FROM:
        base = f'{BUCKET}/aigfs.{init:%Y%m%d}/{init:%H}/model/atmos/grib2/aigfs.t{init:%H}z'
        return [f'{base}.sfc.f{step:03d}.grib2', f'{base}.pres.f{step:03d}.grib2'], 1
    return [f'{BUCKET}/graphcastgfs.{init:%Y%m%d}/{init:%H}/forecasts_13_levels/graphcastgfs.t{init:%H}z.pgrb2.0p25.f{step:03d}'], 0


def step_job(job):
    init, step = job
    files, era = urls(init, step)
    raw = {}
    for url in files:
        idx = get(url + '.idx')
        if not idx:
            return job, None
        lines = [l.split(':') for l in idx.decode().splitlines()]
        offsets = sorted({int(l[1]) for l in lines})
        for line in lines:
            key = (line[3], line[4])
            if key not in WANTED or WANTED[key] in raw:
                continue
            start = int(line[1])
            later = [o for o in offsets if o > start]
            content = get(url, (start, later[0] - 1)) if later else get(url, (start, start + 8_000_000))
            if not content:
                return job, None
            fields = decode(content)
            raw[WANTED[key]] = next(v for k, v in fields.items() if not k.startswith('_'))
    if len(raw) < len(WANTED):
        return job, None
    return job, {'wind_u_10m': raw['u10'], 'wind_v_10m': raw['v10'], 'temperature_2m': [x - 273.15 for x in raw['t2m']],
                 'mslp': [x / 100 for x in raw['mslp']], 'wind_u_925hpa': raw['u925'], 'wind_v_925hpa': raw['v925'],
                 'temperature_925hpa': [x - 273.15 for x in raw['t925']], 'wind_u_850hpa': raw['u850'], 'wind_v_850hpa': raw['v850'],
                 'temperature_850hpa': [x - 273.15 for x in raw['t850']], 'era': [era] * len(raw['u10'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='var/mos-multi/models')
    parser.add_argument('--start', default=None)
    parser.add_argument('--procs', type=int, default=16)
    args = parser.parse_args()
    out = Path(args.root) / 'aigfs'
    out.mkdir(parents=True, exist_ok=True)
    first = max(FIRST_RUN, pd.Timestamp(args.start + '-01')) if args.start else FIRST_RUN
    inits = pd.date_range(first, pd.Timestamp.utcnow().tz_localize(None).normalize(), freq='D')
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out / f'{month}.parquet'
        stored = pd.read_parquet(path) if path.exists() else None
        done = set(stored.init) if stored is not None else set()
        todo = [pd.Timestamp(i) for i in group.values if pd.Timestamp(i) not in done]
        if not todo:
            continue
        started = time.time()
        results = {}
        with ProcessPoolExecutor(max_workers=args.procs) as pool:
            for (init, step), result in pool.map(step_job, [(i, s) for i in todo for s in STEPS], chunksize=2):
                results[(init, step)] = result
        complete = [i for i in todo if all(results.get((i, s)) for s in STEPS)]
        records = [{'station': station, 'init': i, 'lead_h': s, **{k: v[n] for k, v in results[(i, s)].items()}}
                   for i in complete for s in STEPS for n, station in enumerate(STATIONS)]
        if records:
            frames = ([stored] if stored is not None else []) + [pd.DataFrame(records)]
            pd.concat(frames, ignore_index=True).sort_values(['init', 'lead_h', 'station']).to_parquet(path, index=False)
        print(f'  {month}: {len(complete)}/{len(todo)} runs complete, {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    main()
