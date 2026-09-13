"""The owner-facing report intentionally permits numerical WN3 guidance."""
import json
import unittest
from pathlib import Path
from test_weathernext3 import fixture, NOW
from kcdw.common import iso_z
from kcdw.evidence import prepare
from kcdw.renderer import render
from kcdw.validation import validate_analysis


class NumericalGuidanceTests(unittest.TestCase):
    def test_generation_receives_actual_numeric_summaries(self):
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        snapshot['collected_at'] = iso_z(NOW)
        snapshot['sources']['weather_next3'] = {'ok': True, 'fetched_at': iso_z(NOW), 'data': fixture()}
        result = prepare(snapshot)
        field = result['sources']['weather_next3']['data']['days'][1]['fields']['precipitation_1h']
        self.assertEqual(field['mean_sum_mm'], 12)
        self.assertEqual(field['hourly_p90_max'], 1.5)
        self.assertEqual(result['model_guidance_policy']['preferred_after_48h'], 'WeatherNext 3')
        self.assertIn('fields', snapshot['sources']['weather_next3']['data']['forecast'])

    def test_numeric_wn3_prose_is_valid_and_rendered(self):
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        analysis = json.loads(Path('tests/fixtures/sample_analysis.json').read_text())
        analysis['summary'] = 'Synthetic WeatherNext 3 guidance has 1 mm mean rainfall and 5 m/s mean wind.'
        validate_analysis(analysis, snapshot)
        html, _ = render(snapshot, analysis)
        self.assertIn(analysis['summary'], html)


if __name__ == '__main__': unittest.main()
