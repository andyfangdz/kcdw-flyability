import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from kcdw import event_update, event_renderer
from kcdw.event_narrative_evidence import build_event_evidence
from test_events import EVENT, NOW
from test_event_comparison import ComparisonTests

class LowCloudWiringTests(unittest.TestCase):
    def test_columnar_evidence_preserves_profiles_and_member_identity_counts(self):
        from test_cloud_layer_signals import Client, SNAPSHOT, NOW as CLOCK
        from kcdw.cloud_layer_signals import collect_layer_signals
        from kcdw.low_cloud_analysis import narrative_cloud_evidence
        s = dict(SNAPSHOT)
        s['cloud_layer_signals'] = collect_layer_signals(Client(), s, CLOCK)
        layer = narrative_cloud_evidence(s, CLOCK)['layers']
        row = dict(zip(layer['profile_columns'], layer['profiles']['gfs'][0]))
        self.assertEqual(row['rh925_pct'], 95)
        self.assertEqual(row['height925_msl_m'], 780)
        self.assertEqual(row['temperature850_c'], 10)
        self.assertEqual(row['wind_from_deg'], 180)
        self.assertEqual(layer['ensembles']['gefs'][-1], ['all_three', [3,3], [3,3], [3,3], [3,3]])

    def test_missing_ceiling_is_unknown_not_zero_or_clearing(self):
        from kcdw.low_cloud_analysis import render_low_cloud
        with patch('kcdw.low_cloud_analysis.low_cloud_evidence', return_value={'ceiling':None,'layers':None}):
            page = render_low_cloud({}, NOW)
        self.assertIn('unavailable', page)
        self.assertNotIn('0 ft', page)
        self.assertNotIn('clearing', page)

    def test_profile_and_member_failure_remains_independent(self):
        from test_cloud_layer_signals import Client, SNAPSHOT, NOW as CLOCK
        from kcdw.cloud_layer_signals import collect_layer_signals
        from kcdw.low_cloud_analysis import render_low_cloud, low_cloud_evidence
        s = dict(SNAPSHOT)
        s['cloud_layer_signals'] = collect_layer_signals(Client(), s, CLOCK)
        s['cloud_layer_signals']['profiles']['ifs']['data']['points'][0]['levels']['925']['relative_humidity'] = 101
        self.assertIsNone(low_cloud_evidence(s, CLOCK)['layers']['profiles']['ifs'])
        page = render_low_cloud(s, CLOCK)
        self.assertIn('Cloudy ensemble members', page)
        self.assertIn('3/3', page)
        self.assertIn('Current native ceiling unavailable', page)

    def test_new_diagnostics_reach_narration_and_archive(self):
        snapshot=ComparisonTests().snapshot()
        ceiling={'marker':'native'}; layers={'marker':'profiles and members'}
        def narrative(s, directory, now):
            self.assertEqual(s['cloud_ceiling'], ceiling)
            self.assertEqual(s['cloud_layer_signals'], layers)
            return None
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(event_update,'upcoming_events',return_value=[EVENT]), \
             patch.object(event_update,'collect_event',return_value=snapshot), \
             patch.object(event_update,'collect_wind',return_value=None), \
             patch.object(event_update,'collect_native_wind',return_value=None), \
             patch.object(event_update,'build_wind_trends',return_value=None), \
             patch.object(event_update, 'collect_afds', return_value=None), \
             patch.object(event_update,'collect_ceiling',return_value=ceiling,create=True) as c, \
             patch.object(event_update,'collect_layer_signals',return_value=layers,create=True) as l, \
             patch.object(event_update,'generate_event_narrative',side_effect=narrative) as n, \
             patch.object(event_update,'render',return_value=('<html>ok</html>',{})):
            self.assertEqual(event_update.update(Path(tmp),None,NOW),0)
            self.assertEqual(c.call_count,1); self.assertEqual(l.call_count,1)
            self.assertIsNone(snapshot['event_narrative'])
            n.assert_called_once()
            self.assertIn('cloud_ceiling', (Path(tmp)/'events'/EVENT.slug/'current/snapshot.json').read_text())

    def test_validated_low_cloud_evidence_has_own_citable_source(self):
        snapshot=ComparisonTests().snapshot()
        packet={'ceiling':{'diagnostic':'validated'},'layers':None}
        with patch('kcdw.event_narrative_evidence.low_cloud_evidence',return_value=packet,create=True):
            result=build_event_evidence(snapshot,NOW)
        source=next(s for s in result['sources'] if s['id']=='low_cloud_analysis')
        self.assertEqual(source['status'],'available')
        self.assertEqual(source['evidence'],packet)

    def test_low_cloud_section_is_visible_before_rh_charts(self):
        snapshot=ComparisonTests().snapshot()
        fragment='<section id="low-cloud-analysis"><h2>Low-cloud analysis</h2></section>'
        with patch.object(event_renderer,'render_low_cloud',return_value=fragment,create=True):
            page,_=event_renderer.render(snapshot,NOW)
        self.assertIn(fragment,page)
        self.assertIn('href="#low-cloud-analysis"',page)
