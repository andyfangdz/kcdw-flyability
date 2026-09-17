"""Deterministic GFS chart integration; all weather here is synthetic."""
import re
import unittest
from unittest.mock import patch

from kcdw import event_ensemble, event_renderer
import test_event_comparison as comparison_tests
from test_events import EVENT, NOW, FakeClient


class GfsComparisonTests(unittest.TestCase):
    def snapshot(self):
        snapshot = comparison_tests.ComparisonTests().snapshot()
        axis = snapshot['models']['gefs']['data']['hourly']['time']
        values = {'precipitation': 1, 'wind_speed_10m': 20, 'wind_gusts_10m': 30,
                  'cloud_cover_low': 90, 'pressure_msl': 999, 'temperature_2m': 14}
        snapshot['gfs'] = {'ok': True, 'data': {
            'model_id': 'gfs_global', 'model': 'GFS operational (deterministic)',
            'fetched_at': snapshot['collected_at'], 'metadata': {},
            'hourly': {'time': axis, **{key: [value] * len(axis) for key, value in values.items()}}}}
        return snapshot

    def render(self, snapshot, available=True):
        with patch.object(event_renderer, 'gfs_status', return_value={'available': available, 'error': 'stale or missing'}, create=True):
            return event_renderer.render(snapshot, NOW)

    def test_deterministic_line_on_all_six_axes_without_band_or_members(self):
        snapshot = self.snapshot()
        markup, health = self.render(snapshot)
        self.assertIn('id="compare-gfs" type="checkbox" checked', markup)
        for field in ('rain', 'wind', 'gust', 'cloud', 'temperature', 'pressure'):
            svg = comparison_tests.ComparisonTests().svg(markup, field)
            group = svg.split('<g data-model="gfs"')[1].split('</g>', 1)[0]
            self.assertIn('<polyline', group)
            self.assertNotIn('<polygon', group)
            self.assertNotIn('comparison-band', group)
            self.assertIn('Deterministic', group)
        self.assertTrue(all(v == 999 for v in comparison_tests.ComparisonTests().values(markup, 'pressure', 'gfs')))
        self.assertTrue(all(v == 20 for v in comparison_tests.ComparisonTests().values(markup, 'wind', 'gfs')))
        self.assertTrue(health['gfs']['available'])
        self.assertNotIn('gfs', snapshot['models'])
        self.assertIn('#compare-gfs:not(:checked)', markup)

    def test_missing_stale_source_explicit_and_does_not_remove_other_models(self):
        markup, health = self.render(self.snapshot(), False)
        self.assertFalse(health['gfs']['available'])
        self.assertNotIn('<g data-model="gfs"', markup)
        self.assertIn('GFS operational unavailable', markup)
        self.assertIn('<g data-model="gefs"', markup)
        self.assertIn('<g data-model="wn3"', markup)

    def test_missing_values_stay_gaps_without_invented_band(self):
        snapshot = self.snapshot()
        snapshot['gfs']['data']['hourly']['precipitation'][48] = None
        markup, _ = self.render(snapshot)
        svg = comparison_tests.ComparisonTests().svg(markup, 'rain')
        group = svg.split('<g data-model="gfs"')[1].split('</g>', 1)[0]
        self.assertEqual(group.count('<polyline'), 2)
        self.assertNotIn('<polygon', group)

    def test_event_window_summary_uses_opening_wind_and_closing_rain(self):
        snapshot = self.snapshot()
        h = snapshot['gfs']['data']['hourly']
        opening, closing = h['time'].index('2026-09-24T12:00:00Z'), h['time'].index('2026-09-24T21:00:00Z')
        h['precipitation'][opening] = 99  # outside preceding-hour window
        h['precipitation'][closing] = 5
        h['wind_speed_10m'][opening] = 40
        h['wind_speed_10m'][closing] = 99  # outside instantaneous window
        markup, _ = self.render(snapshot)
        summary = markup.split('class="gfs-window"')[1].split('</p>', 1)[0]
        self.assertIn('13.0 mm', summary)
        self.assertIn('40.0 kt', summary)
        self.assertNotIn('99.0', summary)

    def test_missing_window_rain_is_not_summed_as_zero(self):
        snapshot = self.snapshot()
        h = snapshot["gfs"]["data"]["hourly"]
        h["precipitation"][h["time"].index("2026-09-24T16:00:00Z")] = None
        markup, _ = self.render(snapshot)
        summary = markup.split('class="gfs-window"')[1].split("</p>", 1)[0]
        self.assertIn("unavailable (window gaps) rain", summary)
        self.assertNotIn("8.0 mm", summary)

    def test_evening_utc_rollover_preserves_gfs_and_full_chart_axis(self):
        from datetime import datetime
        for stamp in ('2026-09-14T23:59:00+00:00', '2026-09-15T00:00:00+00:00',
                      '2026-09-15T03:59:00+00:00', '2026-09-15T04:00:00+00:00'):
            with self.subTest(stamp=stamp):
                now = datetime.fromisoformat(stamp)
                start, end = event_ensemble.event_range(EVENT, now)
                utc_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
                with patch.object(event_ensemble, 'collect_model', return_value={}), \
                     patch.object(event_ensemble, 'collect_weathernext_comparator', return_value={}), \
                     patch.object(event_ensemble, 'collect_weather_next3', return_value={}), \
                     patch('kcdw.synoptic_context.collect_context', return_value={}), \
                     patch.object(event_ensemble, 'collect_gfs', return_value={'ok': True}) as collect:
                    result = event_ensemble.collect_event(FakeClient(), EVENT, now)
                self.assertEqual(collect.call_args.args[1:3], (max(start, utc_day), end))
                self.assertEqual(result['range']['start'], event_ensemble.iso_z(start))
                self.assertTrue(result['gfs']['ok'])

    def test_collector_wires_independent_gfs_without_ensemble_count_change(self):
        expected = {'ok': False, 'data': None, 'error': 'GFS offline'}
        with patch.object(event_ensemble, 'collect_gfs', return_value=expected, create=True) as collect:
            snapshot = event_ensemble.collect_event(FakeClient(), EVENT, NOW)
        self.assertEqual(snapshot['gfs'], expected)
        self.assertEqual(len(snapshot['models']), len(event_ensemble.MODELS))
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[1:3], event_ensemble.event_range(EVENT, NOW))
