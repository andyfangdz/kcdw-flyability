"""Bounded saved forecast values, never observations or current guidance.

API: build_forecast_history(snapshot, runs_dir, now) and
validate_forecast_history(envelope, event_dict, now) return an envelope or None.
`hourly` has a per-source sparse UTC `time` axis and field dictionaries with
same-length `center`, `low`, `high` arrays. Units match the comparison charts
(mm, kt, %, hPa, Celsius; RH %). All valid times precede `cutoff`; `start` is
an actual saved non-null value's valid time. `provenance.state` is historical.
No readiness, quorum, narrative, source snapshot or archive is modified.
"""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from .common import UTC, iso_z, parse_time
from .event_ensemble import BOUNDS, MODELS, VARIABLES, validate_snapshot, validate_weather_next3
from .events import TZ, _event

MAX_BYTES = 400_000
MAX_ARCHIVES = 256
MAX_ARCHIVE_BYTES = 8_000_000
MAX_READ_BYTES = 256_000_000
MAX_ENTRIES = 4096
MAX_DAYS = 32
RH_FIELDS = ('relative_humidity_2m', 'relative_humidity_1000hPa',
             'relative_humidity_925hPa', 'relative_humidity_850hPa')
LABELS = {s.key: s.name for s in MODELS} | {
    'wn3': 'WeatherNext 3', 'wn2': 'WeatherNext 2', 'gfs': 'GFS operational',
    'ifs': 'ECMWF IFS', 'aifs_single': 'AIFS Single'}
STATISTICS = {key: 'median' for key in LABELS} | {
    'wn3': 'mean', 'wn2': 'mean', 'gfs': 'deterministic',
    'ifs': 'deterministic', 'aifs_single': 'deterministic'}
RH_ALIASES = {'gefs', 'ecmwf_ens', 'aifs_ens', 'gfs', 'ifs', 'aifs_single'}


def _require(condition):
    if not condition:
        raise ValueError('invalid forecast history')


def _stamp(value):
    _require(isinstance(value, str) and value.endswith('Z'))
    moment = parse_time(value)
    _require(iso_z(moment) == value)
    return moment


def _hour(value):
    moment = _stamp(value)
    _require(moment.minute == moment.second == moment.microsecond == 0)
    return moment


def _aware(now):
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    return now.astimezone(UTC)


def _size(value):
    return len(json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _point(key, field, center, low, high):
    values = (center, low, high)
    _require(all(v is None or (type(v) in (int, float) and math.isfinite(v)) for v in values))
    if center is None:
        _require(low is None and high is None)
        return
    lower, upper = (0, float('inf')) if field in RH_FIELDS else BOUNDS[field]
    if key == 'wn3' and field in {'precipitation', 'wind_speed_10m'}:
        upper = 1500 if field == 'precipitation' else 160*3600/1852
    _require(lower <= center <= upper)
    if STATISTICS[key] == 'deterministic':
        _require(low is None and high is None)
    else:
        if low is None or high is None:
            raise ValueError('missing forecast range')
        _require(low <= high)
        if key != 'wn2':
            _require(lower <= low <= upper and lower <= high <= upper)
        else:
            spread_max = 150 if field == 'pressure_msl' else upper
            _require(abs((center-low)-(high-center)) < 0.001)
            _require(0 <= center-low <= spread_max)
        if STATISTICS[key] == 'median':
            _require(low <= center <= high)


def validate_forecast_history(envelope, event_dict, now):
    """Fail closed on persisted history; only the envelope has a 12-hour TTL."""
    try:
        now = _aware(now)
        event = _event(event_dict)
        _require(isinstance(envelope, dict) and _size(envelope) <= MAX_BYTES)
        _require(set(envelope) == {'version', 'event', 'collected_at', 'cutoff', 'start', 'sources', 'provenance'})
        _require(type(envelope['version']) is int and envelope['version'] == 1 and envelope['event'] == event_dict)
        collected = _stamp(envelope['collected_at'])
        _require(timedelta(0) <= now-collected <= timedelta(hours=12))
        cutoff, start = _hour(envelope['cutoff']), _hour(envelope['start'])
        event_start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
        _require(cutoff < event_start and cutoff-timedelta(days=MAX_DAYS) <= start < cutoff)
        sources = envelope['sources']
        _require(isinstance(sources, dict) and 0 < len(sources) <= len(LABELS))
        first = []
        for key, source in sources.items():
            _require(key in LABELS and set(source) == {'label', 'statistic', 'hourly'})
            _require(source['label'] == LABELS[key] and source['statistic'] == STATISTICS[key])
            hourly = source['hourly']
            _require(isinstance(hourly, dict))
            times = hourly['time']
            _require(isinstance(times, list) and 0 < len(times) <= MAX_DAYS*24)
            axis = [_hour(t) for t in times]
            _require(all(start <= t < cutoff for t in axis) and all(a < b for a, b in zip(axis, axis[1:])))
            allowed: set[str] = set(VARIABLES) if key not in {'ifs', 'aifs_single'} else set()
            if key in RH_ALIASES:
                allowed.update(RH_FIELDS)
            if key == 'wn3':
                allowed -= {'cloud_cover_low', 'wind_gusts_10m'}
            fields = set(hourly)-{'time'}
            _require(bool(fields) and fields <= allowed)
            has_value = [False]*len(axis)
            for field in fields:
                fan = hourly[field]
                _require(isinstance(fan, dict) and set(fan) == {'center', 'low', 'high'})
                _require(all(isinstance(v, list) and len(v) == len(axis) for v in fan.values()))
                _require(any(v is not None for v in fan['center']))
                for i, values in enumerate(zip(fan['center'], fan['low'], fan['high'])):
                    _point(key, field, *values)
                    has_value[i] |= values[0] is not None
            _require(all(has_value))
            first.append(axis[0])
        _require(min(first) == start)
        provenance = envelope['provenance']
        _require(isinstance(provenance, dict) and set(provenance) == {
            'state', 'selection', 'archives_read', 'oldest_collection', 'newest_collection', 'truncated'})
        _require(provenance['state'] == 'historical' and provenance['selection'] == 'latest saved collection per source/field/time')
        _require(type(provenance['archives_read']) is int and 1 <= provenance['archives_read'] <= MAX_ARCHIVES)
        _require(type(provenance['truncated']) is bool)
        oldest, newest = (_stamp(provenance[k]) for k in ('oldest_collection', 'newest_collection'))
        _require(oldest <= newest <= collected)
        return copy.deepcopy(envelope)
    except Exception:
        return None


def _validated_sources(archive, collected):
    """Validate each provider independently, using the archive's own clock."""
    for spec in MODELS:
        try:
            source = archive.get('models', {}).get(spec.key, {})
            if source.get('ok') is not True:
                continue
            isolated = dict(archive, models={s.key: {'ok': False} for s in MODELS}, weathernext2={'ok': False})
            isolated['models'][spec.key] = source
            validate_snapshot(isolated)
            yield spec.key, source['data']['hourly'], 'fan'
        except Exception:
            continue
    try:
        source = archive.get('weathernext2', {})
        if source.get('ok') is True:
            isolated = dict(archive, models={s.key: {'ok': False} for s in MODELS})
            validate_snapshot(isolated)
            yield 'wn2', source['data']['hourly'], 'sd'
    except Exception:
        pass
    try:
        source = archive.get('weathernext3', {})
        if source.get('ok') is True:
            data = source['data']
            validate_weather_next3(data, collected)
            if data['status']['available']:
                forecast = data['forecast']
                hourly = {'time': forecast['valid_time_utc']}
                for field, original, factor in (
                    ('precipitation', 'precipitation_1h', 1), ('wind_speed_10m', 'wind_speed_10m', 3600/1852),
                    ('pressure_msl', 'sea_level_pressure', .01), ('temperature_2m', 'temperature_2m', 1)):
                    fan = forecast['fields'].get(original)
                    if fan:
                        hourly[field] = {k: [v*factor if v is not None else None for v in fan[k]] for k in ('mean', 'p10', 'p90')}
                yield 'wn3', hourly, 'mean'
    except Exception:
        pass
    try:
        from .gfs_guidance import validate_gfs
        if validate_gfs(archive.get('gfs'), collected)['available']:
            yield 'gfs', archive['gfs']['data']['hourly'], 'deterministic'
    except Exception:
        pass
    for name, ensemble in (('event_moisture', False), ('event_moisture_ensemble', True)):
        try:
            from .event_moisture import validate_moisture
            from .event_moisture_ensemble import validate_moisture_ensemble
            validator = validate_moisture_ensemble if ensemble else validate_moisture
            result = validator(archive.get(name), collected)
            for key, source in result['models'].items():
                if source.get('available') and key in RH_ALIASES:
                    hourly = source['data']['hourly']
                    yield key, {k: v for k, v in hourly.items() if k == 'time' or k in RH_FIELDS}, 'fan' if ensemble else 'deterministic'
        except Exception:
            continue


def _archives(root):
    """Read oldest/date anchors before newest files within explicit budgets.

    Standard run IDs preserve archive order after copying/restoring files. Other
    names use mtime only as a selection hint; JSON collection times still decide
    value precedence. Any enumeration/file/read cap is reported as sampled.
    """
    paths, truncated = [], False
    with os.scandir(root) as entries:
        for i, entry in enumerate(entries):
            if i >= MAX_ENTRIES:
                truncated = True
                break
            if entry.is_dir(follow_symlinks=False):
                path = Path(entry.path)/'snapshot.json'
                try:
                    if path.is_file() and not path.is_symlink():
                        try:
                            at = datetime.strptime(entry.name.split('.')[0], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).timestamp()
                        except ValueError:
                            at = path.stat().st_mtime
                        paths.append((at, str(path), path))
                except OSError:
                    continue
    ordered = sorted(paths)
    if len(ordered) <= MAX_ARCHIVES:
        return [p for _, _, p in ordered], truncated
    selected = [p for _, _, p in ordered[:max(1, MAX_ARCHIVES//4)]]
    seen = set(selected)
    days = set()
    for at, _, path in ordered:
        day = datetime.fromtimestamp(at, UTC).date()
        if day not in days:
            days.add(day)
            if path not in seen and len(selected) < MAX_ARCHIVES:
                selected.append(path)
                seen.add(path)
    for _, _, path in reversed(ordered):
        if len(selected) == MAX_ARCHIVES:
            break
        if path not in seen:
            selected.append(path)
            seen.add(path)
    return selected, True


def build_forecast_history(snapshot, runs_dir, now):
    """Read local run snapshots only; latest non-null saved value wins.

    Nulls do not erase earlier usable values. Missing fields never borrow values
    from another model. Same-collection ties use deterministic archive path order.
    Budget truncation removes newest historical rows, preserving earliest history.
    """
    try:
        now = _aware(now)
        collected = _stamp(snapshot['collected_at'])
        _require(timedelta(0) <= now-collected <= timedelta(hours=12))
        _event(snapshot['event'])
        cutoff = _hour(snapshot['range']['start'])
        floor = cutoff-timedelta(days=MAX_DAYS)
        paths, truncated = _archives(runs_dir)
    except Exception:
        return None
    points, collections = {}, []
    read_bytes = read_count = 0
    for path in paths:
        try:
            size = path.stat().st_size
            if size > MAX_ARCHIVE_BYTES:
                continue
            if read_bytes+size > MAX_READ_BYTES:
                truncated = True
                break
            with path.open('rb') as stream:
                raw = stream.read(MAX_ARCHIVE_BYTES+1)
            read_bytes += len(raw)
            read_count += 1
            _require(len(raw) <= MAX_ARCHIVE_BYTES)
            archive = json.loads(raw)
            archive_event = _event(archive['event']).as_dict()
            # Editorial descriptions do not change the mission. Validate each
            # provider against its own original snapshot; retain all other
            # event identity checks and bind the output to the current event.
            _require(all(archive_event[k] == v for k, v in snapshot['event'].items()
                         if k != 'description'))
            at = _stamp(archive['collected_at'])
            _require(at <= collected)
            used = False
            for key, hourly, kind in _validated_sources(archive, at):
                try:
                    # Provider validators accept ISO fractional seconds; emit
                    # canonical ISOZ below, rather than rejecting valid WN3 axes.
                    axis = [parse_time(t) for t in hourly['time']]
                    for field in (*VARIABLES, *RH_FIELDS):
                        if field not in hourly:
                            continue
                        try:
                            raw_field = hourly[field]
                            if kind in ('fan', 'mean'):
                                arrays = [raw_field[k] for k in ('p50' if kind == 'fan' else 'mean', 'p10', 'p90')]
                            elif kind == 'sd':
                                spread = hourly[field+'_spread']
                                arrays = [raw_field, [a-b for a, b in zip(raw_field, spread)], [a+b for a, b in zip(raw_field, spread)]]
                            else:
                                arrays = [raw_field, [None]*len(axis), [None]*len(axis)]
                            _require(all(len(v) == len(axis) for v in arrays))
                            for i, t in enumerate(axis):
                                if not floor <= t < cutoff:
                                    continue
                                values = tuple(a[i] for a in arrays)
                                _point(key, field, *values)
                                if values[0] is None:
                                    continue
                                identity = (key, field, iso_z(t))
                                rank = (at, str(path))
                                if identity not in points or rank > points[identity][0]:
                                    points[identity] = (rank, values)
                                used = True
                        except Exception:
                            continue
                except Exception:
                    continue
            if used:
                collections.append(at)
        except Exception:
            continue
    if not points:
        return None
    sources = {}
    for key in LABELS:
        times = sorted({t for k, _, t in points if k == key})
        if not times:
            continue
        hourly: dict = {'time': times}
        for field in (*VARIABLES, *RH_FIELDS):
            if not any((key, field, t) in points for t in times):
                continue
            hourly[field] = {stat: [points[(key, field, t)][1][i] if (key, field, t) in points else None for t in times]
                             for i, stat in enumerate(('center', 'low', 'high'))}
        sources[key] = {'label': LABELS[key], 'statistic': STATISTICS[key], 'hourly': hourly}
    envelope = {'version': 1, 'event': copy.deepcopy(snapshot['event']), 'collected_at': snapshot['collected_at'],
                'cutoff': iso_z(cutoff), 'start': min(s['hourly']['time'][0] for s in sources.values()),
                'sources': sources, 'provenance': {'state': 'historical',
                    'selection': 'latest saved collection per source/field/time', 'archives_read': read_count,
                    'oldest_collection': iso_z(min(collections)), 'newest_collection': iso_z(max(collections)),
                    'truncated': truncated}}
    while _size(envelope) > MAX_BYTES and sources:
        key = max(sources, key=lambda k: sources[k]['hourly']['time'][-1])
        hourly = sources[key]['hourly']
        hourly['time'].pop()
        for field in list(hourly):
            if field != 'time':
                for values in hourly[field].values():
                    values.pop()
                if not any(v is not None for v in hourly[field]['center']):
                    del hourly[field]
        if not hourly['time']:
            del sources[key]
        envelope['provenance']['truncated'] = True
    return validate_forecast_history(envelope, snapshot['event'], now)
