"""NOAA RRFS 3 km (2dfld CONUS) at every station from the AWS open-data bucket (research).

RRFS replaces NAM operationally on 2026-10-14; noaa-rrfs-ops-pds keeps every run of its
parallel feed from 2026-08-12. For each 00Z run and each forecast hour that lands on local
06-21 of lead days 1-3 (34-49, 58-73, 82-84 h), reads the .idx and range-fetches only the
wanted messages: 10 m, 80 m and boundary-layer u/v, surface gust, 2 m and 80 m
temperature, 2 m dew point, boundary-layer height, friction velocity, surface CAPE and
total cloud. One CONUS field serves all stations. Winds are grid-relative on the Lambert
grid and are rotated to true north. Writes <root>/rrfs/YYYY-MM.parquet (station column).
Usage: var/mos-venv/bin/python research/mos/extract_rrfs.py [--root var/mos-multi/models] [--start YYYY-MM] [--procs 8]
"""
import argparse
import json
import math
import os
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

BUCKET = 'https://noaa-rrfs-ops-pds.s3.amazonaws.com'
FIRST_RUN = pd.Timestamp('2026-08-12')
STEPS = list(range(34, 50)) + list(range(58, 74)) + list(range(82, 85))  # local 06-21 on lead days 1-3 (EDT and EST)
WANTED = {('UGRD', '10 m above ground'): 'wind_10m', ('VGRD', '10 m above ground'): 'wind_10m_v', ('UGRD', '80 m above ground'): 'wind_80m',
          ('VGRD', '80 m above ground'): 'wind_80m_v', ('UGRD', 'planetary boundary layer'): 'wind_pbl', ('VGRD', 'planetary boundary layer'): 'wind_pbl_v',
          ('GUST', 'surface'): 'gust', ('TMP', '2 m above ground'): 'temperature_2m', ('TMP', '80 m above ground'): 'temperature_80m',
          ('DPT', '2 m above ground'): 'dew_point_2m', ('HPBL', 'surface'): 'pbl_height', ('FRICV', 'surface'): 'friction_velocity',
          ('CAPE', 'surface'): 'cape', ('TCDC', 'entire atmosphere (considered as a single layer)'): 'total_cloud'}
STATIONS = json.loads(Path(__file__).with_name('stations.json').read_text())
KNOTS = 3600 / 1852


def get(url, byte_range=None):
    headers = {'Range': f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(3 * (attempt + 1))
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


_GRID = {}


def station_index(g):
    """Nearest grid point per station, computed once per process and grid (a 1799 x 1059 Lambert grid)."""
    import eccodes as ec
    key = (ec.codes_get(g, 'Nx'), ec.codes_get(g, 'Ny'))
    if key not in _GRID:
        lats, lons = ec.codes_get_array(g, 'latitudes'), ec.codes_get_array(g, 'longitudes')
        idx = []
        for st in STATIONS.values():
            dlon = ((lons - st['lon'] % 360) + 180) % 360 - 180
            idx.append(int(np.argmin((lats - st['lat']) ** 2 + (dlon * math.cos(math.radians(st['lat']))) ** 2)))
        _GRID[key] = (np.array(idx), lons[idx].tolist())
    return _GRID[key]


def decode(content):
    """{shortName: [value per station]} for every field in the message, plus grid-rotation attributes."""
    import eccodes as ec
    out = {}
    with tempfile.NamedTemporaryFile(suffix='.grib2') as tmp:
        tmp.write(content); tmp.flush()
        with open(tmp.name, 'rb') as f:
            while True:
                g = ec.codes_grib_new_from_file(f)
                if g is None:
                    break
                try:
                    idx, lons = station_index(g)
                    out[ec.codes_get(g, 'shortName')] = ec.codes_get_values(g)[idx].astype(float).tolist()
                    out['_lon'] = lons
                    if ec.codes_is_defined(g, 'uvRelativeToGrid') and ec.codes_get(g, 'gridType') == 'lambert':  # lat/lon grids need no rotation
                        out['_relative'] = ec.codes_get(g, 'uvRelativeToGrid')
                        out['_lov'] = ec.codes_get(g, 'LoVInDegrees')
                        out['_latin'] = ec.codes_get(g, 'Latin1InDegrees')
                finally:
                    ec.codes_release(g)
    return out


PAIRS = {'10m': ('wind_10m', 'wind_10m_v'), '80m': ('wind_80m', 'wind_80m_v'), 'pbl': ('wind_pbl', 'wind_pbl_v')}
KELVIN = ('temperature_2m', 'temperature_80m', 'dew_point_2m')


def step_job(job):
    """All wanted fields for one (init, step); None unless every field was read."""
    init, step = job
    url = f'{BUCKET}/rrfs.{init:%Y%m%d}/{init:%H}/rrfs.t{init:%H}z.2dfld.3km.f{step:03d}.conus.grib2'
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
        raw[WANTED[key]] = decode(content)
    if len(raw) < len(WANTED):
        return job, None
    result = {}
    for level, (uk, vk) in PAIRS.items():
        fu, fv = raw[uk], raw[vk]
        u = next(v for k, v in fu.items() if not k.startswith('_'))
        v = next(x for k, x in fv.items() if not k.startswith('_'))
        if fu.get('_relative'):
            cone = math.sin(math.radians(fu['_latin']))
            rot = [math.radians(cone * (((lon - fu['_lov']) + 180) % 360 - 180)) for lon in fu['_lon']]
            u, v = ([math.cos(a) * x + math.sin(a) * y for a, x, y in zip(rot, u, v)],
                    [-math.sin(a) * x + math.cos(a) * y for a, x, y in zip(rot, u, v)])
        result[f'wind_u_{level}'], result[f'wind_v_{level}'] = u, v
    for col, fields in raw.items():
        if col in {k for pair in PAIRS.values() for k in pair}:
            continue
        values = next(v for k, v in fields.items() if not k.startswith('_'))
        if col == 'gust':
            values = [x * KNOTS for x in values]
        elif col in KELVIN:
            values = [x - 273.15 for x in values]
        result[col] = values
    return job, result


def main():
    """Incremental: runs already stored are skipped; a run is stored only when every step and field was read."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='var/mos-multi/models')
    parser.add_argument('--start', default=None)
    parser.add_argument('--procs', type=int, default=8)
    args = parser.parse_args()
    out = Path(args.root) / 'rrfs'
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
        jobs = [(i, s) for i in todo for s in STEPS]
        results = {}
        with ProcessPoolExecutor(max_workers=args.procs) as pool:
            for (init, step), result in pool.map(step_job, jobs, chunksize=2):
                results[(init, step)] = result
        records = []
        complete = [i for i in todo if all(results.get((i, s)) for s in STEPS)]
        for init in complete:
            for step in STEPS:
                result = results[(init, step)]
                for n, station in enumerate(STATIONS):
                    records.append({'station': station, 'init': init, 'lead_h': step, **{k: v[n] for k, v in result.items()}})
        if records:
            frames = ([stored] if stored is not None else []) + [pd.DataFrame(records)]
            pd.concat(frames, ignore_index=True).sort_values(['init', 'lead_h', 'station']).to_parquet(path, index=False)
        print(f'  {month}: {len(complete)}/{len(todo)} new runs complete, {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    main()
