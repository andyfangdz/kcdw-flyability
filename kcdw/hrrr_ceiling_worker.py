"""Isolated ecCodes decoder for native HRRR cloud ceiling within 100 nm of KCDW.

Input (stdin JSON): {"model_init": ISO Z, "leads": [int, ...], "out": ".../grid.npz"}.
URLs are built here from the pinned AWS bucket, never accepted from input. The
output path must be an .npz under the repository's var/events directory.
Stdout: one JSON summary with per-lead statistics and byte-range proofs.
Invoked by kcdw.hrrr_ceiling using the persistent native-weather virtualenv.
"""
import concurrent.futures
import hashlib
import json
import math
import re
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAT, LON = 40.8752, -74.2814
NM_KM = 1.852
RADII_KM = {'25km': 25.0, '50nm': 50 * NM_KM, '100nm': 100 * NM_KM}
BOX_KM = 100 * NM_KM + 15
THRESHOLDS_FT = (1000, 3000, 7000)
FT_PER_M = 3.280839895
COMPASS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')
# name: (idx variable, idx level, typeOfLevel, parameterCategory, parameterNumber)
FIELDS = {'ceiling': ('HGT', 'cloud ceiling', 'cloudCeiling', 3, 5),
          'terrain': ('HGT', 'surface', 'surface', 3, 5),
          'low_cloud': ('LCDC', 'low cloud layer', 'lowCloudLayer', 6, 3)}


def require(ok):
    if not ok:
        raise ValueError('native HRRR validation failed')


def url_for(init, lead):
    return (f'https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{init:%Y%m%d}/conus/'
            f'hrrr.t{init:%H}z.wrfsfcf{lead:02d}.grib2')


def max_lead(init):
    return 48 if init.hour % 6 == 0 else 18


def fetch(url, limit, byte_range=None):
    import requests
    headers = {'Range': f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    with requests.get(url, headers=headers, timeout=(5, 20), stream=True, allow_redirects=False) as response:
        require(response.status_code == (206 if byte_range else 200))
        proof = None
        if byte_range:
            a, b = byte_range
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            require(match is not None)
            x, y, total = map(int, match.groups())
            require((x, y) == (a, b) and b < total <= 1_000_000_000)
            proof = dict(start=a, end=b, total=total, bytes=b - a + 1)
        content = bytearray()
        for block in response.iter_content(65536):
            content.extend(block)
            require(len(content) <= limit)
        if proof:
            require(len(content) == proof['bytes'])
            proof['sha256'] = hashlib.sha256(content).hexdigest()
        return bytes(content), proof


def indexed_ranges(text, init, lead, names):
    rows = [line.split(':') for line in text.splitlines()]
    require(0 < len(rows) <= 400)
    offsets = [int(row[1]) for row in rows]
    require(offsets == sorted(set(offsets)) and offsets[0] == 0)
    step = 'anl' if lead == 0 else f'{lead} hour fcst'
    selected = {}
    for i, row in enumerate(rows[:-1]):
        require(len(row) >= 6)
        for name in names:
            var, level = FIELDS[name][:2]
            if row[3:5] == [var, level] and row[5] == step:
                require(row[2] == f'd={init:%Y%m%d%H}' and name not in selected)
                a, b = offsets[i], offsets[i + 1] - 1
                require(0 < b - a + 1 <= 8_000_000)
                selected[name] = (a, b)
    require(set(selected) == set(names))
    return selected


def decode(content, name, init, lead, with_grid=False):
    import eccodes as ec
    import numpy as np
    require(content[:4] == b'GRIB' and content[-4:] == b'7777' and int.from_bytes(content[8:16], 'big') == len(content))
    g = ec.codes_new_from_message(content)
    try:
        _, _, kind, category, number = FIELDS[name]
        valid = init + timedelta(hours=lead)
        expected = {'edition': 2, 'discipline': 0, 'parameterCategory': category, 'parameterNumber': number,
                    'typeOfLevel': kind, 'gridType': 'lambert', 'Nx': 1799, 'Ny': 1059,
                    'dataDate': int(init.strftime('%Y%m%d')), 'dataTime': int(init.strftime('%H%M')),
                    'validityDate': int(valid.strftime('%Y%m%d')), 'validityTime': int(valid.strftime('%H%M'))}
        require(all(ec.codes_get(g, k) == v for k, v in expected.items()))
        values = ec.codes_get_values(g).reshape(1059, 1799)
        missing = ec.codes_get(g, 'missingValue')
        values = np.where((values == missing) | ~np.isfinite(values) | (values > 1e19), np.nan, values)
        grid = None
        if with_grid:
            lats = ec.codes_get_array(g, 'latitudes').reshape(1059, 1799)
            lons = ec.codes_get_array(g, 'longitudes').reshape(1059, 1799)
            grid = (lats, np.where(lons > 180, lons - 360, lons))
        return values, grid
    finally:
        ec.codes_release(g)


def distance_bearing(lat, lon):
    import numpy as np
    lat1, lon1 = math.radians(LAT), math.radians(LON)
    lat2, lon2 = np.radians(lat), np.radians(lon)
    a = np.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    km = 2 * 6371.0 * np.arcsin(np.minimum(1, np.sqrt(a)))
    y = np.sin(lon2 - lon1) * np.cos(lat2)
    x = math.cos(lat1) * np.sin(lat2) - math.sin(lat1) * np.cos(lat2) * np.cos(lon2 - lon1)
    return km, (np.degrees(np.arctan2(y, x)) + 360) % 360


def window(lats, lons):
    """Row/column slice covering the 100 nm circle plus a margin."""
    import numpy as np
    km, _ = distance_bearing(lats, lons)
    rows, cols = np.nonzero(km <= BOX_KM)
    require(rows.size > 1000)
    return slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1)


def statistics(agl, low, km, bearing):
    """Per-lead summary; agl is ft above model ground with NaN for no ceiling."""
    import numpy as np
    def num(v, places=0):
        return None if v is None or not np.isfinite(v) else round(float(v), places)
    centre = np.unravel_index(np.argmin(km), km.shape)
    require(km[centre] <= 3)
    near = km <= 10
    ceil10 = agl[near & np.isfinite(agl)]
    counts = {}
    for name, radius in RADII_KM.items():
        inside = km <= radius
        counts[name] = {'cells': int(inside.sum()), **{f'below_{t}': int((inside & (agl < t)).sum()) for t in THRESHOLDS_FT}}
    low_cells = np.isfinite(agl) & (agl < 3000) & (km <= RADII_KM['100nm'])
    nearest = None
    if low_cells.any():
        idx = np.unravel_index(np.argmin(np.where(low_cells, km, np.inf)), km.shape)
        nearest = {'distance_nm': round(float(km[idx]) / NM_KM, 1),
                   'direction': COMPASS[int(round(float(bearing[idx]) / 22.5)) % 16],
                   'ceiling_ft': round(float(agl[idx]))}
    return {'kcdw': {'ceiling_agl_ft': num(agl[centre]), 'low_cloud_pct': num(low[centre], 1)},
            'within_10km': {'cells': int(near.sum()), 'ceiling_cells': int(ceil10.size),
                            'min_ft': num(ceil10.min()) if ceil10.size else None,
                            'max_ft': num(ceil10.max()) if ceil10.size else None},
            'counts': counts, 'nearest_below_3000': nearest}


def main():
    import numpy as np
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(160)
    request = json.loads(sys.stdin.read(4097))
    require(set(request) == {'model_init', 'leads', 'out'})
    init = datetime.strptime(request['model_init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    leads = request['leads']
    require(isinstance(leads, list) and 0 < len(leads) <= 10 and leads == sorted(set(leads)))
    require(all(type(h) is int and 0 <= h <= max_lead(init) for h in leads))
    out = Path(request['out']).resolve()
    require(out.suffix == '.npz' and (ROOT / 'var/events').resolve() in out.parents)

    def one(lead):
        url = url_for(init, lead)
        text, _ = fetch(url + '.idx', 200_000)
        names = ('ceiling', 'low_cloud', 'terrain') if lead == leads[0] else ('ceiling', 'low_cloud')
        ranges = indexed_ranges(text.decode('ascii'), init, lead, names)
        decoded, proofs = {}, {}
        for name, byte_range in ranges.items():
            content, proofs[name] = fetch(url, 8_000_000, byte_range)
            decoded[name] = decode(content, name, init, lead, with_grid=(name == 'terrain'))
        return lead, url, decoded, proofs

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, leads))
    terrain, (lats, lons) = results[0][2]['terrain']
    rows, cols = window(lats, lons)
    lats, lons, terrain = lats[rows, cols], lons[rows, cols], terrain[rows, cols]
    require(np.isfinite(terrain).all())
    km, bearing = distance_bearing(lats, lons)
    summary, arrays = [], {'lat': lats.astype('float32'), 'lon': lons.astype('float32')}
    for lead, url, decoded, proofs in results:
        ceiling = decoded['ceiling'][0][rows, cols]
        low = decoded['low_cloud'][0][rows, cols]
        agl = np.maximum(0, ceiling - terrain) * FT_PER_M
        arrays[f'agl_{lead}'] = agl.astype('float32')
        arrays[f'low_{lead}'] = low.astype('float32')
        summary.append({'lead': lead, 'valid_at': (init + timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'source_url': url, 'fields': {k: proofs[k] for k in ('ceiling', 'low_cloud')},
                        **statistics(agl, low, km, bearing)})
    np.savez_compressed(out, **arrays)
    print(json.dumps({'grid': {'rows': int(lats.shape[0]), 'cols': int(lats.shape[1])},
                      'terrain': {'lead': leads[0], 'proof': results[0][3]['terrain']},
                      'frames': summary}, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
