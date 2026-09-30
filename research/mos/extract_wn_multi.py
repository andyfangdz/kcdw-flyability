"""WeatherNext 2 and 3 for every station in stations.json, one BigQuery query per 00Z run (research).

One query covers all stations (neighbouring stations share storage blocks, so it
bills ~3.5x a single-station query for WN2 rather than 22x). Each returned grid
point is assigned to the station(s) nearest to it. Incremental and guarded like
the single-station extractors. Writes var/mos-multi/models/{wn2,wn3}/YYYY-MM.parquet.
Usage: var/mos-venv/bin/python research/mos/extract_wn_multi.py wn2|wn3 [FIRST_DATE]
"""
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extract_wn2 as w2  # noqa: E402
import extract_wn3 as w3  # noqa: E402
from kcdw.weathernext2_bigquery import grid  # noqa: E402
from kcdw.weathernext3_bigquery import query  # noqa: E402

STATIONS = json.loads((Path(__file__).resolve().parent / 'stations.json').read_text())
MAX_RUN_BYTES = 2 * 1024 ** 3
MAX_TOTAL_BYTES = 1600 * 1024 ** 3


def multi_body(kind, init):
    base = (w2 if kind == 'wn2' else w3).body(init)
    if kind == 'wn2':
        points = {grid(v['lat'], v['lon']) for v in STATIONS.values()}
        where = ' OR '.join(f'ST_DWITHIN(t.geography, ST_GEOGPOINT({lon}, {lat}), 100)' for lat, lon in sorted(points))
    else:
        where = ' OR '.join(f"ST_DWITHIN(t.geography, ST_GEOGPOINT({v['lon']}, {v['lat']}), 7800)" for v in STATIONS.values())
    base['query'] = (base['query'].replace('ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)', f'({where})')
                     .replace('SELECT TO_JSON_STRING(STRUCT(', 'SELECT TO_JSON_STRING(STRUCT(ST_Y(t.geography) AS lat, ST_X(t.geography) AS lon, '))
    base['queryParameters'] = [p for p in base['queryParameters'] if p['name'] not in ('latitude', 'longitude')]
    base['maxResults'] = 10000
    return base


def assign(rows):
    """{station: rows at the grid point nearest that station}."""
    points = sorted({(r['lat'], r['lon']) for r in rows})
    out = {}
    for name, s in STATIONS.items():
        d = [math.hypot(p[0] - s['lat'], (p[1] - s['lon']) * math.cos(math.radians(s['lat']))) for p in points]
        best = points[int(np.argmin(d))]
        out[name] = [r for r in rows if (r['lat'], r['lon']) == best]
    return out


def records(kind, rows, init):
    recs = []
    for station, mine in assign(rows).items():
        if kind == 'wn2':
            for rec in w2.reduce(mine, init):
                recs.append(dict(rec, station=station))
        else:
            for row in mine:
                recs.append({'station': station, 'init': init.replace(tzinfo=None), 'lead_h': int(row['hours']),
                             'speed_10m_mean': row['wind_speed_10m_mean'] * w3.KNOTS, 'speed_10m_p10': row['wind_speed_10m_p10'] * w3.KNOTS,
                             'speed_10m_p90': row['wind_speed_10m_p90'] * w3.KNOTS, 'wind_u_10m_mean': row['u_component_of_wind_10m_mean'],
                             'wind_v_10m_mean': row['v_component_of_wind_10m_mean'], 'temperature_2m_mean': row['temperature_2m_mean'] - 273.15,
                             'low_cloud_cover_mean': row['low_cloud_cover_mean'], 'low_cloud_cover_p90': row['low_cloud_cover_p90']})
    return recs


def main(kind, first=None):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    session = AuthorizedSession(credentials)
    out = Path(f'var/mos-multi/models/{kind}')
    out.mkdir(parents=True, exist_ok=True)
    first = first or ('2022-01-01' if kind == 'wn2' else '2026-01-01')
    inits = pd.date_range(first, datetime.now(timezone.utc).date(), freq='D', tz='UTC')
    spent = 0
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out / f'{month}.parquet'
        previous = pd.read_parquet(path) if path.exists() else None
        done = set(pd.to_datetime(previous.init)) if previous is not None else set()
        todo = [i for i in group if i.tz_localize(None) not in done]
        if not todo:
            continue
        started = time.time()

        def one(init):
            for attempt in range(5):
                try:
                    result = query(session, 'aviation-486817', multi_body(kind, init.to_pydatetime()))
                    break
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(30 * (attempt + 1))
            return records(kind, result['rows'], init.to_pydatetime()), int(result['statistics'].get('totalBytesBilled') or 0)

        recs, billed_month = [], 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for r, billed in pool.map(one, todo):
                if billed > MAX_RUN_BYTES:
                    raise SystemExit(f'a run billed {billed / 2**20:.0f} MiB; stopping')
                recs += r; billed_month += billed
        spent += billed_month
        if recs:
            frame = pd.DataFrame(recs)
            frame = pd.concat([previous, frame], ignore_index=True) if previous is not None else frame
            frame.sort_values(['station', 'init', 'lead_h']).to_parquet(path, index=False)
        print(f'{month}: {len(todo)} runs, {billed_month / 2**30:.1f} GiB (session {spent / 2**30:.1f}), {time.time() - started:.0f} s', flush=True)
        if spent > MAX_TOTAL_BYTES:
            raise SystemExit('session billing guard reached')


if __name__ == '__main__':
    main(*sys.argv[1:])
