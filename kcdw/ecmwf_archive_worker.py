"""Isolated optional ecCodes worker: archived ECMWF IFS open data (AWS mirror) at KCDW; no arbitrary URLs.

Request (stdin JSON): {"items": [{"init": "YYYY-MM-DDTHH:00:00Z", "lead": int}, ...]}.
For each item it reads 10 m u/v at ``lead`` (instantaneous) and 10fg at
``lead + 6`` (maximum gust over the preceding six hours), verifying GRIB
identity and the 0.25-degree grid, and returns the KCDW grid-point values in
knots. Items that are unavailable return {"error": ...}; one bad date never
fails the batch. Prints one JSON list.
"""
import concurrent.futures
import json
import math
import signal
import sys
from datetime import datetime, timedelta, timezone

BUCKET = 'https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com'
KCDW = (40.8752, 285.7186)
KNOTS = 3600 / 1852
MAX_FIELD = 4_000_000
MAX_ITEMS = 60


def require(ok):
    if not ok:
        raise ValueError('ecmwf archive validation failed')


def url_for(init, lead):
    return f'{BUCKET}/{init:%Y%m%d}/{init:%H}z/ifs/0p25/oper/{init:%Y%m%d%H}0000-{lead}h-oper-fc'


def fetch(url, limit, byte_range=None, attempts=6):
    """Bounded GET with retries: the S3 mirror answers bursts with 503 SlowDown."""
    import time
    for attempt in range(attempts):
        try:
            return _fetch(url, limit, byte_range)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(min(30, 2 * 2 ** attempt))


def _fetch(url, limit, byte_range=None):
    import requests
    headers = {'Range': f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    with requests.get(url, headers=headers, timeout=(4, 30), stream=True, allow_redirects=False) as r:
        require(r.status_code == (206 if byte_range else 200))
        content = bytearray()
        for block in r.iter_content(65536):
            content.extend(block)
            require(len(content) <= limit)
    if byte_range:
        require(len(content) == byte_range[1] - byte_range[0] + 1)
    return bytes(content)


def ranges(init, lead, params):
    rows = [json.loads(line) for line in fetch(url_for(init, lead) + '.index', 400_000).decode('ascii').splitlines()]
    found = {}
    for row in rows:
        if row.get('param') in params and 'number' not in row:
            require(row.get('date') == init.strftime('%Y%m%d') and row.get('time') == init.strftime('%H%M'))
            require(row.get('step') == str(lead) and row.get('type') == 'fc' and row.get('stream') == 'oper' and row.get('levtype') == 'sfc')
            require(row['param'] not in found)
            a, n = row['_offset'], row['_length']
            require(type(a) is int and type(n) is int and a >= 0 and 0 < n <= MAX_FIELD)
            found[row['param']] = (a, a + n - 1)
    require(set(found) == set(params))
    return found


def decode(content, param, init, lead):
    import eccodes as ec
    require(content[:4] == b'GRIB' and content[-4:] == b'7777')
    g = ec.codes_new_from_message(content)
    try:
        valid = init + timedelta(hours=lead)
        expected = dict(centre='ecmf', gridType='regular_ll', Ni=1440, Nj=721,
                        dataDate=int(init.strftime('%Y%m%d')), dataTime=int(init.strftime('%H%M')),
                        validityDate=int(valid.strftime('%Y%m%d')), validityTime=int(valid.strftime('%H%M')))
        if param == '10fg':
            expected.update(paramId=49, stepType='max', startStep=lead - 6, endStep=lead)
        else:
            expected.update(paramId={'10u': 165, '10v': 166}[param], stepType='instant', endStep=lead)
        require(all(ec.codes_get(g, k) == v for k, v in expected.items()))
        p = ec.codes_grib_find_nearest(g, *KCDW)[0]
        value = float(p['value'])
        require(float(p['lat']) == 41.0 and (float(p['lon']) + 180) % 360 - 180 == -74.25)
        require(math.isfinite(value) and abs(value) <= 150 and value != ec.codes_get(g, 'missingValue'))
        return value
    finally:
        ec.codes_release(g)


def sample(item):
    init = datetime.strptime(item['init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    lead = item['lead']
    try:
        uv = ranges(init, lead, ('10u', '10v'))
        u, v = (decode(fetch(url_for(init, lead) + '.grib2', MAX_FIELD, uv[p]), p, init, lead) for p in ('10u', '10v'))
        gr = ranges(init, lead + 6, ('10fg',))
        gust = decode(fetch(url_for(init, lead + 6) + '.grib2', MAX_FIELD, gr['10fg']), '10fg', init, lead + 6)
        require(gust >= 0)
        return dict(init=item['init'], lead=lead, sust_kt=round(math.hypot(u, v) * KNOTS, 2),
                    from_deg=round(math.degrees(math.atan2(-u, -v)) % 360, 1), gust_kt=round(gust * KNOTS, 2))
    except Exception as exc:
        return dict(init=item['init'], lead=lead, error=type(exc).__name__)


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(900)
    request = json.loads(sys.stdin.read(20_000))
    require(set(request) == {'items'})
    items = request['items']
    require(isinstance(items, list) and 1 <= len(items) <= MAX_ITEMS)
    for item in items:
        require(set(item) == {'init', 'lead'} and type(item['lead']) is int and 0 <= item['lead'] <= 354 and item['lead'] % 6 == 0)
        init = datetime.strptime(item['init'], '%Y-%m-%dT%H:%M:%SZ')
        require(init.hour in (0, 12) and not init.minute)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(sample, items))
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
