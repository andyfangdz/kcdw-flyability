"""Run-pinned deterministic model matrix for the expected flight window.

One row per global model from Open-Meteo's single-runs API, so every value is
bound to the explicit ``run`` requested; no rolling response is relabeled. Each
model contributes its newest run covering the window plus the covering run
before it, giving a same-window run-to-run change. Because a run-pinned result
never changes, results are cached per (model, run): each refresh only probes
for newer runs, and an API outage serves the cached rows. Cloud base is a surface
temperature/dew-point estimate, never a ceiling. Screening reads use fixed,
displayed thresholds; they are not probabilities or a go/no-go decision.
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
from .events import TZ, Event

VERSION = 1
API = 'https://single-runs-api.open-meteo.com/v1/forecast'
MODELS = (
    ('ifs', 'ECMWF IFS', 'ecmwf_ifs'),
    ('aifs', 'ECMWF AIFS', 'ecmwf_aifs025_single'),
    ('gfs', 'NCEP GFS', 'gfs_global'),
    ('icon', 'DWD ICON', 'icon_global'),
    ('ukmo', 'UKMO Global', 'ukmo_global_deterministic_10km'),
)
FIELDS = ('cloud_cover_low', 'cloud_cover_mid', 'temperature_2m', 'dew_point_2m', 'wind_speed_10m',
          'wind_direction_10m', 'wind_gusts_10m', 'precipitation', 'relative_humidity_925hPa', 'pressure_msl')
REQUIRED = ('cloud_cover_low', 'temperature_2m', 'dew_point_2m', 'wind_speed_10m', 'wind_direction_10m', 'precipitation')
MAX_CANDIDATES = 8
MAX_FORECAST_DAYS = 16
DEADLINE_SECONDS = 120
RETRY_PAUSE_SECONDS = 45
MAX_WORKERS = 2
SETTLED_AFTER = timedelta(hours=12)
LATER_HOURS = 3
FEET_PER_DEGREE = 125 * 3.28084
THRESHOLDS = {'rain_mm': 1.0, 'overcast_pct': 80, 'broken_pct': 50, 'base_ft': 3000, 'gust_kt': 25}
READS = {
    'rain': ('Rain in window', 'poor'),
    'low_overcast': ('Overcast, low bases', 'poor'),
    'high_overcast': ('Overcast, higher bases', 'marginal'),
    'broken': ('Broken low cloud', 'marginal'),
    'scattered': ('Scattered or clear', 'good'),
}
TONE_RANK = {'poor': 0, 'marginal': 1, 'good': 2}


def flight_window(snapshot: dict) -> tuple[datetime, datetime, str]:
    event = Event(**snapshot['event'])
    day = datetime.combine(event.day, datetime.min.time(), TZ)
    timing = snapshot.get('event_timing')
    if isinstance(timing, dict) and timing.get('flight_start') and timing.get('flight_end'):
        clock = lambda text: day + timedelta(hours=int(text[:2]), minutes=int(text[3:]))
        start, end = clock(timing['flight_start']), clock(timing['flight_end'])
        if start < end and start.minute == end.minute == 0:
            return start.astimezone(UTC), end.astimezone(UTC), 'expected flight'
    return ((day + timedelta(hours=event.start_hour)).astimezone(UTC),
            (day + timedelta(hours=event.end_hour)).astimezone(UTC), 'event window')


def candidate_runs(now: datetime) -> list[datetime]:
    latest = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    latest -= timedelta(hours=latest.hour % 6)
    return [latest - timedelta(hours=6 * i) for i in range(MAX_CANDIDATES)]


def run_url(model_id: str, run: datetime, days: int, latitude: float, longitude: float) -> str:
    return API + '?' + urlencode(dict(latitude=f'{latitude:.4f}', longitude=f'{longitude:.4f}', hourly=','.join(FIELDS),
                                      models=model_id, wind_speed_unit='kn', precipitation_unit='mm', timezone='UTC',
                                      timeformat='iso8601', forecast_days=str(days), run=run.strftime('%Y-%m-%dT%H:%M')))


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def classify(values: dict) -> str:
    low, base = values['low_cloud_pct'], values['base_ft']
    if values['rain_mm'] >= THRESHOLDS['rain_mm']:
        return 'rain'
    if low >= THRESHOLDS['overcast_pct']:
        return 'low_overcast' if base < THRESHOLDS['base_ft'] else 'high_overcast'
    return 'broken' if low >= THRESHOLDS['broken_pct'] else 'scattered'


def tone(values: dict) -> str:
    result = READS[values['read']][1]
    gust = values.get('gust_kt')
    return 'marginal' if result == 'good' and gust is not None and gust >= THRESHOLDS['gust_kt'] else result


def summarize(raw: dict, start: datetime, end: datetime) -> dict | None:
    """Window statistics from one run-pinned response, or None when the run does not cover the window."""
    hourly = raw.get('hourly') if isinstance(raw, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get('time'), list):
        return None
    if raw.get('utc_offset_seconds') != 0:
        return None
    axis = {}
    for i, stamp in enumerate(hourly['time']):
        try:
            moment = parse_time(stamp)
        except (ValueError, AttributeError, TypeError):
            return None
        axis[moment if moment.tzinfo else moment.replace(tzinfo=UTC)] = i
    later_end = end + timedelta(hours=LATER_HOURS)
    instants = [start + timedelta(hours=h) for h in range(int((end - start).total_seconds() // 3600) + 1)]
    later = [end + timedelta(hours=h) for h in range(1, LATER_HOURS + 1)]
    if any(t not in axis for t in instants + later) or later[-1] != later_end:
        return None

    def series(key, moments):
        column = hourly.get(key)
        if not isinstance(column, list) or len(column) != len(hourly['time']):
            return [None] * len(moments)
        return [_number(column[axis[t]]) for t in moments]

    if any(v is None for key in REQUIRED for v in series(key, instants)):
        return None
    if any(v is None for key in ('cloud_cover_low', 'temperature_2m', 'dew_point_2m') for v in series(key, later)):
        return None
    base = lambda moments: min(max(0.0, t - d) * FEET_PER_DEGREE
                               for t, d in zip(series('temperature_2m', moments), series('dew_point_2m', moments)))
    speeds, directions = series('wind_speed_10m', instants), series('wind_direction_10m', instants)
    east = sum(s * math.sin(math.radians(d)) for s, d in zip(speeds, directions))
    north = sum(s * math.cos(math.radians(d)) for s, d in zip(speeds, directions))
    gusts = [v for v in series('wind_gusts_10m', instants) if v is not None]
    values = {
        'low_cloud_pct': round(_mean(series('cloud_cover_low', instants))),
        'mid_cloud_pct': None if _mean(series('cloud_cover_mid', instants)) is None else round(_mean(series('cloud_cover_mid', instants))),
        'base_ft': int(round(base(instants), -2)),
        'later_low_cloud_pct': round(_mean(series('cloud_cover_low', later))),
        'later_base_ft': int(round(base(later), -2)),
        'wind_dir_deg': int(round(math.degrees(math.atan2(east, north)) % 360)) % 360,
        'wind_kt': round(_mean(speeds), 1),
        'gust_kt': round(max(gusts), 1) if len(gusts) == len(instants) else None,
        'rain_mm': round(sum(series('precipitation', instants[1:])), 2),
        'rh925_pct': None if _mean(series('relative_humidity_925hPa', instants)) is None else round(_mean(series('relative_humidity_925hPa', instants))),
        'mslp_hpa': None if _mean(series('pressure_msl', instants)) is None else round(_mean(series('pressure_msl', instants)), 1),
    }
    values['read'] = classify(values)
    values['tone'] = tone(values)
    grid = {'latitude': _number(raw.get('latitude')), 'longitude': _number(raw.get('longitude'))}
    return {'values': values, 'grid': grid}


def change(current: dict, previous: dict | None) -> str:
    if not previous:
        return 'unknown'
    now, then = current['values'], previous['values']
    rank = TONE_RANK[now['tone']] - TONE_RANK[then['tone']]
    if rank:
        return 'better' if rank > 0 else 'worse'
    cloud, base = now['low_cloud_pct'] - then['low_cloud_pct'], now['base_ft'] - then['base_ft']
    if cloud <= -20 or (base >= 500 and cloud <= 0):
        return 'better'
    if cloud >= 20 or (base <= -500 and cloud >= 0):
        return 'worse'
    return 'steady'


def _problem(exc) -> str:
    text = str(exc)
    return ('not published' if 'HTTP Error 400' in text else 'rate limit' if 'HTTP Error 429' in text else
            'timeout' if 'timed out' in text.lower() else 'error')


def load_cache(path, window: dict) -> dict:
    """Run-pinned results never change, so keep them per (model, run); any mismatch starts an empty cache."""
    fresh = {'version': VERSION, 'thresholds': dict(THRESHOLDS), 'window': window, 'entries': {}}
    if path is None:
        return fresh
    try:
        cache = json.loads(Path(path).read_text(encoding='utf-8'))
        if all(cache.get(k) == fresh[k] for k in ('version', 'thresholds', 'window')) and isinstance(cache.get('entries'), dict):
            return cache
    except (OSError, ValueError, AttributeError):
        pass
    return fresh


def collect_model(client, spec, runs, days, start, end, latitude, longitude, deadline, cached: dict, now: datetime) -> dict:
    key, label, model_id = spec
    found, problems = [], set()
    for run in runs:
        if len(found) == 2:
            break
        stamp = iso_z(run)
        if stamp in cached:
            if cached[stamp]:
                found.append(cached[stamp])
            continue
        if time.monotonic() > deadline:
            continue
        url = run_url(model_id, run, days, latitude, longitude)
        try:
            raw = client.get(url)
        except Exception as exc:
            problems.add(_problem(exc))
            continue
        packet = summarize(raw, start, end)
        if packet:
            cached[stamp] = {'run': stamp, 'source_url': url, **packet}
            found.append(cached[stamp])
        elif now - run >= SETTLED_AFTER and isinstance(raw, dict) and isinstance(raw.get('hourly'), dict) and raw['hourly'].get('time'):
            cached[stamp] = None  # a settled run that stops short of the window will never cover it
    for stamp in [s for s in cached if s not in {iso_z(run) for run in runs}]:
        del cached[stamp]
    row = {'key': key, 'label': label, 'model_id': model_id, 'ok': bool(found)}
    if found:
        row.update(current=found[0], previous=found[1] if len(found) == 2 else None,
                   change=change(found[0], found[1] if len(found) == 2 else None))
    else:
        row['error'] = 'no recent run covers the window'
    # "not published" is the normal answer for a cycle that is still running; anything else is worth a retry.
    row['retry'] = bool(problems - {'not published'}) and len(found) < 2
    if not found and problems - {'not published'}:
        row['error'] = 'upstream ' + ', '.join(sorted(problems - {'not published'}))
    return row


def collect_matrix(client, snapshot: dict, now: datetime, cache_path=None, sleep=time.sleep) -> dict | None:
    start, end, kind = flight_window(snapshot)
    now = now.astimezone(UTC)
    days = (end + timedelta(hours=LATER_HOURS)).date().toordinal() - (now - timedelta(hours=6 * MAX_CANDIDATES)).date().toordinal() + 1
    if now >= end or days > MAX_FORECAST_DAYS:
        return None
    airport = snapshot['airport']
    window = {'start': iso_z(start), 'end': iso_z(end), 'later_end': iso_z(end + timedelta(hours=LATER_HOURS)), 'kind': kind}
    cache = load_cache(cache_path, window)
    deadline = time.monotonic() + DEADLINE_SECONDS
    attempt = lambda spec: collect_model(client, spec, candidate_runs(now), days, start, end, airport['latitude'],
                                         airport['longitude'], deadline, cache['entries'].setdefault(spec[0], {}), now)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rows = list(pool.map(attempt, MODELS))
        # The refresh shares Open-Meteo's per-minute allowance with the ensemble collectors that run just before it.
        if any(row['retry'] for row in rows) and time.monotonic() + RETRY_PAUSE_SECONDS < deadline:
            sleep(RETRY_PAUSE_SECONDS)
            rows = [attempt(spec) if row['retry'] else row for spec, row in zip(MODELS, rows)]
    for row in rows:
        del row['retry']
    if cache_path is not None:
        atomic_write(cache_path, json.dumps(cache, allow_nan=False, separators=(',', ':')) + '\n')
    if not any(row['ok'] for row in rows):
        return None
    return {'version': VERSION, 'collected_at': iso_z(datetime.now(UTC) if getattr(client, 'direct_native', False) else now),
            'snapshot_collected_at': snapshot['collected_at'],
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'window': window, 'thresholds': dict(THRESHOLDS), 'models': rows,
            'source': 'Open-Meteo single-runs API; each row bound to its requested run', 'license': 'CC BY 4.0'}


def validate_matrix(packet, snapshot: dict) -> dict:
    """Recheck a persisted packet before rendering; raise ValueError on any inconsistency."""
    def require(condition):
        if not condition:
            raise ValueError('invalid model matrix')
    require(isinstance(packet, dict) and packet.get('version') == VERSION)
    require(packet.get('snapshot_collected_at') == snapshot['collected_at'])
    require(packet.get('event') == {k: snapshot['event'][k] for k in ('slug', 'date', 'window')})
    start, end, kind = flight_window(snapshot)
    require(packet.get('window') == {'start': iso_z(start), 'end': iso_z(end),
                                     'later_end': iso_z(end + timedelta(hours=LATER_HOURS)), 'kind': kind})
    require(packet.get('thresholds') == THRESHOLDS)
    rows = packet.get('models')
    require(isinstance(rows, list) and [r.get('key') if isinstance(r, dict) else None for r in rows] == [m[0] for m in MODELS])
    collected = parse_time(packet['collected_at'])
    for row, (key, label, model_id) in zip(rows, MODELS):
        require(row.get('label') == label and row.get('model_id') == model_id and isinstance(row.get('ok'), bool))
        if not row['ok']:
            continue
        runs = [row.get('current'), row.get('previous')]
        require(isinstance(runs[0], dict) and (runs[1] is None or isinstance(runs[1], dict)))
        for item in filter(None, runs):
            run = parse_time(item['run'])
            require(run <= collected and run.hour % 6 == 0 and collected - run <= timedelta(hours=6 * MAX_CANDIDATES + 6))
            values = item.get('values')
            require(isinstance(values, dict))
            for name, low, high, optional in (('low_cloud_pct', 0, 100, False), ('later_low_cloud_pct', 0, 100, False),
                                              ('mid_cloud_pct', 0, 100, True), ('base_ft', 0, 40000, False),
                                              ('later_base_ft', 0, 40000, False), ('wind_dir_deg', 0, 359, False),
                                              ('wind_kt', 0, 150, False), ('gust_kt', 0, 200, True), ('rain_mm', 0, 500, False),
                                              ('rh925_pct', 0, 120, True), ('mslp_hpa', 850, 1100, True)):
                value = values.get(name)
                require((optional and value is None) or (_number(value) is not None and low <= value <= high))
            require(values.get('read') == classify(values) and values.get('tone') == tone(values))
        require(runs[1] is None or parse_time(runs[1]['run']) < parse_time(runs[0]['run']))
        require(row.get('change') == change(runs[0], runs[1]))
    require(any(row['ok'] for row in rows))
    return packet
