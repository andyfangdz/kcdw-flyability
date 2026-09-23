"""Bounded narrative evidence uses synthetic sources only."""
import json
import unittest
from datetime import timedelta

from kcdw.event_narrative_evidence import build_event_evidence
from test_ensemble_trends import NOW, snapshot


class EvidenceTests(unittest.TestCase):
    def sources(self, data, now=NOW):
        return {s['id']: s for s in build_event_evidence(data, now)['sources']}

    def test_validated_snapshot_changes_are_citable_and_not_plot_history(self):
        from unittest.mock import patch
        data = snapshot()
        data['event_changes'] = {'unvalidated': 'input'}
        data['forecast_history'] = {'must_not_enter': 'plot history'}
        packet = {'historical': True, 'comparison_kind': 'saved forecast snapshots'}
        with patch('kcdw.event_narrative_evidence.validated_event_changes', return_value=packet, create=True) as validate:
            source = self.sources(data)['snapshot_changes']
        validate.assert_called_once_with(data, NOW)
        self.assertEqual(source['status'], 'available')
        self.assertEqual(source['evidence'], packet)
        self.assertNotIn('must_not_enter', json.dumps(source))
        with patch('kcdw.event_narrative_evidence.validated_event_changes', return_value=None, create=True):
            self.assertEqual(self.sources(data)['snapshot_changes']['status'], 'unavailable')

    def test_size_pressure_compacts_hourly_detail_before_dropping_changes(self):
        from unittest.mock import patch
        data = snapshot()
        baseline_bytes = len(json.dumps(build_event_evidence(data, NOW)).encode())
        packet = {'historical': True, 'bounded_comparison_fixture': 'x' * 12000}
        with patch('kcdw.event_narrative_evidence.validated_event_changes', return_value=packet), \
             patch('kcdw.event_narrative_evidence.MAX_BYTES', baseline_bytes + 1000):
            result = build_event_evidence(data, NOW)
        sources = {s['id']: s for s in result['sources']}
        self.assertEqual(sources['snapshot_changes']['status'], 'available')
        self.assertEqual(sources['wn3_point']['status'], 'available')
        self.assertLessEqual(len(json.dumps(result).encode()), baseline_bytes + 1000)

    def test_size_pressure_trims_old_run_points_before_losing_comparison(self):
        from unittest.mock import patch
        from test_run_history import fixture, NOW as history_now
        data=snapshot();data['ensemble_run_history']=fixture()
        comparison={'historical':True,'bounded_comparison_fixture':'x'*12000}
        with patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence.MAX_BYTES',1000000):
            complete=build_event_evidence(data,history_now)
        for source in complete['sources']:
            for key in ('event_samples','rain_intervals'):
                if key in source['evidence']:
                    del source['evidence'][key]
                    source['evidence']['hourly_detail_omitted']='Window totals and fixed-time summaries retained; hourly detail omitted for size.'
        cap=len(json.dumps(complete).encode())-500
        with patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence.MAX_BYTES',cap):
            result=build_event_evidence(data,history_now)
        sources={s['id']:s for s in result['sources']}
        self.assertEqual(sources['snapshot_changes']['status'],'available')
        self.assertEqual(sources['run_history']['status'],'available')
        points=sources['run_history']['evidence']['points']
        self.assertLess(len(points),16)
        for key in {p['model_key'] for p in points}:
            expected=[p for p in next(s for s in complete['sources'] if s['id']=='run_history')['evidence']['points'] if p['model_key']==key][-2:]
            self.assertTrue(all(p in points for p in expected))
        self.assertLessEqual(len(json.dumps(result).encode()),cap)

    def test_prioritized_eviction_keeps_flight_wind_and_rounds_values(self):
        from unittest.mock import patch
        from kcdw.event_narrative_evidence import EVICTION_ORDER
        data=snapshot();data['narrative_priority_version']=1
        comparison={'historical':True,'rh':71.91409004263917,'bounded_comparison_fixture':'x'*12000}
        with patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence.PRIORITY_MAX_BYTES',1000000):
            complete=build_event_evidence(data,NOW)
        available=[s['id'] for s in complete['sources'] if s['status']=='available']
        cap=len(json.dumps(complete).encode())*2//3
        with patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence.PRIORITY_MAX_BYTES',cap):
            result=build_event_evidence(data,NOW)
        sources={s['id']:s for s in result['sources']}
        self.assertEqual(sources['snapshot_changes']['evidence']['rh'],71.91)
        omitted=[i for i in available if sources[i]['status']=='unavailable']
        kept=[i for i in available if sources[i]['status']=='available']
        self.assertTrue(omitted)
        rank={alias:i for i,alias in enumerate(EVICTION_ORDER)}
        self.assertLess(max(rank.get(i,-1) for i in omitted),min(rank.get(i,-1) for i in kept if i in rank))
        self.assertLessEqual(len(json.dumps(result).encode()),cap)

    def test_api_and_fresh_models(self):
        data = snapshot()
        result = build_event_evidence(data, NOW)
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['event'], data['event'])
        self.assertEqual(result['collected_at'], data['collected_at'])
        self.assertEqual(self.sources(data)['aifs_ens']['status'], 'available')

    def test_aged_and_isolated_malformed(self):
        data = snapshot()
        data['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = 'bad'
        sources = self.sources(data)
        self.assertEqual(sources['gefs']['status'], 'unavailable')
        self.assertEqual(sources['aifs_ens']['status'], 'available')
        aged = self.sources(snapshot(), NOW + timedelta(hours=30))
        self.assertTrue(all(aged[k]['status'] == 'unavailable' for k in ('gefs', 'aifs_ens', 'wn3_point')))

    def test_secrets_and_bound(self):
        data = snapshot()
        sentinel = 'UNEXPECTED_SECRET_SENTINEL'
        data['secret'] = sentinel * 100000
        data['models']['aifs_ens']['data']['secret'] = sentinel
        data['models']['geps']['error'] = '/home/private/' + sentinel
        result = json.dumps(build_event_evidence(data, NOW))
        self.assertNotIn(sentinel, result)
        self.assertNotIn('sample_counts', result)
        self.assertNotIn('<svg', result)
        self.assertLessEqual(len(result.encode()), 60000)

    def test_time_windows_and_cloud_quantiles(self):
        evidence = self.sources(snapshot())['aifs_ens']['evidence']
        self.assertEqual(evidence['event_window']['instant_sampling'], 'start inclusive; end exclusive')
        self.assertEqual(evidence['event_window']['rain_sampling'], 'preceding-hour totals; opening excluded; closing included')
        self.assertEqual(evidence['low_cloud']['midday']['p50'], 40)
        self.assertEqual(evidence['low_cloud']['window_mean']['p50'], 40)
        self.assertEqual(evidence['low_cloud']['unit'], '%')

    def test_missing_cloud_is_not_zero(self):
        data = snapshot()
        d = data['models']['geps']['data']
        f = d['hourly']['cloud_cover_low']
        for k in ('p10', 'p25', 'p50', 'p75', 'p90'):
            f[k] = [None] * len(f[k])
        f['sample_counts'] = [0] * len(f['sample_counts'])
        d['window']['low_cloud_mean_percent'] = None
        d['window']['cloud_members_complete'] = 0
        cloud = self.sources(data)['geps']['evidence']['low_cloud']
        self.assertIsNone(cloud['midday']['p50'])
        self.assertIsNone(cloud['window_mean'])

    def test_wn3_mean_units_no_rain_band(self):
        source = self.sources(snapshot())['wn3_point']
        self.assertEqual(source['status'], 'available')
        evidence = source['evidence']
        self.assertEqual(evidence['statistic'], 'mean')
        self.assertEqual(evidence['pressure']['unit'], 'hPa')
        self.assertEqual(evidence['wind']['unit'], 'kt')
        self.assertIsNone(evidence['rain_window']['p10'])
        self.assertIsNone(evidence['rain_window']['p90'])
    def test_official_discussion_dated_overlap_and_aging(self):
        from kcdw.ensemble_trends import _event
        from kcdw.common import iso_z
        data = snapshot()
        _, sample, _ = _event(data['event'])
        product = {'id': 'cpc_8-14', 'title': '8–14 day discussion',
                   'issued_at': iso_z(NOW), 'valid_start': iso_z(sample-timedelta(days=2)),
                   'valid_end': iso_z(sample+timedelta(days=2)),
                   'excerpt': 'National context. ' * 80 + 'Northeast maritime flow and regional pattern.',
                   'source_url': 'https://www.cpc.ncep.noaa.gov/products/predictions/610day/fxus06.html'}
        data['synoptic_context'] = {'extended': {'ok': True, 'fetched_at': iso_z(NOW),
            'data': {'cpc': {'products': [product]}, 'wpc': {'products': []}}}}
        source = self.sources(data)['cpc_wpc']
        self.assertEqual(source['status'], 'available')
        p = source['evidence']['products'][0]
        self.assertIn('Northeast maritime', p['text'])
        self.assertIn('full mission overlap', p['coverage'])
        self.assertEqual(p['issued_at'], iso_z(NOW))
        self.assertEqual(self.sources(data, NOW+timedelta(hours=60))['cpc_wpc']['status'], 'unavailable')
        product['valid_end'] = 'invalid'
        self.assertEqual(self.sources(data)['cpc_wpc']['status'], 'unavailable')

    def test_history_remains_historical_with_unavailable_current(self):
        from test_run_history import fixture, NOW as history_now
        data = snapshot()
        data['ensemble_run_history'] = fixture()
        data['ensemble_run_history']['notes'] = ['UNEXPECTED_SECRET_SENTINEL']
        source = self.sources(data, history_now)['run_history']
        self.assertEqual(source['status'], 'available')
        points = source['evidence']['points']
        self.assertEqual(len(points), 16)
        self.assertTrue(all(p['is_current'] is False for p in points))
        self.assertNotIn('source_url', json.dumps(source))
        self.assertNotIn('UNEXPECTED_SECRET_SENTINEL', json.dumps(source))

    def test_gfs_deterministic(self):
        from test_ensemble_trends import TrendTests
        data = snapshot()
        data['gfs'] = TrendTests().gfs()
        source = self.sources(data)['gfs']
        self.assertEqual(source['status'], 'available')
        self.assertEqual(source['evidence']['statistic'], 'deterministic')
        self.assertIsNone(source['evidence']['pressure']['p10'])
        self.assertIsNone(source['evidence']['rain_window']['p90'])

    def test_units_and_precise_rain_endpoints(self):
        data = snapshot()
        e = self.sources(data)['wn3_point']['evidence']
        f = data['weathernext3']['data']['forecast']
        from kcdw.common import parse_time
        i = [parse_time(t) for t in f['valid_time_utc']].index(parse_time(e['sample_time']))
        self.assertAlmostEqual(e['pressure']['mean'], f['fields']['sea_level_pressure']['mean'][i]/100)
        self.assertAlmostEqual(e['wind']['mean'], f['fields']['wind_speed_10m']['mean'][i]*3600/1852)
        self.assertEqual(e['rain_intervals'][0]['start'], e['event_window']['start'])
        self.assertEqual(e['rain_intervals'][-1]['end'], e['event_window']['end'])
        self.assertEqual(e['event_samples'][0]['at'], e['event_window']['start'])
        self.assertNotEqual(e['event_samples'][-1]['at'], e['event_window']['end'])
        self.assertIn('p90', e['rain_intervals'][0])

    def test_many_long_bulletins_stay_bounded(self):
        from kcdw.common import iso_z
        data = snapshot()
        product = {'id': 'cpc_8-14', 'issued_at': iso_z(NOW),
                   'valid_start': iso_z(NOW+timedelta(days=8)), 'valid_end': iso_z(NOW+timedelta(days=14)),
                   'excerpt': 'Regional flow. ' * 10000}
        products = [dict(product, id='cpc_'+str(i)) for i in range(16)]
        data['synoptic_context'] = {'extended': {'data': {'cpc': {'products': products}, 'wpc': {'products': []}}}}
        self.assertLessEqual(len(json.dumps(build_event_evidence(data, NOW)).encode()), 60000)


if __name__ == '__main__':
    unittest.main()
