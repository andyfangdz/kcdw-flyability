"""Isolated ecCodes worker for native model soundings at KCDW; no arbitrary URLs.

Request (stdin JSON): {"source": "gfs" | "aigfs", "init": "YYYY-MM-DDTHH:00:00Z", "lead": int}.
GFS: one NOMADS grib-filter request for a 0.25-degree 3x3 box around KCDW with
height, temperature, relative humidity and wind on 19 levels (1000-200 hPa,
25-50 hPa apart) plus the surface (~20 KB). AIGFS: .idx byte ranges from NOAA's
AWS bucket for height, temperature, specific humidity and wind on its 10 levels
from 1000 to 200 hPa plus 2 m temperature/dew point, surface pressure and 10 m
wind (~45 MB; AIGFS has no subsetting service). Every message is checked for
centre, level, run and valid time. AIGFS has no terrain height, so the surface
height comes from the hypsometric equation below the 1000 hPa height.
Prints one JSON profile: surface first, then levels above ground.
"""
import json
import math
import signal
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from aigfs_worker import BUCKET as AIGFS_BUCKET, KCDW, KNOTS, fetch, require

NOMADS = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl'
GFS_LEVELS = (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200)
AIGFS_LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200)
MAX_SUBSET = 200_000
MAX_FIELD = 3_000_000


def dewpoint(e_hpa):
    e = max(e_hpa, 1e-3)
    return 243.5 * math.log(e / 6.112) / (17.67 - math.log(e / 6.112))


def rh_dewpoint(tmpc, rh):
    es = 6.112 * math.exp(17.67 * tmpc / (tmpc + 243.5))
    return min(tmpc, dewpoint(es * max(min(rh, 100.0), 1.0) / 100))


def q_dewpoint(tmpc, q, pres):
    return min(tmpc, dewpoint(q * pres / (0.622 + 0.378 * q)))


def wind(u, v):
    return round(math.degrees(math.atan2(-u, -v)) % 360, 1), round(math.hypot(u, v) * KNOTS, 2)


def nearest(g):
    import eccodes as ec
    p = ec.codes_grib_find_nearest(g, *KCDW)[0]
    value = float(p['value'])
    require(float(p['distance']) <= 20.0 and math.isfinite(value) and value != ec.codes_get(g, 'missingValue'))
    return value, float(p['lat']), float(p['lon'])


def check(g, init, lead):
    import eccodes as ec
    valid = init + timedelta(hours=lead)
    require(ec.codes_get(g, 'centre') == 'kwbc')
    require(ec.codes_get(g, 'dataDate') == int(init.strftime('%Y%m%d')) and ec.codes_get(g, 'dataTime') == int(init.strftime('%H%M')))
    require(ec.codes_get(g, 'validityDate') == int(valid.strftime('%Y%m%d')) and ec.codes_get(g, 'validityTime') == int(valid.strftime('%H%M')))


def gfs(init, lead):
    import eccodes as ec
    query = {'dir': f'/gfs.{init:%Y%m%d}/{init:%H}/atmos', 'file': f'gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}'}
    query.update({f'var_{v}': 'on' for v in ('HGT', 'TMP', 'RH', 'UGRD', 'VGRD', 'DPT', 'PRES')})
    query.update({f'lev_{p}_mb': 'on' for p in GFS_LEVELS})
    query.update({'lev_2_m_above_ground': 'on', 'lev_10_m_above_ground': 'on', 'lev_surface': 'on',
                  'subregion': '', 'toplat': '41.25', 'leftlon': '285.5', 'rightlon': '286', 'bottomlat': '40.75'})
    url = NOMADS + '?' + urlencode(query)
    content = fetch(url, MAX_SUBSET)
    require(content[:4] == b'GRIB')
    fields, grid = {}, None
    for message in content.split(b'GRIB')[1:]:
        g = ec.codes_new_from_message(b'GRIB' + message)
        try:
            check(g, init, lead)
            key = (ec.codes_get(g, 'shortName'), ec.codes_get(g, 'typeOfLevel'), ec.codes_get(g, 'level'))
            value, lat, lon = nearest(g)
            require(grid in (None, (lat, lon)))
            grid = (lat, lon)
            fields[key] = value
        finally:
            ec.codes_release(g)
    surface = {'pres': fields[('sp', 'surface', 0)] / 100, 'hght': fields[('orog', 'surface', 0)],
               'tmpc': fields[('2t', 'heightAboveGround', 2)] - 273.15, 'dwpc': fields[('2d', 'heightAboveGround', 2)] - 273.15,
               'u': fields[('10u', 'heightAboveGround', 10)], 'v': fields[('10v', 'heightAboveGround', 10)]}
    levels = []
    for p in GFS_LEVELS:
        t = fields[('t', 'isobaricInhPa', p)] - 273.15
        levels.append({'pres': float(p), 'hght': fields[('gh', 'isobaricInhPa', p)], 'tmpc': t,
                       'dwpc': rh_dewpoint(t, fields[('r', 'isobaricInhPa', p)]),
                       'u': fields[('u', 'isobaricInhPa', p)], 'v': fields[('v', 'isobaricInhPa', p)]})
    return url, grid, surface, levels


def length(url):
    import requests
    r = requests.head(url, timeout=(4, 20), allow_redirects=False)
    require(r.status_code == 200)
    return int(r.headers['Content-Length'])


def aigfs(init, lead):
    import eccodes as ec
    wanted = {'sfc': {('TMP', '2 m above ground'), ('DPT', '2 m above ground'), ('PRES', 'surface'),
                      ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground')},
              'pres': {(v, f'{p} mb') for v in ('HGT', 'TMP', 'SPFH', 'UGRD', 'VGRD') for p in AIGFS_LEVELS}}
    fields, grid, source = {}, None, None
    for kind, keys in wanted.items():
        url = f'{AIGFS_BUCKET}/aigfs.{init:%Y%m%d}/{init:%H}/model/atmos/grib2/aigfs.t{init:%H}z.{kind}.f{lead:03d}.grib2'
        source = source or url
        rows = [line.split(':') for line in fetch(url + '.idx', 100_000).decode('ascii').splitlines()]
        require(1 < len(rows) <= 500 and all(len(r) >= 6 for r in rows))
        size = None
        for i, row in enumerate(rows):
            if (row[3], row[4]) in keys:
                require(row[2] == f'd={init:%Y%m%d%H}' and row[5] == ('anl' if lead == 0 else f'{lead} hour fcst'))
                if i + 1 == len(rows) and size is None:
                    size = length(url)  # the last message ends at the end of the file
                start, end = int(row[1]), (int(rows[i + 1][1]) if i + 1 < len(rows) else size) - 1
                require(0 <= start <= end and end - start + 1 <= MAX_FIELD)
                g = ec.codes_new_from_message(fetch(url, MAX_FIELD, (start, end)))
                try:
                    check(g, init, lead)
                    value, lat, lon = nearest(g)
                    require(grid in (None, (lat, lon)))
                    grid = (lat, lon)
                    fields[(row[3], row[4])] = value
                finally:
                    ec.codes_release(g)
        require(keys <= set(fields))
    psfc = fields[('PRES', 'surface')] / 100
    t2 = fields[('TMP', '2 m above ground')]
    # Hypsometric surface height from the 1000 hPa surface using the 2 m temperature.
    z1000 = fields[('HGT', '1000 mb')]
    hght = z1000 - 287.05 * t2 / 9.80665 * math.log(psfc / 1000)
    surface = {'pres': psfc, 'hght': hght, 'tmpc': t2 - 273.15, 'dwpc': fields[('DPT', '2 m above ground')] - 273.15,
               'u': fields[('UGRD', '10 m above ground')], 'v': fields[('VGRD', '10 m above ground')]}
    levels = []
    for p in AIGFS_LEVELS:
        t = fields[('TMP', f'{p} mb')] - 273.15
        levels.append({'pres': float(p), 'hght': fields[('HGT', f'{p} mb')], 'tmpc': t,
                       'dwpc': q_dewpoint(t, fields[('SPFH', f'{p} mb')], p),
                       'u': fields[('UGRD', f'{p} mb')], 'v': fields[('VGRD', f'{p} mb')]})
    return source, grid, surface, levels


def profile(surface, levels):
    """Surface (10 m wind), then levels above it; strictly decreasing pressure, increasing height."""
    rows = [surface]
    rows += [lv for lv in levels if lv['pres'] < surface['pres'] - 1 and lv['hght'] > surface['hght'] + 10]
    out = []
    for r in rows:
        wdir, wspd = wind(r['u'], r['v'])
        out.append([round(r['pres'], 1), round(r['hght'], 1), round(r['tmpc'], 2), round(min(r['dwpc'], r['tmpc']), 2), wdir, wspd])
    require(len(out) >= 6)
    require(all(a[0] > b[0] and a[1] < b[1] for a, b in zip(out, out[1:])))
    require(all(-90 < r[2] < 60 and -120 < r[3] <= r[2] and 0 <= r[5] <= 250 for r in out))
    return out


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(200)
    request = json.loads(sys.stdin.read(4096))
    require(set(request) == {'source', 'init', 'lead'} and request['source'] in ('gfs', 'aigfs'))
    init = datetime.strptime(request['init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    lead = request['lead']
    require(init.hour % 6 == 0 and type(lead) is int and 0 <= lead <= 384 and (lead <= 120 or lead % 3 == 0))
    require(request['source'] == 'gfs' or lead % 6 == 0)
    url, grid, surface, levels = (gfs if request['source'] == 'gfs' else aigfs)(init, lead)
    print(json.dumps({'source_url': url, 'grid': {'latitude': round(grid[0], 4), 'longitude': round((grid[1] + 180) % 360 - 180, 4)},
                      'valid': (init + timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                      'levels': profile(surface, levels)}, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
