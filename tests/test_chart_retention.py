import copy
import unittest
from datetime import timedelta
from unittest.mock import patch
from tests.test_events import EVENT, NOW, FakeClient
from kcdw.event_ensemble import collect_event


class ChartRetentionTests(unittest.TestCase):
    def setUp(self):
        with patch('kcdw.event_ensemble.collect_weather_next3', side_effect=RuntimeError('offline fixture')):
            self.previous = collect_event(FakeClient(), EVENT, NOW)
        self.current = copy.deepcopy(self.previous)
        self.current['collected_at'] = (NOW+timedelta(hours=1)).isoformat().replace('+00:00','Z')
        # Mission-hour data exists, but a leading-up hour has gone missing.
        fan = self.current['models']['gefs']['data']['hourly']['pressure_msl']
        fan['sample_counts'][48] = 0
        for key in ('p10','p50','p90'):
            fan[key][48] = None

    def retain(self, candidate=None, now=None):
        from kcdw.chart_retention import retain_chart_coverage
        with patch('kcdw.chart_retention._archives', return_value=iter([(candidate or self.previous, {})])):
            retain_chart_coverage(self.current, '/unused', now or NOW+timedelta(hours=1))

    def test_total_model_outage_reaches_retention_but_default_stays_fail_closed(self):
        from kcdw.event_ensemble import MODELS
        client=FakeClient(broken=[spec.key for spec in MODELS])
        with self.assertRaisesRegex(RuntimeError,'no ensemble model'):
            collect_event(client,EVENT,NOW+timedelta(hours=1))
        with patch('kcdw.event_ensemble.collect_weather_next3',side_effect=RuntimeError('offline fixture')):
            self.current=collect_event(client,EVENT,NOW+timedelta(hours=1),allow_empty=True)
        self.assertFalse(any(source['ok'] for source in self.current['models'].values()))
        self.retain()
        self.assertTrue(all(source['ok'] for source in self.current['models'].values()))
        self.assertTrue(self.current['models']==self.previous['models'])

    def test_expired_native_packet_is_skipped_at_current_clock(self):
        previous=copy.deepcopy(self.previous)
        previous['models']['gefs']['data']['metadata']['direct_native']=True
        with patch('kcdw.chart_retention.validate_snapshot'), patch('kcdw.direct_ensemble.validate_normalized',side_effect=ValueError('native run expired')):
            self.retain(candidate=previous)
        self.assertIsNone(self.current['models']['gefs']['data']['hourly']['pressure_msl']['p50'][48])
        self.assertFalse(self.current.get('chart_retention'))

    def test_preserves_full_series_and_original_clock_without_mutating_source(self):
        previous=copy.deepcopy(self.previous)
        self.retain()
        self.assertTrue(self.current['models']['gefs']==self.previous['models']['gefs'], 'full saved GEFS series must be retained')
        self.assertEqual(self.previous,previous)
        self.assertEqual(self.current['chart_retention'][0]['source_collected_at'],self.previous['collected_at'])
        self.assertEqual(self.current['collected_at'],(NOW+timedelta(hours=1)).isoformat().replace('+00:00','Z'))

    def test_complete_new_series_is_not_replaced(self):
        self.current['models']['gefs']=copy.deepcopy(self.previous['models']['gefs'])
        self.current['models']['gefs']['data']['hourly']['pressure_msl']['p50'][48]+=0.1
        self.retain()
        self.assertFalse(self.current.get('chart_retention'))

    def test_native_core_coverage_does_not_erase_an_existing_gust_series(self):
        self.current['models']['gefs']=copy.deepcopy(self.previous['models']['gefs'])
        source=self.current['models']['gefs']['data']
        source['metadata']['direct_native']=True
        fan=source['hourly']['wind_gusts_10m']
        fan['sample_counts']=[0]*len(fan['sample_counts'])
        for key in ('p10','p50','p90'):
            fan[key]=[None]*len(fan[key])
        self.retain()
        self.assertTrue(self.current['models']['gefs']==self.previous['models']['gefs'], 'full saved GEFS series must be retained')

    def test_stale_or_wrong_range_archive_cannot_fill(self):
        original=copy.deepcopy(self.current)
        self.retain(now=NOW+timedelta(hours=13))
        self.assertEqual(self.current,original)
        wrong=copy.deepcopy(self.previous);wrong['range']['end']='2026-09-27T04:00:00Z'
        self.retain(candidate=wrong)
        self.assertEqual(self.current,original)

    def test_repeated_retention_never_renews_original_age(self):
        self.retain()
        retained=copy.deepcopy(self.current)
        self.current['models']['gefs']={'ok':False,'data':None}
        self.current.pop('chart_retention')
        self.current['collected_at']=(NOW+timedelta(hours=13)).isoformat().replace('+00:00','Z')
        self.retain(candidate=retained,now=NOW+timedelta(hours=13))
        self.assertFalse(self.current['models']['gefs']['ok'])

    def test_complete_rh_envelope_keeps_its_own_original_clock(self):
        from tests.test_event_moisture_ensemble import Client
        from kcdw.event_moisture_ensemble import collect_moisture_ensemble
        from kcdw.common import parse_time
        class RHClient(Client):
            def get(inner, url):
                if '/static/meta.json' in url:
                    return {'last_run_initialisation_time': (NOW-timedelta(hours=8)).timestamp(),
                            'last_run_availability_time': (NOW-timedelta(hours=4)).timestamp(),
                            'data_end_time': parse_time(self.previous['range']['end']).timestamp(),
                            'temporal_resolution_seconds': 10800}
                return super().get(url)
        packet=collect_moisture_ensemble(RHClient(),parse_time(self.previous['range']['start']),parse_time(self.previous['range']['end']),NOW)
        self.previous['event_moisture_ensemble']=packet
        self.current['event_moisture_ensemble']=copy.deepcopy(packet)
        self.current['event_moisture_ensemble']['models']['gefs']={'ok':False,'data':None}
        self.retain()
        self.assertEqual(self.current['event_moisture_ensemble'],packet)
        self.assertEqual(packet['collected_at'],self.previous['collected_at'])
        self.assertEqual(self.current['chart_retention'][-1]['scope'],'event_moisture_ensemble')

    def test_tampered_retention_cannot_launder_original_age(self):
        self.retain()
        retained=copy.deepcopy(self.current)
        retained['chart_retention'][0]['sha256']='bad'
        self.current['models']['gefs']={'ok':False,'data':None}
        self.current.pop('chart_retention')
        self.retain(candidate=retained)
        self.assertFalse(self.current['models']['gefs']['ok'])

    def test_retained_source_clock_reaches_page_and_narrative(self):
        from kcdw.event_renderer import render
        from kcdw.event_narrative_evidence import build_event_evidence
        self.retain()
        html,_=render(self.current,NOW+timedelta(hours=1))
        self.assertIn('Full-range data retained',html)
        evidence=build_event_evidence(self.current,NOW+timedelta(hours=1))
        self.assertEqual(evidence['retained_chart_sources'][0]['source_collected_at'],self.previous['collected_at'])

    def test_saved_note_is_visible_and_escaped(self):
        from kcdw.chart_retention import render_retention
        self.retain()
        html=render_retention(self.current)
        self.assertIn('retained',html.lower())
        self.assertIn('Open-Meteo',html)
        self.assertIn('16:00 EDT',html)
