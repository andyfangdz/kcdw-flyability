import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kcdw import calibrated_wind as cw
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc


def hour(h, lead=3):
    # SNAPSHOT: event 2026-09-24, flight 10:00-12:00 EDT (UTC-4).
    valid = datetime(2026, 9, 24, tzinfo=UTC) + timedelta(hours=h + 4)
    init = datetime(2026, 9, 24 - lead, 0, tzinfo=UTC)
    return {'hour': h, 'valid_utc': valid.strftime('%Y-%m-%dT%H:%M:%SZ'), 'init_utc': init.strftime('%Y-%m-%dT%H:%M:%SZ'), 'lead_day': lead,
            'sust_kt': [4.9, 5.8, 6.8], 'gust_kt': [12.1, 14.1, 16.8], 'metar_peak_kt': [4.6, 6.9, 12.1], 'dir_deg': 242,
            'dir_confidence': 0.95, 'p_gust_ge20': 0.01, 'p_spread_ge10': 0.18, 'p_metar_gust': 0.13}


def forecast(generated=NOW - timedelta(hours=2)):
    return {'version': 1, 'generated_at': generated.strftime('%Y-%m-%dT%H:%M:%SZ'), 'station': 'KCDW',
            'trained_at': (generated - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'training_period': ['2021-06-01', '2026-09-10'],
            'quantiles': [0.1, 0.5, 0.9], 'coverage_target': 0.8,
            'skill': {'period': ['2024-11-14', '2026-09-11'], 'hours': 60752, 'afternoon_hours': 15439,
                      'gust_mae_kt': {'calibrated': 3.34, 'nbm': 4.17, 'raw_ecmwf_ens': 4.24}, 'sust_mae_kt': {'calibrated': 1.64, 'nbm': 2.02}},
            'days': {'2026-09-24': [hour(h) for h in range(6, 22)]}}


class CalibratedWindTests(unittest.TestCase):
    def collect(self, data, now=NOW):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'forecast.json'
            path.write_text(json.dumps(data))
            snapshot = copy.deepcopy(SNAPSHOT)
            snapshot['calibrated_wind'] = cw.collect_calibrated(snapshot, path, now)
            return snapshot

    def test_collects_validates_and_summarizes_the_flight(self):
        snapshot = self.collect(forecast())
        cw.validate_calibrated(snapshot['calibrated_wind'], snapshot)
        s = cw.summary(snapshot['calibrated_wind'], snapshot)
        self.assertEqual(s['hours_local'], [10, 11])
        self.assertEqual(s['peak_gust_kt'], [12.1, 14.1, 16.8])
        self.assertEqual(cw.calibrated_evidence(snapshot, NOW)['skill_1_4pm']['gust_mae_kt']['nbm'], 4.17)

    def test_render_marks_flight_and_escapes_small_probabilities(self):
        data = forecast()
        data['days']['2026-09-24'][0]['p_gust_ge20'] = 0.001
        data['days']['2026-09-24'][4]['p_gust_ge20'] = 0.001  # 10 a.m., inside the table
        html = cw.render_calibrated(self.collect(data), NOW)
        self.assertIn('id="calibrated-wind"', html)
        self.assertEqual(html.count('<tr data-flight>'), 2)
        self.assertIn('&lt;1%', html)
        self.assertIn('from 242°, with a true peak gust around 14 kt (80% range 12–17)', html)
        self.assertIn('versus 4.2 kt for NBM', html)

    def test_stale_missing_or_inconsistent_forecasts_are_not_shown(self):
        self.assertIsNone(self.collect(forecast(NOW - timedelta(hours=40)))['calibrated_wind'])
        other = forecast(); other['days'] = {'2026-09-25': other['days']['2026-09-24']}
        self.assertIsNone(self.collect(other)['calibrated_wind'])
        for mutate in (lambda d: d['days']['2026-09-24'][3].update(gust_kt=[16, 14, 12]),
                       lambda d: d['days']['2026-09-24'][3].update(valid_utc='2026-09-24T12:00:00Z'),
                       lambda d: d['days']['2026-09-24'][3].update(lead_day=5),
                       lambda d: d['days']['2026-09-24'][3].update(p_metar_gust=1.4),
                       lambda d: d.update(generated_at=(NOW + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))):
            data = forecast(); mutate(data)
            with self.assertRaises(ValueError):
                self.collect(data)

    def test_tampered_packet_renders_nothing(self):
        snapshot = self.collect(forecast())
        snapshot['calibrated_wind']['hours'][2]['sust_kt'] = [1, 900, 2]
        self.assertEqual(cw.render_calibrated(snapshot, NOW), '')
        self.assertIsNone(cw.calibrated_evidence(snapshot, NOW))

    def test_missing_file_is_simply_unavailable(self):
        self.assertIsNone(cw.collect_calibrated(copy.deepcopy(SNAPSHOT), '/nonexistent/forecast.json', NOW))


if __name__ == '__main__':
    unittest.main()
