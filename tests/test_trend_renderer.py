"""Synthetic archive regression tests for the compact trend section."""
import copy
import re
import unittest
from datetime import datetime, timedelta, timezone

from kcdw.trend_renderer import render_trends

NOW = datetime(2026, 9, 13, 18, tzinfo=timezone.utc)
EVENT = dict(slug='checkride', date='2026-09-24', window='08-17')
KEYS = ('gefs', 'ecmwf_ens', 'aifs_ens', 'geps', 'wn3', 'gfs')


def point(hours=0, center=1010, current=True):
    return dict(collected_at=(NOW-timedelta(hours=hours)).isoformat(), run_time=None,
                run_binding='rolling', is_current=current,
                metrics={m: dict(center=v, low=v-1 if m != 'rain' else 0, high=v+1)
                         for m, v in [('pressure', center), ('wind', 10), ('rain', 2)]})


def archive(keys=KEYS, count=2) -> dict:
    return dict(version=1, as_of=NOW.isoformat(), event=EVENT.copy(),
                sample_time='2026-09-24T16:00:00Z', notes=[], models={
                    k: dict(label=k, statistic='mean' if k == 'wn3' else 'deterministic' if k == 'gfs' else 'median',
                            provenance='source', current_available=True,
                            points=[point((count-1-i)*3, 1010+i, i == count-1) for i in range(count)])
                    for k in keys})


class TrendTests(unittest.TestCase):
    def chart(self, markup, metric):
        return re.search(r'<svg[^>]*data-metric="'+metric+r'".*?</svg>', markup, re.S).group()

    def test_shared_charts_controls_statistics_and_budget(self):
        text = render_trends(archive(count=8), EVENT, NOW)
        self.assertIn('id="ensemble-trends"', text)
        self.assertEqual(text.count('<svg'), 3)
        self.assertEqual(text.count('type="checkbox"'), 6)
        self.assertLess(len(text), 30000)
        for word in ('mean', 'median', 'deterministic', 'noon', 'full window', 'retrieval', 'UTC', 'asynchronous'):
            self.assertIn(word, text)
        for key in KEYS:
            self.assertIn('data-model="'+key+'"', self.chart(text, 'pressure'))
        self.assertNotIn('<script', text)
        full = archive(count=8)
        for key, model in full['models'].items():
            model['label'] = {'gfs': 'GFS operational', 'wn3': 'WeatherNext 3'}.get(key, key.upper()+' ensemble')
        full['pressure_comparisons'] = {key: [dict(collected_at=point(3)['collected_at'], gfs=990, center=1010, low=1009, high=1011, is_current=False), dict(collected_at=point()['collected_at'], gfs=1005, center=1011, low=1010, high=1012, is_current=True)] for key in KEYS if key != 'gfs'}
        self.assertLess(len(render_trends(full, EVENT, NOW)), 30000)

    def test_update_x_positions_are_shared_not_index_aligned(self):
        a = archive(('gfs', 'gefs'))
        a['models']['gefs']['points'][0] = point(2, 1010, False)
        text = self.chart(render_trends(a, EVENT, NOW), 'pressure')
        xs = re.findall(r'<circle cx="([\d.]+)"', text)
        self.assertGreaterEqual(len(set(xs)), 3)
        self.assertNotIn('2026-09-24', text.split('</title>', 1)[-1])

    def test_missing_breaks_lines_and_bands_not_zero(self):
        a = archive(('gefs', 'wn3', 'gfs'), 3)
        a['models']['gefs']['points'][1]['metrics']['pressure'] = None
        for p in a['models']['wn3']['points']:
            p['metrics']['rain']['low'] = p['metrics']['rain']['high'] = None
        text = render_trends(a, EVENT, NOW)
        pressure = self.chart(text, 'pressure')
        gefs = re.search(r'<g data-model="gefs".*?</g>', pressure, re.S).group()
        self.assertNotIn(' L', gefs)
        for metric, key in [('rain', 'wn3'), ('pressure', 'gfs')]:
            group = re.search(r'<g data-model="'+key+r'".*?</g>', self.chart(text, metric), re.S).group()
            self.assertNotIn('class="band"', group)
        self.assertIn('missing', text)

    def test_outage_only_labels_actual_current_status(self):
        a = archive(('gfs', 'gefs'))
        a['models']['gefs']['points'][0]['is_current'] = False
        text = render_trends(a, EVENT, NOW)
        self.assertNotIn('historical only', text)
        a['models']['gefs']['current_available'] = False
        text = render_trends(a, EVENT, NOW)
        self.assertIn('gefs / median · historical only', text)
        self.assertIn('current unavailable', text)
        a['as_of'] = (NOW-timedelta(hours=9)).isoformat()
        self.assertIn('Outdated', render_trends(a, EVENT, NOW))

    def test_old_or_unmarked_last_point_is_not_current(self):
        for mutation in ('unmarked', 'old-fetch'):
            a = archive(('gefs',))
            last = a['models']['gefs']['points'][-1]
            if mutation == 'unmarked':
                last['is_current'] = False
            else:
                last['collected_at'] = (NOW-timedelta(hours=1)).isoformat()
            self.assertIn('gefs / median · historical only', render_trends(a, EVENT, NOW))

    def test_wn3_rain_never_infers_a_window_quantile_band(self):
        text = self.chart(render_trends(archive(('wn3',)), EVENT, NOW), 'rain')
        self.assertNotIn('class="band"', text)

    def test_headline_keeps_other_model_comparisons_collapsed(self):
        a = archive()
        a['pressure_comparisons'] = {key: [
            dict(collected_at=point(3)['collected_at'], gfs=990, center=1010, low=1009, high=1011, is_current=False),
            dict(collected_at=point()['collected_at'], gfs=1005, center=1011, low=1010, high=1012, is_current=True)] for key in KEYS if key != 'gfs'}
        text = render_trends(a, EVENT, NOW)
        match = re.search(r'<p class="trend-summary">(.*?)</p>', text)
        assert match is not None
        headline = match.group(1)
        self.assertLess(len(headline), 700)
        self.assertIn('wn3', headline)
        self.assertIn('aifs_ens', headline)
        self.assertNotIn('gefs:', headline)
        self.assertRegex(text, r'<details[^>]*><summary>All pressure-gap comparisons</summary>.*gefs:')

    def test_one_sample_does_not_claim_a_trend(self):
        text = render_trends(archive(count=1), EVENT, NOW)
        self.assertIn('No history yet', text)
        self.assertIn('no change comparison', text)
        self.assertNotIn('converging', text)

    def test_outlier_summary_reports_prior_and_latest_central_and_edge_gaps(self):
        a = archive(('gfs', 'gefs'))
        a['models']['gfs']['points'] = [point(3, 990, False), point(0, 1005)]
        unpaired = render_trends(a, EVENT, NOW)
        self.assertNotIn('narrowing', unpaired)
        self.assertNotIn('central gap 20.0 →', unpaired)
        a['pressure_comparisons'] = {'gefs': [
            dict(collected_at=point(3)['collected_at'], gfs=990, center=1010, low=1009, high=1011, is_current=False),
            dict(collected_at=point()['collected_at'], gfs=1005, center=1011, low=1010, high=1012, is_current=True)]}
        text = render_trends(a, EVENT, NOW)
        self.assertIn('same-fetch', text)
        self.assertIn('central gap 20.0 → 6.0 hPa', text)
        self.assertIn('p10-edge gap 19.0 → 5.0 hPa', text)
        self.assertIn('narrowing', text)
        self.assertIn('not a flight verdict', text)

    def test_defensive_event_escaping_invalid_values_and_age(self):
        a = archive(('gefs',))
        a['models']['gefs']['label'] = '<script>alert(1)</script>'
        a['models']['gefs']['points'][0]['metrics']['wind']['center'] = float('nan')
        a['models']['gefs']['points'][0]['metrics']['pressure']['center'] = 100000
        a['models']['gefs']['points'].append(point(80, 950))
        text = render_trends(a, EVENT, NOW)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('100000', text)
        self.assertNotIn('nan', text)
        self.assertNotIn('950.0', text)
        a['event']['window'] = '09-17'
        self.assertIn('event mismatch', render_trends(a, EVENT, NOW))
        for bad in (None, {}, [], {'models': []}, {'version': 2}):
            self.assertIn('unavailable', render_trends(bad, EVENT, NOW))

    def test_paired_current_outlier_requires_fresh_available_sources(self):
        a = archive(('gfs', 'gefs'))
        a['pressure_comparisons'] = {'gefs': [
            dict(collected_at=point(3)['collected_at'], gfs=990, center=1010, low=1009, high=1011, is_current=False),
            dict(collected_at=point()['collected_at'], gfs=1005, center=1011, low=1010, high=1012, is_current=True)]}
        self.assertIn('current GFS below p10', render_trends(a, EVENT, NOW))
        for mutate in ('outage', 'old', 'invalid'):
            b = copy.deepcopy(a)
            if mutate == 'outage':
                b['models']['gfs']['current_available'] = False
            elif mutate == 'old':
                b['as_of'] = (NOW-timedelta(hours=9)).isoformat()
            else:
                b['pressure_comparisons']['gefs'][-1]['low'] = float('inf')
            self.assertNotIn('current GFS below p10', render_trends(b, EVENT, NOW))

    def test_run_age_expires_sources_after_build(self):
        a = archive(('wn3', 'gfs', 'gefs'))
        for key, hours in [('wn3', 19), ('gfs', 25), ('gefs', 25)]:
            for p in a['models'][key]['points']:
                p['run_time'] = (NOW-timedelta(hours=hours)).isoformat()
                p['run_binding'] = 'response-bound' if key == 'wn3' else 'latest-advertised; not response-bound'
        text = render_trends(a, EVENT, NOW)
        self.assertIn('wn3 / mean · historical only', text)
        self.assertIn('gfs / deterministic · historical only', text)
        self.assertIn('gefs / median · historical only', text)
        self.assertIn('outdated run', text)

    def test_repeated_values_after_a_change_are_not_removed(self):
        a = archive(('gefs',), 3)
        a['models']['gefs']['points'][2]['metrics'] = copy.deepcopy(a['models']['gefs']['points'][0]['metrics'])
        text = self.chart(render_trends(a, EVENT, NOW), 'pressure')
        self.assertEqual(text.count('<circle'), 3)

    def test_wn3_mean_outside_band_and_rolling_cycle_not_bound(self):
        a = archive(('wn3', 'gefs'))
        a['models']['wn3']['points'][0]['metrics']['pressure'] = dict(center=1020, low=1000, high=1010)
        a['models']['gefs']['points'][0]['run_time'] = '2026-09-13T00:00:00Z'
        text = render_trends(a, EVENT, NOW)
        self.assertIn('1020.0', text)
        self.assertIn('rolling; cycle not bound', text)
        self.assertIn('p10–p90', text)
        self.assertEqual(a, copy.deepcopy(a))


if __name__ == '__main__':
    unittest.main()
