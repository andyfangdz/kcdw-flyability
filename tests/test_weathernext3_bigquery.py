"""Production WN3 BigQuery failure, cache and archive contracts."""
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from kcdw.weathernext3_bigquery import BigQueryStore, query
from kcdw.weathernext3 import LEGACY_SOURCE, collect_weather_next3, validate_weather_next3
from test_weathernext3 import FakeStore, RUN, NOW, fixture, iso


def response():
    rows=FakeStore().fetch(iso(RUN))['rows']
    return dict(rows=rows, job=dict(projectId='aviation-486817',jobId='test_job',location='US'),
                statistics=dict(totalBytesProcessed='142494624',totalBytesBilled='142606336',cacheHit=False))


class BigQueryTests(unittest.TestCase):
    def test_cache_reuses_validated_complete_run_without_second_query(self):
        with tempfile.TemporaryDirectory() as root, patch('kcdw.weathernext3_bigquery.query', return_value=response()) as remote:
            store=BigQueryStore(session=Mock(),cache_dir=root)
            a=store.fetch(iso(RUN));b=store.fetch(iso(RUN))
            self.assertEqual(remote.call_count,1)
            self.assertFalse(a['provenance']['local_cache_hit'])
            self.assertTrue(b['provenance']['local_cache_hit'])
            self.assertEqual(a['provenance']['retrieved_at'],b['provenance']['retrieved_at'])
            self.assertEqual(len(b['rows']),360)
            self.assertFalse(remote.call_args.args[2]['dryRun'])
            self.assertGreater(int(remote.call_args.args[2]['maximumBytesBilled']),0)

    def test_corrupt_cache_is_refetched_and_invalid_upstream_is_not_cached(self):
        with tempfile.TemporaryDirectory() as root, patch('kcdw.weathernext3_bigquery.query', return_value=response()) as remote:
            store=BigQueryStore(session=Mock(),cache_dir=root)
            store.fetch(iso(RUN))
            path=next(Path(root).glob('*.json'))
            data=json.loads(path.read_text());data['rows'][0]['temperature_2m_mean']=None
            path.write_text(json.dumps(data))
            store.fetch(iso(RUN))
            self.assertEqual(remote.call_count,2)
        for mutate in [lambda r:r['rows'].pop(),
                       lambda r:r['rows'][0].update(init_time=iso(RUN+timedelta(hours=6))),
                       lambda r:r['rows'][0].update(latitude=40.8)]:
            with tempfile.TemporaryDirectory() as root:
                bad=response();mutate(bad)
                with patch('kcdw.weathernext3_bigquery.query',return_value=bad):
                    with self.assertRaises(ValueError):
                        BigQueryStore(session=Mock(),cache_dir=root).fetch(iso(RUN))
                self.assertEqual(list(Path(root).glob('*.json')),[])

    def test_discovery_is_bounded_and_rejects_non_synoptic_or_future_rows(self):
        with patch('kcdw.weathernext3_bigquery.query',return_value={'rows':[{'init_time':iso(RUN)}]}) as remote:
            self.assertEqual(BigQueryStore(session=Mock()).candidates(NOW),[RUN])
            body=remote.call_args.args[2]
            self.assertIn('t.init_time >= @lower',body['query'])
            self.assertIn('LIMIT 5',body['query'])
            self.assertNotIn('UNNEST(t.forecast)',body['query'])
            for value in [RUN+timedelta(hours=1),RUN+timedelta(days=1)]:
                remote.return_value={'rows':[{'init_time':iso(value)}]}
                with self.assertRaises(ValueError):
                    BigQueryStore(session=Mock()).candidates(NOW)

    def test_collection_only_falls_back_for_absent_run(self):
        store=FakeStore();store.candidates=Mock(return_value=[RUN+timedelta(hours=6),RUN])
        store.fetch=Mock(side_effect=[LookupError(),FakeStore().fetch(iso(RUN))])
        result=collect_weather_next3(NOW+timedelta(hours=6),[RUN+timedelta(hours=9)],store=store)
        self.assertTrue(result['status']['fallback'])
        for error in [RuntimeError('billing denied'),ValueError('partial run')]:
            store.fetch=Mock(side_effect=error)
            with self.assertRaises(ValueError):
                collect_weather_next3(NOW+timedelta(hours=6),store=store)
            self.assertEqual(store.fetch.call_count,1)

    def test_old_zarr_archives_remain_valid_and_sources_cannot_be_mixed(self):
        data=fixture();forecast=data['forecast'];forecast['source']=LEGACY_SOURCE
        forecast.pop('query');count=len(forecast['valid_time_utc'])*39
        forecast['transfer']=dict(objects=count,network_objects=count,cache_objects=0,object_bytes=count,network_bytes=count)
        data['status'].update(authentication='gcs_authenticated_read_succeeded',transport='GCS gRPC whole-object reads')
        validate_weather_next3(data,NOW)
        data['status']['transport']='BigQuery REST'
        with self.assertRaises(ValueError):validate_weather_next3(data,NOW)
        with self.assertRaises(ValueError):validate_weather_next3(fixture(),NOW+timedelta(days=2))

    def test_query_polls_and_reads_all_pages(self):
        def reply(value):
            return Mock(status_code=200,json=Mock(return_value=value))
        ref=dict(projectId='aviation-486817',jobId='job_test',location='US')
        session=Mock()
        session.post.return_value=reply(dict(jobReference=ref,jobComplete=False))
        session.get.side_effect=[
            reply(dict(jobComplete=True,rows=[{'f':[{'v':'{"a":1}'}]}],pageToken='next')),
            reply(dict(jobComplete=True,rows=[{'f':[{'v':'{"a":2}'}]}])),
            reply(dict(status={},statistics={'query':{'totalBytesBilled':'42'}}))]
        result=query(session,'aviation-486817',{'dryRun':False})
        self.assertEqual(result['rows'],[{'a':1},{'a':2}])
        self.assertEqual(result['statistics']['totalBytesBilled'],'42')


if __name__=='__main__':unittest.main()
