"""Isolated optional ecCodes worker: archived ECMWF IFS open data at KCDW; no arbitrary URLs.

Request (stdin JSON): {"items": [{"init": "YYYY-MM-DDTHH:00:00Z", "lead": int}, ...]}.
For each item it reads 10 m u/v at ``lead`` (instantaneous) and the maximum 10 m
gust over ``lead``..``lead + 6``, verifying GRIB identity and the 0.25-degree grid,
and returns the KCDW grid-point values in knots. The gust is assembled from the
windows each step carries, walking back from ``lead + 6``: open data holds 1-hour
``10fg`` to 90 h, 3-hour ``10fg3`` from 93 to 144 h and 6-hour ``10fg`` beyond,
so a 6-hour maximum is one or two fields from 93 h on; before that each
3-hourly step keeps only its last hour, and ``gust_hours`` reports the 2 hours sampled. Reads ECMWF's Google Cloud mirror,
falling back to the AWS mirror (which throttles bursts with 503 SlowDown). Items
that are unavailable return {"error": ...}; one bad date never fails the batch.
Prints one JSON list.
"""
import concurrent.futures
import json
import math
import signal
import sys
from datetime import datetime, timedelta, timezone

MIRRORS = ('https://storage.googleapis.com/ecmwf-open-data', 'https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com')
BUCKET = MIRRORS[0]
# Index name -> accepted GRIB paramIds. From about 2025-02 to 2026-02 every gust window was encoded as
# 237318 (max_i10fg); the window itself is always checked from the message's start and end steps.
GUSTS = {'10fg': {49, 237318}, '10fg3': {228028, 237318}, '10fg6': {228029, 237318}}
KCDW = (40.8752, 285.7186)
KNOTS = 3600 / 1852
MAX_FIELD = 4_000_000
MAX_ITEMS = 60


def require(ok):
    if not ok:
        raise ValueError('ecmwf archive validation failed')


def url_for(init, lead, mirror=BUCKET):
    return f'{mirror}/{init:%Y%m%d}/{init:%H}z/ifs/0p25/oper/{init:%Y%m%d%H}0000-{lead}h-oper-fc'


def mirrored(init, lead, suffix, limit, byte_range=None):
    """Fetch from the first mirror that answers; both carry the same files."""
    for i, mirror in enumerate(MIRRORS):
        try:
            return fetch(url_for(init, lead, mirror) + suffix, limit, byte_range, attempts=3 if i + 1 < len(MIRRORS) else 6)
        except Exception:
            if i + 1 == len(MIRRORS):
                raise


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


def index(init, lead):
    return [json.loads(line) for line in mirrored(init, lead, '.index', 400_000).decode('ascii').splitlines()]


def ranges(init, lead, params, rows=None):
    rows = index(init, lead) if rows is None else rows
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
        if param in GUSTS:
            require(ec.codes_get(g, 'paramId') in GUSTS[param])
            expected.update(stepType='max', endStep=lead)
        else:
            expected.update(paramId={'10u': 165, '10v': 166}[param], stepType='instant', endStep=lead)
        require(all(ec.codes_get(g, k) == v for k, v in expected.items()))
        p = ec.codes_grib_find_nearest(g, *KCDW)[0]
        value = float(p['value'])
        require(float(p['lat']) == 41.0 and (float(p['lon']) + 180) % 360 - 180 == -74.25)
        require(math.isfinite(value) and abs(value) <= 150 and value != ec.codes_get(g, 'missingValue'))
        return (value, ec.codes_get(g, 'startStep')) if param in GUSTS else value
    finally:
        ec.codes_release(g)


def output_steps(lead):
    """Open-data output steps in (lead, lead + 6]: every 3 h to 144 h, then every 6 h."""
    return [h for h in range(lead + 6, lead, -1) if (h <= 144 and h % 3 == 0) or h % 6 == 0]


def six_hour_gust(init, lead):
    """Maximum gust over lead..lead+6 and the hours its windows cover (6 from 93 h on; 2 before,
    where each 3-hourly step keeps only its last hour). Windows must not overlap or leave the span."""
    values, covered, previous_start = [], 0, lead + 6
    for end in output_steps(lead):
        rows = index(init, end)
        params = sorted({r.get('param') for r in rows if r.get('param') in GUSTS and 'number' not in r})
        require(len(params) == 1)
        found = ranges(init, end, tuple(params), rows)
        value, start = decode(mirrored(init, end, '.grib2', MAX_FIELD, found[params[0]]), params[0], init, end)
        require(lead <= start < end <= previous_start and value >= 0)
        values.append(value)
        covered += end - start
        previous_start = start
    require(values and 1 <= covered <= 6)
    return max(values), covered


def sample(item):
    init = datetime.strptime(item['init'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    lead = item['lead']
    try:
        uv = ranges(init, lead, ('10u', '10v'))
        u, v = (decode(mirrored(init, lead, '.grib2', MAX_FIELD, uv[p]), p, init, lead) for p in ('10u', '10v'))
        gust, hours = six_hour_gust(init, lead)
        return dict(init=item['init'], lead=lead, sust_kt=round(math.hypot(u, v) * KNOTS, 2),
                    from_deg=round(math.degrees(math.atan2(-u, -v)) % 360, 1), gust_kt=round(gust * KNOTS, 2), gust_hours=hours)
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
