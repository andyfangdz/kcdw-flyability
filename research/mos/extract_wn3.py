"""WeatherNext 3 ensemble statistics at KCDW from BigQuery, one 00Z run per day (archive from 2026-01).

Uses the per-run point pattern proven in kcdw.wn3_climatology (~50-70 MB billed per run).
Writes var/mos/models/wn3/YYYY-MM.parquet in the extract_dynamical.py layout.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kcdw.weathernext3_bigquery import DEFAULT_TABLE, query  # noqa: E402

POINT = (-74.3, 40.9)
LEADS = list(range(1, 199))
COLUMNS = ('wind_speed_10m_mean', 'wind_speed_10m_p10', 'wind_speed_10m_p90', 'u_component_of_wind_10m_mean',
           'v_component_of_wind_10m_mean', 'temperature_2m_mean', 'low_cloud_cover_mean', 'low_cloud_cover_p90')
MAX_RUN_BYTES = 512 * 1024 ** 2
OUT = Path('var/mos/models/wn3')
KNOTS = 3600 / 1852


def body(init):
    columns = ', '.join(f'f.{c}' for c in COLUMNS)
    sql = f"""SELECT TO_JSON_STRING(STRUCT(f.hours AS hours, {columns})) AS point_json
FROM `{DEFAULT_TABLE}` t CROSS JOIN UNNEST(t.forecast) f
WHERE t.init_time = @init AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100) AND f.hours IN UNNEST(@hours)
ORDER BY f.hours"""
    scalar = lambda name, kind, value: dict(name=name, parameterType={'type': kind}, parameterValue={'value': str(value)})
    return dict(query=sql, useLegacySql=False, dryRun=False, location='US', timeoutMs=60000, maxResults=400,
                maximumBytesBilled=str(8 * 1024 ** 4), parameterMode='NAMED', queryParameters=[
                    scalar('init', 'TIMESTAMP', init.isoformat().replace('+00:00', 'Z')), scalar('longitude', 'FLOAT64', POINT[0]),
                    scalar('latitude', 'FLOAT64', POINT[1]),
                    dict(name='hours', parameterType={'type': 'ARRAY', 'arrayType': {'type': 'INT64'}},
                         parameterValue={'arrayValues': [{'value': str(h)} for h in LEADS]})])


def main(first='2026-01-01'):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    session = AuthorizedSession(credentials)
    OUT.mkdir(parents=True, exist_ok=True)
    cycles = [int(c) for c in os.environ.get('MOS_CYCLES', '0').split(',')]
    days = pd.date_range(first, datetime.now(timezone.utc).date(), freq='D', tz='UTC')
    inits = pd.DatetimeIndex(sorted(d + pd.Timedelta(hours=c) for d in days for c in cycles))
    inits = inits[inits <= pd.Timestamp.now(tz='UTC')]
    out = OUT if cycles == [0] else OUT.parent / (OUT.name + '_c' + ''.join(f'{c:02d}' for c in cycles))  # extra cycles kept apart
    out.mkdir(parents=True, exist_ok=True)
    current = pd.Timestamp.utcnow().strftime('%Y-%m')
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out / f'{month}.parquet'
        if path.exists() and month != current:
            continue
        records, billed_total, started = [], 0, time.time()
        # Incremental: runs already stored are never re-queried (BigQuery's result cache lasts about a day).
        previous = pd.read_parquet(path) if path.exists() else None
        done = set(pd.to_datetime(previous.init)) if previous is not None else set()
        for init in group:
            if init.tz_localize(None) in done:
                continue
            result = query(session, 'aviation-486817', body(init.to_pydatetime()))
            billed = int(result['statistics'].get('totalBytesBilled') or 0)
            if billed > MAX_RUN_BYTES:
                raise SystemExit(f'a run billed {billed / 2**20:.0f} MiB; stopping')
            billed_total += billed
            for row in result['rows']:
                rec = {'init': init.tz_localize(None), 'lead_h': int(row['hours'])}
                rec['speed_10m_mean'] = row['wind_speed_10m_mean'] * KNOTS
                rec['speed_10m_p10'] = row['wind_speed_10m_p10'] * KNOTS
                rec['speed_10m_p90'] = row['wind_speed_10m_p90'] * KNOTS
                rec['wind_u_10m_mean'], rec['wind_v_10m_mean'] = row['u_component_of_wind_10m_mean'], row['v_component_of_wind_10m_mean']
                rec['temperature_2m_mean'] = row['temperature_2m_mean'] - 273.15
                rec['low_cloud_cover_mean'], rec['low_cloud_cover_p90'] = row['low_cloud_cover_mean'], row['low_cloud_cover_p90']
                records.append(rec)
        if records:
            frame = pd.DataFrame(records)
            frame = pd.concat([previous, frame], ignore_index=True) if previous is not None else frame
            frame.sort_values(['init', 'lead_h']).to_parquet(path, index=False)
        print(f'{month}: {len({r["init"] for r in records})}/{len(group)} runs, {billed_total / 2**30:.2f} GiB billed, {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    main(*sys.argv[1:])
