import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from kcdw.common import UTC, iso_z
from kcdw import weathernext2_bigquery as bq
from kcdw.event_wn2_members import collect_members, validate_members
from kcdw.event_wn2_members_view import render_wn2_members
from test_event_wn2_members import SNAP, NOW

INIT = NOW.replace(hour=0)
TIMES = [datetime(2026, 9, 24, h, tzinfo=UTC) for h in (12, 18)]
PROVENANCE = dict(table=bq.TABLE, project='test-project', job_id='job_test', location='US',
                  bytes_processed=123, bytes_billed=10485760, query_cache_hit=False, local_cache_hit=False)


def rows():
    return [dict(init_time=iso_z(INIT), valid_time=iso_z(t), hours=int((t-INIT).total_seconds()/3600), latitude=41.0, longitude=-74.25,
                 members=[dict(member=str(i), u10=-3., v10=-3., u100=-6., v100=-6., pressure=102500.,
                               rain=.0012 if i < 2 else -.0000002) for i in range(64)]) for t in TIMES]


class Store:
    def candidates(self, *args):
        return [INIT]

    def fetch(self, *args):
        return dict(rows=rows(), provenance=copy.deepcopy(PROVENANCE))


class WN2BigQueryTests(unittest.TestCase):
    def packet(self):
        return collect_members(None, SNAP, NOW, store=Store())

    def test_native_member_conversion_and_enclosing_rain_window(self):
        p = self.packet()
        self.assertEqual(validate_members(p, SNAP), p)
        self.assertNotIn('low_cloud_pct', p['samples'][0])
        self.assertEqual(p['samples'][0]['sector'], dict(count=64, n=64))
        self.assertEqual(p['samples'][0]['wind_10m_kt']['p50'], 8.2)
        self.assertEqual(p['samples'][0]['pressure_hpa']['p50'], 1025.)
        self.assertEqual(p['rain'], dict(count=2, n=64, p90_mm=0., start=iso_z(TIMES[0]), end=iso_z(TIMES[1])))
        html = render_wn2_members(dict(SNAP, weathernext2_members=p), NOW)
        for phrase in ('Independent WN2', 'bound to the', 'not a flight-window rain estimate', 'WN3 supplies the primary'):
            self.assertIn(phrase, html)
        self.assertNotIn('keep a low deck', html)

    def test_rejects_member_identity_time_grid_and_value_corruption(self):
        for mutate in (lambda r: r[0]['members'].pop(), lambda r: r[0]['members'][1].update(member='0'),
                       lambda r: r[0]['members'][0].update(rain=-.01), lambda r: r[0]['members'][0].update(u10=float('nan')),
                       lambda r: r[0].update(latitude=40.75), lambda r: r[0].update(hours=6), lambda r: r.reverse()):
            bad = rows(); mutate(bad)
            with self.assertRaises(ValueError):
                bq.validate_rows(bad, INIT, TIMES, 40.875, -74.281)

    def test_packet_rejects_tampering(self):
        for mutate in (lambda p: p.update(init_time=iso_z(NOW+timedelta(hours=6))),
                       lambda p: p['rain'].update(start=p['window']['start']),
                       lambda p: p['samples'][0].update(cloudy=dict(count=2,n=64)),
                       lambda p: p['bigquery'].update(table='other'), lambda p: p['samples'][0]['sector'].update(n=48),
                       lambda p: p['grid_point'].update(latitude=40.75)):
            p=self.packet();mutate(p)
            with self.assertRaises(ValueError): validate_members(p, SNAP)

    def test_query_has_partition_geography_and_native_time_filters(self):
        body=bq.request_body(INIT,TIMES,40.875,-74.281,2*1024**4)
        for sql in ('t.init_time = @init', 'ST_DWITHIN(t.geography', 'f.time IN UNNEST(@valid)'):
            self.assertIn(sql,body['query'])
        self.assertNotIn('cloud',body['query'])
        with self.assertRaises(ValueError): bq.request_body(INIT,[TIMES[0]+timedelta(hours=1)],41,-74.25,1)

    def test_cache_reuses_validated_result_and_recovers_corruption(self):
        result=dict(rows=rows(),job=dict(projectId='test-project',jobId='job_test',location='US'),
                    statistics=dict(totalBytesProcessed='123',totalBytesBilled='10485760',cacheHit=False))
        with tempfile.TemporaryDirectory() as directory, patch.object(bq,'query',return_value=result) as query:
            store=bq.BigQueryStore(session=object(),cache_dir=directory)
            first=store.fetch(INIT,TIMES,41,-74.25)
            second=store.fetch(INIT,TIMES,41,-74.25)
            self.assertEqual(query.call_count,1)
            self.assertFalse(first['provenance']['local_cache_hit'])
            self.assertTrue(second['provenance']['local_cache_hit'])
            path=next(store.cache_dir.glob('*.json'));bad=json.loads(path.read_text());bad['rows'][0]['members'].pop();path.write_text(json.dumps(bad))
            store.fetch(INIT,TIMES,41,-74.25)
            self.assertEqual(query.call_count,2)

    def test_calm_does_not_count_as_northeast(self):
        data=rows()
        for row in data:
            for m in row['members']:m.update(u10=0.,v10=0.)
        with patch.object(Store,'fetch',return_value=dict(rows=data,provenance=PROVENANCE)):
            self.assertEqual(self.packet()['samples'][0]['sector'],dict(count=0,n=64))
