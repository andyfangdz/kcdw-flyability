"""Shared chart regressions; all source data are synthetic."""
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from kcdw import event_ensemble
from kcdw.event_renderer import Chart, MODEL_COLORS, render
from test_events import EVENT, NOW, FakeClient, synthetic_wn3


class ComparisonTests(unittest.TestCase):
    def snapshot(self):
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=synthetic_wn3()):
            snapshot = event_ensemble.collect_event(FakeClient(), EVENT, NOW)
        hourly = snapshot['models']['ecmwf_ens']['data']['hourly']
        snapshot['weathernext2'] = {'ok': True, 'data': {'model_id': 'google_weathernext2_ensemble_mean', 'hourly': {'time': hourly['time']}, 'metadata': {'fresh': True}}}
        for field in ('pressure_msl', 'wind_speed_10m', 'cloud_cover_low', 'precipitation', 'temperature_2m'):
            value = {'pressure_msl': 1015, 'wind_speed_10m': 8, 'cloud_cover_low': 30, 'precipitation': .2, 'temperature_2m': 18}[field]
            snapshot['weathernext2']['data']['hourly'][field] = [value] * len(hourly['time'])
            snapshot['weathernext2']['data']['hourly'][field + '_spread'] = [1] * len(hourly['time'])
        return snapshot

    def svg(self, markup, field):
        match = re.search(r'<div data-comparison-field="' + field + r'">.*?(<svg.*?</svg>)', markup, re.S)
        assert match is not None, f'Missing shared {field} SVG'
        return match.group(1)

    def test_sources_share_svg_and_controls_label_statistics(self):
        markup, _ = render(self.snapshot(), NOW)
        for field in ('pressure', 'wind', 'temperature', 'rain'):
            svg = self.svg(markup, field)
            for key in ('gefs', 'ecmwf_ens', 'aifs_ens', 'geps', 'wn3', 'wn2'):
                self.assertIn('data-model="' + key + '"', svg)
        self.assertIn('data-model="wn3"', self.svg(markup, 'rain'))
        self.assertNotIn('data-model="wn3"', self.svg(markup, 'cloud'))
        self.assertIn('WN3: no cloud', markup)
        self.assertIn('Median (p50)', markup)
        self.assertIn('Mean ±1 SD', markup)
        self.assertIn('id="compare-bands" type="checkbox" checked', markup)
        self.assertEqual(markup.count('data-wn3-field='), 4)
        self.assertNotIn('WeatherNext 2 / pressure', markup)
        self.assertNotEqual(MODEL_COLORS['wn3'], MODEL_COLORS['aifs_ens'])
        self.assertIn('<span class="legend-item"><span class="swatch"', markup)

    def test_shared_pressure_axis_has_readable_tick_density(self):
        from kcdw.event_renderer import _ticks
        ticks = _ticks(1007, 1036)
        visible = [t for t in ticks if 1007 <= t <= 1036]
        self.assertGreaterEqual(len(visible), 3)
        self.assertLessEqual(len(visible), 7)

    def test_wn3_focus_is_four_range_charts_not_just_cards(self):
        markup, _ = render(self.snapshot(), NOW)
        focus = markup.split('<section id="wn3-numbers"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(focus.count('<svg'), 4)
        self.assertEqual(focus.count('<polygon'), 4)
        self.assertEqual(focus.count('<polyline'), 4)
        self.assertLess(focus.index('<svg'), focus.index('Mean event rainfall'))
        self.assertIn('not the full ensemble minimum', focus)
        stale, _ = render(self.snapshot(), NOW + timedelta(hours=72))
        self.assertNotIn('data-wn3-field=', stale)

    def test_actual_timestamp_axis_and_gap_bands(self):
        chart = Chart([NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=4)], 0, 10, (0, 2))
        self.assertAlmostEqual(chart.x(1) - chart.x(0), (chart.x(2) - chart.x(0)) / 4)
        chart.band([1, None, 2], [3, None, 4], '#000', .2)
        self.assertNotIn('<polygon', ''.join(chart.parts))

    def test_unit_conversion_and_timestamp_tooltips(self):
        markup, _ = render(self.snapshot(), NOW)
        self.assertIn('1012.35 hPa', self.svg(markup, 'pressure'))
        self.assertIn('8.02 kt', self.svg(markup, 'wind'))
        self.assertIn('2026-09-24T12:00:00Z', self.svg(markup, 'wind'))
        self.assertIn('mm / preceding hour', markup)

    def test_partial_wn3_coverage_keeps_timestamp_positions(self):
        from kcdw.common import parse_time
        from kcdw.event_renderer import _comparison_charts, _ok_models
        snapshot = self.snapshot()
        models = _ok_models(snapshot)
        times = [parse_time(t) for t in models[0]['hourly']['time']]
        forecast = snapshot['weathernext3']['data']['forecast']
        indices = [i for i, t in enumerate(forecast['valid_time_utc']) if times[6] <= parse_time(t) <= times[-7]]
        forecast['valid_time_utc'] = [forecast['valid_time_utc'][i] for i in indices]
        for field in forecast['fields'].values():
            for stat in ('mean', 'p10', 'p90'):
                field[stat] = [field[stat][i] for i in indices]
        with patch.object(event_ensemble, 'weathernext3_diagnostic', return_value={'available': True}):
            markup = _comparison_charts(snapshot, models, times, (56, 65), NOW)
        self.assertIn('not the full display', markup)
        svg = self.svg(markup, 'pressure')
        wn3_group = svg.split('<g data-model="wn3">')[1]
        match = re.search(r'<polyline points="([^"]+)"', wn3_group)
        assert match is not None
        points = match.group(1).split()
        expected = Chart(times, 0, 1, (56, 65))
        self.assertEqual(float(points[0].split(',')[0]), round(expected.x(6), 1))
        self.assertEqual(float(points[-1].split(',')[0]), round(expected.x(len(times)-7), 1))
        self.assertEqual(len(points), len(times)-12)

    def test_missing_old_rain_outage_and_stale_wn3(self):
        snapshot = self.snapshot()
        for model in snapshot['models'].values():
            model['data']['hourly'].pop('precipitation', None)
        snapshot['models']['gefs'] = {'ok': False, 'error': 'offline'}
        for now in (NOW + timedelta(hours=72),):
            markup, health = render(snapshot, now)
            self.assertFalse(health['weathernext3']['available'])
            self.assertNotIn('data-model="wn3"', self.svg(markup, 'pressure'))
            self.assertIn('data-model="ecmwf_ens"', self.svg(markup, 'pressure'))
            self.assertIn('data-model="wn2"', self.svg(markup, 'rain'))
            self.assertNotIn('id="wn3-numbers"', markup)
        snapshot['weathernext3'] = {'ok': False, 'error': 'offline'}
        markup, _ = render(snapshot, NOW)
        self.assertIn('data-model="aifs_ens"', self.svg(markup, 'wind'))
        self.assertNotIn('data-model="wn3"', self.svg(markup, 'wind'))
