"""Independent member RH fans; descriptive spread, not calibrated flight odds.

The chart range is retained separately from the API's UTC-day-clipped axis.
Only normalized fans and member identities survive collection, never member
values. Pressure-level values are masked against that same member's surface
pressure; surface RH does not require pressure.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
from typing import Any
from urllib.parse import urlencode

from .common import UTC, iso_z
from .event_ensemble import ENDPOINT, LAT, LON, MODELS as ALL_MODELS, _metadata, _percentile

MODELS = tuple(spec for spec in ALL_MODELS if spec.key in ('gefs', 'ecmwf_ens', 'aifs_ens'))
RH_FIELDS = ('relative_humidity_2m', 'relative_humidity_1000hPa',
             'relative_humidity_925hPa', 'relative_humidity_850hPa')
VARIABLES = (*RH_FIELDS, 'surface_pressure')
LEVELS = dict(zip(RH_FIELDS[1:], (1000, 925, 850)))
UNITS = {'time': 'iso8601 UTC', **{field: '%' for field in RH_FIELDS}, 'surface_pressure': 'hPa'}
MEMBER_KEY = re.compile(r'^(relative_humidity_(?:2m|1000hPa|925hPa|850hPa)|surface_pressure)(?:_member(\d{2}))?$')
BINDING_NOTE = ('Latest advertised dataset metadata, not an immutable run binding. '
                'Short 06/18Z IFS cycles may not cover the extended rolling response; '
                'earlier long cycles can supply that horizon.')
FAILURE = 'RH ensemble unavailable or validation failed'


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone-aware datetime required')
    return value.astimezone(UTC)


def _timestamp(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('canonical UTC timestamp required')
    dt = _utc(datetime.fromisoformat(value.replace('Z', '+00:00')))
    if iso_z(dt) != value:
        raise ValueError('noncanonical UTC timestamp')
    return dt


def _number(value: object, low, high, nullable=False) -> Any:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid numeric value')
    return value


def _axes(start, end, collected):
    start, end, collected = _utc(start), _utc(end), _utc(collected)
    if any(t.minute or t.second or t.microsecond for t in (start, end)) or start >= end:
        raise ValueError('invalid hourly range')
    # Bound allocation on corrupt persisted envelopes (API horizon is far shorter).
    if end - start > timedelta(days=32):
        raise ValueError('range too long')
    clipped = max(start, collected.replace(hour=0, minute=0, second=0, microsecond=0))
    if clipped >= end:
        raise ValueError('range already ended')
    count = int((end-clipped).total_seconds()/3600)
    return clipped, [iso_z(clipped+timedelta(hours=i)) for i in range(count)]


def _fresh(value, now, age):
    dt = _timestamp(value)
    if not now-timedelta(hours=age) <= dt <= now+timedelta(minutes=5):
        raise ValueError('stale or future timestamp')
    return dt


def _grid(grid):
    for coord, target in (('latitude', LAT), ('longitude', LON)):
        if abs(_number(grid[coord], -180, 180)-target) > .5:
            raise ValueError('grid mismatch')


def _label(spec):
    return (f'{spec.name} RH member p10–p90 range and median via Open-Meteo; '
            'descriptive model spread, not calibrated flight odds. Latest dataset metadata '
            'is not an exact run binding.')


def _raw_members(raw, spec, axis):
    if not isinstance(raw, dict):
        raise ValueError('response must be an object')
    _grid(raw)
    for key in ('model', 'model_id'):
        if key in raw and raw[key] != spec.model_id:
            raise ValueError('model identity mismatch')
    if raw.get('timezone') != 'GMT' or type(raw.get('utc_offset_seconds')) is not int or raw['utc_offset_seconds'] != 0:
        raise ValueError('UTC response required')
    h, units = raw['hourly'], raw['hourly_units']
    if not isinstance(h, dict) or not isinstance(units, dict) or units.get('time') != 'unixtime':
        raise ValueError('invalid hourly units')
    expected = [_timestamp(t).timestamp() for t in axis]
    times = h.get('time')
    if not isinstance(times, list) or len(times) != len(expected):
        raise ValueError('invalid time axis')
    if any(_number(t, -1e11, 1e11) != e for t, e in zip(times, expected)):
        raise ValueError('exact time axis mismatch')
    members = {field: {} for field in VARIABLES}
    for key, values in h.items():
        if key == 'time':
            continue
        match = MEMBER_KEY.fullmatch(key)
        if not match:
            raise ValueError('unexpected member field')
        field, member = match.group(1), match.group(2) or '00'
        if int(member) >= spec.members or member in members[field]:
            raise ValueError('invalid or duplicate member identity')
        if not isinstance(values, list) or len(values) != len(axis):
            raise ValueError('member length mismatch')
        missing_field = units.get(key) in (None, 'undefined') and all(v is None for v in values)
        if units.get(key) != UNITS[field] and not missing_field:
            raise ValueError('member unit mismatch')
        bounds = (300, 1100) if field == 'surface_pressure' else (0, 100)
        members[field][member] = [_number(v, *bounds, nullable=True) for v in values]
    return members


def _fans(members, axis):
    hourly: dict = {'time': list(axis)}
    for field in RH_FIELDS:
        fan = {key: [] for key in ('p10', 'p50', 'p90', 'sample_counts')}
        for index in range(len(axis)):
            sample = []
            for member, values in members[field].items():
                value = values[index]
                if value is None:
                    continue
                if field in LEVELS:
                    pressures = members['surface_pressure'].get(member)
                    if pressures is None or pressures[index] is None or LEVELS[field] > pressures[index]:
                        continue
                sample.append(value)
            fan['sample_counts'].append(len(sample))
            for pct in (10, 50, 90):
                fan[f'p{pct}'].append(_percentile(sample, pct) if sample else None)
        hourly[field] = fan
    return hourly


def _validate_data(data, spec, axis, end, collected, now):
    if isinstance(data, dict) and data.get('metadata', {}).get('direct_native') is True:
        from .direct_ensemble import validate_normalized
        validate_normalized(data, spec, now)
        if data['hourly'].get('time') != axis or data['hourly_units'] != UNITS:
            raise ValueError('direct RH axis/units mismatch')
        ids = data['member_ids']
        if set(ids) != set(VARIABLES):
            raise ValueError('direct RH member fields mismatch')
        offered = set(data['metadata']['member_ids'])
        for field, members in ids.items():
            if members != sorted(set(members)) or not set(members) <= offered:
                raise ValueError('direct RH member identity mismatch')
        if set(data['hourly']) != {'time', *RH_FIELDS}:
            raise ValueError('direct RH fields mismatch')
        for field in RH_FIELDS:
            fan = data['hourly'][field]
            if set(fan) != {'p10', 'p50', 'p90', 'sample_counts'} or any(len(v) != len(axis) for v in fan.values()):
                raise ValueError('direct RH fan mismatch')
            maximum = len(set(ids[field]) & set(ids['surface_pressure'])) if field in LEVELS else len(ids[field])
            for i, count in enumerate(fan['sample_counts']):
                values = [fan[f'p{p}'][i] for p in (10, 50, 90)]
                if type(count) is not int or not 0 <= count <= maximum:
                    raise ValueError('direct RH sample count mismatch')
                if count:
                    checked = [_number(v, 0, float('inf')) for v in values]
                    if checked != sorted(checked) or (count == 1 and len(set(checked)) != 1):
                        raise ValueError('direct RH quantiles mismatch')
                elif any(v is not None for v in values):
                    raise ValueError('direct RH values without samples')
        return deepcopy(data)
    if isinstance(data, dict) and 'direct_fallback_reason' in data.get('metadata', {}):
        from .direct_ensemble import FALLBACK
        if data['metadata']['direct_fallback_reason'] != FALLBACK:
            raise ValueError('fallback provenance mismatch')
        legacy = deepcopy(data)
        del legacy['metadata']['direct_fallback_reason']
        _validate_data(legacy, spec, axis, end, collected, now)
        return deepcopy(data)
    allowed = {'model_id', 'model', 'label', 'fetched_at', 'hourly', 'members',
               'metadata', 'grid_point', 'hourly_units', 'endpoint', 'member_ids'}
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError('normalized data contract mismatch')
    if (data['model_id'] != spec.model_id or data['model'] != spec.name or
            type(data['members']) is not int or data['members'] != spec.members or
            data['endpoint'] != ENDPOINT or data['label'] != _label(spec)):
        raise ValueError('identity or provenance mismatch')
    fetched = _fresh(data['fetched_at'], now, 12)
    if abs(fetched-collected) > timedelta(minutes=5):
        raise ValueError('fetch/collection timestamp mismatch')
    _grid(data['grid_point'])
    if set(data['grid_point']) != {'latitude', 'longitude'} or data['hourly_units'] != UNITS:
        raise ValueError('grid or units mismatch')
    meta = data['metadata']
    expected_meta = {'ok', 'fresh', 'covers_display', 'binding_note', 'initialization_time',
                     'availability_time', 'data_end_time', 'native_timestep_hours', 'dataset', 'model_id'}
    if (not isinstance(meta, dict) or set(meta) != expected_meta or meta['ok'] is not True or
            meta['fresh'] is not True or meta['binding_note'] != BINDING_NOTE or
            meta['dataset'] != spec.metadata_dataset or meta['model_id'] != spec.model_id):
        raise ValueError('metadata provenance mismatch')
    initialized = _fresh(meta['initialization_time'], now, 24)
    available, data_end = _timestamp(meta['availability_time']), _timestamp(meta['data_end_time'])
    if not initialized <= available <= fetched+timedelta(minutes=5) or data_end <= available:
        raise ValueError('metadata times inconsistent')
    if (type(meta['covers_display']) is not bool or meta['covers_display'] != (data_end >= end-timedelta(hours=1)) or
            type(meta['native_timestep_hours']) is not int or meta['native_timestep_hours'] not in (1, 3, 6)):
        raise ValueError('metadata coverage or resolution mismatch')
    identities = data['member_ids']
    if not isinstance(identities, dict) or set(identities) != set(VARIABLES):
        raise ValueError('member provenance missing')
    expected_ids = {f'{i:02}' for i in range(spec.members)}
    for ids in identities.values():
        if not isinstance(ids, list) or any(type(i) is not str or i not in expected_ids for i in ids) or ids != sorted(set(ids)):
            raise ValueError('member provenance mismatch')
    hourly = data['hourly']
    if not isinstance(hourly, dict) or set(hourly) != {'time', *RH_FIELDS} or hourly['time'] != axis:
        raise ValueError('normalized fields or axis mismatch')
    for field in RH_FIELDS:
        fan = hourly[field]
        if not isinstance(fan, dict) or set(fan) != {'p10', 'p50', 'p90', 'sample_counts'}:
            raise ValueError('fan contract mismatch')
        if any(not isinstance(v, list) or len(v) != len(axis) for v in fan.values()):
            raise ValueError('fan length mismatch')
        usable = set(identities[field])
        if field in LEVELS:
            usable &= set(identities['surface_pressure'])
        for index, count in enumerate(fan['sample_counts']):
            if type(count) is not int or not 0 <= count <= len(usable):
                raise ValueError('sample count mismatch')
            values = [fan[f'p{pct}'][index] for pct in (10, 50, 90)]
            if count == 0:
                if any(v is not None for v in values):
                    raise ValueError('values without samples')
            else:
                checked = [_number(v, 0, 100) for v in values]
                if checked != sorted(checked) or (count == 1 and len(set(checked)) != 1):
                    raise ValueError('quantile order/count mismatch')
    return deepcopy(data)


def collect_moisture_ensemble(client, start, end, now):
    """Fetch the three explicitly selected ensembles, failing independently."""
    start, end, now = _utc(start), _utc(end), _utc(now)
    clipped, axis = _axes(start, end, now)
    envelope = {'version': 1, 'collected_at': iso_z(now),
                'range': {'start': iso_z(start), 'end': iso_z(end)}, 'models': {}}
    for spec in MODELS:
        fallback = False
        if getattr(client, 'direct_native', False) is True:
            try:
                from .direct_ensemble import collect_rh
                data = collect_rh(client, spec, clipped, end, now)
                _validate_data(data, spec, axis, end, now, datetime.now(UTC))
                envelope['models'][spec.key] = {'ok': True, 'data': data, 'error': None}
                continue
            except Exception:
                fallback = True
        try:
            params = {'latitude': LAT, 'longitude': LON, 'models': spec.model_id,
                      'hourly': ','.join(VARIABLES), 'timezone': 'GMT', 'timeformat': 'unixtime',
                      'start_hour': clipped.strftime('%Y-%m-%dT%H:%M'),
                      'end_hour': (end-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
            raw = client.get(ENDPOINT+'?'+urlencode(params))
            members = _raw_members(raw, spec, axis)
            metadata = _metadata(client, spec, now, end)
            # _metadata may include upstream exception text: never persist it.
            if metadata.get('ok') is not True:
                raise ValueError('metadata unavailable')
            metadata.update(dataset=spec.metadata_dataset, model_id=spec.model_id)
            if fallback:
                from .direct_ensemble import FALLBACK
                metadata['direct_fallback_reason'] = FALLBACK
            data = {'model_id': spec.model_id, 'model': spec.name, 'label': _label(spec),
                    'members': spec.members, 'fetched_at': iso_z(now), 'endpoint': ENDPOINT,
                    'metadata': metadata, 'hourly_units': dict(UNITS),
                    'grid_point': {key: raw[key] for key in ('latitude', 'longitude')},
                    'member_ids': {field: sorted(rows) for field, rows in members.items()},
                    'hourly': _fans(members, axis)}
            _validate_data(data, spec, axis, end, now, now)
            envelope['models'][spec.key] = {'ok': True, 'data': data, 'error': None}
        except Exception:
            envelope['models'][spec.key] = {'ok': False, 'data': None, 'error': FAILURE}
    return envelope


def validate_moisture_ensemble(envelope, now):
    """Revalidate persisted normalized fans; never expose upstream error text."""
    result = {'models': {spec.key: {'available': False, 'data': None, 'error': FAILURE}
                         for spec in MODELS}, 'range': None}
    try:
        now = _utc(now)
        if not isinstance(envelope, dict) or type(envelope.get('version')) is not int or envelope['version'] != 1:
            raise ValueError('unsupported envelope')
        collected = _fresh(envelope['collected_at'], now, 12)
        display = envelope['range']
        if not isinstance(display, dict) or set(display) != {'start', 'end'}:
            raise ValueError('range mismatch')
        start, end = _timestamp(display['start']), _timestamp(display['end'])
        _, axis = _axes(start, end, collected)
        result['range'] = {'start': iso_z(start), 'end': iso_z(end)}
    except (ValueError, TypeError, KeyError, OverflowError):
        return result
    for spec in MODELS:
        try:
            source = envelope['models'][spec.key]
            if source['ok'] is not True:
                continue
            data = _validate_data(source['data'], spec, axis, end, collected, now)
            result['models'][spec.key] = {'available': True, 'data': data, 'error': None}
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            continue
    return result
