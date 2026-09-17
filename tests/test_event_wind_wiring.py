"""Wind research is snapshot-bound supplemental evidence, not readiness."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from kcdw import event_renderer, event_update
from kcdw.event_narrative_evidence import build_event_evidence
import test_event_comparison as comparison
from test_events import EVENT, NOW

SURFACE={'window':{'start':'2026-09-24T14:00:00Z','end':'2026-09-24T16:00:00Z'},
 'ensembles':{'gefs':{'label':'GEFS','fetched_at':NOW.isoformat(),
 'advertised_init':NOW.isoformat(),'data_end':'2026-10-01T00:00:00Z','binding':'latest-advertised; not response-bound',
 'sustained':{'p50':8,'p90':13,'n':31},'direction_counts':{'NE':18,'E':11,'N':1,'S':1},
 'gust_max':{'p50':15,'p90':28,'n':31,'ge20':8,'ge25':4,'ge30':2},
 'crosswind':{'04':{'p50':6,'p90':11,'n':31,'ge15':1},'10':{'p50':8,'p90':18,'n':31,'ge15':6}}},'aifs_ens':None},
 'nws':{'issued_at':NOW.isoformat(),'fetched_at':NOW.isoformat(),'url':'https://api.weather.gov/gridpoints/OKX/23,48',
 'samples':[{'at':'2026-09-24T14:00:00Z','wind_kt':6,'gust_kt':13,'from_deg':50}]},'notes':[]}
NATIVE={'window':SURFACE['window'],'models':{'gfs':{'label':'GFS','init':NOW.isoformat(),'fetched_at':NOW.isoformat(),
 'samples':[{'at':'2026-09-24T15:00:00Z','surface':{'speed_kt':7.3,'from_true_deg':65},
 '925':{'speed_kt':15,'from_true_deg':68},'850':{'speed_kt':19,'from_true_deg':85},
 'gust':{'kt':11.5,'start':'2026-09-24T15:00:00Z','end':'2026-09-24T15:00:00Z','semantics':'instantaneous gust diagnostic'}}]}},'notes':[]}

class WindWiringTests(unittest.TestCase):
    def snapshot(self):
        s=comparison.ComparisonTests().snapshot()
        s.update(event_wind={'marker':True},native_wind={'marker':True},wind_trends=None)
        return s

    def test_grid_styles_are_shared_without_changing_geometry(self):
        from datetime import datetime,timedelta,timezone
        import re,xml.etree.ElementTree as ET
        start=datetime(2026,9,17,4,tzinfo=timezone.utc)
        c=event_renderer.Chart([start+timedelta(hours=h) for h in range(49)],0,10,(0,2))
        page=c.render('Test','kt','note',[],[0,5,10])
        root=ET.fromstring(re.search(r'<svg.*?</svg>',page).group())
        groups=root.findall('g')
        self.assertEqual({g.get('stroke') for g in groups},{'#d4dad2','#c9d0c7','#a9b3a8'})
        self.assertTrue(all(g.get('stroke-width')=='1' for g in groups))
        lines=[line for g in groups for line in g.findall('line')]
        self.assertEqual(len(lines),8)
        self.assertTrue(all('stroke' not in line.attrib and 'stroke-width' not in line.attrib for line in lines))
        day=next(g for g in groups if g.get('stroke')=='#c9d0c7')
        self.assertEqual([float(line.get('x1')) for line in day],[round(c.x(i),1) for i in (0,24,48)])

    def test_legacy_archives_gain_neither_sources_nor_section(self):
        from kcdw.event_wind_view import render_wind, wind_sources
        s=comparison.ComparisonTests().snapshot()
        self.assertEqual(render_wind(s,NOW),'')
        self.assertEqual(wind_sources(s,NOW),{})
        self.assertFalse(any(x['id'].startswith('wind_') for x in build_event_evidence(s,NOW)['sources']))

    def test_wind_is_citable_preserves_weather_and_health(self):
        s=self.snapshot();original=copy.deepcopy(s)
        with patch('kcdw.event_wind_view.wind_sources',return_value={'wind_surface':SURFACE,'wind_native':NATIVE,'wind_trends':None}):
            evidence=build_event_evidence(s,NOW)
            page,health=event_renderer.render(s,NOW)
        sources={x['id']:x for x in evidence['sources']}
        self.assertEqual(sources['wind_surface']['status'],'available')
        self.assertEqual(sources['wind_native']['status'],'available')
        self.assertEqual(sources['wind_trends']['status'],'unavailable')
        self.assertIn('id="wind-analysis"',page)
        self.assertIn('href="#wind-analysis"',page)
        _,before=event_renderer.render(comparison.ComparisonTests().snapshot(),NOW)
        self.assertEqual(health,before)
        self.assertEqual(s,original)
        self.assertLessEqual(len(json.dumps(evidence).encode()),60000)

    def test_wind_packets_trigger_lossless_moisture_compaction(self):
        fan={'p10':20,'p50':60,'p90':95,'members':31}
        raw={'models':[{'key':'gefs','available':True,'statistic':'p10/p50/p90',
            'samples':[{'at':f'2026-09-24T{h}:00:00Z','surface_RH_percent':fan,
                'levels':[{'pressure_hPa':p,'RH_percent':fan,'height_m_MSL':None} for p in (1000,925,850)]}
                for h in ('12','16','20')]}]}
        with patch('kcdw.event_wind_view.wind_sources',return_value={'wind_surface':SURFACE}), \
             patch('kcdw.event_moisture_view.moisture_evidence',return_value=copy.deepcopy(raw)):
            evidence=build_event_evidence(self.snapshot(),NOW)
        packed=next(s['evidence'] for s in evidence['sources'] if s['id']=='low_level_rh')
        self.assertIn('sample_columns',packed)
        self.assertEqual(packed['sample_columns'],['at','surface_RH_percent','levels'])
        def fan_back(v):
            return dict(zip(('p10','p50','p90','members'),v)) if isinstance(v,list) else v
        model=packed['models'][0]
        unpacked=[{'at':at,'surface_RH_percent':fan_back(rh),
            'levels':[{'pressure_hPa':p,'RH_percent':fan_back(r),'height_m_MSL':fan_back(height)} for p,r,height in levels]}
            for at,rh,levels in model['sample_rows']]
        self.assertEqual(unpacked,raw['models'][0]['samples'])
        self.assertLess(len(json.dumps(model['sample_rows'])),len(json.dumps(raw['models'][0]['samples'])))

    def test_snapshot_change_compaction_is_lossless(self):
        from kcdw.event_evidence_compact import compact_changes
        pair={'alias':'GEFS','previous':{'fetched_at':'old','grid':{'latitude':41.,'longitude':-74.25},
            'run_binding':'rolling','samples':{'morning':{'surface':{'p10':20,'p50':60,'p90':95,'members':31},'925hPa':None},'noon':None,'afternoon':None}},
            'current':{'metric':{'mean':8,'p10':4,'p90':12,'unit':'kt'},'unknown':[True,False,None]}}
        raw={'guidance':{'gefs':pair},'notes':['Unchanged guidance'],'future_field':{'untouched':3}}
        packed=compact_changes(raw)
        def restore(value):
            if isinstance(value,list):
                if value and isinstance(value[0],str) and value[0].startswith('@') and value[0][1:] in packed['column_layouts']:
                    return {key:restore(v) for key,v in zip(packed['column_layouts'][value[0][1:]],value[1:])}
                return [restore(v) for v in value]
            if isinstance(value,dict):return {k:restore(v) for k,v in value.items()}
            return value
        decoded=restore({k:v for k,v in packed.items() if k not in ('column_layouts','row_encoding')})
        self.assertEqual(decoded,raw)
        self.assertEqual(raw['guidance']['gefs']['previous']['samples']['morning']['surface']['members'],31)

    def test_budget_preserves_changes_before_secondary_official_prose(self):
        changes={'notes':['comparison '*1700]}
        surface=copy.deepcopy(SURFACE)
        surface['ensembles']['gefs']['samples']=[{'at':'2026-09-24T14:00:00Z','sustained':{'p50':8,'p90':13,'n':31},'direction_counts':{'NE':31}}]*240
        product={'id':'wpc_test','status':'current','coverage':'ends before mission',
                 'issued_at':NOW.isoformat(),'valid_start':NOW.isoformat(),'valid_end':NOW.isoformat(),
                 'text':'regional words '*300,'scope':'Regional only','title':'WPC'}
        with patch('kcdw.event_wind_view.wind_sources',return_value={'wind_surface':surface,'wind_native':NATIVE}), \
             patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=changes), \
             patch('kcdw.event_narrative_evidence._official',return_value={'products':[dict(product,id=str(i)) for i in range(35)]}):
            evidence=build_event_evidence(self.snapshot(),NOW)
        sources={x['id']:x for x in evidence['sources']}
        self.assertEqual(sources['snapshot_changes']['status'],'available')
        self.assertEqual(sources['wind_surface']['status'],'available')
        self.assertEqual(sources['wind_surface']['evidence']['ensembles']['gefs']['gust_max']['ge25'],4)
        self.assertLessEqual(len(json.dumps(evidence).encode()),60000)
        for alias in ('nhc','cpc_wpc'):
            self.assertEqual(sources[alias]['status'],'available')
            self.assertTrue(all(p['coverage']=='ends before mission' for p in sources[alias]['evidence']['products']))

    def test_tables_escape_labels_and_show_missing_gusts_and_assumptions(self):
        from kcdw.event_wind_view import render_wind
        surface=copy.deepcopy(SURFACE);surface['ensembles']['gefs']['label']='<script>GEFS</script>'
        with patch('kcdw.event_wind_view.wind_sources',return_value={'wind_surface':surface,'wind_native':NATIVE,'wind_trends':None}):
            page=render_wind(self.snapshot(),NOW)
        self.assertGreaterEqual(page.count('<table'),3)
        self.assertIn('scope="col"',page)
        self.assertIn('scope="row"',page)
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('Unavailable',page)
        self.assertIn('mean direction',page)
        self.assertIn('not calibrated probabilities',page)
        self.assertIn('925',page)
        self.assertIn('11.5 · instant',page)
        self.assertNotIn('11:00–11:00 maximum',page)

    def test_collections_precede_narrative_and_fail_independently(self):
        for fail in (False,True):
            s=comparison.ComparisonTests().snapshot()
            def narrative(data,*_):
                self.assertEqual(data['event_wind'],None if fail else {'surface':1})
                self.assertEqual(data['native_wind'],{'native':1})
                self.assertEqual(data['wind_trends'],{'history':1})
                return None
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(event_update,'upcoming_events',return_value=[EVENT]), \
                 patch.object(event_update,'collect_event',return_value=s), \
                 patch.object(event_update,'collect_afds',return_value=None), \
                 patch.object(event_update,'collect_ceiling',return_value=None), \
                 patch.object(event_update,'collect_layer_signals',return_value=None), \
                 patch.object(event_update,'collect_wind',side_effect=ValueError('private') if fail else None,return_value={'surface':1}), \
                 patch.object(event_update,'collect_native_wind',return_value={'native':1}), \
                 patch.object(event_update,'build_wind_trends',return_value={'history':1}), \
                 patch.object(event_update,'generate_event_narrative',side_effect=narrative), \
                 patch.object(event_update,'render',return_value=('<html/>',{})):
                self.assertEqual(event_update.update(Path(tmp),None,NOW),0)
