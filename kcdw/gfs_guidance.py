"""Pinned operational GFS, not HRRR/seamless or a GEFS control member.

Open-Meteo's rolling forecast has no immutable response-bound cycle ID. Static
metadata is independently fetched latest-advertised freshness evidence only;
never label it as the cycle that produced the returned values. Both backing
GFS datasets must be fresh. Missing values stay null (unknown), not zero.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from .common import UTC, iso_z

ENDPOINT = 'https://api.open-meteo.com/v1/forecast'
MODEL_ID = 'gfs_global'
MODEL = 'GFS operational (deterministic)'
LAT, LON = 40.8752, -74.2814
VARIABLES = ('pressure_msl', 'precipitation', 'wind_speed_10m', 'wind_gusts_10m', 'cloud_cover_low', 'temperature_2m')
UNITS = {'time': 'unixtime', 'pressure_msl': 'hPa', 'precipitation': 'mm', 'wind_speed_10m': 'kn',
         'wind_gusts_10m': 'kn', 'cloud_cover_low': '%', 'temperature_2m': '°C'}
BOUNDS = {'pressure_msl': (750, 1150), 'precipitation': (0, 500), 'wind_speed_10m': (0, 300),
          'wind_gusts_10m': (0, 300), 'cloud_cover_low': (0, 100), 'temperature_2m': (-120, 80)}
METADATA_URLS = {name: f'https://api.open-meteo.com/data/{name}/static/meta.json'
                 for name in ('ncep_gfs013', 'ncep_gfs025')}
PROVENANCE = 'latest-advertised; not response-bound'
PROVENANCE_LIMIT = ('The rolling API does not identify an immutable response-bound model run. '
                    'Model identity is pinned by the request, not independently echoed by the response. '
                    'Static dataset metadata advertises the latest run separately; it does not prove '
                    'that every returned value comes from that run or a single run.')
SAMPLING = ('16-day GFS forecast; native hourly through 120 hours, native 3-hourly thereafter '
            'interpolated to hourly by Open-Meteo. Hourly points are not independent native forecasts.')
MAX_FETCH_AGE = timedelta(hours=12)
MAX_RUN_AGE = timedelta(hours=24)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value, label):
    _require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value),
             f'{label} must be a finite number, not bool')
    return value


def _datetime(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             'timezone-aware datetime required')
    return value.astimezone(UTC)


def _timestamp(value):
    _require(isinstance(value, str), 'ISOZ timestamp required')
    parsed = _datetime(datetime.fromisoformat(value.replace('Z', '+00:00')))
    _require(iso_z(parsed) == value, 'canonical ISOZ timestamp required')
    return parsed


def _axis(start, end):
    start, end = _datetime(start), _datetime(end)
    _require(start.minute == start.second == start.microsecond == 0 and
             end.minute == end.second == end.microsecond == 0, 'window must use whole UTC hours')
    _require(timedelta(0) < end-start <= timedelta(days=16), 'window must be positive and at most 16 days')
    return [start + timedelta(hours=i) for i in range(int((end-start).total_seconds()/3600))]


def _fresh(stamp, now, maximum, label):
    _require(timedelta(0) <= now-stamp <= maximum, f'{label} is future or stale')


def _series(hourly, units, expected, time_unit):
    _require(isinstance(hourly, dict) and set(hourly) == {'time', *VARIABLES}, 'unexpected hourly fields')
    _require(isinstance(units, dict) and units == dict(UNITS, time=time_unit), 'unexpected hourly units')
    _require(hourly['time'] == expected, 'time axis differs from exact requested UTC hourly axis')
    for variable in VARIABLES:
        values = hourly[variable]
        _require(isinstance(values, list) and len(values) == len(expected), f'{variable} length mismatch')
        low, high = BOUNDS[variable]
        for value in values:
            if value is not None:
                _require(low <= _number(value, variable) <= high, f'{variable} outside physical bounds')


def _grid(grid):
    _require(isinstance(grid, dict), 'grid point missing')
    latitude, longitude = _number(grid['latitude'], 'latitude'), _number(grid['longitude'], 'longitude')
    _require(abs(latitude-LAT) <= 0.25 and abs(longitude-LON) <= 0.25, 'grid point is not near requested KCDW')
    _require(grid['requested_latitude'] == LAT and grid['requested_longitude'] == LON, 'requested grid mismatch')


def _validate(source, now):
    now = _datetime(now)
    _require(isinstance(source, dict) and source.get('ok') is True, 'GFS source unavailable')
    data = source['data']
    if isinstance(data, dict) and data.get('schema_version') == 2:
        from .direct_deterministic import validate_direct_data
        validate_direct_data(data, now, 'gfs')
        return
    _require(isinstance(data, dict), 'GFS data must be an object')
    _require(data['model_id'] == MODEL_ID and data['model'] == MODEL and data['endpoint'] == ENDPOINT,
             'GFS identity or endpoint mismatch')
    fetched = _timestamp(data['fetched_at'])
    _fresh(fetched, now, MAX_FETCH_AGE, 'GFS fetch')
    start, end = _timestamp(data['requested_start']), _timestamp(data['requested_end'])
    times = _axis(start, end)
    day = fetched.replace(hour=0, minute=0, second=0, microsecond=0)
    _require(start >= day and end <= day+timedelta(days=16), 'requested axis outside 16-day forecast window')
    _grid(data['grid_point'])
    _series(data['hourly'], data['hourly_units'], [iso_z(t) for t in times], 'iso8601')
    metadata = data['metadata']
    _require(metadata['provenance'] == PROVENANCE and metadata['provenance_limits'] == PROVENANCE_LIMIT,
             'model run provenance must be latest-advertised, not response-bound')
    _require(metadata['sampling'] == SAMPLING and metadata['precipitation_semantics'] == 'preceding-hour total' and
             metadata['null_semantics'] == 'missing/unknown; never clear weather', 'sampling semantics missing')
    _require(metadata['model_init_is_response_bound'] is False, 'unsupported response-bound model init')
    datasets = metadata['datasets']
    _require(isinstance(datasets, dict) and set(datasets) == set(METADATA_URLS), 'GFS metadata datasets mismatch')
    for name, url in METADATA_URLS.items():
        item = datasets[name]
        _require(item['endpoint'] == url, 'metadata endpoint mismatch')
        _require(item['fetched_at'] == data['fetched_at'], 'metadata fetch provenance mismatch')
        init = _timestamp(item['latest_advertised_init'])
        available = _timestamp(item['latest_advertised_available_at'])
        modified = _timestamp(item['latest_advertised_modified_at'])
        coverage = _timestamp(item['data_end_time'])
        for label, stamp in [('initialization', init), ('availability', available), ('modification', modified)]:
            _fresh(stamp, now, MAX_RUN_AGE, f'{name} {label}')
            _require(stamp <= fetched, 'metadata timestamp is after fetch')
        _require(init <= modified <= available, 'metadata timestamp order invalid')
        _require(init.hour in (0, 6, 12, 18) and init.minute == init.second == 0, 'GFS cycle is not synoptic')
        _require(times[-1] <= coverage <= init+timedelta(days=17), 'metadata forecast coverage insufficient or implausible')
        _require(_number(item['temporal_resolution_seconds'], 'metadata time resolution') == 3600 and
                 _number(item['update_interval_seconds'], 'metadata update interval') == 21600, 'metadata cadence mismatch')


def validate_gfs(source, now):
    """Fail closed, without throwing, including for persisted normalized data.

    A 12-hour fetch TTL and 24-hour latest-advertised init/availability TTL are
    conservative display policies, not claims of response-bound cycle freshness.
    """
    try:
        _validate(source, now)
        return {'available': True, 'error': ''}
    except Exception as exc:
        return {'available': False, 'error': f'GFS: {exc}'}


def collect_gfs(client, start, end, now):
    """Fetch [start,end) hourly UTC; isolate all source/network/validation failures."""
    if getattr(client, 'direct_native', False) is True:
        try:
            from .direct_deterministic import collect_native_profile, normalize_profile, validate_direct_data
            packet = collect_native_profile('gfs', start, end, now)
            data = normalize_profile(packet, start, end, 'gfs')
            validate_direct_data(data, datetime.now(UTC), 'gfs')
            return {'ok': True, 'data': data, 'error': ''}
        except Exception:
            pass
    try:
        now = _datetime(now)
        times = _axis(start, end)
        params = {'latitude': LAT, 'longitude': LON, 'models': MODEL_ID, 'hourly': ','.join(VARIABLES),
                  'wind_speed_unit': 'kn', 'temperature_unit': 'celsius', 'precipitation_unit': 'mm',
                  'timezone': 'GMT', 'timeformat': 'unixtime',
                  'start_hour': times[0].strftime('%Y-%m-%dT%H:%M'),
                  'end_hour': times[-1].strftime('%Y-%m-%dT%H:%M')}
        raw = client.get(ENDPOINT + '?' + urlencode(params))
        _require(isinstance(raw, dict), 'forecast response must be an object')
        _require(raw.get('timezone') == 'GMT' and _number(raw.get('utc_offset_seconds'), 'UTC offset') == 0,
                 'forecast response is not UTC')
        # The single-model API normally omits model ID. Reject an explicit conflicting echo.
        for key in ('model', 'model_id', 'models'):
            _require(key not in raw or raw[key] == MODEL_ID, 'forecast model identity mismatch')
        hourly = raw['hourly']
        for stamp in hourly['time']:
            _number(stamp, 'forecast time')
        _series(hourly, raw['hourly_units'], [int(t.timestamp()) for t in times], 'unixtime')
        grid = {'latitude': raw['latitude'], 'longitude': raw['longitude'],
                'requested_latitude': LAT, 'requested_longitude': LON}
        _grid(grid)
        datasets = {}
        for name, url in METADATA_URLS.items():
            meta = client.get(url)
            item = {'endpoint': url, 'fetched_at': iso_z(now)}
            for target, field in [('latest_advertised_init', 'last_run_initialisation_time'),
                                  ('latest_advertised_available_at', 'last_run_availability_time'),
                                  ('latest_advertised_modified_at', 'last_run_modification_time'),
                                  ('data_end_time', 'data_end_time')]:
                stamp = _number(meta[field], field)
                _require(stamp == int(stamp), 'metadata epoch must use whole seconds')
                item[target] = iso_z(datetime.fromtimestamp(stamp, UTC))
            for field in ('temporal_resolution_seconds', 'update_interval_seconds'):
                item[field] = meta[field]
            datasets[name] = item
        data = {'model_id': MODEL_ID, 'model': MODEL, 'endpoint': ENDPOINT, 'fetched_at': iso_z(now),
                'requested_start': iso_z(times[0]), 'requested_end': iso_z(times[-1]+timedelta(hours=1)),
                'grid_point': grid, 'hourly_units': dict(UNITS, time='iso8601'),
                'hourly': dict(hourly, time=[iso_z(t) for t in times]),
                'metadata': {'provenance': PROVENANCE, 'provenance_limits': PROVENANCE_LIMIT,
                             'model_init_is_response_bound': False, 'datasets': datasets,
                             'sampling': SAMPLING, 'documentation': 'https://open-meteo.com/en/docs/gfs-api',
                             'precipitation_semantics': 'preceding-hour total',
                             'null_semantics': 'missing/unknown; never clear weather'}}
        source = {'ok': True, 'data': data, 'error': ''}
        if getattr(client, 'direct_native', False) is True:
            from .direct_deterministic import FALLBACK
            data['metadata']['direct_fallback_reason'] = FALLBACK
        _validate(source, now)
        return source
    except Exception as exc:
        return {'ok': False, 'data': None, 'error': f'GFS: {exc}'}
