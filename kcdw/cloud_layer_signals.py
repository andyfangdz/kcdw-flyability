"""Bounded supplemental cloud-layer screens; never ceilings or flight odds.

Six independent explicit-model requests, with all fields for each model in the
same fetch. No joins to previous snapshots and no claimed response-bound run.
Only opening/noon/closing-minus-one-hour scalar points and member counts survive.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import re
from urllib.parse import urlencode

from .common import UTC, iso_z
from .events import TZ, _event
from .event_moisture import MODELS as PROFILE_MODELS, ENDPOINT as PROFILE_ENDPOINT
from .event_moisture_ensemble import MODELS as ENSEMBLE_MODELS, _number, _utc, _timestamp
from .event_ensemble import ENDPOINT as ENSEMBLE_ENDPOINT, LAT, LON

LEVELS = (1000, 925, 850)
SURFACE = ('temperature_2m', 'dew_point_2m', 'relative_humidity_2m',
           'wind_direction_10m', 'wind_speed_10m', 'surface_pressure')
PROFILE_FIELDS = SURFACE + tuple(f'{field}_{level}hPa' for level in LEVELS
                                for field in ('temperature', 'relative_humidity', 'geopotential_height'))
ENSEMBLE_FIELDS = ('cloud_cover_low', 'relative_humidity_925hPa', 'relative_humidity_2m', 'surface_pressure')
FIELD_UNITS = {field: ('%' if 'humidity' in field or field == 'cloud_cover_low' else
                      'hPa' if field == 'surface_pressure' else 'm' if 'geopotential' in field else
                      '°' if field == 'wind_direction_10m' else 'kn' if field == 'wind_speed_10m' else '°C')
               for field in set(PROFILE_FIELDS + ENSEMBLE_FIELDS)}
SCREENS = ('low_cloud', 'rh925', 'joint_low_cloud_rh925', 'joint_surface_rh925')
THRESHOLDS = {'low_cloud_pct': 75, 'rh_pct': 90, 'surface_pressure_gt_hpa': 925}
NOTES = ('Descriptive model-member screens, not calibrated probabilities, cloud bases, ceilings or flight categories. '
         'Denominators are actual members with every required field; null count means no eligible members. '
         'All-three uses the same members at three sample times, not continuous persistence. '
         'Hourly output may interpolate coarser native steps. Heights are geopotential metres MSL, not AGL. '
         'Missing values remain unknown. Pressure levels at or below model ground are masked.')
BINDING = ('All fields are from one explicit-model fetch, not an immutable response-bound initialization; '
           'no previous snapshot fields are joined. Latest short IFS cycles need not cover the rolling horizon.')
ERROR = 'Layer source unavailable or invalid'


def _require(value):
    if not value:
        raise ValueError('invalid layer signals')


def _shape(value, keys):
    _require(isinstance(value, dict) and set(value) == set(keys))


def _context(snapshot, now):
    now = _utc(now)
    event = _event(snapshot['event'])
    stamp = _timestamp(snapshot['collected_at'])
    _require(timedelta(0) <= now - stamp <= timedelta(hours=12))
    display = snapshot['range']
    _shape(display, ('start', 'end'))
    start, end = (_timestamp(display[k]) for k in ('start', 'end'))
    _require(timedelta(0) < end-start <= timedelta(days=32))
    midnight = datetime.combine(event.day, datetime.min.time(), TZ)
    opening = midnight + timedelta(hours=event.start_hour)
    closing = midnight + timedelta(hours=event.end_hour)
    # For non-noon windows use the middle hour, keeping each point in-window.
    middle = midnight + timedelta(hours=12)
    if not opening <= middle < closing:
        middle = opening + timedelta(hours=(event.end_hour-event.start_hour-1)//2)
    times = [t.astimezone(UTC) for t in (opening, middle, closing-timedelta(hours=1))]
    _require(all(start <= t < end for t in times) and times == sorted(times))
    _require(now.replace(hour=0, minute=0, second=0, microsecond=0) <= times[0])
    return now, times


def _bounds(field):
    return ((0, 100) if FIELD_UNITS[field] == '%' else (300, 1100) if field == 'surface_pressure' else
            (-1000, 10000) if 'geopotential' in field else (0, 360) if field == 'wind_direction_10m' else
            (0, 300) if field == 'wind_speed_10m' else (-120, 65))


def _value(field, value):
    return _number(value, *_bounds(field), nullable=True)


def _metadata(resolution):
    return {'same_fetch': True, 'model_init_is_response_bound': False, 'initialization_time': None,
            'native_timestep_hours': resolution, 'sampling': 'hourly; may be native-step interpolated',
            'binding_note': BINDING}


def _failed():
    return {'ok': False, 'data': None, 'error': ERROR}


def _raw(client, endpoint, model_id, fields, times, members=1):
    params = {'latitude': LAT, 'longitude': LON, 'models': model_id, 'hourly': ','.join(fields),
              'timezone': 'GMT', 'timeformat': 'unixtime', 'temperature_unit': 'celsius',
              'wind_speed_unit': 'kn', 'start_hour': times[0].strftime('%Y-%m-%dT%H:%M'),
              'end_hour': times[-1].strftime('%Y-%m-%dT%H:%M')}
    # Repository collector.Client exposes get(url), not get_json.
    raw = client.get(endpoint + '?' + urlencode(params))
    _require(isinstance(raw, dict) and raw.get('timezone') == 'GMT' and
             type(raw.get('utc_offset_seconds')) is int and raw['utc_offset_seconds'] == 0)
    for field in ('model', 'model_id', 'models'):
        _require(field not in raw or raw[field] == model_id)
    grid = {k: raw[k] for k in ('latitude', 'longitude')}
    _grid(grid)
    hourly, units = raw['hourly'], raw['hourly_units']
    _require(isinstance(hourly, dict) and isinstance(units, dict) and units.get('time') == 'unixtime')
    axis = [int((times[0] + timedelta(hours=i)).timestamp())
            for i in range(int((times[-1]-times[0]).total_seconds()/3600)+1)]
    _require(isinstance(hourly.get('time'), list) and hourly['time'] == axis)
    for t in hourly['time']:
        _number(t, 0, 1e11)
    indices = [axis.index(int(t.timestamp())) for t in times]
    data = {field: {} for field in fields}
    for key, values in hourly.items():
        if key == 'time':
            continue
        match = re.fullmatch(r'(.+?)(?:_member(\d{2}))?', key)
        if match is None:
            raise ValueError('invalid member field')
        field, member = match.group(1), match.group(2) or '00'
        _require(field in data and int(member) < members and member not in data[field])
        _require(isinstance(values, list) and len(values) == len(axis))
        _require(units.get(key) == FIELD_UNITS[field] or
                 (units.get(key) in (None, 'undefined') and all(v is None for v in values)))
        for value in values:
            _value(field, value)
        data[field][member] = [values[i] for i in indices]
    _require(any(data.values()))
    return data, grid


def _grid(grid):
    _shape(grid, ('latitude', 'longitude'))
    for key, target in (('latitude', LAT), ('longitude', LON)):
        _require(abs(_number(grid[key], -180, 180)-target) <= .5)


def _stats(eligible, passing):
    return {'count': len(passing) if eligible else None, 'denominator': len(eligible)}


def _ensemble(raw, times):
    points, eligibility, passes = [], [], []
    for i, time in enumerate(times):
        sets = {}
        for field in ENSEMBLE_FIELDS:
            sets[field] = {m: v[i] for m, v in raw[field].items() if v[i] is not None}
        cloud, rh, surface, pressure = (sets[f] for f in ENSEMBLE_FIELDS)
        rh = {m: v for m, v in rh.items() if pressure.get(m, 0) > 925}
        eligible = {'low_cloud': set(cloud), 'rh925': set(rh),
                    'joint_low_cloud_rh925': set(cloud) & set(rh),
                    'joint_surface_rh925': set(surface) & set(rh)}
        passing = {'low_cloud': {m for m in cloud if cloud[m] >= 75},
                   'rh925': {m for m in rh if rh[m] >= 90},
                   'joint_low_cloud_rh925': {m for m in eligible['joint_low_cloud_rh925'] if cloud[m] >= 75 and rh[m] >= 90},
                   'joint_surface_rh925': {m for m in eligible['joint_surface_rh925'] if surface[m] >= 90 and rh[m] >= 90}}
        eligibility.append(eligible)
        passes.append(passing)
        points.append({'time': iso_z(time), 'screens': {s: _stats(eligible[s], passing[s]) for s in SCREENS}})
    return points, {s: _stats(set.intersection(*(e[s] for e in eligibility)),
                             set.intersection(*(p[s] for p in passes))) for s in SCREENS}


def _thermal(levels):
    result = {}
    for lower, upper in ((1000, 925), (925, 850)):
        a, b = levels[str(lower)], levels[str(upper)]
        delta = lapse = inversion = None
        values = [a['temperature'], b['temperature'], a['geopotential_height'], b['geopotential_height']]
        if all(v is not None for v in values) and values[3] > values[2]:
            delta = round(values[1]-values[0], 3)
            lapse = round(-delta * 1000/(values[3]-values[2]), 3)
            inversion = delta > 0
        result[f'{lower}_{upper}'] = {'temperature_change_c': delta, 'lapse_c_per_km': lapse, 'inversion': inversion}
    return result


def _profiles(raw, times):
    points = []
    for i, time in enumerate(times):
        value = lambda field: raw[field].get('00', [None]*3)[i]
        surface = {field: value(field) for field in SURFACE}
        levels = {}
        for level in LEVELS:
            pressure = surface['surface_pressure']
            status = 'unknown_ground' if pressure is None else 'below_ground' if level >= pressure else 'above_ground'
            levels[str(level)] = {'status': status, **{field: value(f'{field}_{level}hPa') if status == 'above_ground' else None
                                  for field in ('temperature', 'relative_humidity', 'geopotential_height')}}
        points.append({'time': iso_z(time), 'surface': surface, 'levels': levels, 'thermal': _thermal(levels)})
    return points


def _specs():
    return [('ensembles', s.key, s.model_id, s.name, ENSEMBLE_ENDPOINT, s.members,
             6 if s.key == 'aifs_ens' else 3) for s in ENSEMBLE_MODELS] + [
            ('profiles', key, s['model_id'], s['model'], PROFILE_ENDPOINT, 1, s['resolution']//3600)
            for key, s in PROFILE_MODELS.items()]


def collect_layer_signals(client, snapshot, now):
    """Return a validated bounded envelope or None for invalid snapshot context."""
    try:
        now, times = _context(snapshot, now)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    direct = getattr(client, 'direct_native', False) is True
    envelope = {'version': 2 if direct else 1, 'collected_at': iso_z(now), 'snapshot_collected_at': snapshot['collected_at'],
                'event': deepcopy(snapshot['event']), 'range': deepcopy(snapshot['range']),
                'sample_times': [iso_z(t) for t in times], 'limitations': NOTES,
                'thresholds': dict(THRESHOLDS), 'ensembles': {}, 'profiles': {}}
    for family, key, model_id, name, endpoint, members, resolution in _specs():
        envelope[family][key] = _failed()
        if direct and family == 'profiles':
            try:
                from .direct_layers import profile
                envelope[family][key] = profile(snapshot, key, times, now)
                continue
            except (KeyError, TypeError, ValueError, IndexError):
                pass  # Explicitly scoped rolling fallback; never mix fields.
        try:
            fields = ENSEMBLE_FIELDS if family == 'ensembles' else PROFILE_FIELDS
            raw, grid = _raw(client, endpoint, model_id, fields, times, members)
            data = {'model_id': model_id, 'model': name, 'endpoint': endpoint, 'fetched_at': iso_z(now),
                    'grid_point': grid, 'metadata': _metadata(resolution),
                    'units': {f: FIELD_UNITS[f] for f in fields}}
            if family == 'ensembles':
                data['points'], data['all_three'] = _ensemble(raw, times)
                data['expected_members'] = members
            else:
                data['points'] = _profiles(raw, times)
            envelope[family][key] = {'ok': True, 'data': data, 'error': None}
        except Exception:
            pass  # Independent failures; no URLs/tokens/exception text persisted.
    return validate_layer_signals(envelope, snapshot, now)


def _validate_stat(stat, maximum):
    _shape(stat, ('count', 'denominator'))
    n, count = stat['denominator'], stat['count']
    _require(type(n) is int and 0 <= n <= maximum)
    _require(count is None if n == 0 else type(count) is int and 0 <= count <= n)


def _validate_screens(screens, maximum):
    _shape(screens, SCREENS)
    for stat in screens.values():
        _validate_stat(stat, maximum)
    for joint, parents in (('joint_low_cloud_rh925', ('low_cloud', 'rh925')),
                           ('joint_surface_rh925', ('rh925',))):
        for parent in parents:
            _require(screens[joint]['denominator'] <= screens[parent]['denominator'])
            _require((screens[joint]['count'] or 0) <= (screens[parent]['count'] or 0))


def _validate_source(source, spec, collected, times):
    family, key, model_id, name, endpoint, members, resolution = spec
    _shape(source, ('ok', 'data', 'error'))
    _require(source['ok'] is True and source['error'] is None)
    data = source['data']
    _shape(data, {'model_id', 'model', 'endpoint', 'fetched_at', 'grid_point', 'metadata', 'units', 'points'} |
           ({'all_three', 'expected_members'} if family == 'ensembles' else set()))
    _require(data['model_id'] == model_id and data['model'] == name and data['endpoint'] == endpoint)
    _require(data['fetched_at'] == collected)
    _grid(data['grid_point'])
    _require(data['metadata'] == _metadata(resolution))
    # Equality alone accepts bool as 1; enforce explicit metadata scalar types.
    _require(type(data['metadata']['native_timestep_hours']) is int and
             data['metadata']['same_fetch'] is True and data['metadata']['model_init_is_response_bound'] is False)
    fields = ENSEMBLE_FIELDS if family == 'ensembles' else PROFILE_FIELDS
    _require(data['units'] == {f: FIELD_UNITS[f] for f in fields})
    points = data['points']
    _require(isinstance(points, list) and len(points) == 3)
    if family == 'ensembles':
        _require(type(data['expected_members']) is int and data['expected_members'] == members)
        for point, time in zip(points, times):
            _shape(point, ('time', 'screens'))
            _require(point['time'] == iso_z(time))
            _validate_screens(point['screens'], members)
        _validate_screens(data['all_three'], members)
        for screen in SCREENS:
            stat = data['all_three'][screen]
            for point in points:
                sample = point['screens'][screen]
                _require(stat['denominator'] <= sample['denominator'] and
                         (stat['count'] or 0) <= (sample['count'] or 0))
    else:
        for point, time in zip(points, times):
            _shape(point, ('time', 'surface', 'levels', 'thermal'))
            _require(point['time'] == iso_z(time))
            _shape(point['surface'], SURFACE)
            for field, value in point['surface'].items():
                _value(field, value)
            _shape(point['levels'], (str(level) for level in LEVELS))
            for level in LEVELS:
                layer = point['levels'][str(level)]
                _shape(layer, ('status', 'temperature', 'relative_humidity', 'geopotential_height'))
                pressure = point['surface']['surface_pressure']
                status = 'unknown_ground' if pressure is None else 'below_ground' if level >= pressure else 'above_ground'
                _require(layer['status'] == status)
                for field in ('temperature', 'relative_humidity', 'geopotential_height'):
                    _value(f'{field}_{level}hPa', layer[field])
                    _require(status == 'above_ground' or layer[field] is None)
            expected = _thermal(point['levels'])
            _shape(point['thermal'], expected)
            for pair, values in expected.items():
                actual = point['thermal'][pair]
                _shape(actual, values)
                for field in ('temperature_change_c', 'lapse_c_per_km'):
                    _number(actual[field], -1e9, 1e9, nullable=True)
                _require(actual['inversion'] is None or type(actual['inversion']) is bool)
                _require(actual == values)
    return deepcopy(source)


def validate_layer_signals(envelope, snapshot, now):
    """Fail closed on outer context, isolate invalid sources, return detached copy.

    Count contracts are internally checked; without raw arrays persisted counts
    cannot be independently recomputed. Collection computes them from actual IDs.
    """
    try:
        now, times = _context(snapshot, now)
        _shape(envelope, ('version', 'collected_at', 'snapshot_collected_at', 'event', 'range',
                          'sample_times', 'limitations', 'thresholds', 'ensembles', 'profiles'))
        _require(type(envelope['version']) is int and envelope['version'] in (1, 2))
        collected = _timestamp(envelope['collected_at'])
        _require(_timestamp(snapshot['collected_at']) <= collected <= now and now-collected <= timedelta(hours=12))
        _require(envelope['snapshot_collected_at'] == snapshot['collected_at'])
        _require(envelope['event'] == snapshot['event'] and envelope['range'] == snapshot['range'])
        _require(envelope['sample_times'] == [iso_z(t) for t in times])
        _require(envelope['limitations'] == NOTES and envelope['thresholds'] == THRESHOLDS)
        for family in ('ensembles', 'profiles'):
            _shape(envelope[family], (s[1] for s in _specs() if s[0] == family))
        result = {k: deepcopy(v) for k, v in envelope.items() if k not in ('ensembles', 'profiles')}
        result.update(ensembles={}, profiles={})
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    for spec in _specs():
        family, key = spec[:2]
        try:
            source = envelope[family][key]
            if envelope['version'] == 2 and family == 'profiles' and isinstance(source.get('data'), dict) and 'native_reference' in source['data']:
                from .direct_layers import profile
                expected = profile(snapshot, key, times, now)
                _require(source == expected)
                result[family][key] = expected
            else:
                result[family][key] = _validate_source(source, spec, envelope['collected_at'], times)
        except (KeyError, TypeError, ValueError, OverflowError, IndexError):
            result[family][key] = _failed()
    return result
