"""Isolated optional ecCodes worker for native RRFS 3 km point winds; no arbitrary URLs.

Request (stdin JSON): {"init": "YYYY-MM-DDTHH:00:00Z", "leads": [int, ...]}.
Each lead reads the NOAA .idx, then byte ranges for surface GUST and 10 m U/V
only (~5 MB per lead), verifies GRIB identity and grid, takes the grid point nearest
KCDW, and rotates grid-relative U/V to true north. Prints one JSON list.
"""
import concurrent.futures
import hashlib
import json
import math
import re
import signal
import sys
from datetime import datetime, timedelta, timezone

BUCKET = 'https://noaa-rrfs-ops-pds.s3.amazonaws.com'
KCDW = (40.8752, 285.7186)
MAX_FIELD = 4_000_000
MAX_LEADS = 72
KNOTS = 3600 / 1852
FIELDS = {'gust': ('GUST', 'surface', 260065, 'surface', 0),
          'u': ('UGRD', '10 m above ground', 165, 'heightAboveGround', 10),
          'v': ('VGRD', '10 m above ground', 166, 'heightAboveGround', 10)}
GRID = dict(gridType='lambert', Nx=1799, Ny=1059, LoVInDegrees=262.5, Latin1InDegrees=38.5, Latin2InDegrees=38.5)


def require(ok):
    if not ok:
        raise ValueError('rrfs validation failed')


def url_for(init, lead):
    return f'{BUCKET}/rrfs.{init:%Y%m%d}/{init:%H}/rrfs.t{init:%H}z.2dfld.3km.f{lead:03d}.conus.grib2'


def fetch(url, limit, byte_range=None):
    import requests
    headers = {'Range': f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    with requests.get(url, headers=headers, timeout=(4, 20), stream=True, allow_redirects=False) as r:
        require(r.status_code == (206 if byte_range else 200))
        content = bytearray()
        for block in r.iter_content(65536):
            content.extend(block)
            require(len(content) <= limit)
    if byte_range:
        require(len(content) == byte_range[1] - byte_range[0] + 1)
    return bytes(content)


def ranges(text, init, lead):
    rows = [line.split(':') for line in text.splitlines()]
    require(1 < len(rows) <= 2000 and all(len(r) >= 6 for r in rows))
    offsets = [int(r[1]) for r in rows]
    require(offsets == sorted(set(offsets)) and offsets[0] == 0)
    found = {}
    for i, row in enumerate(rows[:-1]):
        for name, (var, level, *_rest) in FIELDS.items():
            if (row[3], row[4]) == (var, level):
                require(row[2] == f'd={init:%Y%m%d%H}' and row[5] == ('anl' if lead == 0 else f'{lead} hour fcst'))
                require(name not in found)
                found[name] = (offsets[i], offsets[i + 1] - 1)
    require(set(found) == set(FIELDS))
    for a, b in found.values():
        require(0 <= a <= b and b - a + 1 <= MAX_FIELD)
    return found


def decode(content, name, init, lead):
    import eccodes as ec
    require(content[:4] == b'GRIB' and content[-4:] == b'7777')
    g = ec.codes_new_from_message(content)
    try:
        _, _, param, kind, level = FIELDS[name]
        valid = init + timedelta(hours=lead)
        expected = dict(centre='kwbc', paramId=param, typeOfLevel=kind, level=level, stepType='instant',
                        dataDate=int(init.strftime('%Y%m%d')), dataTime=int(init.strftime('%H%M')),
                        validityDate=int(valid.strftime('%Y%m%d')), validityTime=int(valid.strftime('%H%M')), **GRID)
        require(all(ec.codes_get(g, k) == v for k, v in expected.items()))
        p = ec.codes_grib_find_nearest(g, *KCDW)[0]
        value = float(p['value'])
        require(float(p['distance']) <= 3.0 and math.isfinite(value) and abs(value) <= 150)
        require(value != ec.codes_get(g, 'missingValue'))
        relative = ec.codes_get(g, 'uvRelativeToGrid') if name != 'gust' else None
        return dict(value=value, index=int(p['index']), lat=float(p['lat']), lon=float(p['lon']),
                    relative=relative, sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    finally:
        ec.codes_release(g)


def sample(init, lead):
    url = url_for(init, lead)
    found = ranges(fetch(url + '.idx', 400_000).decode('ascii'), init, lead)
    fields = {name: decode(fetch(url, MAX_FIELD, r), name, init, lead) for name, r in found.items()}
    require(len({f['index'] for f in fields.values()}) == 1)
    u, v, point = fields['u']['value'], fields['v']['value'], fields['u']
    require(fields['u']['relative'] == fields['v']['relative'] and fields['u']['relative'] in (0, 1))
    if fields['u']['relative']:
        # Lambert conformal: true north is rotated by cone * (lon - LoV) at this point.
        angle = math.radians(math.sin(math.radians(GRID['Latin1InDegrees'])) * (point['lon'] - GRID['LoVInDegrees']))
        u, v = math.cos(angle) * u + math.sin(angle) * v, -math.sin(angle) * u + math.cos(angle) * v
    require(fields['gust']['value'] >= 0)
    return dict(at=(init + timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'), lead=lead, url=url,
                gust_kt=round(fields['gust']['value'] * KNOTS, 2), wind_kt=round(math.hypot(u, v) * KNOTS, 2),
                from_deg=round(math.degrees(math.atan2(-u, -v)) % 360, 1),
                latitude=round(point['lat'], 4), longitude=round((point['lon'] + 180) % 360 - 180, 4),
                proofs={name: dict(sha256=f['sha256'], bytes=f['bytes']) for name, f in fields.items()})


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(320)
    request = json.loads(sys.stdin.read(4097))
    require(set(request) == {'init', 'leads'})
    init = datetime.strptime(request['init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    leads = request['leads']
    require(isinstance(leads, list) and 1 <= len(leads) <= MAX_LEADS and leads == sorted(set(leads)))
    require(all(type(h) is int and 0 <= h <= 84 for h in leads))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(lambda h: sample(init, h), leads))
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
