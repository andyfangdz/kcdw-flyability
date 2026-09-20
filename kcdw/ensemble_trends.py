"""Bounded, fixed-valid-time guidance history, not model-cycle probabilities.

Collection timestamps are fetches. Equal consecutive selected payloads collapse
onto the latest fetch; advertised metadata changes do not create model cycles.
WN3 and validated direct-native sources have bound runs. No trend payload is reused.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .common import UTC, iso_z
from .events import TZ, SLUG, WINDOW
from .event_ensemble import MODELS, UNITS, LAT, LON, validate_snapshot
from .gfs_guidance import validate_gfs
from .weathernext3 import validate_weather_next3

MAX_FILES = 64
MAX_BYTES = 8 * 1024 * 1024
MAX_POINTS = 8
LOOKBACK = timedelta(hours=72)
ADVERTISED = 'latest-advertised; not response-bound'
BOUND = 'response-bound'
MIXED = 'mixed response-bound and latest-advertised'
SOURCE_CHANGE = 'Source/grid/sampling changes can affect snapshot differences; these are not pure initialization trends. Incompatible grids are omitted.'


def _direct_provenance(data):
    """Project native metadata ONLY after the caller's source validator succeeds."""
    meta = data.get('metadata', {})
    if meta.get('provenance') != 'direct-native':
        return None
    run = meta.get('initialization_time')
    if (meta.get('model_init_is_response_bound') is not True
            or meta.get('source_provider') not in ('NOAA', 'ECMWF', 'ECCC')
            or not isinstance(meta.get('sampling'), str) or not meta['sampling'].strip()
            or iso_z(_time(run)) != run):
        raise ValueError('invalid direct provenance')
    return {k: meta[k] for k in ('provenance', 'model_init_is_response_bound',
                               'initialization_time', 'source_provider', 'sampling')}
LABELS = {s.key: s.name for s in MODELS} | {'wn3': 'WeatherNext 3', 'gfs': 'GFS operational'}
AIRPORT = {'icao': 'KCDW', 'latitude': LAT, 'longitude': LON, 'timezone': 'America/New_York'}


def _time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('UTC timestamp required')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _event(event):
    identity = {k: event[k] for k in ('slug', 'date', 'window')}
    if not SLUG.fullmatch(identity['slug']) or not WINDOW.fullmatch(identity['window']):
        raise ValueError('event identity invalid')
    start, end = map(int, identity['window'].split('-'))
    if not 0 <= start < end <= 24:
        raise ValueError('event window invalid')
    day = datetime.fromisoformat(identity['date'])
    if day.date().isoformat() != identity['date']:
        raise ValueError('event date invalid')
    opening = day.replace(tzinfo=TZ) + timedelta(hours=start)
    sample = opening + timedelta(hours=(end-start)//2)
    rain_times = [opening + timedelta(hours=i) for i in range(1, end-start+1)]
    return identity, sample.astimezone(UTC), [t.astimezone(UTC) for t in rain_times]


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _metric(center, low=None, high=None):
    if center is None:
        return None
    if not _finite(center) or any(v is not None and not _finite(v) for v in (low, high)):
        raise ValueError('nonfinite metric')
    return {'center': center, 'low': low, 'high': high}


def _fresh(stamp, now, hours):
    if not timedelta(0) <= now-_time(stamp) <= timedelta(hours=hours):
        raise ValueError('stale or future source')


def _source(snapshot, key):
    if key in {s.key for s in MODELS}:
        models = snapshot.get('models')
        source = models.get(key) if isinstance(models, dict) else None
    else:
        source = snapshot.get('weathernext3' if key == 'wn3' else key)
    return source if isinstance(source, dict) else {}


def _grid(source, key):
    d = source['data']
    if key == 'wn3':
        f = d['forecast']
        return (f['latitude'], f['longitude'])
    g = d['grid_point']
    return (g['latitude'], g['longitude'])


def _point(snapshot, key, check_at, sample, rain_times):
    source = _source(snapshot, key)
    if source.get('ok') is not True:
        raise ValueError('source unavailable')
    d = source['data']
    if d.get('explicit_last_good') or source.get('explicit_last_good'):
        raise ValueError('last good is not current source')
    run = None
    direct = None
    binding = ADVERTISED
    if key in {s.key for s in MODELS}:
        # Existing normalized contract, isolated so another source cannot veto it.
        isolated = {k: snapshot[k] for k in ('event', 'range', 'collected_at', 'collection_started_at', 'direct_native_version') if k in snapshot}
        isolated['models'] = {s.key: source if s.key == key else {'ok': False} for s in MODELS}
        validate_snapshot(isolated)
        if d['hourly_units'] != dict(UNITS, time='iso8601 UTC'):
            raise ValueError('units mismatch')
        _fresh(d['fetched_at'], check_at, 12)
        meta = d.get('metadata', {})
        direct = _direct_provenance(d)
        if direct:
            run, binding = direct['initialization_time'], BOUND
            _fresh(run, check_at, 24)
            if _time(run) > _time(d['fetched_at']):
                raise ValueError('metadata chronology')
        elif meta.get('ok'):
            _fresh(meta['initialization_time'], check_at, 24)
            if not _time(meta['initialization_time']) <= _time(meta['availability_time']) <= _time(d['fetched_at']) + timedelta(minutes=5):
                raise ValueError('metadata chronology')
            run = iso_z(_time(meta['initialization_time']))
        h = d['hourly']; i = h['time'].index(iso_z(sample))
        metrics = {name: _metric(h[field]['p50'][i], h[field]['p10'][i], h[field]['p90'][i])
                   for name, field in (('pressure', 'pressure_msl'), ('wind', 'wind_speed_10m'))}
        rain = d['window']['rain_total_mm']
        if rain is not None and any(not 0 <= rain[k] <= 500*len(rain_times) for k in ('min','p10','median','p90','max')):
            raise ValueError('rain total bounds')
        metrics['rain'] = None if rain is None else _metric(rain['median'], rain['p10'], rain['p90'])
    elif key == 'wn3':
        validate_weather_next3(d, check_at)
        f = d['forecast']; times = [_time(t) for t in f['valid_time_utc']]
        i = times.index(sample); fields = f['fields']
        metrics = {}
        for name, field, scale in (('pressure', 'sea_level_pressure', .01), ('wind', 'wind_speed_10m', 3600/1852)):
            values = fields[field]
            metrics[name] = _metric(values['mean'][i]*scale, values['p10'][i]*scale, values['p90'][i]*scale)
        metrics['rain'] = _metric(sum(fields['precipitation_1h']['mean'][times.index(t)] for t in rain_times))
        run, binding = iso_z(_time(f['response_init_utc'])), BOUND
    else:
        if not validate_gfs(source, check_at)['available']:
            raise ValueError('GFS unavailable')
        h = d['hourly']; times = [_time(t) for t in h['time']]; i = times.index(sample)
        metrics = {name: _metric(h[field][i]) for name, field in (('pressure', 'pressure_msl'), ('wind', 'wind_speed_10m'))}
        rain = [h['precipitation'][times.index(t)] for t in rain_times]
        metrics['rain'] = _metric(sum(rain)) if all(v is not None for v in rain) else None
        direct = _direct_provenance(d)
        if direct:
            run, binding = direct['initialization_time'], BOUND
            _fresh(run, check_at, 24)
            if _time(run) > _time(d['fetched_at']):
                raise ValueError('metadata chronology')
        else:
            runs = {v['latest_advertised_init'] for v in d['metadata']['datasets'].values()}
            run = next(iter(runs)) if len(runs) == 1 else None
    if not any(v is not None for v in metrics.values()):
        raise ValueError('no selected metrics')
    return {'collected_at': iso_z(_time(snapshot['collected_at'])), 'run_time': run,
            'run_binding': binding, 'metrics': metrics, 'is_current': False,
            **({'source_provenance': direct} if direct else {})}


def _archives(root):
    """Open at most 64 recent regular snapshots; never follow symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root_fd = os.open(root, flags | os.O_DIRECTORY)
    except OSError:
        return
    try:
        candidates = []
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
        for _, name in heapq.nlargest(MAX_FILES, candidates):
            try:
                directory_fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=root_fd)
                try:
                    fd = os.open('snapshot.json', flags, dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
                with os.fdopen(fd, 'rb') as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                        continue
                    data = file.read(MAX_BYTES+1)
                if len(data) <= MAX_BYTES:
                    decoded = json.loads(data)
                    if isinstance(decoded, dict):
                        yield decoded
            except (OSError, ValueError, TypeError):
                continue
    finally:
        os.close(root_fd)


def build_trends(current: dict, archives: Path, now: datetime) -> dict:
    """Return independently validated, comparable, capped model histories.

    Historical freshness is evaluated at collection; current freshness is checked
    against ``now``. No failed latest source is replaced with archived last-good.
    """
    if now.tzinfo is None:
        raise ValueError('aware now required')
    identity, sample, rain_times = _event(current['event'])
    collected = _time(current['collected_at'])
    result = {'version': 1, 'as_of': iso_z(collected), 'event': identity, 'sample_time': iso_z(sample), 'models': {},
              'notes': ['Collection timestamps are fetches, not verified model cycles; unchanged selected values collapse to the latest fetch.',
                        'Pressure: hPa and wind: kt at the same fixed event instant. Rain: mm over the full local event window, opening excluded and closing included.',
                        'Conventional centers are medians with p10–p90. WN3 centers are means; rain sums means only, without a summed quantile band. GFS is deterministic, without a band.',
                        'Up to 72 hours, 64 recent snapshots and 8 distinct retained records per model. Missing values are unknown, not zero; no confidence or flight-probability conversion.']}
    history = []
    for snapshot in _archives(Path(archives)):
        try:
            stamp = _time(snapshot['collected_at'])
            if now-LOOKBACK <= stamp < collected and stamp <= now and _event(snapshot['event'])[0] == identity and snapshot['airport'] == AIRPORT:
                history.append((stamp, snapshot))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    # Conflicting archive entries at one fetch time cannot be ordered into a
    # trend. Omit that timestamp rather than arbitrarily choosing a forecast.
    from collections import Counter
    counts = Counter(stamp for stamp, _ in history)
    if any(count > 1 for count in counts.values()):
        result['notes'].append('Duplicate archive collection timestamps were omitted; no arbitrary forecast order was inferred.')
    history = [(stamp, snapshot) for stamp, snapshot in history if counts[stamp] == 1]
    history.sort(key=lambda item: item[0])
    paired_sources = {}
    for key, label in LABELS.items():
        model = {'label': label, 'statistic': 'mean' if key == 'wn3' else 'deterministic' if key == 'gfs' else 'median',
                 'provenance': BOUND if key == 'wn3' else ADVERTISED, 'current_available': False, 'points': []}
        result['models'][key] = model
        points = model['points']; grid = None
        try:
            grid = _grid(_source(current, key), key)
        except (KeyError, TypeError):
            pass
        # If latest source has no grid, anchor history to its latest valid grid.
        valid = []
        for stamp, snapshot in history + [(collected, current)]:
            is_current = snapshot is current
            try:
                if snapshot['airport'] != AIRPORT or stamp > now or stamp < now-LOOKBACK:
                    continue
                point = _point(snapshot, key, now if is_current else stamp, sample, rain_times)
                point['is_current'] = is_current
                valid.append((point, _grid(_source(snapshot, key), key), id(snapshot)))
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
                continue
        if grid is None and valid:
            grid = valid[-1][1]
        for point, point_grid, snapshot_id in valid:
            if point_grid != grid:
                continue
            paired_sources.setdefault(snapshot_id, {})[key] = point
            model['current_available'] |= point['is_current']
            equal = bool(points) and points[-1]['metrics'] == point['metrics']
            equal = equal and (points[-1].get('source_provenance') == point.get('source_provenance'))
            if key == 'wn3' or point['run_binding'] == BOUND:
                equal = equal and points[-1]['run_time'] == point['run_time']
            if equal:
                points[-1] = point
            else:
                points.append(point)
            del points[:-MAX_POINTS]
        bindings = {p['run_binding'] for p in points}
        if bindings:
            model['provenance'] = next(iter(bindings)) if len(bindings) == 1 else MIXED
        if any('source_provenance' in p for p in points):
            result['version'] = 2
    comparisons = {key: [] for key in LABELS if key != 'gfs'}
    for _, snapshot in history + [(collected, current)]:
        records = paired_sources.get(id(snapshot), {})
        gfs = records.get('gfs', {}).get('metrics', {}).get('pressure')
        if gfs is None:
            continue
        for key, pairs in comparisons.items():
            record = records.get(key)
            if not record or record['metrics']['pressure'] is None:
                continue
            pressure = record['metrics']['pressure']
            if pressure['low'] is None or pressure['high'] is None:
                continue
            pair = {'collected_at': record['collected_at'], 'gfs': gfs['center'],
                    **pressure, 'is_current': record['is_current']}
            fields = ('gfs', 'center', 'low', 'high')
            if pairs and all(pairs[-1][f] == pair[f] for f in fields):
                pairs[-1] = pair
            else:
                pairs.append(pair)
            del pairs[:-MAX_POINTS]
    result['pressure_comparisons'] = comparisons
    if result['version'] == 2:
        result['notes'].append(SOURCE_CHANGE)
    result['notes'].append('Pressure gaps pair GFS and ensemble values from the same collection snapshot and valid instant; underlying model cycles can still be asynchronous.')
    return result


def validate_trends(trends: dict, event_dict: dict) -> bool:
    """Fail-closed standalone persisted trend contract for renderers."""
    try:
        identity, sample, rain_times = _event(event_dict)
        if set(trends)-{'pressure_comparisons'} != {'version','as_of','event','sample_time','models','notes'} or type(trends['version']) is not int or trends['version'] not in (1, 2):
            return False
        as_of = _time(trends['as_of'])
        if iso_z(as_of) != trends['as_of'] or trends['event'] != identity or trends['sample_time'] != iso_z(sample) or set(trends['models']) != set(LABELS):
            return False
        if not isinstance(trends['notes'], list) or len(trends['notes']) > 12 or any(not isinstance(n, str) or len(n) > 1000 for n in trends['notes']):
            return False
        if trends['version'] == 2 and SOURCE_CHANGE not in trends['notes']:
            return False
        for key, model in trends['models'].items():
            if set(model) != {'label','statistic','provenance','current_available','points'} or model['label'] != LABELS[key]:
                return False
            allowed = {BOUND if key == 'wn3' else ADVERTISED} if trends['version'] == 1 else {BOUND, ADVERTISED, MIXED}
            if model['statistic'] != ('mean' if key == 'wn3' else 'deterministic' if key == 'gfs' else 'median') or model['provenance'] not in allowed or type(model['current_available']) is not bool:
                return False
            points = model['points']
            if not isinstance(points, list) or len(points) > MAX_POINTS:
                return False
            bindings = {p['run_binding'] for p in points}
            if points and model['provenance'] != (next(iter(bindings)) if len(bindings) == 1 else MIXED):
                return False
            previous = None; current_count = 0
            for i, point in enumerate(points):
                fields = {'collected_at','run_time','run_binding','metrics','is_current'}
                if trends['version'] == 2 and 'source_provenance' in point:
                    fields.add('source_provenance')
                    direct = _direct_provenance({'metadata': point['source_provenance']})
                    if not direct or direct != point['source_provenance'] or point['run_time'] != direct['initialization_time'] or point['run_binding'] != BOUND:
                        return False
                elif key != 'wn3' and point['run_binding'] != ADVERTISED:
                    return False
                if key == 'wn3' and point['run_binding'] != BOUND:
                    return False
                if set(point) != fields or type(point['is_current']) is not bool:
                    return False
                stamp = _time(point['collected_at'])
                if iso_z(stamp) != point['collected_at'] or not as_of-LOOKBACK <= stamp <= as_of or (previous is not None and stamp <= previous):
                    return False
                previous = stamp
                if (model['provenance'] != MIXED and point['run_binding'] != model['provenance']) or (point['run_time'] is None and point['run_binding'] == BOUND):
                    return False
                if point['run_time'] is not None and _time(point['run_time']) > stamp:
                    return False
                if point['is_current']:
                    current_count += 1
                    if i != len(points)-1 or stamp != as_of:
                        return False
                if set(point['metrics']) != {'pressure','rain','wind'}:
                    return False
                for metric, value in point['metrics'].items():
                    if value is None:
                        continue
                    if set(value) != {'center','low','high'} or not _finite(value['center']):
                        return False
                    low, high = {'pressure': (750,1150), 'wind': (0, 320), 'rain': (0,1500*len(rain_times))}[metric]
                    if any(v is not None and (not _finite(v) or not low <= v <= high) for v in value.values()):
                        return False
                    if (value['low'] is None) != (value['high'] is None):
                        return False
                    if value['low'] is not None and value['low'] > value['high']:
                        return False
                    if (key == 'gfs' or key == 'wn3' and metric == 'rain') and value['low'] is not None:
                        return False
                    if key not in ('gfs','wn3') and value['low'] is not None and not value['low'] <= value['center'] <= value['high']:
                        return False
            if bool(current_count) != model['current_available'] or current_count > 1:
                return False
        if 'pressure_comparisons' in trends:
            comparisons = trends['pressure_comparisons']
            if not isinstance(comparisons, dict) or set(comparisons) != set(LABELS)-{'gfs'}:
                return False
            for key, pairs in comparisons.items():
                if not isinstance(pairs, list) or len(pairs) > MAX_POINTS:
                    return False
                previous = None
                for i, pair in enumerate(pairs):
                    if set(pair) != {'collected_at','gfs','center','low','high','is_current'} or type(pair['is_current']) is not bool:
                        return False
                    stamp = _time(pair['collected_at'])
                    if iso_z(stamp) != pair['collected_at'] or not as_of-LOOKBACK <= stamp <= as_of or (previous is not None and stamp <= previous):
                        return False
                    previous = stamp
                    if any(not _finite(pair[f]) or not 750 <= pair[f] <= 1150 for f in ('gfs','center','low','high')) or pair['low'] > pair['high']:
                        return False
                    if key != 'wn3' and not pair['low'] <= pair['center'] <= pair['high']:
                        return False
                    if pair['is_current'] and (i != len(pairs)-1 or stamp != as_of or not trends['models'][key]['current_available'] or not trends['models']['gfs']['current_available']):
                        return False
        json.dumps(trends, allow_nan=False)
        return True
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return False
