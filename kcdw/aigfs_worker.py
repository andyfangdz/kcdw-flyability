"""Isolated optional ecCodes worker for NOAA AIGFS point winds; no arbitrary URLs.

AIGFS is NOAA's operational AI global model (GraphCast architecture, GFS/GDAS
initial conditions), published 6-hourly on a 0.25-degree grid. Request (stdin
JSON): {"init": "YYYY-MM-DDTHH:00:00Z", "leads": [int, ...]} with 6-hourly leads.
Each lead reads the NOAA .idx files, then byte ranges for 10 m, 925 hPa and
850 hPa U/V plus mean sea-level pressure only (~6 MB per lead), verifies GRIB
identity and grid, and takes the grid point nearest KCDW. Winds are earth-relative
on this regular grid. Prints one JSON list.
"""
import concurrent.futures
import hashlib
import json
import math
import signal
import sys
from datetime import datetime, timedelta, timezone

BUCKET = 'https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com'
KCDW = (40.8752, 285.7186)
MAX_FIELD = 3_000_000
MAX_LEADS = 8
HORIZON = 384
KNOTS = 3600 / 1852
# name: (file, idx variable, idx level, paramId, typeOfLevel, level)
FIELDS = {'u10': ('sfc', 'UGRD', '10 m above ground', 165, 'heightAboveGround', 10),
          'v10': ('sfc', 'VGRD', '10 m above ground', 166, 'heightAboveGround', 10),
          'mslp': ('sfc', 'PRMSL', 'mean sea level', 260074, 'meanSea', 0),
          'u925': ('pres', 'UGRD', '925 mb', 131, 'isobaricInhPa', 925),
          'v925': ('pres', 'VGRD', '925 mb', 132, 'isobaricInhPa', 925),
          'u850': ('pres', 'UGRD', '850 mb', 131, 'isobaricInhPa', 850),
          'v850': ('pres', 'VGRD', '850 mb', 132, 'isobaricInhPa', 850)}
GRID = dict(gridType='regular_ll', Ni=1440, Nj=721, iDirectionIncrementInDegrees=0.25,
            latitudeOfFirstGridPointInDegrees=90.0, longitudeOfFirstGridPointInDegrees=0.0)


def require(ok):
    if not ok:
        raise ValueError('aigfs validation failed')


def url_for(init, kind, lead):
    return f'{BUCKET}/aigfs.{init:%Y%m%d}/{init:%H}/model/atmos/grib2/aigfs.t{init:%H}z.{kind}.f{lead:03d}.grib2'


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


def ranges(text, init, lead, kind):
    rows = [line.split(':') for line in text.splitlines()]
    require(1 < len(rows) <= 500 and all(len(r) >= 6 for r in rows))
    offsets = [int(r[1]) for r in rows]
    require(offsets == sorted(set(offsets)) and offsets[0] == 0)
    found = {}
    for i, row in enumerate(rows[:-1]):
        for name, (file, var, level, *_rest) in FIELDS.items():
            if file == kind and (row[3], row[4]) == (var, level):
                require(row[2] == f'd={init:%Y%m%d%H}' and row[5] == ('anl' if lead == 0 else f'{lead} hour fcst'))
                require(name not in found)
                found[name] = (offsets[i], offsets[i + 1] - 1)
    require(set(found) == {n for n, spec in FIELDS.items() if spec[0] == kind})
    for a, b in found.values():
        require(0 <= a <= b and b - a + 1 <= MAX_FIELD)
    return found


def decode(content, name, init, lead):
    import eccodes as ec
    require(content[:4] == b'GRIB' and content[-4:] == b'7777')
    g = ec.codes_new_from_message(content)
    try:
        _, _, _, param, kind, level = FIELDS[name]
        valid = init + timedelta(hours=lead)
        expected = dict(centre='kwbc', paramId=param, typeOfLevel=kind, level=level, stepType='instant',
                        dataDate=int(init.strftime('%Y%m%d')), dataTime=int(init.strftime('%H%M')),
                        validityDate=int(valid.strftime('%Y%m%d')), validityTime=int(valid.strftime('%H%M')), **GRID)
        require(all(ec.codes_get(g, k) == v for k, v in expected.items()))
        p = ec.codes_grib_find_nearest(g, *KCDW)[0]
        value = float(p['value'])
        require(float(p['distance']) <= 20.0 and math.isfinite(value) and value != ec.codes_get(g, 'missingValue'))
        require(abs(value) <= 150 if name != 'mslp' else 85_000 <= value <= 110_000)
        return dict(value=value, index=int(p['index']), lat=float(p['lat']), lon=float(p['lon']),
                    sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    finally:
        ec.codes_release(g)


def wind(u, v):
    return round(math.hypot(u, v) * KNOTS, 2), round(math.degrees(math.atan2(-u, -v)) % 360, 1)


def sample(init, lead):
    fields = {}
    for kind in ('sfc', 'pres'):
        url = url_for(init, kind, lead)
        for name, r in ranges(fetch(url + '.idx', 100_000).decode('ascii'), init, lead, kind).items():
            fields[name] = decode(fetch(url, MAX_FIELD, r), name, init, lead)
    require(len({f['index'] for f in fields.values()}) == 1)
    point = fields['u10']
    out = dict(at=(init + timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'), lead=lead, url=url_for(init, 'sfc', lead),
               latitude=round(point['lat'], 4), longitude=round((point['lon'] + 180) % 360 - 180, 4),
               mslp_hpa=round(fields['mslp']['value'] / 100, 1),
               proofs={name: dict(sha256=f['sha256'], bytes=f['bytes']) for name, f in fields.items()})
    for level in ('10', '925', '850'):
        out[f'wind{level}_kt'], out[f'from{level}_deg'] = wind(fields['u' + level]['value'], fields['v' + level]['value'])
    return out


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(200)
    request = json.loads(sys.stdin.read(4097))
    require(set(request) == {'init', 'leads'})
    init = datetime.strptime(request['init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    require(init.hour % 6 == 0 and not init.minute)
    leads = request['leads']
    require(isinstance(leads, list) and 1 <= len(leads) <= MAX_LEADS and leads == sorted(set(leads)))
    require(all(type(h) is int and 0 <= h <= HORIZON and h % 6 == 0 for h in leads))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda h: sample(init, h), leads))
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
