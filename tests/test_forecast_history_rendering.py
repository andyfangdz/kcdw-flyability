"""Historical forecast display remains separate from current evidence."""
import copy
import html
import json
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from kcdw import event_renderer as renderer
from kcdw.common import parse_time, iso_z
import test_gfs_comparison as gfs_tests
from test_events import NOW


class ForecastHistoryRenderingTests(unittest.TestCase):
    def setUp(self):
        # Saved-forecast chart history is switched off in production; these tests keep the retained code path honest.
        switch = patch.object(renderer, 'CHART_HISTORY', True)
        switch.start()
        self.addCleanup(switch.stop)
        self.snapshot = gfs_tests.GfsComparisonTests().snapshot()
        self.models = renderer._ok_models(self.snapshot)
        self.times = [parse_time(t) for t in self.models[0]['hourly']['time']]

    def values(self, markup, model):
        group = re.search(r'<g data-model="' + model + r'"[^>]*data-values="([^"]+)"', markup)
        return json.loads(html.unescape(group[1]))

    def test_extended_and_trimmed_current_arrays_timestamp_aligned(self):
        for times in ([self.times[0]-timedelta(hours=1)] + self.times, self.times[2:]):
            with self.subTest(start=times[0]):
                markup = renderer._comparison_charts(self.snapshot, self.models, times, (30, 39), NOW)
                for model in ('gefs', 'wn2'):
                    values = self.values(markup, model)
                    self.assertEqual(len(values), len(times))
                    if times[0] < self.times[0]:
                        self.assertIsNone(values[0])
                    else:
                        raw = self.snapshot['weathernext2']['data']['hourly']['precipitation'] if model == 'wn2' else self.models[0]['hourly']['precipitation']['p50']
                        self.assertEqual(values[0], raw[2])

    def history(self):
        start = self.times[0] - timedelta(hours=3)
        return {'sources': {'gefs': {'label': 'NCEP GEFS', 'statistic': 'median', 'hourly': {
            'time': [iso_z(start+timedelta(hours=i)) for i in range(4)],
            'precipitation': {'center': [0, None, 2, 999], 'low': [0, None, 1, 998], 'high': [1, None, 3, 1000]}}}}}

    def test_history_zero_gaps_and_current_precedence(self):
        times = [self.times[0]-timedelta(hours=i) for i in (3,2,1)] + self.times
        rows = renderer._history_series(self.snapshot, self.history(), 'precipitation', times,
                                        {'gefs': [None, None, 7] + [1]*len(self.times)})
        self.assertEqual(rows[0][1][:4], [0, None, None, None])
        self.assertEqual(rows[0][2][:4], [0, None, None, None])

    def test_historical_axis_distinction_and_snapshot_unchanged(self):
        self.snapshot['forecast_history'] = self.history()
        original = copy.deepcopy(self.snapshot)
        with patch.object(renderer, '_validated_history', return_value=self.history()):
            markup, _ = renderer.render(self.snapshot, NOW)
        self.assertEqual(self.snapshot, original)
        self.assertIn('Earlier saved forecasts—not observations', markup)
        from kcdw.events import TZ
        first = parse_time(self.history()['sources']['gefs']['hourly']['time'][0]).astimezone(TZ)
        date_axis = re.search(r'<div class="forecast-x-axis">(.*?)</div>', markup).group(1)
        self.assertIn(f'title="{first:%Y-%m-%d}"',date_axis)
        self.assertIn('data-history="saved-forecast"', markup)
        self.assertIn('saved forecast / Median', markup)
        starts = set(re.findall(r'data-axis-start="([^"]+)"', markup))
        self.assertEqual(starts, {self.history()['sources']['gefs']['hourly']['time'][0]})
        self.assertIn('id="compare-gefs" type="checkbox" checked', markup)
        self.assertIn('data-forecast-view="checkride"', markup)

    def test_real_validator_accepts_history_and_rejects_tampering(self):
        from kcdw.forecast_history import validate_forecast_history, LABELS
        stamp = iso_z(self.times[0]-timedelta(hours=3))
        envelope = {'version': 1, 'event': self.snapshot['event'],
                    'collected_at': self.snapshot['collected_at'],
                    'cutoff': self.snapshot['range']['start'], 'start': stamp,
                    'sources': {'gefs': {'label': LABELS['gefs'], 'statistic': 'median',
                        'hourly': {'time': [stamp], 'precipitation': {'center': [0], 'low': [0], 'high': [1]}}}},
                    'provenance': {'state': 'historical', 'selection': 'latest saved collection per source/field/time',
                        'archives_read': 1, 'oldest_collection': stamp, 'newest_collection': stamp, 'truncated': False}}
        self.assertIsNotNone(validate_forecast_history(envelope, self.snapshot['event'], NOW))
        self.snapshot['forecast_history'] = envelope
        markup, _ = renderer.render(self.snapshot, NOW)
        self.assertIn('data-history="saved-forecast"', markup)
        self.assertIn('data-axis-start="' + stamp + '"', markup)
        for key, wrong in (('collected_at', iso_z(NOW-timedelta(minutes=1))),
                           ('cutoff', iso_z(parse_time(envelope['cutoff'])+timedelta(hours=1)))):
            with self.subTest(binding=key):
                saved = envelope[key]
                envelope[key] = wrong
                self.assertIsNotNone(validate_forecast_history(envelope, self.snapshot['event'], NOW))
                self.assertIsNone(renderer._validated_history(self.snapshot, NOW))
                envelope[key] = saved
        envelope['sources']['gefs']['statistic'] = 'deterministic'
        markup, _ = renderer.render(self.snapshot, NOW)
        self.assertNotIn('data-history="saved-forecast"', markup)
        self.assertNotIn('data-axis-start="' + stamp + '"', markup)

    def test_shared_axes_and_historical_rh_gaps(self):
        from kcdw import event_moisture_view as moisture
        from test_event_moisture_view import fixture
        history = self.history()
        h = history['sources']['gefs']['hourly']
        for field, _ in moisture.FIELDS:
            h[field] = {'center': [30, None, 70, None], 'low': [20, None, 60, None], 'high': [40, None, 80, None]}
        history['sources']['wn3'] = copy.deepcopy(history['sources']['gefs'])
        history['sources']['wn3'].update(label='WeatherNext 3', statistic='mean')
        self.snapshot['forecast_history'] = history
        with patch.object(renderer, '_validated_history', return_value=history), patch.object(moisture, 'validated', return_value=fixture()):
            markup, _ = renderer.render(self.snapshot, NOW)
        self.assertEqual(len(set(re.findall(r'data-axis-start="([^"]+)"', markup))), 1)
        rh = markup.split('data-moisture-field="relative_humidity_2m"')[1].split('</svg>')[0]
        self.assertIn('data-rh-model="gefs" data-history="saved-forecast"', rh)
        values = self.values(rh, 'rh-gefs')
        self.assertEqual([values[i] if i < len(values) else None for i in range(4)], [30, None, 70, None])
        self.assertIn('data-history="saved-forecast"', markup.split('data-comparison-field="rain"')[1].split('</svg>')[0])
        self.assertIn('data-forecast-today="' + self.snapshot['range']['start'] + '"', markup)
        self.assertLess(len(markup.encode()), 800000)

    def test_saved_tooltip_omits_only_trailing_missing_hours(self):
        chart=renderer.Chart(self.times,0,100,None)
        values=[0,None,50]+[None]*(len(self.times)-3)
        renderer._saved_group(chart,('gefs',values,values,values,'Median'),'GEFS','#000','%')
        encoded=re.search(r'data-values="([^"]+)"',''.join(chart.parts)).group(1)
        self.assertEqual(json.loads(html.unescape(encoded)),[0,None,50])

    def test_archive_limits_expose_sampled_coverage(self):
        for sampled in (False, True):
            with self.subTest(sampled=sampled):
                history=self.history()
                history['provenance']={'truncated':sampled}
                markup=renderer._comparison_charts(self.snapshot,self.models,self.times,(30,39),NOW,history)
                self.assertEqual('data-history-coverage="sampled"' in markup,sampled)

    def test_chart_history_is_off_by_default_and_charts_start_at_the_forecast_range(self):
        self.snapshot['forecast_history'] = self.history()
        with patch.object(renderer, 'CHART_HISTORY', False), patch.object(renderer, '_validated_history', return_value=self.history()) as validated:
            markup, _ = renderer.render(self.snapshot, NOW)
        validated.assert_not_called()
        self.assertNotIn('data-history="saved-forecast"', markup)
        self.assertNotIn('Earlier saved forecasts—not observations', markup)
        self.assertIn('Charts run from today through the checkride', markup)

    def test_rejected_history_cannot_extend_or_render(self):
        self.snapshot['forecast_history'] = {'untrusted': 'persisted junk'}
        with patch.object(renderer, '_validated_history', return_value=None):
            markup, _ = renderer.render(self.snapshot, NOW)
        self.assertNotIn('data-history="saved-forecast"', markup)
        self.assertNotIn('persisted junk', markup)


if __name__ == '__main__':
    unittest.main()
