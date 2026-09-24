"""WeatherNext 3 7-day forecasts for a flight window, from BigQuery, one run per query.

WN3 has no gust, but it adds sustained wind with spread and low cloud, which no
other archive keeps. For each past date the 12Z run seven days earlier is read at
the flight-window hours for the grid point nearest KCDW. A multi-run query dry-runs
at hundreds of petabytes, so only the per-run pattern proven in production is used,
with a cost guard: stop when any query bills more than MAX_QUERY_BYTES or the run
total exceeds MAX_TOTAL_BYTES. Results are cached per date permanently. The
archive starts in January 2026.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import UTC, atomic_write, iso_z, parse_time
from .events import TZ

FIRST_RUN = date(2026, 1, 1)
LEAD_DAYS = 7
MAX_QUERY_BYTES = 200 * 1024 ** 2
MAX_TOTAL_BYTES = 20 * 1024 ** 3
POINT = (-74.3, 40.9)
KNOTS = 3600 / 1852
RUNWAYS = (30, 83)
COLUMNS = ('wind_speed_10m_mean', 'wind_speed_10m_p90', 'u_component_of_wind_10m_mean', 'v_component_of_wind_10m_mean',
           'low_cloud_cover_mean', 'low_cloud_cover_p90', 'total_precipitation_1hr_mean')


def _xw(speed, direction):
    return min(speed * abs(math.sin(math.radians(direction - h))) for h in RUNWAYS)


def window_utc(day, start_local, end_local):
    """Hourly UTC samples of the local flight window on ``day`` (inclusive)."""
    clock = lambda hm: datetime.combine(day, datetime.strptime(hm, '%H:%M').time(), TZ).astimezone(UTC)
    start, end = clock(start_local), clock(end_local)
    return [start + timedelta(hours=h) for h in range(int((end - start).total_seconds() // 3600) + 1)]


def run_for(day):
    return datetime.combine(day - timedelta(days=LEAD_DAYS), datetime.min.time(), UTC).replace(hour=12)


def cache_path(var, start_local):
    return Path(var) / 'wn3-climatology' / f'kcdw-{start_local.replace(":", "")}.json'


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('days'), dict) else {'days': {}}
    except (OSError, ValueError):
        return {'days': {}}


def summarize(rows, hours):
    """Window summary from WN3 rows (native units); None unless every hour is present."""
    by_hour = {r['hours']: r for r in rows}
    if sorted(by_hour) != sorted(hours):
        return None
    top = max(by_hour.values(), key=lambda r: r['wind_speed_10m_mean'])
    sust = top['wind_speed_10m_mean'] * KNOTS
    direction = math.degrees(math.atan2(-top['u_component_of_wind_10m_mean'], -top['v_component_of_wind_10m_mean'])) % 360
    return {'sust_kt': round(sust, 2), 'sust_p90_kt': round(max(r['wind_speed_10m_p90'] for r in rows) * KNOTS, 2),
            'from_deg': round(direction, 1), 'low_cloud': round(max(r['low_cloud_cover_mean'] for r in rows) * 100, 1),
            'low_cloud_p90': round(max(r['low_cloud_cover_p90'] for r in rows) * 100, 1),
            'rain_mm': round(sum(r['total_precipitation_1hr_mean'] for r in rows[1:]) * 1000, 2)}


def _query(session, project, table, run, hours):
    from .weathernext3_bigquery import query
    columns = ', '.join(f'f.{c}' for c in COLUMNS)
    sql = f"""SELECT TO_JSON_STRING(STRUCT(t.init_time AS init, f.hours AS hours, {columns})) AS point_json
FROM `{table}` t CROSS JOIN UNNEST(t.forecast) f
WHERE t.init_time = @init AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100) AND f.hours IN UNNEST(@hours)
ORDER BY f.hours"""
    scalar = lambda name, kind, value: dict(name=name, parameterType={'type': kind}, parameterValue={'value': str(value)})
    body = dict(query=sql, useLegacySql=False, dryRun=False, location='US', timeoutMs=10000, maxResults=50,
                maximumBytesBilled=str(8 * 1024 ** 4), parameterMode='NAMED', queryParameters=[
                    scalar('init', 'TIMESTAMP', iso_z(run)), scalar('longitude', 'FLOAT64', POINT[0]), scalar('latitude', 'FLOAT64', POINT[1]),
                    dict(name='hours', parameterType={'type': 'ARRAY', 'arrayType': {'type': 'INT64'}},
                         parameterValue={'arrayValues': [{'value': str(h)} for h in hours]})])
    result = query(session, project, body)
    return result['rows'], int(result['statistics'].get('totalBytesBilled') or 0)


def update(path, start_local, end_local, first, last, now, limit=None, runner=None):
    """Fill missing dates newest first. ``runner(run, hours) -> (rows, billed_bytes)``."""
    if runner is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from .weathernext3_bigquery import DEFAULT_TABLE
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        session = AuthorizedSession(credentials)
        runner = lambda run, hours: _query(session, 'aviation-486817', DEFAULT_TABLE, run, hours)
    cache = load(path)
    days, spent, done = cache['days'], 0, 0
    d = last
    while d >= max(first, FIRST_RUN + timedelta(days=LEAD_DAYS)) and (limit is None or done < limit):
        key = d.isoformat()
        run = run_for(d)
        if key not in days and run + timedelta(hours=8) <= now:
            hours = [int((t - run).total_seconds() // 3600) for t in window_utc(d, start_local, end_local)]
            rows, billed = runner(run, hours)
            spent += billed
            if billed > MAX_QUERY_BYTES or spent > MAX_TOTAL_BYTES:
                raise RuntimeError(f'WN3 climatology cost guard: {billed} bytes this query, {spent} total')
            value = summarize(rows, hours)
            days[key] = dict(value, init=iso_z(run)) if value else {'missing': True, 'init': iso_z(run)}
            done += 1
            if done % 10 == 0:
                atomic_write(path, json.dumps(cache, separators=(',', ':'), sort_keys=True) + '\n')
        d -= timedelta(days=1)
    cache.update(start_local=start_local, end_local=end_local, updated_at=iso_z(now))
    cache['billed_bytes'] = cache.get('billed_bytes', 0) + spent
    atomic_write(path, json.dumps(cache, separators=(',', ':'), sort_keys=True) + '\n')
    return cache


def rows(cache, since=None):
    """[[date, sustained, None, crosswind, rain, low cloud, low cloud p90]] for dates with values."""
    out = []
    for key, e in sorted(cache['days'].items()):
        if e.get('missing') or (since and key < since):
            continue
        out.append([key, round(e['sust_kt'], 1), None, round(_xw(e['sust_kt'], e['from_deg']), 1), round(e['rain_mm'], 1),
                    round(e['low_cloud'], 1), round(e['low_cloud_p90'], 1)])
    return out


def current(snapshot, start_local, end_local):
    """The snapshot's WN3 run summarized the same way for the event day, or None."""
    f = snapshot['weathernext3']['data']['forecast']
    F = f['fields']
    axis = {parse_time(t): i for i, t in enumerate(f['valid_time_utc'])}
    from .events import Event
    times = window_utc(Event(**snapshot['event']).day, start_local, end_local)
    if any(t not in axis for t in times):
        return None
    ix = [axis[t] for t in times]
    top = max(ix, key=lambda i: F['wind_speed_10m']['mean'][i])
    sust = F['wind_speed_10m']['mean'][top] * KNOTS
    direction = math.degrees(math.atan2(-F['u_component_of_wind_10m']['mean'][top], -F['v_component_of_wind_10m']['mean'][top])) % 360
    return [round(sust, 1), None, round(_xw(sust, direction), 1), round(sum(F['precipitation_1h']['mean'][i] for i in ix[1:]), 1),
            round(max(F['low_cloud_cover']['mean'][i] for i in ix), 1), round(max(F['low_cloud_cover']['p90'][i] for i in ix), 1)]
