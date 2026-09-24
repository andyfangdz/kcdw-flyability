"""ECMWF IFS 7-day forecasts for a flight window, from the open-data archive on AWS.

Open-Meteo's run archive has no ECMWF gust, so this reads native GRIB instead.
For each past date the 00/12Z run about seven days earlier supplies 10 m wind
at the flight's six-hourly time T0 (the flight start rounded down) and 10fg,
the maximum gust over T0..T0+6 h. Results are cached per date permanently;
the one-time backfill runs from scripts/ecmwf_archive_backfill.py and each
refresh only adds a few new dates. 10fg exists from the 2024-11-12 runs.
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import UTC, atomic_write, iso_z, parse_time
from .events import TZ

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / 'var/native-weather-venv/bin/python'
WORKER = Path(__file__).with_name('ecmwf_archive_worker.py')
BUCKET = 'https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com'
FIRST_GUST_RUN = date(2024, 11, 12)
BATCH = 20
RETRY_AFTER = timedelta(days=2)
AVAILABLE_AFTER = timedelta(hours=9)  # AWS mirror lag after initialization
RUNWAYS = (30, 83)


def _xw(speed, direction):
    return min(speed * abs(math.sin(math.radians(direction - h))) for h in RUNWAYS)


def sample_time(day, start_local):
    """Flight start on ``day`` (local HH:MM), in UTC, rounded down to six hours."""
    local = datetime.combine(day, datetime.strptime(start_local, '%H:%M').time(), TZ).astimezone(UTC)
    return local.replace(hour=local.hour - local.hour % 6, minute=0, second=0, microsecond=0)


def seven_day_item(day, start_local):
    t0 = sample_time(day, start_local)
    lead = 174 if t0.hour in (6, 18) else 168
    return {'init': iso_z(t0 - timedelta(hours=lead)), 'lead': lead}


def cache_path(var, start_local):
    return Path(var) / 'ecmwf-archive' / f'kcdw-{start_local.replace(":", "")}.json'


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('days'), dict) else {'days': {}}
    except (OSError, ValueError):
        return {'days': {}}


def _run(items):
    result = subprocess.run([str(PYTHON), str(WORKER)], input=json.dumps({'items': items}), text=True,
                            capture_output=True, check=True, timeout=960)
    return json.loads(result.stdout)


def update(path, start_local, first, last, now, limit=None, runner=_run, retry_errors=False):
    """Fill missing dates newest first; errors are retried after RETRY_AFTER."""
    cache = load(path)
    days = cache['days']
    todo = []
    d = last
    while d >= max(first, FIRST_GUST_RUN + timedelta(days=8)):
        key = d.isoformat()
        entry = days.get(key)
        item = seven_day_item(d, start_local)
        stale_error = entry and 'error' in entry and (retry_errors or now - parse_time(entry['checked_at']) >= RETRY_AFTER)
        if (entry is None or stale_error) and parse_time(item['init']) + AVAILABLE_AFTER <= now:
            todo.append((key, item))
        d -= timedelta(days=1)
    if limit is not None:
        todo = todo[:limit]
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            results = runner([item for _, item in chunk])
        except (subprocess.SubprocessError, ValueError, OSError):
            continue  # a throttled or timed-out batch is simply retried on a later run
        for (key, item), out in zip(chunk, results):
            if out.get('init') != item['init'] or out.get('lead') != item['lead']:
                continue
            days[key] = ({'error': out['error'], 'checked_at': iso_z(now)} if 'error' in out else
                         {k: out[k] for k in ('init', 'lead', 'sust_kt', 'from_deg', 'gust_kt')})
        cache.update(start_local=start_local, updated_at=iso_z(now))
        atomic_write(path, json.dumps(cache, separators=(',', ':'), sort_keys=True) + '\n')
    return cache


EE_PYTHON = ROOT / 'var/gfs-earth-engine-venv/bin/python'
EE_WORKER = Path(__file__).with_name('ecmwf_ee_worker.py')
EE_MIN_OVERLAP = 20
EE_TOLERANCE_KT = 0.05
KNOTS = 3600 / 1852


def _run_ee(request):
    result = subprocess.run([str(EE_PYTHON), str(EE_WORKER)], input=json.dumps(request), text=True,
                            capture_output=True, check=True, timeout=330)
    return json.loads(result.stdout)


def fill_from_earth_engine(path, start_local, first, last, now, runner=_run_ee):
    """Fill missing or failed dates from Earth Engine after it matches native GRIB on overlap.

    Returns the number of dates filled; raises ValueError when the cross-check fails.
    """
    cache = load(path)
    days = cache['days']
    wanted = {}
    d = max(first, FIRST_GUST_RUN + timedelta(days=8))
    while d <= last:
        item = seven_day_item(d, start_local)
        wanted.setdefault((parse_time(item['init']).hour, item['lead']), {})[item['init']] = d.isoformat()
        d += timedelta(days=1)
    samples = {}
    for (hour, lead), inits in wanted.items():
        for row in runner({'creation_hour': hour, 'lead': lead, 'since': (min(inits)[:10])}):
            init = iso_z(datetime.fromtimestamp(row['created_ms'] / 1000, UTC))
            if init in inits:
                samples[inits[init]] = {'init': init, 'lead': lead, 'sust_kt': round(math.hypot(row['u'], row['v']) * KNOTS, 2),
                                        'from_deg': round(math.degrees(math.atan2(-row['u'], -row['v'])) % 360, 1),
                                        'gust_kt': round(row['gust'] * KNOTS, 2)}
    native = [(k, e) for k, e in days.items() if 'error' not in e and e.get('source') != 'earth-engine' and k in samples]
    if len(native) < EE_MIN_OVERLAP or any(abs(samples[k][f] - e[f]) > EE_TOLERANCE_KT for k, e in native for f in ('sust_kt', 'gust_kt')):
        raise ValueError('Earth Engine values do not match native GRIB on overlapping dates')
    cache['ee_verified_at'] = iso_z(now)
    filled = 0
    for key, value in samples.items():
        entry = days.get(key)
        if entry is None or 'error' in entry:
            days[key] = dict(value, source='earth-engine')
            filled += 1
    cache.update(start_local=start_local, updated_at=iso_z(now))
    atomic_write(path, json.dumps(cache, separators=(',', ':'), sort_keys=True) + '\n')
    return filled


def _earth_engine_item(init, lead, runner=_run_ee):
    """One run via Earth Engine in the worker's output shape, or an error item."""
    try:
        for row in runner({'creation_hour': init.hour, 'lead': lead, 'since': init.date().isoformat()}):
            if iso_z(datetime.fromtimestamp(row['created_ms'] / 1000, UTC)) == iso_z(init):
                return {'init': iso_z(init), 'lead': lead, 'sust_kt': math.hypot(row['u'], row['v']) * KNOTS,
                        'from_deg': math.degrees(math.atan2(-row['u'], -row['v'])) % 360, 'gust_kt': row['gust'] * KNOTS}
    except (subprocess.SubprocessError, ValueError, OSError, KeyError):
        pass
    return {'init': iso_z(init), 'lead': lead, 'error': 'unavailable'}


def rows(cache, since=None):
    """[[date, sustained, gust, crosswind]] for dates with native values."""
    out = []
    for key, e in sorted(cache['days'].items()):
        if 'error' in e or (since and key < since):
            continue
        out.append([key, round(e['sust_kt'], 1), round(e['gust_kt'], 1), round(_xw(e['gust_kt'], e['from_deg']), 1)])
    return out


def current(day, start_local, now, runner=_run, path=None, ee_runner=_run_ee):
    """Latest available 00/12Z run's forecast for the event's six-hourly time, or None.

    With ``path``, the result is kept per (event day, init) so each run is read once.
    """
    t0 = sample_time(day, start_local)
    cache = load(path) if path else None
    saved = (cache or {}).get('current') or {}
    init = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    init -= timedelta(hours=init.hour % 12)
    for _ in range(4):
        lead = int((t0 - init).total_seconds() // 3600)
        if init + AVAILABLE_AFTER <= now and 0 <= lead <= 354 and lead % 6 == 0:
            if saved.get('day') == day.isoformat() and saved.get('init') == iso_z(init) and saved.get('lead') == lead:
                return {k: saved[k] for k in ('init', 'lead', 'sust', 'gust', 'xw', 'from_deg')}
            try:
                [out] = runner([{'init': iso_z(init), 'lead': lead}])
                verified = (cache or {}).get('ee_verified_at')
                if 'error' in out and verified and now - parse_time(verified) <= timedelta(days=7):
                    # The AWS mirror throttles intermittently; Earth Engine matched GRIB this week.
                    out = _earth_engine_item(init, lead, ee_runner)
                if 'error' not in out:
                    result = {'init': iso_z(init), 'lead': lead, 'sust': out['sust_kt'], 'gust': out['gust_kt'],
                              'xw': round(_xw(out['gust_kt'], out['from_deg']), 1), 'from_deg': out['from_deg']}
                    if cache is not None:
                        fresh = load(path)  # the backfill may have written meanwhile
                        fresh['current'] = {'day': day.isoformat(), **result}
                        atomic_write(path, json.dumps(fresh, separators=(',', ':'), sort_keys=True) + '\n')
                    return result
            except (subprocess.SubprocessError, ValueError, OSError):
                pass
        init -= timedelta(hours=12)
    return None
