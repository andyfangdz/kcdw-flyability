#!/usr/bin/env python3
"""Compare WN3 scan estimates. Every request is a non-executing dry run."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.wn3_bigquery_point import DEFAULT_TABLE, request_body


def cases(run, valid):
    body = request_body(DEFAULT_TABLE, run, [valid], ['temperature_2m_mean'], 1024**3)
    point = 'ST_DWITHIN(t.geography, ST_GEOGPOINT(@longitude, @latitude), 100)'
    select = f'SELECT f.temperature_2m_mean FROM `{DEFAULT_TABLE}` t CROSS JOIN UNNEST(t.forecast) f'
    filters = {
        'one_init_point_one_hour': f't.init_time = @init AND {point} AND f.time IN UNNEST(@valid)',
        'one_init_no_spatial_one_hour': 't.init_time = @init AND f.time IN UNNEST(@valid)',
        'one_init_point_all_hours': f't.init_time = @init AND {point}',
        'one_day_point_one_hour': f'DATE(t.init_time) = DATE(@init) AND {point} AND f.time IN UNNEST(@valid)',
        'two_days_point_one_hour': f't.init_time >= TIMESTAMP_TRUNC(@init, DAY) AND t.init_time < TIMESTAMP_ADD(TIMESTAMP_TRUNC(@init, DAY), INTERVAL 2 DAY) AND {point} AND f.time IN UNNEST(@valid)',
        'all_inits_point_one_hour': f'{point} AND f.time IN UNNEST(@valid)',
        'future_init_point_one_hour': f"t.init_time = TIMESTAMP('2099-01-01') AND {point} AND f.time IN UNNEST(@valid)",
        'literal_point_one_hour': "t.init_time = @init AND ST_DWITHIN(t.geography, ST_GEOGPOINT(-74.3, 40.9), 100) AND f.time IN UNNEST(@valid)",
        'polygon_point_one_hour': 't.init_time = @init AND ST_INTERSECTS(t.geography_polygon, ST_GEOGPOINT(@longitude, @latitude)) AND f.time IN UNNEST(@valid)',
    }
    result = {name: select + ' WHERE ' + where for name, where in filters.items()}
    result['one_init_point_168_hours'] = select + f' WHERE t.init_time = @init AND {point} AND f.hours BETWEEN 1 AND 168'
    result['one_init_point_limit_1'] = result['one_init_point_one_hour'] + ' LIMIT 1'
    result['point_time_provenance'] = body['query']
    result['point_coordinates_only'] = f'SELECT ST_X(t.geography), ST_Y(t.geography) FROM `{DEFAULT_TABLE}` t WHERE t.init_time=@init AND {point}'
    result['point_init_only'] = f'SELECT t.init_time FROM `{DEFAULT_TABLE}` t WHERE t.init_time=@init AND {point}'
    result['temperature_lead_24'] = select + f' WHERE t.init_time=@init AND {point} AND f.hours=24'
    result['nested_hour_array'] = f'SELECT ARRAY(SELECT AS STRUCT f.time, f.temperature_2m_mean FROM UNNEST(t.forecast) f WHERE f.time IN UNNEST(@valid)) FROM `{DEFAULT_TABLE}` t WHERE t.init_time=@init AND {point}'
    return body['queryParameters'], result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True)
    p.add_argument('--run', required=True)
    p.add_argument('--valid', required=True)
    args = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.project):
        p.error('invalid project')
    params, queries = cases(args.run, args.valid)
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    with AuthorizedSession(credentials) as session:
        for name, sql in queries.items():
            # jobs.insert dryRun exposes estimate accuracy and partition count
            # when BigQuery makes them available; it does not create a job.
            r = session.post(f'https://bigquery.googleapis.com/bigquery/v2/projects/{args.project}/jobs', json={
                'jobReference': {'projectId': args.project, 'location': 'US'},
                'configuration': {'dryRun': True, 'query': {
                    'query': sql, 'useLegacySql': False, 'parameterMode': 'NAMED',
                    'queryParameters': params, 'maximumBytesBilled': str(1024**3)}}}, timeout=35)
            data = r.json()
            print(json.dumps({'case': name, 'sql': sql, 'http_status': r.status_code,
                              'statistics': data.get('statistics'), 'error': data.get('error')}), flush=True)


if __name__ == '__main__':
    main()
