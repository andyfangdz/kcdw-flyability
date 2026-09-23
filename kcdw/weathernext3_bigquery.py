"""Bounded WeatherNext 3 BigQuery transport and local run cache."""
from __future__ import annotations

import json
import math
import re
import time
import uuid
import os
import fcntl
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .weathernext3_zarr import KCDW, parse_utc, run_name

DEFAULT_TABLE = "866962084172.WeatherNext_3.weathernext_3_0_0_0p1deg"
SOURCE_URL = "https://developers.google.com/weathernext/guides/bigquery"


def initialization(run):
    """BigQuery supports whole-hour inits, including the shorter interim runs."""
    init = parse_utc(run)
    if init.minute or init.second or init.microsecond:
        raise ValueError('run must be a whole UTC hour')
    return init


def horizon_hours(run):
    return 360 if initialization(run).hour % 6 == 0 else 48


def request_body(table, run, valid, arrays, maximum_bytes, execute=False):
    from .weathernext3 import FIELD_SPECS, OPTIONAL_FIELD_SPECS, STATISTICS
    ARRAYS = {f"{spec.array}_{stat}" for spec in (FIELD_SPECS | OPTIONAL_FIELD_SPECS).values() for stat in STATISTICS}
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", table):
        raise ValueError("invalid table identifier")
    init = initialization(run)
    horizon = horizon_hours(run)
    times = sorted(set(parse_utc(v) for v in valid))
    if not times or any(not 1 <= (v-init).total_seconds()/3600 <= horizon or
                        (v-init).total_seconds() % 3600 for v in times):
        raise ValueError(f"valid times must be hourly leads 1..{horizon}")
    if not arrays or not set(arrays) <= ARRAYS or maximum_bytes <= 0:
        raise ValueError("invalid arrays or byte cap")
    # This probe targets the documented regular 0.1-degree grid. Query the
    # nearest center directly, without carrying entire forecast records through
    # a window function. Python rejects duplicate/missing rows or wrong centers.
    columns = ", ".join("f." + a for a in sorted(set(arrays)))
    sql = f"""SELECT TO_JSON_STRING(STRUCT(
  t.init_time AS init_time, ST_Y(t.geography) AS latitude,
  ST_X(t.geography) AS longitude, f.time AS valid_time, f.hours AS hours,
  {columns})) AS point_json
FROM `{table}` t CROSS JOIN UNNEST(t.forecast) f
WHERE t.init_time = @init
  AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)
  AND f.time IN UNNEST(@valid)
ORDER BY f.time"""
    def scalar(name, kind, value):
        return dict(name=name, parameterType={"type": kind}, parameterValue={"value": str(value)})
    return dict(query=sql, useLegacySql=False, dryRun=not execute,
                maximumBytesBilled=str(maximum_bytes), location="US", timeoutMs=10000,
                maxResults=360, parameterMode="NAMED", queryParameters=[
                    scalar("init", "TIMESTAMP", init.isoformat()),
                    scalar("longitude", "FLOAT64", round(KCDW[1], 1)),
                    scalar("latitude", "FLOAT64", round(KCDW[0], 1)),
                    dict(name="valid", parameterType={"type": "ARRAY", "arrayType": {"type": "TIMESTAMP"}},
                         parameterValue={"arrayValues": [{"value": v.isoformat()} for v in times]})])


def validate_rows(rows, run, valid, arrays):
    from .weathernext3 import FIELD_SPECS, OPTIONAL_FIELD_SPECS, STATISTICS
    init = parse_utc(run)
    expected = sorted(set(parse_utc(v) for v in valid))
    if [parse_utc(r["valid_time"]) for r in rows] != expected:
        raise ValueError("missing, duplicate, or unordered forecast hours")
    grids = set()
    for row in rows:
        if parse_utc(row["init_time"]) != init or row["hours"] != (parse_utc(row["valid_time"])-init).total_seconds()/3600:
            raise ValueError("initialization/lead mismatch")
        lat, lon = row["latitude"], row["longitude"]
        if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat-round(KCDW[0], 1)) <= 1e-5 and abs(lon-round(KCDW[1], 1)) <= 1e-5):
            raise ValueError("invalid grid point")
        grids.add((lat, lon))
        for spec in (FIELD_SPECS | OPTIONAL_FIELD_SPECS).values():
            for stat in STATISTICS:
                name = f"{spec.array}_{stat}"
                if name not in arrays:
                    continue
                value = row.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not spec.low <= spec.convert(value) <= spec.high:
                    raise ValueError(f"invalid value: {name}")
            lo, hi = f"{spec.array}_p10", f"{spec.array}_p90"
            if lo in arrays and hi in arrays and row[lo] > row[hi]:
                raise ValueError("reversed percentile band")
    if len(grids) != 1:
        raise ValueError("mixed grid points")


def query(session, project, body):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project):
        raise ValueError("invalid billing project")
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}"
    def decode(response):
        data = response.json()
        if response.status_code != 200 or data.get("errors"):
            raise RuntimeError(json.dumps(data.get("error") or data.get("errors")))
        return data
    body = dict(body, requestId=str(uuid.uuid4()))
    data = decode(session.post(base + "/queries", json=body, timeout=35))
    if body["dryRun"]:
        return {"dry_run": True, "estimated_bytes": data.get("totalBytesProcessed"), "rows": []}
    ref = data["jobReference"]
    params = {"location": ref["location"], "timeoutMs": 10000, "maxResults": 360}
    rows = []
    deadline = time.monotonic() + 180
    while True:
        if time.monotonic() > deadline:
            session.post(base + f"/jobs/{ref['jobId']}/cancel", params={"location": ref["location"]}, timeout=30)
            raise TimeoutError(f"BigQuery deadline exceeded; cancellation requested for {ref['jobId']}")
        if data.get("jobComplete"):
            rows.extend(json.loads(r["f"][0]["v"]) for r in data.get("rows", []))
            if not data.get("pageToken"):
                break
            params["pageToken"] = data["pageToken"]
        data = decode(session.get(base + f"/queries/{ref['jobId']}", params=params, timeout=35))
    job = decode(session.get(base + f"/jobs/{ref['jobId']}", params={"location": ref["location"]}, timeout=30))
    if job.get("status", {}).get("errorResult"):
        raise RuntimeError("BigQuery job failed")
    return {"dry_run": False, "job": ref, "statistics": job.get("statistics", {}).get("query", {}), "rows": rows}


def validate_provenance(value):
    if not isinstance(value, dict) or set(value) != {
        'table', 'project', 'job_id', 'location', 'bytes_processed', 'bytes_billed',
        'query_cache_hit', 'local_cache_hit', 'retrieved_at'}:
        raise ValueError('invalid BigQuery provenance')
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.weathernext_3_0_0_0p1deg', value['table']):
        raise ValueError('invalid BigQuery table')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value['project']) or not re.fullmatch(r'[A-Za-z0-9_-]+', value['job_id']):
        raise ValueError('invalid BigQuery job identity')
    if value['location'] != 'US' or any(type(value[k]) is not int or value[k] < 0 for k in ('bytes_processed', 'bytes_billed')):
        raise ValueError('invalid BigQuery usage')
    if any(type(value[k]) is not bool for k in ('query_cache_hit', 'local_cache_hit')):
        raise ValueError('invalid BigQuery cache flags')
    parse_utc(value['retrieved_at'])


class BigQueryStore:
    """One complete run per cache entry; no GCS fallback.

    Cache entries retain original retrieval/job provenance. File locking avoids
    duplicate jobs across the report and history processes. Only complete,
    validated runs are cached. Cache age never overrides caller freshness.
    """
    def __init__(self, *, session=None, project=None, table=None, cache_dir=None):
        self.project = project or os.environ.get('WN3_BIGQUERY_PROJECT', 'aviation-486817')
        self.table = table or os.environ.get('WN3_BIGQUERY_TABLE', DEFAULT_TABLE)
        self.cap = int(os.environ.get('WN3_BIGQUERY_MAX_BYTES_BILLED', str(8 * 1024**4)))
        self.cache_dir = Path(cache_dir or os.environ.get('WN3_BIGQUERY_CACHE_DIR', 'var/wn3-bigquery'))
        self.session = session

    def _session(self):
        if self.session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            self.session = AuthorizedSession(credentials)
        return self.session

    def candidates(self, now, *, include_interim=False):
        """Discover published runs; interim 48-hour cycles are explicitly opt-in."""
        lower = now - timedelta(hours=24)
        limit = 25 if include_interim else 5
        cycle_filter = '' if include_interim else 'AND MOD(EXTRACT(HOUR FROM t.init_time), 6) = 0'
        body = request_body(self.table, now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                            [(now.replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(hours=1)).isoformat()],
                            ['temperature_2m_mean'], min(self.cap, 32*1024**3), True)
        body['query'] = f'''SELECT TO_JSON_STRING(STRUCT(t.init_time AS init_time))
FROM `{self.table}` t
WHERE t.init_time >= @lower AND t.init_time <= @upper
  {cycle_filter}
  AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)
ORDER BY t.init_time DESC LIMIT {limit}'''
        body['queryParameters'] = [p for p in body['queryParameters'] if p['name'] in ('longitude', 'latitude')]
        body['queryParameters'] += [dict(name=k, parameterType={'type':'TIMESTAMP'}, parameterValue={'value':v.isoformat()})
                                    for k,v in [('lower', lower), ('upper', now)]]
        result = query(self._session(), self.project, body)
        runs = [initialization(r['init_time']) if include_interim else run_name(r['init_time'])[1]
                for r in result['rows']]
        if len(runs) > limit or runs != sorted(set(runs), reverse=True) or any(not lower <= r <= now for r in runs):
            raise ValueError('invalid BigQuery run discovery')
        return runs

    def fetch(self, run, arrays=None):
        from .weathernext3 import FIELD_SPECS, STATISTICS
        arrays = sorted(set(arrays or [f'{s.array}_{stat}' for s in FIELD_SPECS.values() for stat in STATISTICS]))
        init = initialization(run)
        valid = [(init+timedelta(hours=h)).isoformat() for h in range(1, horizon_hours(run)+1)]
        body = request_body(self.table, run, valid, arrays, self.cap, True)
        identity = dict(table=self.table, run=init.isoformat(), arrays=arrays, version=1)
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.cache_dir / (key+'.json')
        with (self.cache_dir / (key+'.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if path.stat().st_size > 3_000_000:
                    raise ValueError('oversized cache')
                cached = json.loads(path.read_text())
                if cached['identity'] != identity:
                    raise ValueError('cache identity mismatch')
                validate_rows(cached['rows'], run, valid, arrays)
                validate_provenance(cached['provenance'])
                provenance = cached['provenance']
                retrieved = parse_utc(provenance['retrieved_at'])
                if provenance['table'] != self.table or not init <= retrieved <= datetime.now(timezone.utc)+timedelta(minutes=5):
                    raise ValueError('invalid cached provenance')
                # Refresh recent runs periodically in case provider data changes.
                if datetime.now(timezone.utc)-retrieved > timedelta(hours=6) and datetime.now(timezone.utc)-init < timedelta(days=2):
                    raise ValueError('cache refresh due')
                provenance['local_cache_hit'] = True
                return cached
            except (OSError, ValueError, TypeError, KeyError):
                pass
            result = query(self._session(), self.project, body)
            if not result['rows']:
                raise LookupError('BigQuery run not published')
            validate_rows(result['rows'], run, valid, arrays)
            stats, job = result['statistics'], result['job']
            provenance = dict(table=self.table, project=job['projectId'], job_id=job['jobId'], location=job['location'],
                              bytes_processed=int(stats['totalBytesProcessed']), bytes_billed=int(stats['totalBytesBilled']),
                              query_cache_hit=stats['cacheHit'], local_cache_hit=False,
                              retrieved_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
            validate_provenance(provenance)
            cached = dict(identity=identity, rows=result['rows'], provenance=provenance)
            fd, temporary = tempfile.mkstemp(dir=self.cache_dir, prefix='.run-')
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump(cached, handle, allow_nan=False)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            # Bound weather cache to 128 entries; locks are tiny and reusable.
            entries = []
            for old in self.cache_dir.glob('*.json'):
                try:
                    entries.append((old.stat().st_mtime, old))
                except FileNotFoundError:
                    pass
            for _, old in sorted(entries, reverse=True)[128:]:
                try:
                    old.unlink()
                except FileNotFoundError:
                    pass
            return cached
