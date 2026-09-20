import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from kcdw import event_moisture_view as view
from test_events import EVENT

NOW = datetime(2026, 9, 15, 1, tzinfo=timezone.utc)
START = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
TIMES = [START + timedelta(hours=i) for i in range(10)]


def fixture():
    hourly = {'time': [t.isoformat().replace('+00:00','Z') for t in TIMES],
              'relative_humidity_2m': [95]*10, 'surface_pressure':[1010]*10}
    for level in (1000,925,850):
        hourly[f'relative_humidity_{level}hPa']=[98]*10
        hourly[f'geopotential_height_{level}hPa']=[200 if level==1000 else 800 if level==925 else 1500]*10
    return {'models': {key:{'available':True,'error':'','data':{'hourly':hourly,'fetched_at':NOW.isoformat()}}
                       for key in ('gfs','ifs','aifs_single')}}


class MoistureViewTests(unittest.TestCase):
    def test_supersaturation_is_retained_and_fits_visible_axis(self):
        from kcdw.event_renderer import Chart
        data=fixture()
        for source in data['models'].values():
            for field,_ in view.FIELDS:source['data']['hourly'][field]=[102.25]*10
        with patch.object(view,'validated',return_value=data), patch('kcdw.event_renderer.Chart',wraps=Chart) as chart:
            text=view.render_moisture({},TIMES,(0,9),NOW)
        self.assertIn('102.25',text)
        self.assertTrue(all(call.args[2]>=102.25 for call in chart.call_args_list))

    def test_four_rh_charts_share_axis_and_preserve_model_identity(self):
        with patch.object(view,'validated',return_value=fixture()):
            text=view.render_moisture({},TIMES,(0,9),NOW)
        self.assertEqual(text.count('data-moisture-field='),4)
        self.assertEqual(text.count('class="fixed-y-axis"'),4)
        self.assertEqual(text.count('data-sync-group="forecast"'),4)
        self.assertEqual(text.count('data-rh-model="gfs"'),4)
        self.assertIn('AIFS Single',text)
        self.assertNotIn('<polygon',text)
        self.assertIn('id="rh-gfs" type="checkbox" checked',text)

    def test_ensemble_ranges_and_member_counts_are_visible_by_default(self):
        data=fixture()
        hourly={'time':[t.isoformat().replace('+00:00','Z') for t in TIMES]}
        for field,_ in view.FIELDS:
            hourly[field]={'p10':[30]*10,'p50':[60]*10,'p90':[90]*10,'sample_counts':[31]*10}
        data['models']['gefs']={'available':True,'data':{'hourly':hourly,'members':31,'fetched_at':NOW.isoformat()}}
        with patch.object(view,'validated',return_value=data):
            text=view.render_moisture({},TIMES,(0,9),NOW)
            evidence=view.moisture_evidence({'event':EVENT.as_dict()},NOW)
        self.assertIn('id="rh-bands" type="checkbox" checked',text)
        self.assertEqual(text.count('class="rh-band"'),4)
        self.assertEqual(text.count('<polygon'),4)
        self.assertIn('Members per available hour: NCEP GEFS 31.',text)
        self.assertEqual(text.count('Members per available hour'),1)
        gefs=next(m for m in evidence['models'] if m['key']=='gefs')
        self.assertEqual(gefs['samples'][0]['surface_RH_percent'],{'p10':30,'p50':60,'p90':90,'members':31})
        self.assertIn('p10',json.dumps(gefs))

    def test_count_summary_preserves_per_level_eligibility_in_details(self):
        data=fixture()
        hourly={'time':[t.isoformat() for t in TIMES]}
        for index,(field,_) in enumerate(view.FIELDS):
            hourly[field]={'p10':[30]*10,'p50':[60]*10,'p90':[90]*10,'sample_counts':[31-index]*10}
        data['models']['gefs']={'available':True,'data':{'hourly':hourly,'members':31,'fetched_at':NOW.isoformat()}}
        with patch.object(view,'validated',return_value=data):
            text=view.render_moisture({},TIMES,(0,9),NOW)
        self.assertEqual(text.count('Members per available hour'),1)
        self.assertIn('NCEP GEFS 28–31',text)
        detail=text.split('<details class="chart-reading">',1)[1]
        for index,(_,label) in enumerate(view.FIELDS):
            self.assertIn(label+': NCEP GEFS '+str(31-index),detail)

    def test_flat_lines_are_compact_but_hourly_hover_values_are_preserved(self):
        import re
        import html
        with patch.object(view,'validated',return_value=fixture()):
            text=view.render_moisture({},TIMES,(0,9),NOW)
        points=re.search(r'<polyline points="([^"]+)"',text).group(1).split()
        self.assertEqual(len(points),2)
        values=json.loads(html.unescape(re.search(r'data-values="([^"]+)"',text).group(1)))
        self.assertEqual(len(values),len(TIMES))

    def test_three_hour_plot_samples_keep_full_hourly_values(self):
        import re
        import html
        times=[START+timedelta(hours=i) for i in range(25)]
        data=fixture()
        hourly={'time':[t.isoformat().replace('+00:00','Z') for t in times]}
        hourly.update({field:[20+(i%2)*50 for i in range(25)] for field,_ in view.FIELDS})
        for source in data['models'].values():
            source['data']['hourly']=hourly
        with patch.object(view,'validated',return_value=data):
            text=view.render_moisture({},times,(2,20),NOW)
        points=re.search(r'<polyline points="([^"]+)"',text).group(1).split()
        self.assertLess(len(points),len(times))
        values=json.loads(html.unescape(re.search(r'data-values="([^"]+)"',text).group(1)))
        self.assertEqual(values,hourly['relative_humidity_2m'])
        self.assertIn('data-axis-start="2026-09-24T12:00:00Z"',text)
        self.assertIn('data-axis-end="2026-09-25T12:00:00Z"',text)
        self.assertRegex(text,r'>Sep 25</span>')

    def test_missing_hours_are_gaps_and_stale_models_are_not_drawn(self):
        data=fixture()
        data['models']['ifs']={'available':False,'error':'secret error'}
        data['models']['gfs']['data']['hourly']['relative_humidity_925hPa'][4]=None
        with patch.object(view,'validated',return_value=data):
            text=view.render_moisture({},TIMES,(0,9),NOW)
        self.assertNotIn('data-rh-model="ifs"',text)
        self.assertIn('ECMWF IFS unavailable',text)
        self.assertNotIn('secret error',text)
        self.assertIn('null',text)

    def test_compact_evidence_carries_only_requested_samples_and_units(self):
        data=fixture()
        data['models']['gfs']['data']['secret']='DO_NOT_FORWARD'
        with patch.object(view,'validated',return_value=data):
            packet=view.moisture_evidence({'event':EVENT.as_dict()},NOW)
        self.assertEqual(len(packet['models']),6)
        first=packet['models'][0]
        self.assertEqual(len(first['samples']),3)
        self.assertEqual(first['samples'][0]['surface_RH_percent'],95)
        self.assertEqual(first['samples'][0]['levels'][1]['height_m_MSL'],800)
        self.assertNotIn('DO_NOT_FORWARD',json.dumps(packet))
        self.assertLess(len(json.dumps(packet)),7000)
