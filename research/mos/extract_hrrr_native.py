"""Native HRRR boundary-layer fields at every station for lead day 1 (research).

dynamical.org's HRRR archive has no boundary-layer fields, so this reads NOAA's native
wrfsfc GRIB2 on AWS (noaa-hrrr-bdp-pds) with .idx byte ranges, reusing extract_rrfs.py's
fetch and decode. HRRR's 00Z run reaches 48 h from 2020-12-03, so only lead day 1 (34-48 h,
local 06-20) is covered. Fields: boundary-layer height, friction velocity, the hour's
maximum 10 m wind, 0-1 km shear, 925 and 850 hPa wind speed (speeds are rotation-invariant,
so grid-relative components need no rotation), surface CAPE.
Writes <root>/hrrrn/YYYY-MM.parquet (station column). Incremental like extract_rrfs.py.
Usage: python research/mos/extract_hrrr_native.py [--root var/mos-multi/models] [--start YYYY-MM] [--procs 16]
"""
import argparse
import math
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from extract_rrfs import KNOTS, STATIONS, decode, get

BUCKET = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
FIRST_RUN = pd.Timestamp('2020-12-03')
STEPS = list(range(34, 49))
WANTED = {('HPBL', 'surface'): 'pbl_height', ('FRICV', 'surface'): 'friction_velocity', ('WIND', '10 m above ground'): 'wind_max_10m',
          ('VUCSH', '0-1000 m above ground'): 'shear_u', ('VVCSH', '0-1000 m above ground'): 'shear_v', ('UGRD', '925 mb'): 'u925',
          ('VGRD', '925 mb'): 'v925', ('UGRD', '850 mb'): 'u850', ('VGRD', '850 mb'): 'v850', ('CAPE', 'surface'): 'cape'}


def step_job(job):
    init, step = job
    url = f'{BUCKET}/hrrr.{init:%Y%m%d}/conus/hrrr.t{init:%H}z.wrfsfcf{step:02d}.grib2'
    idx = get(url + '.idx')
    if not idx:
        return job, None
    lines = [l.split(':') for l in idx.decode().splitlines()]
    offsets = sorted({int(l[1]) for l in lines})
    raw = {}
    for line in lines:
        key = (line[3], line[4])
        if key not in WANTED or WANTED[key] in raw:
            continue
        start = int(line[1])
        later = [o for o in offsets if o > start]
        content = get(url, (start, later[0] - 1)) if later else None
        if not content:
            return job, None
        fields = decode(content)
        raw[WANTED[key]] = next(v for k, v in fields.items() if not k.startswith('_'))
    if len(raw) < len(WANTED):
        return job, None
    speed = lambda u, v: [math.hypot(a, b) * KNOTS for a, b in zip(u, v)]
    return job, {'pbl_height': raw['pbl_height'], 'friction_velocity': raw['friction_velocity'], 'cape': raw['cape'],
                 'wind_max_10m': [x * KNOTS for x in raw['wind_max_10m']], 'shear_0_1km': speed(raw['shear_u'], raw['shear_v']),
                 'wind_925': speed(raw['u925'], raw['v925']), 'wind_850': speed(raw['u850'], raw['v850'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='var/mos-multi/models')
    parser.add_argument('--start', default=None)
    parser.add_argument('--procs', type=int, default=16)
    args = parser.parse_args()
    out = Path(args.root) / 'hrrrn'
    out.mkdir(parents=True, exist_ok=True)
    first = max(FIRST_RUN, pd.Timestamp(args.start + '-01')) if args.start else FIRST_RUN
    inits = pd.date_range(first, pd.Timestamp.utcnow().tz_localize(None).normalize() - pd.Timedelta(days=1), freq='D')
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
