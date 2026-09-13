"""Behavior tests for the compact operational event briefing."""
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from kcdw import event_ensemble
from kcdw.event_briefing import operational_briefing
from test_events import FakeClient, EVENT, NOW, synthetic_wn3


class BriefingTests(unittest.TestCase):
    def snapshot(self):
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=synthetic_wn3()):
            return event_ensemble.collect_event(FakeClient(), EVENT, NOW)

    def test_fresh_source_numeric_window_and_unresolved_ceiling(self):
        brief = operational_briefing(self.snapshot(), NOW)
        self.assertIn('WeatherNext 3', brief['source'])
        self.assertEqual(len(brief['cards']), 4)
        self.assertIn('mean total', brief['cards'][0]['value'])
        self.assertIn('peak hourly mean', brief['cards'][1]['value'])
        self.assertEqual(brief['cards'][2]['value'], 'Not resolved')
        self.assertIn('7 days', brief['next_check']['title'])

    def test_rain_and_wind_use_separate_boundary_samples(self):
        snapshot = self.snapshot()
        forecast = snapshot['weathernext3']['data']['forecast']
        opening = forecast['valid_time_utc'].index('2026-09-24T12:00:00Z')
        closing = forecast['valid_time_utc'].index('2026-09-24T21:00:00Z')
        fields = forecast['fields']
        for stat in ('mean','p10','p90'):
            fields['wind_speed_10m'][stat] = [1.0] * 360
            fields['precipitation_1h'][stat] = [0.0] * 360
            fields['precipitation_1h'][stat][opening] = 20
            fields['wind_speed_10m'][stat][closing] = 20
        brief = operational_briefing(snapshot, NOW)
        self.assertEqual(brief['cards'][0]['value'], '0.00 mm mean total')
        self.assertEqual(brief['cards'][1]['value'], '1.9 kt peak hourly mean')
        self.assertEqual(brief['tone'], 'neutral')
        for stat in ('mean','p10','p90'):
            fields['wind_speed_10m'][stat][opening] = 20
        brief = operational_briefing(snapshot, NOW)
        self.assertEqual(brief['tone'], 'watch')
        self.assertIn('wind', brief['headline'].lower())

    def test_outage_uses_comparator_with_distinct_statistics(self):
        snapshot = self.snapshot()
        snapshot['weathernext3'] = {'ok':False, 'error':'offline'}
        brief = operational_briefing(snapshot, NOW)
        self.assertIn('AIFS-ENS', brief['source'])
        self.assertIn('median total', brief['cards'][0]['value'])
        self.assertIn('median member peak', brief['cards'][1]['value'])
        self.assertIn('WN3 unavailable', brief['summary'])

    def test_stale_snapshot_does_not_offer_current_reassurance(self):
        brief = operational_briefing(self.snapshot(), NOW + timedelta(hours=72))
        self.assertEqual(brief['tone'], 'stale')
        self.assertIn('Refresh', brief['headline'])
        self.assertTrue(all(c['value'] == 'Outdated' for c in brief['cards'] if c['label'] != 'Maneuvers ceiling'))
        self.assertIn('now', brief['next_check']['title'].lower())

    def test_near_term_official_guidance_is_not_claimed_collected(self):
        snapshot = self.snapshot()
        now = NOW + timedelta(days=11)
        snapshot['collected_at'] = now.isoformat()
        brief = operational_briefing(snapshot, now)
        self.assertIn('official aviation', brief['headline'].lower())
        self.assertIn('not collected', brief['summary'])

    def test_disagreement_is_not_hidden_by_primary_mean(self):
        snapshot = self.snapshot()
        snapshot['models']['ecmwf_ens']['data']['window']['rain_total_mm']['median'] = 8
        brief = operational_briefing(snapshot, NOW)
        self.assertEqual(brief['cards'][3]['tone'], 'watch')
        self.assertIn('rain', brief['cards'][3]['value'].lower())
        self.assertEqual(brief['tone'], 'watch')


if __name__ == '__main__': unittest.main()
