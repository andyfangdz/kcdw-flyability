"""Direct ECCC GEPS: one bounded grouped GRIB per field/lead, 21 native points.

Official location/nomenclature: https://eccc-msc.github.io/open-data/msc-data/
nwp_geps/readme_geps-datamart_en/ . The documented /today/ alias is mutable,
but each requested filename AND every GRIB identity bind the exact init.
No cycle substitution, raw-global disk cache, gust/cloud proxy or quantiles.
APCP is WMO GRIB2 table 4.2 discipline 0/category 1/number 8 (kg m**-2),
accumulated from init; ecCodes can expose paramId=0 and units=unknown.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'var/direct-geps-points-v1'
MAX_FILE = 20_000_000
BASE = 'https://dd.weather.gc.ca/today/ensemble/geps/grib2/raw'
LOCAL = threading.local()
# name: (filename token, WMO category, WMO number, paramId, units, level kind,
# level, lower physical bound, upper physical bound). RH supersaturation valid.
FIELDS = {
    'r2': ('RH_TGL_2m', 1, 1, 260242, '%', 'heightAboveGround', 2, 0, float('inf')),
    'r1000': ('RH_ISBL_1000', 1, 1, 157, '%', 'isobaricInhPa', 1000, 0, float('inf')),
    'r925': ('RH_ISBL_0925', 1, 1, 157, '%', 'isobaricInhPa', 925, 0, float('inf')),
    'r850': ('RH_ISBL_0850', 1, 1, 157, '%', 'isobaricInhPa', 850, 0, float('inf')),
    'sp': ('PRES_SFC_0', 3, 0, 134, 'Pa', 'surface', 0, 30000, 110000),
    'msl': ('PRMSL_MSL_0', 3, 1, 260074, 'Pa', 'meanSea', 0, 75000, 115000),
    '2t': ('TMP_TGL_2m', 0, 0, 167, 'K', 'heightAboveGround', 2, 153.15, 353.15),
    '10u': ('UGRD_TGL_10m', 2, 2, 165, 'm s**-1', 'heightAboveGround', 10, -160, 160),
    '10v': ('VGRD_TGL_10m', 2, 3, 166, 'm s**-1', 'heightAboveGround', 10, -160, 160),
    'tp': ('APCP_SFC_0', 1, 8, 0, 'kg m**-2', 'surface', 0, 0, 5000),
}
GRID = dict(gridType='regular_ll', Ni=720, Nj=361,
            latitudeOfFirstGridPointInDegrees=-90., longitudeOfFirstGridPointInDegrees=0.,
            latitudeOfLastGridPointInDegrees=90., longitudeOfLastGridPointInDegrees=359.5,
            iDirectionIncrementInDegrees=.5, jDirectionIncrementInDegrees=.5,
            iScansNegatively=0, jScansPositively=1, jPointsAreConsecutive=0,
            alternativeRowScanning=0)


def require(ok):
    if not ok:
        raise ValueError('GEPS native validation failed')


def stamp(t):
    require(isinstance(t, datetime) and t.tzinfo is not None)
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value):
    require(isinstance(value, str) and value.endswith('Z'))
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(stamp(t) == value)
    return t


def check_run(init, lead):
    require(isinstance(init, datetime) and init.tzinfo is not None and init.utcoffset() == timedelta(0))
    require(init.hour in (0, 12) and init.minute == init.second == init.microsecond == 0)
    require(type(lead) is int and 0 <= lead <= 384 and lead % 6 == 0)


def expected_members():
    return [f'{i:02}' for i in range(21)]


def model_specs():
    """Return field -> (paramId, resolved native units, kind, level, lo, hi)."""
    return {name: tuple(spec[3:]) for name, spec in FIELDS.items()}


def url_for(init, lead, field):
    check_run(init, lead)
    require(field in FIELDS)
    return (f'{BASE}/{init:%H}/{lead:03}/CMC_geps-raw_{FIELDS[field][0]}'
            f'_latlon0p5x0p5_{init:%Y%m%d%H}_P{lead:03}_allmbrs.grib2')


def expected_identity(init, lead, member, field):
    check_run(init, lead)
    require(member in expected_members() and field in FIELDS)
    _, cat, num, pid, unit, kind, level, _, _ = FIELDS[field]
    valid = init + timedelta(hours=lead)
    return dict(edition=2, centre='cwao', subCentre=0, typeOfGeneratingProcess=4,
                productDefinitionTemplateNumber=11 if field == 'tp' else 1,
                typeOfEnsembleForecast=1 if member == '00' else 4,
                number=int(member), numberOfForecastsInEnsemble=21,
                discipline=0, parameterCategory=cat, parameterNumber=num,
                tablesVersion=4, localTablesVersion=0, paramId=pid,
                units='unknown' if field == 'tp' else unit,
                typeOfLevel=kind, level=level, dataDate=int(init.strftime('%Y%m%d')),
                dataTime=int(init.strftime('%H%M')), validityDate=int(valid.strftime('%Y%m%d')),
                validityTime=int(valid.strftime('%H%M')), startStep=0 if field == 'tp' else lead,
                endStep=lead, stepType='accum' if field == 'tp' else 'instant', stepUnits=1, **GRID)


def session():
    # One requests.Session per persistent executor thread, never shared concurrently.
    if not hasattr(LOCAL, 'session'):
        import requests
        LOCAL.session = requests.Session()
        LOCAL.session.headers.update({'User-Agent': 'kcdw-direct-geps/1', 'Accept-Encoding': 'identity'})
    return LOCAL.session


# Keep the threads alive between frames so connections survive repeated leads.
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix='geps')


def fetch(url, limit=MAX_FILE):
    require(0 < limit <= MAX_FILE)
    with session().get(url, stream=True, timeout=(5, 30), allow_redirects=False) as r:
        require(r.status_code == 200)
        length = r.headers.get('Content-Length')
        if length is not None:
            require(0 < int(length) <= limit)
        content = bytearray()
        for block in r.iter_content(65536):
            require(len(content) + len(block) <= limit)
            content.extend(block)
        require(content and (length is None or len(content) == int(length)))
        return bytes(content)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.update(v for k, v in attrs if k == 'href' and v is not None)


def probe(init, last, fields=None):
    """Catalog readiness of exact last-lead field filenames, not full-cycle proof."""
    check_run(init, last)
    names = list(FIELDS) if fields is None else list(fields)
    require(names and len(names) == len(set(names)) and set(names) <= FIELDS.keys())
    try:
        directory = f'{BASE}/{init:%H}/{last:03}/'
        parser = _Links()
        parser.feed(fetch(directory, 2_000_000).decode('utf-8'))
        return all(url_for(init, last, f).rsplit('/', 1)[1] in parser.links for f in names)
    except (OSError, ValueError, UnicodeError):
        return False


def messages(raw):
    """Yield inclusive real file byte ranges; reject padding/truncation/GRIB1."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_FILE)
    offset = 0
    while offset < len(raw):
        require(raw[offset:offset+4] == b'GRIB' and raw[offset+7:offset+8] == b'\x02')
        size = int.from_bytes(raw[offset+8:offset+16], 'big')
        require(20 <= size <= MAX_FILE and offset + size <= len(raw))
        msg = raw[offset:offset+size]
        require(msg[-4:] == b'7777')
        yield offset, offset+size-1, msg
        offset += size


def decode_group(raw, init, lead, field, collected, fetched=None):
    import eccodes as ec
    fetched = fetched or stamp(datetime.now(timezone.utc))
    points = []
    for a, b, msg in messages(raw):
        require(len(points) < 21)
        g = ec.codes_new_from_message(msg)
        try:
            member = f'{int(ec.codes_get(g, "number")):02}'
            expected = expected_identity(init, lead, member, field)
            identity = {k: ec.codes_get(g, k) for k in expected}
            require(identity == expected)
            nearest = ec.codes_grib_find_nearest(g, 40.8752, 285.7186)[0]
            value = float(nearest['value'])
            require(value != ec.codes_get(g, 'missingValue'))
            if ec.codes_get(g, 'bitmapPresent'):
                require(ec.codes_get_array(g, 'bitmap')[int(nearest['index'])] == 1)
            points.append(dict(value=value, latitude=float(nearest['lat']),
                               longitude=(float(nearest['lon'])+180) % 360-180,
                               identity=identity, units=FIELDS[field][4], sha256=hashlib.sha256(msg).hexdigest(),
                               model='geps', init=stamp(init), lead=lead, member=member, field=field,
                               url=url_for(init, lead, field), range=[a,b],
                               collected_at=collected, fetched_at=fetched))
        finally:
            ec.codes_release(g)
    points.sort(key=lambda p: p['member'])
    validate_packet(dict(model='geps', init=stamp(init), offered_members=expected_members(), points=points), parse_time(fetched))
    return points


def validate_packet(packet, now):
    """Offline exact identity/run/field/member and complete grouped-field proof.

    Partial lead/field coverage is allowed for collection progress, but any
    represented field/lead must have all 21 members and contiguous file ranges.
    """
    require(isinstance(packet, dict) and packet.get('model') == 'geps')
    init = parse_time(packet['init'])
    check_run(init, 0)
    require(now-timedelta(hours=24) <= init <= now)
    require(packet['offered_members'] == expected_members())
    points = packet['points']
    require(isinstance(points, list) and 0 < len(points) <= 100000)
    groups = {}
    seen = set()
    for p in points:
        member, name, lead = p['member'], p['field'], p['lead']
        check_run(init, lead)
        require(member in expected_members() and name in FIELDS)
        key = (member, name, lead)
        require(key not in seen)
        seen.add(key)
        require(p['model'] == 'geps' and p['init'] == packet['init'])
        require(p['url'] == url_for(init, lead, name))
        require(p['latitude'] == 41. and p['longitude'] == -74.5)
        value = p['value']
        require(type(value) in (float,int) and math.isfinite(value) and FIELDS[name][7] <= value <= FIELDS[name][8])
        require(p['identity'] == expected_identity(init, lead, member, name))
        require(p['units'] == FIELDS[name][4])
        require(isinstance(p['sha256'], str) and re.fullmatch('[0-9a-f]{64}', p['sha256']) is not None)
        span = p['range']
        require(isinstance(span, list) and len(span) == 2)
        a,b = span
        require(type(a) is int and type(b) is int and 0 <= a <= b < MAX_FILE and b-a+1 >= 20)
        collected, fetched = parse_time(p['collected_at']), parse_time(p['fetched_at'])
        require(init <= collected <= fetched <= now+timedelta(minutes=5) and now-collected <= timedelta(hours=12))
        groups.setdefault((name,lead), []).append(p)
    for group in groups.values():
        require(sorted(p['member'] for p in group) == expected_members())
        ordered = sorted(group, key=lambda p: p['range'][0])
        require(ordered[0]['range'][0] == 0)
        require(all(x['range'][1]+1 == y['range'][0] for x,y in zip(ordered, ordered[1:])))
        require(len({(p['collected_at'],p['fetched_at']) for p in group}) == 1)
    if 'requested_leads' in packet:
        leads = packet['requested_leads']
        require(isinstance(leads, list) and len(leads) == len(set(leads)))
        for lead in leads:
            check_run(init, lead)
        require(all(p['lead'] in leads for p in points))
    return packet


def path_for(init, lead, field):
    check_run(init, lead)
    require(field in FIELDS)
    return CACHE / init.strftime('%Y%m%d%H') / f'{lead:03}-{field}.json'


def _field(init, lead, field, collected):
    path = path_for(init, lead, field)
    now = parse_time(collected)
    try:
        require(path.stat().st_size <= 200000)
        packet = json.loads(path.read_text())
        validate_packet(packet, now)
        require(packet['init'] == stamp(init) and len(packet['points']) == 21)
        require(all(p['field'] == field and p['lead'] == lead for p in packet['points']))
        return packet['points']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    points = decode_group(fetch(url_for(init, lead, field)), init, lead, field, collected)
    packet = dict(model='geps', init=stamp(init), offered_members=expected_members(), points=points)
    validate_packet(packet, max(now, parse_time(points[0]['fetched_at'])))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as f:
            tmp = f.name
            json.dump(packet, f, allow_nan=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return points


def fetch_frame(init, lead, collected, *, fields=None, workers=6):
    """Return 21 members × selected core fields; default all 10, fail closed.

    collected accepts a UTC datetime or canonical iso-Z. workers is bounded 1–6;
    the persistent six-thread pool retains thread-local HTTP connections.
    """
    check_run(init, lead)
    collected = stamp(collected) if isinstance(collected, datetime) else collected
    now = parse_time(collected)
    require(init <= now <= init+timedelta(hours=24))
    require(type(workers) is int and 1 <= workers <= 6)
    names = list(FIELDS) if fields is None else list(fields)
    require(names and len(names) == len(set(names)) and set(names) <= FIELDS.keys())
    semaphore = threading.BoundedSemaphore(workers)
    def get(name):
        with semaphore:
            return _field(init, lead, name, collected)
    tasks = [_POOL.submit(get, name) for name in names]
    try:
        points = [p for task in tasks for p in task.result()]
    except Exception:
        for task in tasks:
            task.cancel()
        raise
    require(len(points) == len(names)*21)
    return points
