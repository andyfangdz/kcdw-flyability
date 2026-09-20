import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from kcdw import direct_ensemble as d

class CompletedCacheAdapterTests(unittest.TestCase):
    now = datetime(2026, 9, 17, 18, tzinfo=timezone.utc)

    def test_collection_reads_cache_without_starting_downloads(self):
        client=types.SimpleNamespace(direct_native=True)
        packet={'model':'gefs'}
        loader=Mock(return_value=packet)
        module=types.ModuleType('kcdw.native_ensemble_cache');module.load_completed=loader
        with patch.dict(sys.modules, {'kcdw.native_ensemble_cache':module}), patch.object(d,'validate_packet',side_effect=lambda packet,*_:packet), patch('subprocess.run',side_effect=AssertionError('no download from page collection')) as run:
            self.assertIs(d.collect_native(client,'gefs',self.now,self.now+timedelta(days=1),self.now),packet)
            self.assertIs(d.collect_native(client,'gefs',self.now,self.now+timedelta(days=1),self.now),packet)
        loader.assert_called_once();run.assert_not_called()

    def test_completed_version_requires_every_member(self):
        data={'metadata':{'native_version':3},'members':31,'hourly':{'time':[d.iso_z(self.now)],'pressure_msl':{'sample_counts':[30]}}}
        self.assertFalse(d._covers_future(data,('pressure_msl',),self.now,self.now+timedelta(hours=1),self.now))
        data['hourly']['pressure_msl']['sample_counts']=[31]
        self.assertTrue(d._covers_future(data,('pressure_msl',),self.now,self.now+timedelta(hours=1),self.now))

    def test_percent_cloud_is_not_multiplied_and_gust_converts_to_knots(self):
        p={'model':'aifs_ens','init':d.iso_z(self.now),'native_version':3,'points':[
            {'member':'00','lead':h,'field':'lcc','value':80.} for h in (0,6)]}
        m=d.hourly_members(p,[d.iso_z(self.now+timedelta(hours=3))])
        self.assertEqual(m['cloud_cover_low']['00'],[80.])
        p['model']='gefs';p['points']=[{'member':'00','lead':h,'field':'gust','value':10.} for h in (0,6)]
        m=d.hourly_members(p,[d.iso_z(self.now+timedelta(hours=3))])
        self.assertAlmostEqual(m['wind_gusts_10m']['00'][0],10*3600/1852)

    def test_geps_cumulative_rain_is_differenced_per_member(self):
        p={'model':'geps','init':d.iso_z(self.now),'native_version':3,'points':[
            {'member':member,'field':'tp','lead':lead,'value':value} for member,lead,value in [('00',6,12.),('00',12,18.),('01',6,6.),('01',12,18.)]]}
        m=d.hourly_members(p,[d.iso_z(self.now+timedelta(hours=7))])
        self.assertEqual(m['precipitation']['00'],[1.]);self.assertEqual(m['precipitation']['01'],[2.])

    def packet(self, model='gefs'):
        fields={'msl':101000.,'sp':99000.,'2t':290.,'r2':101.,'r1000':102.,'r925':95.,'r850':60.,'10u':3.,'10v':4.,'tp':6.,'gust':8.,'lcc':70.}
        return {'model':model,'init':d.iso_z(self.now),'native_version':3,'offered_members':[f'{i:02}' for i in range(31)],
                'points':[dict(member=f'{i:02}',field=field,lead=lead,value=value,collected_at=d.iso_z(self.now),fetched_at=d.iso_z(self.now))
                          for i in range(31) for lead in (0,6,12) for field,value in fields.items()]}

    def test_full_chart_roundtrips_all_supported_fields_and_member_counts(self):
        from kcdw.event_ensemble import MODELS, VARIABLES
        packet=self.packet();end=self.now+timedelta(hours=12)
        with patch.object(d,'collect_native',return_value=packet):
            chart=d.collect_chart(object(),MODELS[0],None,self.now,end,self.now+timedelta(minutes=1))
        self.assertIsNotNone(chart)
        d.validate_normalized(chart,MODELS[0],self.now)
        for field in VARIABLES:
            self.assertEqual(chart['hourly'][field]['sample_counts'][1:],[31]*11)
        self.assertEqual(chart['hourly']['cloud_cover_low']['p50'][1],70.)
        packet['points']=[p for p in packet['points'] if not(p['member']=='30' and p['field']=='gust' and p['lead']==6)]
        with patch.object(d,'collect_native',return_value=packet):
            self.assertIsNone(d.collect_chart(object(),MODELS[0],None,self.now,end,self.now+timedelta(minutes=1)))

    def test_below_ground_levels_are_masked_not_a_failed_native_source(self):
        from kcdw.event_ensemble import MODELS
        packet=self.packet()
        with patch.object(d,'collect_native',return_value=packet):
            data=d.collect_rh(object(),MODELS[0],self.now,self.now+timedelta(hours=12),self.now+timedelta(minutes=1))
        self.assertIsNotNone(data)
        self.assertEqual(data['hourly']['relative_humidity_1000hPa']['sample_counts'],[0]*12)
        self.assertEqual(data['hourly']['relative_humidity_2m']['p50'],[101.]*12)
        self.assertEqual(data['hourly']['relative_humidity_925hPa']['sample_counts'],[31]*12)

    def test_packet_validation_keeps_members_separate_from_field_identity(self):
        from kcdw.direct_ensemble_worker import expected_identity, url_for
        points=[]
        for field,value in [('msl',101000.),('r850',102.)]:
            points.append(dict(model='gefs',init=d.iso_z(self.now),lead=6,member='00',field=field,value=value,
                latitude=41.,longitude=-74.5,url=url_for('gefs',self.now,6,'00',field=field),range=[0,99],sha256='a'*64,
                identity=expected_identity('gefs',self.now,6,'00',field),collected_at=d.iso_z(self.now),fetched_at=d.iso_z(self.now)))
        packet=dict(model='gefs',init=d.iso_z(self.now),offered_members=[f'{i:02}' for i in range(31)],points=points)
        self.assertIs(d.validate_packet(packet,'gefs',self.now),packet)

    def test_packing_precision_allows_only_documented_tiny_rain_decreases(self):
        packet={'model':'aifs_ens','init':d.iso_z(self.now),'points':[
            {'member':'00','field':'tp','lead':6,'value':.75390625,'packing_error':.001953125},
            {'member':'00','field':'tp','lead':12,'value':.75,'packing_error':.00390625}]}
        axis=[d.iso_z(self.now+timedelta(hours=7))]
        self.assertEqual(d.hourly_members(packet,axis)['precipitation']['00'],[0.])
        packet['points'][1]['value']=.7
        self.assertEqual(d.hourly_members(packet,axis)['precipitation']['00'],[None])

    def test_model_specific_native_capabilities_are_explicit(self):
        self.assertEqual(d.PROVIDERS['geps'],'ECCC')
        self.assertIn('wind_gusts_10m',d.UNAVAILABLE['aifs_ens'])
        self.assertIn('cloud_cover_low',d.UNAVAILABLE['ecmwf_ens'])
        self.assertEqual(d.UNAVAILABLE['gefs'],[])
