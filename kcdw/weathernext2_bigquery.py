"""Run-bound WN2 members at native six-hour steps, with bounded point queries."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .common import UTC, parse_time
from .weathernext3_bigquery import query

TABLE = '871883017250.WeatherNext2.weathernext_2_0_0'
SOURCE_URL = 'https://developers.google.com/weathernext/guides/models-wn2'
FIELDS = {'u10': '10m_u_component_of_wind', 'v10': '10m_v_component_of_wind',
          'u100': '100m_u_component_of_wind', 'v100': '100m_v_component_of_wind',
          'pressure': 'mean_sea_level_pressure', 'rain': 'total_precipitation_6hr'}


def grid(latitude, longitude):
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (latitude, longitude)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError('invalid WN2 coordinates')
    return round(latitude * 4) / 4, round(longitude * 4) / 4


def request_body(init, times, latitude, longitude, cap, *, execute=True):
    if init.minute or init.second or init.microsecond or init.hour % 6 or not times or times != sorted(set(times)) or any(
            not 6 <= (t-init).total_seconds()/3600 <= 360 or (t-init).total_seconds() % 21600 for t in times):
        raise ValueError('invalid WN2 run or native lead times')
    lat, lon = grid(latitude, longitude)
    columns = ', '.join(f'e.`{field}` AS {alias}' for alias, field in FIELDS.items())
    sql = f'''SELECT TO_JSON_STRING(STRUCT(t.init_time, ST_Y(t.geography) AS latitude,
ST_X(t.geography) AS longitude, f.time AS valid_time, f.hours,
ARRAY(SELECT AS STRUCT e.ensemble_member AS member, {columns}
      FROM UNNEST(f.ensemble) e) AS members))
FROM `{TABLE}` t CROSS JOIN UNNEST(t.forecast) f
WHERE t.init_time = @init
AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)
AND f.time IN UNNEST(@valid) ORDER BY f.time'''
    scalar = lambda name, kind, value: dict(name=name, parameterType={'type': kind}, parameterValue={'value': str(value)})
    if type(cap) is not int or cap <= 0:
        raise ValueError('invalid WN2 billing cap')
    return dict(query=sql, useLegacySql=False, dryRun=not execute, maximumBytesBilled=str(cap), location='US',
                timeoutMs=10000, parameterMode='NAMED', queryParameters=[
                    scalar('init', 'TIMESTAMP', init.isoformat()), scalar('latitude', 'FLOAT64', lat),
                    scalar('longitude', 'FLOAT64', lon),
                    dict(name='valid', parameterType={'type': 'ARRAY', 'arrayType': {'type': 'TIMESTAMP'}},
                         parameterValue={'arrayValues': [{'value': t.isoformat()} for t in times]})])


def validate_rows(rows, init, times, latitude, longitude):
    lat, lon = grid(latitude, longitude)
    if [parse_time(r['valid_time']) for r in rows] != times:
        raise ValueError('missing, duplicate or unordered WN2 times')
    for row, at in zip(rows, times):
        if parse_time(row['init_time']) != init or row['hours'] != (at-init).total_seconds()/3600:
            raise ValueError('WN2 initialization/lead mismatch')
        if row['latitude'] != lat or row['longitude'] != lon:
            raise ValueError('WN2 grid mismatch')
        members = row['members']
        if len(members) != 64 or {m['member'] for m in members} != {str(i) for i in range(64)}:
            raise ValueError('WN2 requires all 64 distinct members')
        for member in members:
            for field in FIELDS:
                value = member[field]
                low, high = (75000, 115000) if field == 'pressure' else (-.001, .5) if field == 'rain' else (-150, 150)
                if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f'invalid WN2 {field}')


class BigQueryStore:
    def __init__(self, *, session=None, cache_dir=None):
        self.project = os.environ.get('WN2_BIGQUERY_PROJECT', 'aviation-486817')
        self.cap = int(os.environ.get('WN2_BIGQUERY_MAX_BYTES_BILLED', str(2 * 1024**4)))
        self.cache_dir = Path(cache_dir or os.environ.get('WN2_BIGQUERY_CACHE_DIR', 'var/wn2-bigquery'))
        self.session = session

    def _session(self):
        if self.session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            self.session = AuthorizedSession(credentials)
        return self.session

    def candidates(self, now, latitude, longitude):
        init = now.replace(hour=0, minute=0, second=0, microsecond=0)
        body = request_body(init, [init+timedelta(hours=6)], latitude, longitude, min(self.cap, 32*1024**3))
        body['query'] = f'''SELECT TO_JSON_STRING(STRUCT(t.init_time)) FROM `{TABLE}` t
WHERE t.init_time >= @lower AND t.init_time <= @upper
AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)
ORDER BY t.init_time DESC LIMIT 5'''
        body['queryParameters'] = [p for p in body['queryParameters'] if p['name'] in ('latitude', 'longitude')]
        body['queryParameters'] += [dict(name=k, parameterType={'type': 'TIMESTAMP'}, parameterValue={'value': v.isoformat()})
                                    for k, v in [('lower', now-timedelta(hours=24)), ('upper', now)]]
        runs = [parse_time(r['init_time']) for r in query(self._session(), self.project, body)['rows']]
        if runs != sorted(set(runs), reverse=True) or any(not now-timedelta(hours=24) <= r <= now or r.hour % 6 or r.minute or r.second or r.microsecond for r in runs):
            raise ValueError('invalid WN2 discovery')
        return runs

    def fetch(self, init, times, latitude, longitude):
        body = request_body(init, times, latitude, longitude, self.cap)
        identity = {'table': TABLE, 'init': init.isoformat(), 'times': [t.isoformat() for t in times],
                    'grid': grid(latitude, longitude), 'version': 1}
        # Canonical JSON also makes tuple/list comparison stable after decoding.
        identity = json.loads(json.dumps(identity))
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.cache_dir / (key+'.json')
        with (self.cache_dir / (key+'.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if path.stat().st_size > 3_000_000:
                    raise ValueError('oversized WN2 cache')
                cached = json.loads(path.read_text())
                if cached['identity'] != identity or not timedelta(0) <= datetime.now(UTC)-parse_time(cached['retrieved_at']) < timedelta(hours=6):
                    raise ValueError('WN2 cache refresh due')
                validate_rows(cached['rows'], init, times, latitude, longitude)
                validate_provenance(cached['provenance'])
                cached['provenance']['local_cache_hit'] = True
                return cached
            except (OSError, ValueError, TypeError, KeyError):
                pass
            result = query(self._session(), self.project, body)
            validate_rows(result['rows'], init, times, latitude, longitude)
            stats, job = result['statistics'], result['job']
            cached = dict(identity=identity, rows=result['rows'], retrieved_at=datetime.now(UTC).isoformat(),
                          provenance=dict(table=TABLE, project=job['projectId'], job_id=job['jobId'], location=job['location'],
                                          bytes_processed=int(stats['totalBytesProcessed']), bytes_billed=int(stats['totalBytesBilled']),
                                          query_cache_hit=stats['cacheHit'], local_cache_hit=False))
            validate_provenance(cached['provenance'])
            fd, temporary = tempfile.mkstemp(dir=self.cache_dir, prefix='.run-')
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump(cached, handle, allow_nan=False)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            for old in sorted(self.cache_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[128:]:
                old.unlink(missing_ok=True)
            return cached


def validate_provenance(value):
    import re
    if value.get('table') != TABLE or value.get('location') != 'US' or any(
            not isinstance(value.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value[k]) for k in ('project', 'job_id')):
        raise ValueError('invalid WN2 BigQuery identity')
    if any(type(value.get(k)) is not int or value[k] < 0 for k in ('bytes_processed', 'bytes_billed')) or any(
            type(value.get(k)) is not bool for k in ('query_cache_hit', 'local_cache_hit')):
        raise ValueError('invalid WN2 BigQuery usage')
