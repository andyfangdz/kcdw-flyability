"""WeatherNext 2 member screens for the expected flight window.

Production uses run-bound BigQuery packets (version 2). The legacy collector and
version 1 validator below remain for archived Open-Meteo packets only.

Open-Meteo serves all 64 WeatherNext 2 members; the legacy comparator keeps only
their mean and standard deviation. This packet reduces the members to counts and
quantiles at the model's native six-hour valid times that bracket the flight,
plus window rain counts. WeatherNext 2 has no gust field: 100 m wind is reported
as context, never as a gust. The rolling response is not bound to a model run;
the advertised initialization is recorded as advertised only.
"""
from __future__ import annotations

import math
import re
import urllib.parse
from datetime import datetime, timedelta

from .common import UTC, iso_z, parse_time
from .event_model_matrix import flight_window

VERSION = 1
ENDPOINT = 'https://ensemble-api.open-meteo.com/v1/ensemble'
METADATA = 'https://ensemble-api.open-meteo.com/data/google_weathernext2_ensemble/static/meta.json'
MODEL_ID = 'google_weathernext2_ensemble'
MEMBERS = 64
MIN_MEMBERS = 48
NATIVE_STEP = timedelta(hours=6)
VARIABLES = {'wind_speed_10m': ('kn', 0, 300), 'wind_direction_10m': ('°', 0, 360), 'wind_speed_100m': ('kn', 0, 300),
             'cloud_cover_low': ('%', 0, 100), 'precipitation': ('mm', 0, 500), 'pressure_msl': ('hPa', 750, 1150)}
THRESHOLDS = {'cloudy_pct': 75, 'rain_mm': 1.0, 'sector_from_deg': 20, 'sector_to_deg': 70}
MEMBER_KEY = re.compile(r'^([a-z_0-9]+?)(?:_member(\d{2}))?$')


def sample_times(start: datetime, end: datetime) -> list[datetime]:
    """Native six-hour valid times from the step at or before the flight start to the step at or after its end."""
    first = start.replace(minute=0, second=0, microsecond=0) - timedelta(hours=start.hour % 6)
    times = [first]
    while times[-1] < end:
        times.append(times[-1] + NATIVE_STEP)
    return times


def _quantile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100
    low, high = math.floor(position), math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 1)


def _fan(values):
    return {'n': len(values), 'p10': _quantile(values, 10), 'p50': _quantile(values, 50), 'p90': _quantile(values, 90)}


def _members(hourly: dict, units: dict) -> dict[str, dict[int, list]]:
    grouped = {name: {} for name in VARIABLES}
    for key, column in hourly.items():
        if key == 'time':
            continue
        match = MEMBER_KEY.fullmatch(key)
        if not match or match.group(1) not in VARIABLES or units.get(key) != VARIABLES[match.group(1)][0]:
            raise ValueError('unexpected WeatherNext 2 member field or unit')
        if not isinstance(column, list) or len(column) != len(hourly['time']):
            raise ValueError('WeatherNext 2 member column length mismatch')
        grouped[match.group(1)][int(match.group(2) or 0)] = column
    return grouped


def _values(columns: dict[int, list], index: int, name: str) -> list[float]:
    _, low, high = VARIABLES[name]
    values = []
    for column in columns.values():
        value = column[index]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('WeatherNext 2 member value out of bounds')
        values.append(float(value))
    if len(values) < MIN_MEMBERS:
        raise ValueError('too few WeatherNext 2 members')
    return values


def collect_legacy_members(client, snapshot: dict, now: datetime) -> dict | None:
    start, end, kind = flight_window(snapshot)
    if now.astimezone(UTC) >= end:
        return None
    times = sample_times(start, end)
    params = {'latitude': snapshot['airport']['latitude'], 'longitude': snapshot['airport']['longitude'], 'models': MODEL_ID,
              'hourly': ','.join(VARIABLES), 'start_hour': times[0].strftime('%Y-%m-%dT%H:%M'),
              'end_hour': times[-1].strftime('%Y-%m-%dT%H:%M'), 'timezone': 'GMT', 'timeformat': 'unixtime',
              'wind_speed_unit': 'kn', 'precipitation_unit': 'mm'}
    url = ENDPOINT + '?' + urllib.parse.urlencode(params)
    raw = client.get(url)
    if not isinstance(raw, dict) or raw.get('utc_offset_seconds') != 0:
        raise ValueError('WeatherNext 2 member response must be UTC')
    if abs(raw['latitude'] - params['latitude']) > .5 or abs(raw['longitude'] - params['longitude']) > .5:
        raise ValueError('WeatherNext 2 grid mismatch')
    hourly = raw['hourly']
    hours = int((times[-1] - times[0]).total_seconds() // 3600)
    if hourly['time'] != [int((times[0] + timedelta(hours=h)).timestamp()) for h in range(hours + 1)]:
        raise ValueError('WeatherNext 2 member time axis mismatch')
    grouped = _members(hourly, raw['hourly_units'])
    if any(len(columns) > MEMBERS for columns in grouped.values()):
        raise ValueError('unexpected WeatherNext 2 member count')
    axis = {times[0] + timedelta(hours=h): h for h in range(hours + 1)}
    sector = lambda d: THRESHOLDS['sector_from_deg'] <= d % 360 <= THRESHOLDS['sector_to_deg']
    samples = []
    for moment in times:
        i = axis[moment]
        cloud, direction = _values(grouped['cloud_cover_low'], i, 'cloud_cover_low'), _values(grouped['wind_direction_10m'], i, 'wind_direction_10m')
        samples.append({'at': iso_z(moment), 'wind_10m_kt': _fan(_values(grouped['wind_speed_10m'], i, 'wind_speed_10m')),
                        'wind_100m_kt': _fan(_values(grouped['wind_speed_100m'], i, 'wind_speed_100m')),
                        'pressure_hpa': _fan(_values(grouped['pressure_msl'], i, 'pressure_msl')),
                        'low_cloud_pct': _fan(cloud),
                        'cloudy': {'count': sum(v >= THRESHOLDS['cloudy_pct'] for v in cloud), 'n': len(cloud)},
                        'sector': {'count': sum(sector(v) for v in direction), 'n': len(direction)}})
    rain_hours = [axis[t] for t in axis if start < t <= end]
    totals = []
    for column in grouped['precipitation'].values():
        picked = [column[i] for i in rain_hours]
        if all(v is not None for v in picked):
            totals.append(float(sum(picked)))
    if len(totals) < MIN_MEMBERS or any(not math.isfinite(v) or v < 0 for v in totals):
        raise ValueError('too few WeatherNext 2 rain members')
    metadata = client.get(METADATA)
    advertised = datetime.fromtimestamp(int(metadata['last_run_initialisation_time']), UTC)
    if int(metadata['temporal_resolution_seconds']) != 21_600 or advertised > now.astimezone(UTC) + timedelta(minutes=5):
        raise ValueError('unexpected WeatherNext 2 member metadata')
    return {'version': VERSION, 'model_id': MODEL_ID, 'members': MEMBERS, 'snapshot_collected_at': snapshot['collected_at'],
            'fetched_at': iso_z(datetime.now(UTC) if getattr(client, 'direct_native', False) else now),
            'advertised_init': iso_z(advertised), 'run_binding': 'latest-advertised; not response-bound',
            'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind}, 'thresholds': dict(THRESHOLDS),
            'grid_point': {'latitude': raw['latitude'], 'longitude': raw['longitude']}, 'samples': samples,
            'rain': {'count': sum(v >= THRESHOLDS['rain_mm'] for v in totals), 'n': len(totals), 'p90_mm': _quantile(totals, 90)},
            'source_url': url, 'license': 'CC BY 4.0'}


def validate_members(packet, snapshot: dict) -> dict:
    def require(condition):
        if not condition:
            raise ValueError('invalid WeatherNext 2 member packet')
    if isinstance(packet, dict) and packet.get('version') == 2:
        return validate_bigquery_members(packet, snapshot)
    require(isinstance(packet, dict) and packet.get('version') == VERSION and packet.get('model_id') == MODEL_ID)
    require(packet.get('members') == MEMBERS and packet.get('snapshot_collected_at') == snapshot['collected_at'])
    require(packet.get('thresholds') == THRESHOLDS and packet.get('run_binding') == 'latest-advertised; not response-bound')
    start, end, kind = flight_window(snapshot)
    require(packet.get('window') == {'start': iso_z(start), 'end': iso_z(end), 'kind': kind})
    require(parse_time(packet['advertised_init']) <= parse_time(packet['fetched_at']) + timedelta(minutes=5))
    samples = packet.get('samples')
    require(isinstance(samples, list) and [s.get('at') if isinstance(s, dict) else None for s in samples] == [iso_z(t) for t in sample_times(start, end)])
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    def count(value):
        require(isinstance(value, dict) and type(value.get('count')) is int and type(value.get('n')) is int)
        require(MIN_MEMBERS <= value['n'] <= MEMBERS and 0 <= value['count'] <= value['n'])

    for sample in samples:
        for name, low, high in (('wind_10m_kt', 0, 300), ('wind_100m_kt', 0, 300), ('pressure_hpa', 750, 1150), ('low_cloud_pct', 0, 100)):
            fan = sample.get(name)
            require(isinstance(fan, dict) and MIN_MEMBERS <= fan.get('n', 0) <= MEMBERS)
            require(all(number(fan.get(q)) for q in ('p10', 'p50', 'p90')) and low <= fan['p10'] <= fan['p50'] <= fan['p90'] <= high)
        count(sample.get('cloudy'))
        count(sample.get('sector'))
    count(packet.get('rain'))
    require(number(packet['rain'].get('p90_mm')) and 0 <= packet['rain']['p90_mm'] <= 500)
    grid = packet.get('grid_point')
    require(isinstance(grid, dict) and number(grid.get('latitude')) and number(grid.get('longitude')))
    return packet


BQ_THRESHOLDS = {k: v for k, v in THRESHOLDS.items() if k != 'cloudy_pct'}


def collect_members(client, snapshot: dict, now: datetime, *, store=None) -> dict | None:
    """Collect native, run-bound members; WN3 supplies primary cloud guidance."""
    from .weathernext2_bigquery import BigQueryStore, SOURCE_URL, validate_rows
    start, end, kind = flight_window(snapshot)
    if now.astimezone(UTC) >= end:
        return None
    times = sample_times(start, end)
    lat, lon = snapshot['airport']['latitude'], snapshot['airport']['longitude']
    store = store or BigQueryStore()
    runs = store.candidates(now, lat, lon)
    if not runs:
        raise ValueError('no fresh WN2 run available')
    init = runs[0]
    if not timedelta(0) <= now-init <= timedelta(hours=24):
        raise ValueError('stale or future WN2 run')
    data = store.fetch(init, times, lat, lon)
    validate_rows(data['rows'], init, times, lat, lon)
    samples, rain_totals = [], {str(m): 0.0 for m in range(MEMBERS)}
    for row in data['rows']:
        members = row['members']
        wind = lambda m, height: math.hypot(m['u'+height], m['v'+height]) * 1.9438444924406
        # Meteorological direction is the direction the wind comes FROM.
        directions = [(math.degrees(math.atan2(-m['u10'], -m['v10'])) % 360)
                      for m in members if math.hypot(m['u10'], m['v10']) > 0]
        samples.append({'at': iso_z(parse_time(row['valid_time'])),
                        'wind_10m_kt': _fan([wind(m, '10') for m in members]),
                        'wind_100m_kt': _fan([wind(m, '100') for m in members]),
                        'pressure_hpa': _fan([m['pressure']/100 for m in members]),
                        'sector': {'count': sum(BQ_THRESHOLDS['sector_from_deg'] <= d <= BQ_THRESHOLDS['sector_to_deg'] for d in directions), 'n': MEMBERS}})
        # Each value is the preceding six hours. Include only intervals that
        # overlap the flight, and label the complete enclosing interval.
        at = parse_time(row['valid_time'])
        if at > start and at-NATIVE_STEP < end:
            for m in members:
                # Small negative neural precipitation predictions are clipped
                # to zero; raw values are bounded and retained in the cache.
                rain_totals[m['member']] += max(0.0, m['rain']) * 1000
    totals = list(rain_totals.values())
    packet = {'version': 2, 'model_id': MODEL_ID, 'members': MEMBERS,
              'snapshot_collected_at': snapshot['collected_at'], 'fetched_at': iso_z(now),
              'init_time': iso_z(init), 'run_binding': 'BigQuery init_time',
              'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind},
              'thresholds': dict(BQ_THRESHOLDS), 'grid_point': {'latitude': data['rows'][0]['latitude'], 'longitude': data['rows'][0]['longitude']},
              'samples': samples, 'rain': {'count': sum(v >= BQ_THRESHOLDS['rain_mm'] for v in totals), 'n': MEMBERS,
                                         'p90_mm': _quantile(totals, 90), 'start': iso_z(times[0]), 'end': iso_z(times[-1])},
              'source_url': SOURCE_URL, 'license': 'CC BY 4.0', 'bigquery': data['provenance']}
    return validate_bigquery_members(packet, snapshot)


def validate_bigquery_members(packet, snapshot):
    from .weathernext2_bigquery import SOURCE_URL, grid, validate_provenance
    def require(condition):
        if not condition:
            raise ValueError('invalid run-bound WN2 member packet')
    start, end, kind = flight_window(snapshot)
    times = sample_times(start, end)
    require(packet.get('version') == 2 and packet.get('model_id') == MODEL_ID and packet.get('members') == MEMBERS)
    require(packet.get('snapshot_collected_at') == snapshot['collected_at'])
    require(packet.get('window') == {'start': iso_z(start), 'end': iso_z(end), 'kind': kind})
    require(packet.get('thresholds') == BQ_THRESHOLDS and packet.get('run_binding') == 'BigQuery init_time')
    require(packet.get('source_url') == SOURCE_URL and packet.get('license') == 'CC BY 4.0')
    init, fetched = parse_time(packet['init_time']), parse_time(packet['fetched_at'])
    require(timedelta(0) <= fetched-init <= timedelta(hours=24) and abs(fetched-parse_time(snapshot['collected_at'])) <= timedelta(minutes=20))
    require(init.hour % 6 == 0 and init.minute == init.second == init.microsecond == 0)
    require(all(6 <= (t-init).total_seconds()/3600 <= 360 and (t-init).total_seconds() % 21600 == 0 for t in times))
    lat, lon = grid(snapshot['airport']['latitude'], snapshot['airport']['longitude'])
    require(packet.get('grid_point') == {'latitude': lat, 'longitude': lon})
    validate_provenance(packet['bigquery'])
    samples = packet['samples']
    require([s['at'] for s in samples] == [iso_z(t) for t in times])
    number = lambda v: type(v) in (int, float) and math.isfinite(v)
    def count(c):
        require(type(c.get('count')) is int and type(c.get('n')) is int and c['n'] == MEMBERS and 0 <= c['count'] <= MEMBERS)
    for sample in samples:
        require('cloudy' not in sample and 'low_cloud_pct' not in sample)
        for name, low, high in (('wind_10m_kt', 0, 300), ('wind_100m_kt', 0, 300), ('pressure_hpa', 750, 1150)):
            fan = sample[name]
            require(type(fan.get('n')) is int and fan['n'] == MEMBERS)
            require(all(number(fan.get(q)) for q in ('p10', 'p50', 'p90')) and low <= fan['p10'] <= fan['p50'] <= fan['p90'] <= high)
        count(sample['sector'])
    rain = packet['rain']
    count(rain)
    require(rain.get('start') == iso_z(times[0]) and rain.get('end') == iso_z(times[-1]))
    require(number(rain.get('p90_mm')) and 0 <= rain['p90_mm'] <= 500*(len(times)-1))
    return packet
