import unittest
from unittest.mock import patch
from kcdw.event_narrative_evidence import build_event_evidence
from test_events import NOW
import test_event_comparison as comparison

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


    def test_validated_low_cloud_evidence_has_own_citable_source(self):
        snapshot=comparison.ComparisonTests().snapshot()
        packet={'ceiling':{'diagnostic':'validated'},'layers':None}
        with patch('kcdw.event_narrative_evidence.low_cloud_evidence',return_value=packet,create=True):
            result=build_event_evidence(snapshot,NOW)
        source=next(s for s in result['sources'] if s['id']=='low_cloud_analysis')
        self.assertEqual(source['status'],'available')
        self.assertEqual(source['evidence'],packet)
