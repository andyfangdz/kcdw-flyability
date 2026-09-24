"""Run-pinned deterministic and blend wind/gust guidance around a dated event.

NWS grids start from the National Blend of Models (NBM); its gusts often explain
a grid that differs from the ensembles. Each row is one deterministic scenario
bound to an explicitly requested ``run``: Open-Meteo's single-runs API, or
native NOAA RRFS GRIB (now through the event day) through the isolated worker.
Hourly series cover the chart domain (collection day through the day after the
event); flight-window summaries are always recomputed from those series, never
trusted from storage. Not a vote, probability or pilot/aircraft limit.
"""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .common import UTC, atomic_write, iso_z, parse_time
from .event_model_matrix import flight_window
from .events import TZ, Event

VERSION = 1
API = 'https://single-runs-api.open-meteo.com/v1/forecast'
# key, label, Open-Meteo model, run cadence (h), candidate runs, chart color.
MODELS = (
    ('nbm', 'NBM · NWS blend', 'ncep_nbm_conus', 1, 8, '#2a78d6'),
    ('nam', 'NAM 3 km', 'ncep_nam_conus', 6, 4, '#eb6834'),
    ('hrrr', 'HRRR', 'ncep_hrrr_conus', 6, 4, '#1baf7a'),
    ('ifs', 'ECMWF IFS', 'ecmwf_ifs', 6, 4, '#4a3aa7'),
    ('icon', 'DWD ICON', 'icon_global', 6, 4, '#e87ba4'),
    ('ukmo', 'UKMO Global', 'ukmo_global_deterministic_10km', 6, 4, '#008300'),
    # HRRR's successor shares its hue; charts draw it dashed rather than add a ninth color.
    ('rrfs', 'RRFS 3 km', 'native:rrfs', 6, 4, '#1baf7a'),
)
DASHED = {'rrfs'}
RRFS_ROOT = 'https://noaa-rrfs-ops-pds.s3.amazonaws.com/rrfs.'
RRFS_HORIZON = 84  # hours; 00/06/12/18Z runs
PYTHON = Path(__file__).resolve().parents[1] / 'var/native-weather-venv/bin/python'
NWS_COLOR = '#e34948'
FIELDS = ('wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m', 'pressure_msl', 'precipitation', 'cloud_cover_low')
# Optional context for the week-ahead timeline; absent (older cache, RRFS, NBM low cloud) is unknown.
EXTRAS = (('pressure_msl', 'pressure', 1100), ('precipitation', 'rain', 500), ('cloud_cover_low', 'low_cloud', 100))
MAX_RUN_AGE = timedelta(hours=36)
DEADLINE_SECONDS = 60
MAX_WORKERS = 2
NOTES = [
    'Knots; true degrees. Each row is one deterministic run (NBM is a statistical blend), not an ensemble median, vote or probability.',
    'NWS grid gusts are usually built from NBM; NBM agreement with the grid is not independent confirmation.',
    'Gusts are each model\'s own diagnostic at hourly samples; they are not guaranteed maxima between samples.',
    'Crosswind pairs each hour\'s gust with that hour\'s mean direction; runway availability and limits are not checked.',
]
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _require(ok):
    if not ok:
        raise ValueError('invalid gust guidance')


def _number(value, high):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= high else None


def domain(snapshot) -> tuple[datetime, datetime]:
    """Collection day through the end of the day after the event, hourly, UTC."""
    collected = parse_time(snapshot['collected_at']).astimezone(TZ)
    start = collected.replace(hour=0, minute=0, second=0, microsecond=0)
    day = Event(**snapshot['event']).day
    end = datetime.combine(day + timedelta(days=2), datetime.min.time(), TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def _hours(start, end):
    return [start + timedelta(hours=h) for h in range(int((end - start).total_seconds() // 3600))]


def candidate_runs(now, cadence, count):
    latest = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    latest -= timedelta(hours=latest.hour % cadence)
    return [latest - timedelta(hours=cadence * i) for i in range(count)]


def run_url(model_id, run, days, latitude, longitude):
    return API + '?' + urlencode(dict(latitude=f'{latitude:.4f}', longitude=f'{longitude:.4f}', hourly=','.join(FIELDS),
                                      models=model_id, wind_speed_unit='kn', timezone='UTC', timeformat='iso8601',
                                      forecast_days=str(days), run=run.strftime('%Y-%m-%dT%H:%M')))


def _series(raw, hours):
    """Align one response to the domain; None for absent hours or invalid values."""
    hourly = raw.get('hourly') if isinstance(raw, dict) else None
    _require(isinstance(hourly, dict) and raw.get('utc_offset_seconds') == 0 and isinstance(hourly.get('time'), list))
    axis = {}
    for i, stamp in enumerate(hourly['time']):
        moment = parse_time(stamp)
        axis[moment if moment.tzinfo else moment.replace(tzinfo=UTC)] = i
    out = {}
    for field, name, high in (('wind_speed_10m', 'wind', 150), ('wind_gusts_10m', 'gust', 200), ('wind_direction_10m', 'direction', 360)):
        column = hourly.get(field)
        _require(isinstance(column, list) and len(column) == len(hourly['time']))
        out[name] = [_number(column[axis[t]], high) if t in axis else None for t in hours]
    for field, name, high in EXTRAS:
        column = hourly.get(field)
        if isinstance(column, list) and len(column) == len(hourly['time']):
            out[name] = [_number(column[axis[t]], high) if t in axis else None for t in hours]
    return out


def _covers(series, hours, window, fields=('wind', 'gust', 'direction')):
    start, end, _ = window
    wanted = [i for i, t in enumerate(hours) if start <= t <= end]
    return bool(wanted) and all(series[k][i] is not None for i in wanted for k in fields)


def load_cache(path, key):
    if path is None:
        return {'version': VERSION, 'key': key, 'models': {}}
    try:
        cache = json.loads(Path(path).read_text(encoding='utf-8'))
        if cache.get('version') == VERSION and cache.get('key') == key and isinstance(cache.get('models'), dict):
            return cache
    except (OSError, ValueError, AttributeError):
        pass
    return {'version': VERSION, 'key': key, 'models': {}}


def _rrfs_series(snapshot, run, hours, now):
    """Native RRFS via the isolated ecCodes worker: every hour from now through the event day or the
    84-hour horizon (~5 MB each). Runs that cannot reach the flight window are skipped."""
    import subprocess
    last = datetime.combine(Event(**snapshot['event']).day + timedelta(days=1), datetime.min.time(), TZ).astimezone(UTC)
    first = max(run, now.replace(minute=0, second=0, microsecond=0))
    if flight_window(snapshot)[1] > run + timedelta(hours=RRFS_HORIZON):
        return None  # this run can never reach the flight
    leads = [int((t - run).total_seconds() // 3600) for t in hours if first <= t < last and t <= run + timedelta(hours=RRFS_HORIZON)]
    if not leads:
        return None
    result = subprocess.run([str(PYTHON), str(Path(__file__).with_name('rrfs_worker.py'))],
                            input=json.dumps({'init': iso_z(run), 'leads': leads}), text=True,
                            capture_output=True, check=True, timeout=330)
    samples = {parse_time(x['at']): x for x in json.loads(result.stdout)}
    _require(len(samples) == len(leads))
    pick = lambda key, high: [_number(samples[t][key], high) if t in samples else None for t in hours]
    grid = next(iter(samples.values()))
    return ({'wind': pick('wind_kt', 150), 'gust': pick('gust_kt', 200), 'direction': pick('from_deg', 360)},
            f'{RRFS_ROOT}{run:%Y%m%d}/{run:%H}/', {'latitude': grid['latitude'], 'longitude': grid['longitude']})


def collect_model(client, spec, snapshot, now, deadline, cached):
    """Newest covering run; a cached run is reused, never relabeled."""
    key, label, model_id, cadence, count, _ = spec
    window = flight_window(snapshot)
    start, end = domain(snapshot)
    hours = _hours(start, end)
    airport = snapshot['airport']
    known = cached.get('entry')
    if cached.get('raw_series', {}).get('domain_start') != iso_z(start):
        known = None  # a new display day needs the run's series re-aligned
    short = set(cached.get('short', []))
    for run in candidate_runs(now, cadence, count):
        stamp = iso_z(run)
        if known and parse_time(known['run']) >= run:
            break
        if stamp in short or time.monotonic() > deadline:
            continue
        coord = lambda v, limit: v if type(v) in (int, float) and math.isfinite(v) and abs(v) <= limit else None
        if model_id == 'native:rrfs':
            try:
                fetched = _rrfs_series(snapshot, run, hours, now)
            except Exception:
                continue  # not yet published or a failed range; retried next refresh
            if fetched is None:
                short.add(stamp)
                continue
            series, url, grid = fetched
        else:
            days = (end.astimezone(TZ).date() - run.astimezone(TZ).date()).days + 1
            url = run_url(model_id, run, max(days, 1), airport['latitude'], airport['longitude'])
            try:
                raw = client.get(url)
                series = _series(raw, hours)
            except Exception:
                continue
            grid = {'latitude': coord(raw.get('latitude'), 90), 'longitude': coord(raw.get('longitude'), 180)}
        if _covers(series, hours, window):
            known = {'run': stamp, 'source_url': url, 'fetched_at': iso_z(now), 'grid': grid}
            cached['raw_series'] = {'domain_start': iso_z(start), **series}
            break
        short.add(stamp)  # a published run that stops short never gains coverage
    cached['short'] = sorted(s for s in short if now - parse_time(s) <= MAX_RUN_AGE)
    row = {'key': key, 'label': label, 'model_id': model_id, 'ok': False}
    if known and now - parse_time(known['run']) <= MAX_RUN_AGE and cached.get('raw_series', {}).get('domain_start') == iso_z(start):
        cached['entry'] = known
        series = {k: v for k, v in cached['raw_series'].items() if k != 'domain_start'}
        if _covers(series, hours, window):
            row.update(ok=True, **known, **series)
            return row
    row['error'] = 'no recent run covers the flight window'
    return row


def collect_gusts(client, snapshot, now, cache_path=None):
    now = now.astimezone(UTC)
    start, end, kind = flight_window(snapshot)
    if now >= end:
        return None
    lo, hi = domain(snapshot)
    cache = load_cache(cache_path, f'{snapshot["event"]["slug"]}|{iso_z(start)}|{iso_z(end)}')
    deadline = time.monotonic() + DEADLINE_SECONDS
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rows = list(pool.map(lambda spec: collect_model(client, spec, snapshot, now, deadline,
                                                        cache['models'].setdefault(spec[0], {})), MODELS))
    if cache_path is not None:
        atomic_write(cache_path, json.dumps(cache, allow_nan=False, separators=(',', ':')) + '\n')
    if not any(row['ok'] for row in rows):
        return None
    return {'version': VERSION, 'collected_at': iso_z(now), 'snapshot_collected_at': snapshot['collected_at'],
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind},
            'domain': {'start': iso_z(lo), 'end': iso_z(hi)}, 'models': rows,
            'source': 'Open-Meteo single-runs API (CC BY 4.0)', 'notes': list(NOTES)}


def validate_gusts(packet, snapshot) -> dict:
    """Recheck binding, run cadence/age and every value; raise ValueError on failure."""
    _require(isinstance(packet, dict) and packet.get('version') == VERSION)
    _require(packet.get('snapshot_collected_at') == snapshot['collected_at'])
    _require(packet.get('event') == {k: snapshot['event'][k] for k in ('slug', 'date', 'window')})
    start, end, kind = flight_window(snapshot)
    _require(packet.get('window') == {'start': iso_z(start), 'end': iso_z(end), 'kind': kind})
    lo, hi = domain(snapshot)
    _require(packet.get('domain') == {'start': iso_z(lo), 'end': iso_z(hi)})
    hours = _hours(lo, hi)
    collected = parse_time(packet['collected_at'])
    _require(abs(collected - parse_time(snapshot['collected_at'])) <= timedelta(minutes=30))
    rows = packet.get('models')
    _require(isinstance(rows, list) and [r.get('key') if isinstance(r, dict) else None for r in rows] == [m[0] for m in MODELS])
    for row, (key, label, model_id, cadence, _, _) in zip(rows, MODELS):
        _require(row.get('label') == label and row.get('model_id') == model_id and type(row.get('ok')) is bool)
        if not row['ok']:
            continue
        run = parse_time(row['run'])
        _require(run <= collected and collected - run <= MAX_RUN_AGE and run.hour % cadence == 0 and not run.minute)
        prefix = f'{RRFS_ROOT}{run:%Y%m%d}/{run:%H}/' if model_id == 'native:rrfs' else API + '?'
        _require(isinstance(row.get('source_url'), str) and row['source_url'].startswith(prefix))
        for name, high in (('wind', 150), ('gust', 200), ('direction', 360)):
            values = row.get(name)
            _require(isinstance(values, list) and len(values) == len(hours))
            _require(all(v is None or _number(v, high) is not None for v in values))
        for _, name, high in EXTRAS:
            if name in row:
                _require(isinstance(row[name], list) and len(row[name]) == len(hours))
                _require(all(v is None or _number(v, high) is not None for v in row[name]))
        _require(_covers(row, hours, (start, end, kind)))
    _require(any(row['ok'] for row in rows))
    return packet


def summarize(row, snapshot):
    """Flight-window hourly samples, peak gust and runway crosswind from stored series."""
    from .event_wind import HEADINGS
    start, end, _ = flight_window(snapshot)
    lo, _hi = domain(snapshot)
    samples = []
    for i, t in enumerate(_hours(lo, _hi)):
        if start <= t <= end:
            d = row['direction'][i]
            samples.append({'at': iso_z(t), 'wind_kt': round(row['wind'][i], 1), 'gust_kt': round(row['gust'][i], 1),
                            'from_deg': None if d is None else int(round(d)) % 360})
    known = all(s['from_deg'] is not None for s in samples)
    cross = {rwy: round(max(s['gust_kt'] * abs(math.sin(math.radians(s['from_deg'] - heading))) for s in samples), 1) if known else None
             for rwy, heading in HEADINGS.items()}
    return {'label': row['label'], 'run': row['run'], 'run_binding': 'response-bound (run requested explicitly)',
            'samples': samples, 'peak_gust_kt': max(s['gust_kt'] for s in samples),
            'mean_wind_kt': round(sum(s['wind_kt'] for s in samples) / len(samples), 1),
            'gust_crosswind_max_kt': cross}


def nws_series(snapshot, now, times):
    """Hourly NWS grid values from the already-validated raw grid; None where not covered."""
    from .event_wind import _interval, validate_wind
    envelope = validate_wind(snapshot.get('event_wind'), snapshot, now)
    if not envelope or not envelope.get('nws'):
        return None
    p = envelope['nws']['raw']['properties']
    out = {'issued_at': envelope['nws']['summary']['issued_at']}
    for field, name, scale in (('windSpeed', 'wind', 1.852), ('windGust', 'gust', 1.852), ('windDirection', 'direction', 1)):
        intervals = [(*_interval(item['validTime']), item['value']) for item in p[field]['values']]
        out[name] = [next((None if v is None else round(v / scale, 1) for a, b, v in intervals if a <= t < b), None) for t in times]
    return out


def gust_evidence(snapshot, now):
    """Compact narrative packet: NWS grid alongside each deterministic run."""
    try:
        packet = validate_gusts(snapshot.get('event_gusts'), snapshot)
    except ERRORS:
        return None
    models = {row['key']: summarize(row, snapshot) if row['ok'] else None for row in packet['models']}
    gfs = gfs_summary(snapshot, now)
    if gfs:
        models['gfs'] = gfs
    return {'window': packet['window'], 'models': models, 'notes': packet['notes']}


def gfs_summary(snapshot, now):
    """GFS row from the page's existing validated GFS source, not a second fetch."""
    from .event_renderer import gfs_status
    try:
        if not gfs_status(snapshot, now)['available']:
            return None
        hourly = snapshot['gfs']['data']['hourly']
        start, end, _ = flight_window(snapshot)
        axis = {parse_time(t): i for i, t in enumerate(hourly['time'])}
        hours = [t for t in sorted(axis) if start <= t <= end]
        row = {'label': 'GFS (page source)', 'run': None}
        lo, hi = domain(snapshot)
        grid = _hours(lo, hi)
        series = {name: [hourly[field][axis[t]] if t in axis and field in hourly else None for t in grid]
                  for name, field in (('wind', 'wind_speed_10m'), ('gust', 'wind_gusts_10m'), ('direction', 'wind_direction_10m'))}
        if len(hours) < 2 or not _covers(series, grid, (start, end, None), ('wind', 'gust')):
            return None
        return summarize({**row, **series}, snapshot) | {'run_binding': 'Page GFS source; see its provenance. No direction field, so no crosswind.'}
    except ERRORS:
        return None
