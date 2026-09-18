"""Transport/identity regressions; optional real saved-GRIB smoke."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from kcdw import direct_ensemble_worker as w

INIT = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
NOAA = ('1:0:d=2026091712:GUST:surface:210 hour fcst:ENS=+30\n'
        '2:200:d=2026091712:TCDC:low cloud layer:204-210 hour ave fcst:ENS=+30')

def ecmwf_text():
    base = dict(date='20260917', time='1200', step='168', domain='g', stream='enfo',
                **{'class': 'od', 'type': 'pf', 'number': '1', 'levtype': 'sfc'})
    return '\n'.join(json.dumps(dict(base, param=p, _offset=i*200, _length=200))
                     for i,p in enumerate(('10fg', 'sp')))

class NativeWorkerV3Tests(unittest.TestCase):
    def test_gefs_products_and_numeric_members(self):
        self.assertEqual(len(w.groups('gefs')), 62)
        self.assertIn('00b', w.groups('gefs'))
        self.assertIn('/pgrb2bp5/', w.url_for('gefs', INIT, 210, '30b'))
        self.assertEqual(w.url_for('gefs', INIT, 210, '30b'), w.url_for('gefs', INIT, 210, '30', field='gust'))
        self.assertEqual(w.indexed_ranges('gefs', NOAA, INIT, 210, '30b', 400),
                         {('30','gust'):(0,199), ('30','lcc'):(200,399)})
        with self.assertRaises(ValueError):
            w.indexed_ranges('gefs', NOAA.replace('204-210','0-210'), INIT, 210, '30b', 400)

    def test_model_native_specs_and_shared_identity(self):
        self.assertEqual(w.field_spec('gefs','gust')[:4], (260065,'m s**-1','surface',0))
        self.assertEqual(w.field_spec('gefs','lcc')[:4], (228164,'%','lowCloudLayer',0))
        self.assertEqual(w.field_spec('aifs_ens','lcc')[:4], (3073,'%','lowCloudLayer',0))
        self.assertEqual(w.expected_identity('gefs',INIT,210,'30','lcc')['startStep'], 204)
        self.assertEqual(w.expected_identity('gefs',INIT,210,'30','gust')['stepType'], 'instant')
        for lead,start in ((6,5),(168,162)):
            identity=w.expected_identity('ecmwf_ens',INIT,lead,'01','gust',start_step=start)
            self.assertEqual((identity['startStep'],identity['endStep'],identity['stepType']), (start,lead,'max'))
        for start in (None, -1, 0, 168, 169):
            with self.assertRaises(ValueError):
                w.expected_identity('ecmwf_ens',INIT,168,'01','gust',start_step=start)
        self.assertEqual(w.expected_identity('aifs_ens',INIT,168,'00','tp')['startStep'],0)
        self.assertEqual(w.field_spec('ecmwf_ens','tp')[:2],(228,'m'))
        self.assertGreater(w.field_spec('gefs','r850')[-1],100)

    def test_gcs_preferred_canonical_origin_preserved(self):
        origin=w.url_for('ecmwf_ens',INIT,168,'ef')
        urls=w.urls_for('ecmwf_ens',INIT,168,'ef')
        self.assertTrue(origin.startswith('https://data.ecmwf.int/forecasts/'))
        self.assertTrue(urls[0].startswith('https://storage.googleapis.com/ecmwf-open-data/'))
        self.assertEqual(urls[-1],origin)
        self.assertTrue(w.valid_point_url(urls[0],'ecmwf_ens',INIT,168,'01','gust'))
        self.assertFalse(w.valid_point_url(urls[0]+'?host=evil','ecmwf_ens',INIT,168,'01','gust'))
        self.assertIn(('01','gust'),w.indexed_ranges('ecmwf_ens',ecmwf_text(),INIT,168,'ef'))

    def test_three_hour_gust_catalog_and_identity(self):
        text=ecmwf_text().replace('168','96').replace('10fg','10fg3')
        self.assertIn(('01','gust'),w.indexed_ranges('ecmwf_ens',text,INIT,96,'ef'))
        identity=w.expected_identity('ecmwf_ens',INIT,96,'01','gust',start_step=93,param_id=228028)
        self.assertEqual(identity['paramId'],228028)
        self.assertTrue(w.validate_identity(identity,'ecmwf_ens',INIT,96,'01','gust'))
        with self.assertRaises(ValueError):w.expected_identity('ecmwf_ens',INIT,96,'01','gust',start_step=90,param_id=228028)

    def test_session_is_thread_local_and_persistent(self):
        with patch('requests.Session',side_effect=lambda: MagicMock()):
            w._HTTP=threading.local()
            first=w.session()
            self.assertIs(first,w.session())
            other=[]
            t=threading.Thread(target=lambda:other.append(w.session()));t.start();t.join()
            self.assertIsNot(first,other[0])

    def test_index_cache_avoids_get_head_preserves_clocks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'INDEX_CACHE',Path(tmp)), patch.object(w,'fetch',return_value=NOAA.encode()) as get, patch.object(w,'session') as sess:
            response=MagicMock(status_code=200,headers={'Content-Length':'400'})
            sess.return_value.head.return_value.__enter__.return_value=response
            now=INIT+timedelta(hours=1)
            a=w.index('gefs',INIT,210,'30b',now=now)
            files=list(Path(tmp).rglob('*.json'));self.assertEqual(len(files),1)
            original=files[0].read_bytes()
            self.assertEqual(a,w.index('gefs',INIT,210,'30b',now=now+timedelta(hours=1)))
            self.assertEqual(files[0].read_bytes(),original)
            self.assertEqual(get.call_count,1);self.assertEqual(sess.return_value.head.call_count,1)
            w.index('gefs',INIT,210,'30b',now=now+timedelta(hours=13))
            self.assertEqual(get.call_count,2)

    def test_cache_revalidates_parsed_catalog(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'INDEX_CACHE',Path(tmp)), patch.object(w,'fetch',return_value=ecmwf_text().encode()) as get:
            w.index('ecmwf_ens',INIT,168,'ef',now=INIT+timedelta(hours=1))
            f=next(Path(tmp).rglob('*.json'));p=json.loads(f.read_text())
            p['text']=p['text'].replace('20260917','20260916');f.write_text(json.dumps(p))
            w.index('ecmwf_ens',INIT,168,'ef',now=INIT+timedelta(hours=2))
            self.assertEqual(get.call_count,2)

    def test_point_cache_survives_replica_switch_with_original_clocks(self):
        origin=w.url_for('ecmwf_ens',INIT,168,'ef')
        replica=w.urls_for('ecmwf_ens',INIT,168,'ef')[0]
        p=dict(model='ecmwf_ens',init=w.stamp(INIT),lead=168,member='01',field='sp',
               url=origin,range=[0,199],value=101000.,latitude=41.,longitude=-74.25,
               identity=w.expected_identity('ecmwf_ens',INIT,168,'01','sp'),sha256='a'*64,
               collected_at=w.stamp(INIT+timedelta(hours=1)),fetched_at=w.stamp(INIT+timedelta(hours=1)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'CACHE',Path(tmp)), patch.object(w,'fetch',side_effect=RuntimeError('must not refetch')):
            path=w.path_for('ecmwf_ens',INIT,168,'01','sp');path.parent.mkdir(parents=True);path.write_text(json.dumps(p))
            self.assertEqual(w.point('ecmwf_ens',INIT,168,'01','sp',replica,(0,199),w.stamp(INIT+timedelta(hours=2)),now=INIT+timedelta(hours=2)),p)

    def test_invalidate_stale_incomplete_real_index(self):
        incomplete=ecmwf_text()
        complete='\n'.join(incomplete.replace('"number": "1"',f'"number": "{member}"') for member in range(1,51))
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'INDEX_CACHE',Path(tmp)), patch.object(w,'fetch',side_effect=[incomplete.encode(),complete.encode()]) as get:
            args=('ecmwf_ens',INIT,168,'ef')
            self.assertEqual({m for m,n in w.index(*args,now=INIT)[1]}, {'01'})
            self.assertEqual({m for m,n in w.index(*args,now=INIT)[1]}, {'01'})
            self.assertEqual(get.call_count,1)
            w.invalidate_index(*args)
            self.assertEqual(list(Path(tmp).glob('*.json')),[])
            self.assertEqual({m for m,n in w.index(*args,now=INIT)[1]}, {f'{m:02}' for m in range(1,51)})
            self.assertEqual(get.call_count,2)
            w.invalidate_index(*args);w.invalidate_index(*args)

    def test_versioned_geometry_proofs(self):
        args=('gefs',INIT,210,'30','10u')
        legacy=w.expected_identity(*args,schema=2)
        self.assertNotIn('iDirectionIncrementInDegrees',legacy)
        self.assertTrue(w.validate_identity(legacy,*args))
        full=w.expected_identity(*args)
        self.assertEqual(full['proof_schema'],3)
        self.assertEqual(full['iDirectionIncrementInDegrees'],0.5)
        for key,value in [('iDirectionIncrementInDegrees',1.0),('uvRelativeToGrid',1),('longitudeOfFirstGridPointInDegrees',180),('scanningMode',128)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                w.validate_identity(dict(full,**{key:value}),*args)
        del full['latitudeOfLastGridPointInDegrees']
        with self.assertRaises(ValueError):w.validate_identity(full,*args)
        self.assertNotIn('proof_schema',legacy)
        with self.assertRaises(ValueError):w.validate_identity(dict(legacy,proof_schema=3),*args)
        with self.assertRaises(ValueError):w.validate_identity(dict(legacy,proof_schema=99),*args)
        with self.assertRaises(ValueError):w.validate_identity(dict(legacy,uvRelativeToGrid=1),*args)

    def test_point_resume_uses_current_clock_not_job_start(self):
        args=('gefs',INIT,210,'30','10u')
        url=w.url_for('gefs',INIT,210,'30')
        collected=w.stamp(INIT+timedelta(hours=1))
        p=dict(model='gefs',init=w.stamp(INIT),lead=210,member='30',field='10u',url=url,range=[0,199],value=2.,latitude=41.,longitude=-74.5,identity=w.expected_identity(*args,schema=2),sha256='a'*64,collected_at=collected,fetched_at=w.stamp(INIT+timedelta(hours=2)))
        with tempfile.TemporaryDirectory() as tmp,patch.object(w,'CACHE',Path(tmp)),patch.object(w,'fetch',side_effect=RuntimeError('refetch')):
            path=w.path_for(*args);path.parent.mkdir(parents=True);path.write_text(json.dumps(p))
            before=path.read_bytes()
            self.assertEqual(w.point(*args,url,(0,199),collected,now=INIT+timedelta(hours=3)),p)
            self.assertEqual(path.read_bytes(),before)
            with patch.object(w,'datetime') as clock:
                clock.now.return_value=INIT+timedelta(hours=3)
                clock.fromisoformat.side_effect=datetime.fromisoformat
                self.assertEqual(w.point(*args,url,(0,199),collected),p)
            for current in (INIT+timedelta(minutes=90),INIT+timedelta(hours=14)):
                with self.assertRaises(RuntimeError):w.point(*args,url,(0,199),collected,now=current)

    def test_catalog_member_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            w.indexed_ranges('gefs',NOAA.replace('ENS=+30','ENS=+29'),INIT,210,'30b',400)

    def test_disk_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'INDEX_CACHE',Path(tmp)), patch.object(w,'INDEX_MAX_FILES',1), patch.object(w,'fetch',return_value=NOAA.encode()), patch.object(w,'session') as sess:
            response=MagicMock(status_code=200,headers={'Content-Length':'400'})
            sess.return_value.head.return_value.__enter__.return_value=response
            w.index('gefs',INIT,210,'30b',now=INIT+timedelta(hours=1))
            with patch.object(w,'fetch',return_value=ecmwf_text().encode()):
                w.index('ecmwf_ens',INIT,168,'ef',now=INIT+timedelta(hours=1))
            self.assertEqual(len(list(Path(tmp).glob('*.json'))),1)

    def test_gcs_failure_uses_origin(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'INDEX_CACHE',Path(tmp)), patch.object(w,'fetch',side_effect=[RuntimeError('offline'),ecmwf_text().encode()]) as get:
            url,ranges=w.index('ecmwf_ens',INIT,168,'ef',now=INIT+timedelta(hours=1))
            self.assertEqual(url,w.url_for('ecmwf_ens',INIT,168,'ef'))
            self.assertEqual(get.call_count,2)
            self.assertIn(('01','gust'),ranges)

class SavedGRIBSmoke(unittest.TestCase):
    def test_mutated_noaa_u10_geometry_rejected(self):
        try:import eccodes as ec
        except ImportError:self.skipTest('native-weather-venv required')
        path=Path('/tmp/noaa-fullrange-research/a-UGRD-10_m_above_ground.grib2')
        if not path.exists():self.skipTest('research sample unavailable')
        raw=path.read_bytes()
        w.decode(raw,'gefs',INIT,210,'30','10u')
        mutations={'iDirectionIncrementInDegrees':1.0,'jDirectionIncrementInDegrees':1.0,'uvRelativeToGrid':1,'longitudeOfFirstGridPointInDegrees':1.,'longitudeOfLastGridPointInDegrees':359.,'latitudeOfFirstGridPointInDegrees':89.,'latitudeOfLastGridPointInDegrees':-89.,'iScansNegatively':1,'jScansPositively':1,'jPointsAreConsecutive':1,'alternativeRowScanning':1}
        for key,value in mutations.items():
            with self.subTest(key=key):
                g=ec.codes_new_from_message(raw)
                try:
                    ec.codes_set(g,key,value)
                    mutated=ec.codes_get_message(g)
                finally:ec.codes_release(g)
                with self.assertRaises(ValueError):w.decode(mutated,'gefs',INIT,210,'30','10u')

    def test_actual_saved_gribs(self):
        try:import eccodes
        except ImportError:self.skipTest('native-weather-venv required')
        samples=[('/tmp/noaa-fullrange-research/b-GUST-surface.grib2','gefs',210,'30','gust'),
                 ('/tmp/noaa-fullrange-research/b-TCDC-low_cloud_layer.grib2','gefs',210,'30','lcc'),
                 ('/tmp/noaa-fullrange-research/a-APCP-surface.grib2','gefs',210,'30','tp'),
                 ('/tmp/ecmwf-fullrange-research/ifs-gust-6.grib','ecmwf_ens',6,'15','gust'),
                 ('/tmp/ecmwf-fullrange-research/ifs-gust-168.grib','ecmwf_ens',168,'15','gust'),
                 ('/tmp/ecmwf-fullrange-research/aifs-ens-pf-lcc.grib','aifs_ens',168,'32','lcc'),
                 ('/tmp/ecmwf-fullrange-research/aifs-ens-cf-lcc.grib','aifs_ens',168,'00','lcc'),
                 ('/tmp/ecmwf-fullrange-research/ifs-ef-tp.grib','ecmwf_ens',168,'41','tp'),
                 ('/tmp/ecmwf-fullrange-research/aifs-ens-pf-tp.grib','aifs_ens',168,'38','tp')]
        if not all(Path(s[0]).exists() for s in samples):self.skipTest('research samples unavailable')
        for path,model,lead,member,name in samples:
            with self.subTest(path=path):
                result=w.decode(Path(path).read_bytes(),model,INIT,lead,member,name)
                self.assertEqual(result['identity']['endStep'],lead)
                self.assertEqual(result['identity']['proof_schema'],3)
                self.assertTrue(w.validate_identity(result['identity'],model,INIT,lead,member,name))
                print('REAL GRIB',model,lead,member,name,result['value'],result['identity']['startStep'],result['identity']['stepType'])

if __name__=='__main__':unittest.main()
