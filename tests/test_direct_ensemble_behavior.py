import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from kcdw import direct_ensemble as d
from kcdw.event_ensemble import MODELS

class NativeBehaviorTests(unittest.TestCase):
    def test_incomplete_native_chart_enters_real_open_meteo_fallback(self):
        from tests.test_events import FakeClient, EVENT, NOW
        from kcdw.event_ensemble import collect_model
        client=FakeClient();client.direct_native=True
        with patch.object(d,'collect_chart',return_value=None):
            result=collect_model(client,MODELS[0],EVENT,NOW)
        self.assertIsInstance(result,dict)
        self.assertEqual(result['metadata']['direct_fallback_reason'],d.FALLBACK)
        self.assertTrue(all(result['hourly']['pressure_msl']['sample_counts']))
        self.assertTrue(any('api.open-meteo.com' in url for url in client.urls))

    def test_native_supersaturation_is_preserved_after_interpolation(self):
        from kcdw.direct_ensemble_worker import field_spec
        self.assertGreaterEqual(field_spec('ecmwf_ens','r850')[-1],102)
        packet={'init':'2026-09-17T12:00:00Z','model':'ecmwf_ens','points':[
            {'member':'01','field':'r850','lead':0,'value':102.0},
            {'member':'01','field':'r850','lead':6,'value':98.0}]}
        members=d.hourly_members(packet,['2026-09-17T12:00:00Z','2026-09-17T15:00:00Z','2026-09-17T18:00:00Z'])
        self.assertEqual(members['relative_humidity_850hPa']['01'],[102.0,100.0,98.0])
        self.assertEqual(packet['points'][0]['value'],102.0)

    def test_stale_cached_point_is_refetched_not_returned(self):
        import json,tempfile
        from pathlib import Path
        from kcdw import direct_ensemble_worker as worker
        init=datetime(2026,9,17,0,tzinfo=timezone.utc)
        old=dict(model='gefs',init='2026-09-17T00:00:00Z',lead=6,member='00',field='r850',url='source',range=[0,99],value=90.,collected_at='2026-09-17T01:00:00Z',fetched_at='2026-09-17T01:01:00Z')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'point.json';path.write_text(json.dumps(old))
            with patch.object(worker,'path_for',return_value=path), patch.object(worker,'fetch',side_effect=RuntimeError('refetch required')):
                with self.assertRaisesRegex(RuntimeError,'refetch required'):
                    worker.point('gefs',init,6,'00','r850','source',(0,99),'2026-09-17T14:00:00Z')

    def test_gefs_native_parameter_identity(self):
        from kcdw.direct_ensemble_worker import field_spec
        self.assertEqual(field_spec('gefs','r2')[0],260242)
        self.assertEqual(field_spec('gefs','tp')[:2],(228228,'kg m**-2'))
        self.assertEqual(field_spec('ecmwf_ens','tp')[:2],(228,'m'))
    def test_gefs_rain_uniform_interval_and_missing_fields(self):
        p={'init':'2026-09-17T12:00:00Z','model':'gefs','points':[
            {'member':'00','field':'tp','lead':6,'value':12,'identity':{'startStep':0,'endStep':6}},
            {'member':'00','field':'tp','lead':12,'value':6,'identity':{'startStep':6,'endStep':12}}]}
        axis=['2026-09-17T%02d:00:00Z'%h for h in (12,13,18,19)]
        m=d.hourly_members(p,axis)
        self.assertEqual(m['precipitation']['00'],[None,2,2,1])
        self.assertEqual(m['wind_gusts_10m']['00'],[None]*4)
        self.assertEqual(m['cloud_cover_low']['00'],[None]*4)
    def test_ifs_cumulative_rain_same_member_differences(self):
        p={'init':'2026-09-17T00:00:00Z','model':'ecmwf_ens','points':[
            {'member':m,'field':'tp','lead':h,'value':v,'identity':{'startStep':0,'endStep':h}}
            for m,h,v in [('01',6,.006),('01',12,.018),('02',6,.012),('02',12,.006)]]}
        m=d.hourly_members(p,['2026-09-17T07:00:00Z'])
        self.assertAlmostEqual(m['precipitation']['01'][0],2)
        self.assertIsNone(m['precipitation']['02'][0])
    def test_digest_binds_metadata_except_itself(self):
        a={'metadata':{'initialization_time':'2026-09-17T00:00:00Z'},'hourly':{}}
        d.seal(a); b=deepcopy(a); b['metadata']['initialization_time']='2026-09-17T06:00:00Z'
        self.assertNotEqual(d.digest(a),d.digest(b))
        before=d.digest(a)
        a['metadata']['normalized_sha256']='other'
        self.assertEqual(d.digest(a),before)
    def test_dated_event_not_rejected_before_collection(self):
        now=datetime(2026,9,17,20,tzinfo=timezone.utc)
        with patch.object(d,'collect_native',side_effect=RuntimeError('collector reached')):
            with self.assertRaisesRegex(RuntimeError,'collector reached'):
                d.collect_chart(object(),MODELS[0],object(),now,now+timedelta(hours=4),now)
