"""Integration behavior; synthetic data only, never presented as live weather."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch



class WeatherNextIntegration(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())

    def test_invalid_wn3_cannot_be_preferred_and_fallback_is_dynamic(self):
        from kcdw.evidence import model_guidance_policy
        self.snapshot['sources']['weather_next3'] = {'ok': True, 'data': {}}
        self.snapshot['sources']['aifs_ens'] = {'ok': True}
        self.assertEqual(model_guidance_policy(self.snapshot)['preferred_after_48h'], 'ECMWF AIFS-ENS')
        self.snapshot['sources']['aifs_ens']['ok'] = False
        self.snapshot['sources']['weather_next'] = {'ok': True}
        self.assertEqual(model_guidance_policy(self.snapshot)['preferred_after_48h'], 'WeatherNext 2')
        self.snapshot['sources']['weather_next']['ok'] = False
        self.assertIsNone(model_guidance_policy(self.snapshot)['preferred_after_48h'])

    def test_prepare_uses_validated_compact_wn3_without_mutating_archive(self):
        from kcdw.evidence import prepare
        payload = {'forecast': {'fields': {'private_hourly': [1, 2, 3]}}}
        self.snapshot['sources']['weather_next3'] = {'ok': True, 'data': payload}
        original = copy.deepcopy(self.snapshot)
        compact = {'model': 'WeatherNext 3', 'daily': [{'derived': 'internal_summary'}]}
        with patch('kcdw.weathernext3.validate_weather_next3') as validate, patch('kcdw.weathernext3.summarize_weather_next3', return_value=compact):
            prepared = prepare(self.snapshot)
        self.assertTrue(validate.called)
        self.assertEqual(prepared['model_guidance_policy']['preferred_after_48h'], 'WeatherNext 3')
        self.assertEqual(prepared['sources']['weather_next3']['data'], compact)
        self.assertNotIn('private_hourly', json.dumps(prepared))
        self.assertEqual(self.snapshot, original)

    def test_wn3_never_satisfies_short_term_publication_quorum(self):
        from kcdw.readiness import evidence_readiness
        self.snapshot['sources'] = {'weather_next3': {'ok': True, 'data': {}}}
        self.assertEqual(evidence_readiness(self.snapshot), ([], []))

    def test_source_row_displays_run_and_fetch_without_forecast_values(self):
        from kcdw.renderer import source_rows
        from kcdw.weathernext3 import SOURCE, LEGACY_SOURCE
        self.snapshot['sources'] = {'weather_next3': {'ok': True, 'fetched_at': self.snapshot['collected_at'], 'data': {
            'status': {'actual_run_utc': '2026-09-12T12:00:00Z', 'fetched_at': '2026-09-12T23:00:00Z', 'fallback': False},
            'forecast': {'fields': {'private_hourly': [123456.789]}}
        }}}
        forecast = self.snapshot['sources']['weather_next3']['data']['forecast']
        for provider in (SOURCE, LEGACY_SOURCE):
            with self.subTest(provider=provider):
                forecast['source'] = provider
                html = source_rows(self.snapshot)
                self.assertIn(provider, html)
                self.assertNotIn(LEGACY_SOURCE if provider == SOURCE else SOURCE, html)
                self.assertIn('2026-09-12T12:00:00Z', html)
                self.assertIn('2026-09-12T23:00:00Z', html)
                self.assertNotIn('123456.789', html)

    def test_prepublication_rejects_invalid_wn3_claimed_available(self):
        from kcdw.validation import validate_snapshot_readiness, ValidationError
        self.snapshot['sources']['weather_next3'] = {'ok': True, 'fetched_at': self.snapshot['collected_at'], 'data': {}}
        with self.assertRaisesRegex(ValidationError, 'WeatherNext 3'):
            validate_snapshot_readiness(self.snapshot)


if __name__ == '__main__':
    unittest.main()
