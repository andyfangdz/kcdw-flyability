"""WeatherNext 2 (64 members) at KCDW from BigQuery, one 00Z run per day, resumable.

Same point query as kcdw.weathernext2_bigquery (spatial pruning bills ~200-300 MB per
run although BigQuery's pre-run estimate is ~830 GB, so the cap must exceed that
estimate). Guards: stop if any run bills more than MAX_RUN_BYTES or the session
total exceeds MAX_TOTAL_BYTES. Writes var/mos/models/wn2/YYYY-MM.parquet with
member statistics per lead, in the same layout as extract_dynamical.py.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kcdw.weathernext2_bigquery import TABLE, grid  # noqa: E402
from kcdw.weathernext3_bigquery import query  # noqa: E402

KCDW = (40.8752, -74.2814)
LEADS = list(range(6, 199, 6))
FIELDS = {'u10': '10m_u_component_of_wind', 'v10': '10m_v_component_of_wind', 'u100': '100m_u_component_of_wind',
          'v100': '100m_v_component_of_wind', 't2m': '2m_temperature', 'mslp': 'mean_sea_level_pressure'}
MAX_RUN_BYTES = 1024 ** 3
MAX_TOTAL_BYTES = int(float(os.environ.get('MOS_MAX_TOTAL_GIB', '900')) * 1024 ** 3)
CAP = 2 * 1024 ** 4  # must exceed BigQuery's unpruned pre-run estimate
OUT = Path('var/mos/models/wn2')
KNOTS = 3600 / 1852


def body(init):
    lat, lon = grid(*KCDW)
    columns = ', '.join(f'e.`{field}` AS {alias}' for alias, field in FIELDS.items())
    sql = f'''SELECT TO_JSON_STRING(STRUCT(f.hours, ARRAY(SELECT AS STRUCT e.ensemble_member AS member, {columns}
      FROM UNNEST(f.ensemble) e) AS members))
FROM `{TABLE}` t CROSS JOIN UNNEST(t.forecast) f
WHERE t.init_time = @init AND ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100) AND f.hours IN UNNEST(@hours)
ORDER BY f.hours'''
    scalar = lambda name, kind, value: dict(name=name, parameterType={'type': kind}, parameterValue={'value': str(value)})
    return dict(query=sql, useLegacySql=False, dryRun=False, location='US', timeoutMs=120000, maximumBytesBilled=str(CAP), parameterMode='NAMED',
                queryParameters=[scalar('init', 'TIMESTAMP', init.isoformat()), scalar('latitude', 'FLOAT64', lat),
                                 scalar('longitude', 'FLOAT64', lon),
                                 dict(name='hours', parameterType={'type': 'ARRAY', 'arrayType': {'type': 'INT64'}},
                                      parameterValue={'arrayValues': [{'value': str(h)} for h in LEADS]})])


def reduce(rows, init):
    records = []
    for row in rows:
        m = pd.DataFrame(row['members'])
        rec = {'init': init.replace(tzinfo=None), 'lead_h': int(row['hours'])}
        for name, u, v in (('speed_10m', 'u10', 'v10'), ('speed_100m', 'u100', 'v100')):
            speed = np.hypot(m[u], m[v]) * KNOTS
            level = name.split('_')[1]
            rec[f'wind_u_{level}_mean'], rec[f'wind_v_{level}_mean'] = m[u].mean(), m[v].mean()
            for stat, value in (('mean', speed.mean()), ('std', speed.std()), ('p10', speed.quantile(.1)), ('p50', speed.median()), ('p90', speed.quantile(.9))):
                rec[f'{name}_{stat}'] = value
        rec['temperature_2m_mean'], rec['temperature_2m_std'] = m.t2m.mean() - 273.15, m.t2m.std()
        rec['mslp_mean'] = m.mslp.mean() / 100
        records.append(rec)
    return records


def main(first='2020-10-01'):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    session = AuthorizedSession(credentials)
    OUT.mkdir(parents=True, exist_ok=True)
    spent = 0
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
        started = time.time()
        # Incremental: runs already stored are never re-queried (BigQuery's result cache lasts about a day).
        previous = pd.read_parquet(path) if path.exists() else None
        done = set(pd.to_datetime(previous.init)) if previous is not None else set()
        group = group[[pd.Timestamp(i).tz_localize(None) not in done for i in group.values]]

        def one(init):
            init = pd.Timestamp(init).tz_localize('UTC') if pd.Timestamp(init).tzinfo is None else pd.Timestamp(init)
            result = query(session, 'aviation-486817', body(init.to_pydatetime()))
            billed = int(result['statistics'].get('totalBytesBilled') or 0)
            rows = [json.loads(r) if isinstance(r, str) else r for r in result['rows']]
            return reduce(rows, init.to_pydatetime()), billed

        records, month_bytes = [], 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for recs, billed in pool.map(one, group.values):
                if billed > MAX_RUN_BYTES:
                    raise SystemExit(f'a run billed {billed / 2**20:.0f} MiB; stopping (guard {MAX_RUN_BYTES / 2**20:.0f} MiB)')
                records += recs
                month_bytes += billed
        spent += month_bytes
        if records:
            frame = pd.DataFrame(records)
            frame = pd.concat([previous, frame], ignore_index=True) if previous is not None else frame
            frame.sort_values(['init', 'lead_h']).to_parquet(path, index=False)
        print(f'{month}: {len({r["init"] for r in records})}/{len(group)} runs, {month_bytes / 2**30:.1f} GiB billed '
              f'(session {spent / 2**30:.1f} GiB), {time.time() - started:.0f} s', flush=True)
        if spent > MAX_TOTAL_BYTES:
            raise SystemExit('session billing guard reached')


if __name__ == '__main__':
    main(*sys.argv[1:])
