"""Native deterministic contracts; synthesized fixtures are not weather evidence."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 20, tzinfo=UTC)

class DirectTests(unittest.TestCase):
    def test_gfs_duplicate_index_selects_instant_and_since_init(self):
        from kcdw.direct_deterministic_worker import indexed_ranges
        rows=[('RH','850 mb','12 hour fcst'),('PRES','surface','12 hour fcst'),
              ('LCDC','low cloud layer','12 hour fcst'),('LCDC','low cloud layer','6-12 hour ave fcst'),
              ('APCP','surface','6-12 hour acc fcst'),('APCP','surface','0-12 hour acc fcst'),('END','surface','12 hour fcst')]
        text='\n'.join(f'{i+1}:{i*100}:d=2026091712:{a}:{b}:{c}:' for i,(a,b,c) in enumerate(rows))
        ranges=indexed_ranges('gfs',text,NOW.replace(hour=12),12)
        self.assertEqual(ranges['low'],(200,299))
        self.assertEqual(ranges['rain'],(500,599))
        day=text.replace('12 hour fcst','24 hour fcst').replace('6-12 hour','18-24 hour').replace('0-12 hour','0-1 day')
        self.assertEqual(indexed_ranges('gfs',day,NOW.replace(hour=12),24)['rain'],(500,599))

    def test_supersaturation_preserved_in_proof_and_display(self):
        from kcdw.direct_deterministic import normalize_profile, validate_direct_data
        p=self.packet()
        p['samples'][0]['fields']['r850']['value']=102.0
        p['samples'][1]['fields']['r850']['value']=96.0
        data=normalize_profile(p,NOW.replace(hour=18),NOW.replace(hour=22))
        self.assertEqual(data['hourly']['relative_humidity_850hPa'],[102.0,100.0,98.0,96.0])
        self.assertEqual(data['metadata']['native_proof']['samples'][0]['fields']['r850']['value'],102.0)
        self.assertEqual(validate_direct_data(data,NOW),data)
        bad=copy.deepcopy(p);bad['samples'][0]['fields']['r850']['value']=float('nan')
        with self.assertRaises(ValueError):validate_direct_data(normalize_profile(bad,NOW.replace(hour=18),NOW.replace(hour=22)),NOW)

    def test_aifs_water_rh_is_rederived(self):
        from kcdw.direct_deterministic import derived_rh
        fields={'q850':{'value':.005},'t850':{'value':280}}
        rh=derived_rh(fields,850)
        self.assertTrue(60<rh['value']<80)
        self.assertIn('water',rh['method'])
        self.assertNotIn('identity',rh)

    def test_run_freshness_is_checked_at_display_time(self):
        from kcdw.direct_deterministic import validate_packet, stamp
        p=self.packet(); init=NOW.replace(hour=12); collected=init+timedelta(hours=23)
        p.update(collected_at=stamp(collected),collection_started_at=stamp(collected))
        for sample in p['samples']:sample['fetched_at']=stamp(collected)
        with self.assertRaises(ValueError):validate_packet(p,init+timedelta(hours=34))

    def test_gfs_collection_includes_prior_rain_step(self):
        from kcdw.direct_deterministic_worker import collection_leads
        self.assertEqual(collection_leads('gfs',6,9),[3,6,9])
        self.assertEqual(collection_leads('gfs',0,3),[0,3])
        self.assertEqual(collection_leads('ifs',6,9),[6,9])

    def test_future_initialization_at_original_collection_rejected(self):
        from kcdw.direct_deterministic import validate_packet, stamp
        p=self.packet(); p['collection_started_at']=stamp(NOW.replace(hour=11))
        with self.assertRaises(ValueError): validate_packet(p,NOW+timedelta(hours=1))

    def test_original_sample_collection_cannot_be_rehabilitated(self):
        from kcdw.direct_deterministic import validate_packet, stamp
        p=self.packet()
        p['samples'][0]['collection_origin']={'started_at':stamp(NOW.replace(hour=11)), 'completed_at':stamp(NOW)}
        with self.assertRaises(ValueError): validate_packet(p,NOW)

    def test_partial_humidity_cache_is_retried(self):
        from kcdw.direct_deterministic_worker import humidity_complete
        sample=self.packet()['samples'][0]
        self.assertTrue(humidity_complete(sample))
        sample['fields']['r925']['value']=None
        self.assertFalse(humidity_complete(sample))
        sample['fields']['sp']['value']=90000
        self.assertTrue(humidity_complete(sample))

    def test_required_surface_pressure_null_is_not_direct_coverage(self):
        from kcdw.direct_deterministic import normalize_profile
        p=self.packet(); p['samples'][0]['fields']['sp']['value']=None
        with self.assertRaises(ValueError): normalize_profile(p,NOW.replace(hour=18),NOW.replace(hour=22))

    def test_cache_uses_actual_source_clock_not_earlier_snapshot_start(self):
        import json, tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from kcdw.direct_deterministic import collect_native_profile, stamp
        p=self.packet()
        header={k:v for k,v in p.items() if k!='samples'}
        header.update(collection_started_at=stamp(NOW+timedelta(minutes=10)))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'ifs-test.json').write_text(json.dumps(p))
            with patch('kcdw.direct_deterministic.datetime') as clock, patch('kcdw.direct_deterministic.subprocess.run') as run:
                clock.now.return_value=NOW+timedelta(minutes=10)
                clock.fromisoformat=datetime.fromisoformat
                run.return_value=SimpleNamespace(stdout=json.dumps(header)+'\n')
                collect_native_profile('ifs',NOW.replace(hour=18),NOW.replace(hour=22),NOW-timedelta(minutes=10),cache_dir=directory)
                request=json.loads(run.call_args.kwargs['input'])
                self.assertEqual(len(request['cached']),1)

    def packet(self):
        from kcdw.direct_deterministic import stamp, url_for, expected_identity
        init=NOW.replace(hour=12)
        samples=[]
        for lead in (6,9):
            fields={}
            for name,value in [('r850',80),('r925',70),('r1000',60),('sp',99000),('h850',1500),('t2',290),('d2',285)]:
                fields[name]=dict(value=value,identity=expected_identity('ifs',name,init,lead),latitude=41.0,longitude=-74.25,
                    proof=dict(start=0,end=99,total=1000,bytes=100,sha256='a'*64))
            samples.append(dict(lead=lead,at=stamp(init+timedelta(hours=lead)),url=url_for('ifs',init,lead),fetched_at=stamp(NOW),fields=fields))
        return dict(version=2,kind='direct-deterministic',model='ifs',initialization_time=stamp(init),source_provider='ECMWF',collection_started_at=stamp(NOW),collected_at=stamp(NOW),samples=samples)

    def test_offline_exact_run_validation_and_terrain_mask(self):
        from kcdw.direct_deterministic import normalize_profile, validate_direct_data
        p=self.packet(); start=NOW.replace(hour=18); end=start+timedelta(hours=4)
        d=normalize_profile(p,start,end)
        self.assertEqual(validate_direct_data(d,NOW),d)
        self.assertEqual(d['hourly']['relative_humidity_1000hPa'],[None]*4)
        self.assertEqual(d['hourly']['relative_humidity_850hPa'],[80]*4)
        for mutate in (lambda d:d['hourly']['relative_humidity_850hPa'].__setitem__(1,99),
                       lambda d:d['metadata'].update(initialization_time='2026-09-17T06:00:00Z'),
                       lambda d:d['metadata']['native_proof']['samples'][0].update(url='https://example.org'),
                       lambda d:d['metadata']['native_proof']['samples'][0]['fields']['r850']['identity'].update(units='fraction'),
                       lambda d:d['metadata']['native_proof']['samples'][0]['fields']['r850'].update(latitude=40),
                       lambda d:d['metadata']['native_proof']['samples'][0].update(fetched_at='2026-09-18T00:00:00Z')):
            damaged=copy.deepcopy(d); mutate(damaged)
            with self.assertRaises(Exception): validate_direct_data(damaged,NOW)

    def test_direct_envelope_and_legacy_validation_coexist(self):
        from kcdw.direct_deterministic import normalize_profile, stamp
        from kcdw.event_moisture import validate_moisture
        start=NOW.replace(hour=18); end=start+timedelta(hours=4)
        d=normalize_profile(self.packet(),start,end)
        env=dict(version=1,collected_at=stamp(NOW),range=dict(start=stamp(start),end=stamp(end)),models={'ifs':dict(ok=True,data=d,error='')})
        self.assertTrue(validate_moisture(env,NOW)['models']['ifs']['available'])
        self.assertFalse(validate_moisture(env,NOW+timedelta(hours=13))['models']['ifs']['available'])

    def test_newest_eligible_run_excludes_short_ifs(self):
        from kcdw.direct_deterministic import candidate_runs
        runs = candidate_runs('ifs', NOW, NOW + timedelta(days=9))
        self.assertEqual(runs[0].hour, 12)
        self.assertTrue(all(t.hour in (0,12) for t in runs))
        self.assertEqual(candidate_runs('aifs_single', NOW, NOW+timedelta(days=9))[0].hour,18)

    def test_native_cadence_and_hourly_alignment(self):
        from kcdw.direct_deterministic import native_leads, align_hourly
        self.assertEqual(native_leads('gfs', 118, 127), [117,120,123,126,129])
        self.assertEqual(native_leads('ifs', 142, 151), [141,144,150,156])
        self.assertEqual(align_hourly([0,3], [60,90], [0,1,2,3]), [60,70,80,90])
        self.assertEqual(align_hourly([0,3], [None,90], [0,1,2,3]), [None,None,None,90])

    def test_precipitation_explicit_uniform_allocation_conserves_interval(self):
        from kcdw.direct_deterministic import rain_hourly
        samples=[{'lead':3,'fields':{'rain':{'value':6,'identity':{'startStep':0,'endStep':3}}}},
                 {'lead':6,'fields':{'rain':{'value':8,'identity':{'startStep':0,'endStep':6}}}}]
        values, intervals = rain_hourly(samples, list(range(7)))
        self.assertEqual(values, [None,2,2,2,2/3,2/3,2/3])
        self.assertEqual([(r['start_lead'],r['end_lead'],r['amount_mm']) for r in intervals], [(0,3,6),(3,6,2)])
        samples[1]['fields']['rain']['identity']['startStep']=1
        with self.assertRaises(ValueError): rain_hourly(samples,list(range(7)))

    def test_fixtures_never_activate_native(self):
        from tests.test_event_moisture import FakeClient, START, END, NOW as clock
        from kcdw.event_moisture import collect_moisture
        with patch('kcdw.direct_deterministic.collect_native_profile', side_effect=AssertionError('must not call')):
            self.assertEqual(collect_moisture(FakeClient(), START, END, clock)['version'],1)

    def test_explicit_capability_falls_back_with_safe_reason(self):
        from tests.test_gfs_guidance import FakeClient, START, END, NOW as clock
        from kcdw.gfs_guidance import collect_gfs, validate_gfs
        client=FakeClient(); client.direct_native=True
        with patch('kcdw.direct_deterministic.collect_native_profile', side_effect=RuntimeError('secret')):
            result=collect_gfs(client, START, END, clock)
        self.assertTrue(validate_gfs(result,clock)['available'])
        self.assertEqual(result['data']['metadata']['direct_fallback_reason'],'Direct native source unavailable or invalid')
        self.assertNotIn('secret',repr(result))

if __name__=='__main__': unittest.main()
