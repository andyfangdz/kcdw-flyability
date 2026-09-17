"""Supplemental event relative humidity, never cloud base or flight probability.

Public API (all timestamps are canonical ISO UTC, end exclusive):
    collect_moisture(client, start, end, now) ->
      {version: 1, collected_at, range: {start, end},
       models: {gfs|ifs|aifs_single: {ok, data, error}}}
    validate_moisture(envelope, now) ->
      {range: {start, end} or None,
       models: {gfs|ifs|aifs_single: {available, error, data}}}

Successful data has model_id, model, endpoint, fetched_at, requested_start/end,
 grid_point, hourly_units, hourly, metadata. Validation returns a detached copy,
 revalidates persisted values and units, and masks BOTH RH and geopotential height
 for pressure levels below model surface (level hPa > surface_pressure). Missing
 surface pressure masks all pressure levels at that hour; original gaps remain.

client.get(url) must return parsed JSON. Each model is explicitly pinned at
 /v1/forecast; the response normally does not echo its model ID. UTC hourly requests
 use inclusive start_hour/end_hour, clipped at collection-day UTC midnight, with
 the original display range retained separately. Open-Meteo may interpolate native
 model steps to hourly: these points are not independent native forecasts.

Independent /data/{dataset}/static/meta.json records advertise the latest run,
 NOT the run(s) behind the rolling point response. All required datasets must have
 fresh, chronological metadata, but a short latest IFS cycle's horizon must NOT
 invalidate later rolling RH. metadata.hourly_provenance labels those hours
 'unbound beyond metadata horizon'; even within that horizon attribution is unbound.
 Heights are geopotential metres above MSL, not AGL or inferred ceilings.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from .common import UTC, iso_z

ENDPOINT = 'https://api.open-meteo.com/v1/forecast'
LAT, LON = 40.8752, -74.2814
LEVELS = (1000, 925, 850)
MODELS = {
    'gfs': {'model_id': 'gfs_global', 'model': 'GFS operational',
            'datasets': ('ncep_gfs013', 'ncep_gfs025'), 'resolution': 3600},
    'ifs': {'model_id': 'ecmwf_ifs025', 'model': 'ECMWF IFS',
            'datasets': ('ecmwf_ifs025',), 'resolution': 10800},
    'aifs_single': {'model_id': 'ecmwf_aifs025_single', 'model': 'ECMWF AIFS single',
                    'datasets': ('ecmwf_aifs025_single',), 'resolution': 21600},
}
VARIABLES = ('relative_humidity_2m',) + tuple(
    f'{field}_{level}hPa' for level in LEVELS
    for field in ('relative_humidity', 'geopotential_height')) + ('surface_pressure',)
UNITS = {'time': 'iso8601', **{field: '%' if field.startswith('relative_humidity') else
         'm' if field.startswith('geopotential_height') else 'hPa' for field in VARIABLES}}
PROVENANCE = 'latest-advertised; not response-bound'
PROVENANCE_LIMITS = ('Rolling point values have no immutable response-bound run ID. '
                     'Latest-advertised metadata does not identify their initialization; '
                     'later rolling values may come from an older long cycle.')
INTERPRETATION = ('Relative humidity is moisture guidance, not cloud base, ceiling, '
                  'flight category or flight probability. Heights are geopotential metres MSL. '
                  'Null means missing/unknown, never dry or clear weather.')
MAX_FETCH_AGE = timedelta(hours=12)
MAX_METADATA_AGE = timedelta(hours=24)


def _require(condition):
    if not condition:
        raise ValueError('invalid moisture data')


def _number(value):
    _require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value))
    return value


def _aware(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)
    return value.astimezone(UTC)


def _stamp(value):
    _require(isinstance(value, str))
    result = _aware(datetime.fromisoformat(value.replace('Z', '+00:00')))
    _require(iso_z(result) == value)
    return result


def _fresh(stamp, now, ttl):
    _require(timedelta(0) <= now-stamp <= ttl)


def _axis(start, end):
    start, end = _aware(start), _aware(end)
    _require(all(t.minute == t.second == t.microsecond == 0 for t in (start, end)))
    _require(timedelta(0) < end-start <= timedelta(days=16))
    return [start+timedelta(hours=i) for i in range(int((end-start).total_seconds()/3600))]


def _window(start, end, collected):
    start, end = _aware(start), _aware(end)
    _axis(start, end)
    day = collected.replace(hour=0, minute=0, second=0, microsecond=0)
    clipped = max(start, day)
    _require(end <= day+timedelta(days=16))
    return _axis(clipped, end)


def _grid(grid):
    _require(isinstance(grid, dict))
    _require(abs(_number(grid['latitude'])-LAT) <= 0.25)
    _require(abs(_number(grid['longitude'])-LON) <= 0.25)
    _require(grid['requested_latitude'] == LAT and grid['requested_longitude'] == LON)


def _series(hourly, units, expected, time_unit):
    _require(isinstance(hourly, dict) and set(hourly) == {'time', *VARIABLES})
    _require(isinstance(units, dict) and set(units) == {'time', *VARIABLES})
    _require(units['time'] == time_unit)
    _require(isinstance(hourly['time'], list) and hourly['time'] == expected)
    for field in VARIABLES:
        values = hourly[field]
        _require(isinstance(values, list) and len(values) == len(expected))
        _require(units[field] == UNITS[field] or
                 (units[field] in (None, 'undefined') and all(v is None for v in values)))
        low, high = ((0, 100) if field.startswith('relative_humidity') else
                     (-1000, 10000) if field.startswith('geopotential_height') else (500, 1100))
        for value in values:
            if value is not None:
                _require(low <= _number(value) <= high)


def _metadata_url(dataset):
    return f'https://api.open-meteo.com/data/{dataset}/static/meta.json'


def _collect_metadata(client, spec, now):
    datasets = {}
    for dataset in spec['datasets']:
        url = _metadata_url(dataset)
        raw = client.get(url)
        item = {'endpoint': url, 'fetched_at': iso_z(now)}
        for target, field in (
            ('latest_advertised_init', 'last_run_initialisation_time'),
            ('latest_advertised_available_at', 'last_run_availability_time'),
            ('latest_advertised_modified_at', 'last_run_modification_time'),
            ('data_end_time', 'data_end_time'),
        ):
            value = _number(raw[field])
            _require(value == int(value))
            item[target] = iso_z(datetime.fromtimestamp(value, UTC))
        for field in ('temporal_resolution_seconds', 'update_interval_seconds'):
            item[field] = raw[field]
        datasets[dataset] = item
    return {'provenance': PROVENANCE, 'provenance_limits': PROVENANCE_LIMITS,
            'model_init_is_response_bound': False, 'datasets': datasets,
            'interpretation': INTERPRETATION,
            'sampling': 'Open-Meteo hourly output; may interpolate coarser native model steps'}


def _validated_data(source, spec, collected, times, now):
    _require(isinstance(source, dict) and source.get('ok') is True)
    data = copy.deepcopy(source['data'])
    if isinstance(data, dict) and data.get('schema_version') == 2:
        from .direct_deterministic import validate_direct_data
        _require(data['model_id'] == spec['model_id'])
        _require(data['requested_start'] == iso_z(times[0]) and data['requested_end'] == iso_z(times[-1]+timedelta(hours=1)))
        return validate_direct_data(data, now, 'moisture')
    _require(isinstance(data, dict) and data['model_id'] == spec['model_id'] and
             data['model'] == spec['model'] and data['endpoint'] == ENDPOINT)
    fetched = _stamp(data['fetched_at'])
    _fresh(fetched, now, MAX_FETCH_AGE)
    _require(fetched == collected)
    _require(data['requested_start'] == iso_z(times[0]) and
             data['requested_end'] == iso_z(times[-1]+timedelta(hours=1)))
    _grid(data['grid_point'])
    _series(data['hourly'], data['hourly_units'], [iso_z(t) for t in times], 'iso8601')
    metadata = data['metadata']
    _require(metadata['provenance'] == PROVENANCE and metadata['provenance_limits'] == PROVENANCE_LIMITS)
    _require(metadata['model_init_is_response_bound'] is False and metadata['interpretation'] == INTERPRETATION)
    datasets = metadata['datasets']
    _require(isinstance(datasets, dict) and set(datasets) == set(spec['datasets']))
    horizons = []
    for name in spec['datasets']:
        item = datasets[name]
        _require(item['endpoint'] == _metadata_url(name))
        metadata_fetch = _stamp(item['fetched_at'])
        _fresh(metadata_fetch, now, MAX_METADATA_AGE)
        _require(metadata_fetch == fetched)
        init = _stamp(item['latest_advertised_init'])
        modified = _stamp(item['latest_advertised_modified_at'])
        available = _stamp(item['latest_advertised_available_at'])
        horizon = _stamp(item['data_end_time'])
        for stamp in (init, modified, available):
            _fresh(stamp, now, MAX_METADATA_AGE)
        _require(init <= modified <= available <= metadata_fetch)
        _require(init.hour in (0, 6, 12, 18) and init.minute == init.second == init.microsecond == 0)
        _require(init < horizon <= init+timedelta(days=17))
        _require(horizon.minute == horizon.second == horizon.microsecond == 0)
        _require(_number(item['temporal_resolution_seconds']) == spec['resolution'])
        _require(_number(item['update_interval_seconds']) == 21600)
        horizons.append(horizon)
    # This annotation is recomputed, never trusted from persisted data.
    horizon = min(horizons)
    metadata['latest_advertised_common_horizon'] = iso_z(horizon)
    metadata['hourly_provenance'] = [
        'unbound beyond metadata horizon' if t > horizon else PROVENANCE for t in times]
    hourly = data['hourly']
    for i, surface_pressure in enumerate(hourly['surface_pressure']):
        for level in LEVELS:
            if surface_pressure is None or level > surface_pressure:
                hourly[f'relative_humidity_{level}hPa'][i] = None
                hourly[f'geopotential_height_{level}hPa'][i] = None
    return data


def _unavailable(error='Moisture source unavailable or invalid'):
    return {'available': False, 'error': error, 'data': None}


def validate_moisture(envelope, now):
    """Return per-alias statuses and detached, terrain-masked data; never raise.

    Invalid outer schema/range/collection TTL disables all models; invalid provider
    data disables only that provider. Error strings deliberately exclude exceptions,
    payload excerpts and request URLs (which may contain credentials).
    """
    result = {'range': None, 'models': {key: _unavailable() for key in MODELS}}
    try:
        now = _aware(now)
        _require(isinstance(envelope, dict) and type(envelope['version']) is int and envelope['version'] == 1)
        collected = _stamp(envelope['collected_at'])
        _fresh(collected, now, MAX_FETCH_AGE)
        display = envelope['range']
        start, end = _stamp(display['start']), _stamp(display['end'])
        times = _window(start, end, collected)
        _require(isinstance(envelope['models'], dict))
        result['range'] = {'start': iso_z(start), 'end': iso_z(end)}
    except Exception:
        return result
    for key, spec in MODELS.items():
        try:
            data = _validated_data(envelope['models'][key], spec, collected, times, now)
            result['models'][key] = {'available': True, 'error': '', 'data': data}
        except Exception:
            result['models'][key] = _unavailable()
    return result


def collect_moisture(client, start, end, now):
    """Collect the fixed three models independently; no aviation-quorum changes."""
    envelope = {'version': 1, 'collected_at': None, 'range': None,
                'models': {key: {'ok': False, 'data': None, 'error': 'Moisture collection unavailable'}
                           for key in MODELS}}
    try:
        now, start, end = _aware(now), _aware(start), _aware(end)
        times = _window(start, end, now)
        envelope.update(collected_at=iso_z(now), range={'start': iso_z(start), 'end': iso_z(end)})
    except Exception:
        return envelope
    native = {}
    if getattr(client, 'direct_native', False) is True:
        from concurrent.futures import ThreadPoolExecutor
        from .direct_deterministic import collect_native_profile, normalize_profile, validate_direct_data
        def direct(key):
            try:
                packet = collect_native_profile(key, times[0], end, now)
                data = normalize_profile(packet, times[0], end)
                validate_direct_data(data, datetime.now(UTC))
                return {'ok': True, 'data': data, 'error': ''}
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            native = dict(zip(MODELS, pool.map(direct, MODELS)))
    for key, spec in MODELS.items():
        if native.get(key):
            envelope['models'][key] = native[key]
            continue
        try:
            params = {'latitude': LAT, 'longitude': LON, 'models': spec['model_id'],
                      'hourly': ','.join(VARIABLES), 'timezone': 'GMT', 'timeformat': 'unixtime',
                      'start_hour': times[0].strftime('%Y-%m-%dT%H:%M'),
                      'end_hour': times[-1].strftime('%Y-%m-%dT%H:%M')}
            raw = client.get(ENDPOINT+'?'+urlencode(params))
            _require(isinstance(raw, dict) and raw.get('timezone') == 'GMT' and
                     _number(raw.get('utc_offset_seconds')) == 0)
            for field in ('model', 'model_id', 'models'):
                _require(field not in raw or raw[field] == spec['model_id'])
            for stamp in raw['hourly']['time']:
                _number(stamp)
            _series(raw['hourly'], raw['hourly_units'], [int(t.timestamp()) for t in times], 'unixtime')
            data = {'model_id': spec['model_id'], 'model': spec['model'], 'endpoint': ENDPOINT,
                    'fetched_at': iso_z(now), 'requested_start': iso_z(times[0]), 'requested_end': iso_z(end),
                    'grid_point': {'latitude': raw['latitude'], 'longitude': raw['longitude'],
                                   'requested_latitude': LAT, 'requested_longitude': LON},
                    'hourly_units': dict(raw['hourly_units'], time='iso8601'),
                    'hourly': dict(copy.deepcopy(raw['hourly']), time=[iso_z(t) for t in times]),
                    'metadata': _collect_metadata(client, spec, now)}
            source = {'ok': True, 'error': '', 'data': data}
            # Store only validated, terrain-masked data, and validate it again at display time.
            source['data'] = _validated_data(source, spec, _stamp(envelope['collected_at']), times, now)
            if getattr(client, 'direct_native', False) is True:
                from .direct_deterministic import FALLBACK
                source['data']['metadata']['direct_fallback_reason'] = FALLBACK
            envelope['models'][key] = source
        except Exception:
            # Do not serialize exception text: HTTP client errors may include secret URLs.
            pass
    return envelope
