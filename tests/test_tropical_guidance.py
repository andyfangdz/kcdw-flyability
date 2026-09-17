"""Synthetic WNV3 CSV contracts; no network needed."""

import unittest
from datetime import datetime, timedelta, timezone

from kcdw.tropical_guidance import (
    MODEL_ID, MODEL_NAME, collect_wn3_cyclones, parse_wn3_cyclones,
    render_wn3_cyclones, validate_wn3_cyclones,
)

UTC = timezone.utc
RUN = datetime(2026, 9, 12, 12, tzinfo=UTC)
NOW = RUN + timedelta(hours=3)
URL = 'https://deepmind.google.com/science/weatherlab/download/cyclones/WNV3/ensemble/cyclogenesis/csv/WNV3_2026_09_12T12_00_cyclogenesis.csv'
HEADER = 'init_time,track_id,sample,valid_time,lead_time_hours,lat,lon,minimum_sea_level_pressure_hpa,maximum_sustained_wind_speed_knots\n'


def row(track='1', sample=0, lead=6, lat: float | str=40.9, lon= -74.3):
    return f'{RUN:%Y-%m-%d %H:%M:%S},{track},{sample},{RUN + timedelta(hours=lead):%Y-%m-%d %H:%M:%S},{lead},{lat},{lon},990,65\n'


def source(text=None):
    return {'ok': True, 'fetched_at': NOW.isoformat(), 'error': None,
            'data': parse_wn3_cyclones(text or HEADER + row(), URL)}


class CycloneTests(unittest.TestCase):
    def test_identity_time_and_comments(self):
        data = parse_wn3_cyclones('# license\n# BEGIN DATA\n' + HEADER + row(), URL)
        self.assertEqual(data['model_id'], MODEL_ID)
        self.assertEqual(data['model_name'], MODEL_NAME)
        self.assertEqual(data['ensemble_size'], 64)
        self.assertEqual(data['initialization_time'], '2026-09-12T12:00:00Z')
        self.assertEqual(data['points'][0]['valid_time'], '2026-09-12T18:00:00Z')
        with self.assertRaises(ValueError):
            parse_wn3_cyclones(HEADER + row(), URL.replace('WNV3', 'OPER'))
        with self.assertRaises(ValueError):
            parse_wn3_cyclones(HEADER + row(), URL.replace('12T12', '12T06'))

    def test_observed_storm_identity_preserved(self):
        data = parse_wn3_cyclones(HEADER + row(track='AL142026') + row(track='001') + row(track='AL972026(2)'), URL)
        self.assertEqual({p['track_id'] for p in data['points']}, {'AL142026', '001', 'AL972026(2)'})

    def test_pacific_tracks_excluded_but_gulf_caribbean_atlantic_retained(self):
        rows = [row('1', lat=15, lon=-95), row('EP142026', lat=25, lon=-90),
                row('WP012026', lat=40, lon=-70), row('2', lat=25, lon=-90),
                row('3', lat=15, lon=-75), row('4', lat=15, lon=-25)]
        data = parse_wn3_cyclones(HEADER + ''.join(rows), URL)
        self.assertEqual({p['track_id'] for p in data['points']}, {'2', '3', '4'})
        self.assertEqual(data['global_point_count'], 6)

    def test_panama_pacific_excluded_and_veracruz_gulf_retained(self):
        data = parse_wn3_cyclones(HEADER + row('1', lat=8, lon=-79)
                                  + row('2', lat=20, lon=-96), URL)
        self.assertEqual({p['track_id'] for p in data['points']}, {'2'})
        # Previously archived rectangular-sector data must also be corrected.
        s = source(HEADER + row('1', sample=0) + row('2', sample=1))
        s['data']['points'][0].update(lat=8, lon=-79)
        s['data']['points'][1].update(lat=20, lon=-96)
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('1 member-tracks in 1 unique members', html)
        self.assertIn('North America / western Atlantic window', html)

    def test_legacy_snapshot_filters_pacific_before_counts_and_map(self):
        s = source(HEADER + row('1', sample=0) + row('2', sample=1))
        s['data']['points'][1].update(lat=15, lon=-95)
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('1 member-tracks in 1 unique members', html)

    def test_north_america_filter_applies_to_old_snapshots_and_counts(self):
        s = source(HEADER + row('1', sample=0, lat=25, lon=-90)
                   + row('2', sample=1, lat=18, lon=-66)
                   + row('3', sample=2, lat=50, lon=-55)
                   + row('4', sample=3, lat=20, lon=-25)
                   + row('5', sample=4, lat=55, lon=-15)
                   + row('6', sample=5, lat=65, lon=-55))
        # Keep broad Atlantic source data; apply the new view to existing archives.
        self.assertEqual(len(s['data']['points']), 6)
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('3 member-tracks in 3 unique members', html)
        self.assertIn('North America / western Atlantic window', html)
        self.assertNotIn('Cabo Verde', html)
        import xml.etree.ElementTree as ET
        svg = ET.fromstring(html[html.index('<svg '):html.index('</svg>')+6])
        tracks = svg.find('.//*[@data-layer="tracks"]')
        self.assertIsNotNone(tracks)
        assert tracks is not None
        self.assertEqual(len(tracks.findall('circle')), 3)

    def test_regional_empty_is_not_missing_or_all_clear(self):
        s = source(HEADER + row(lat=20, lon=-25))
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('No North America / western Atlantic track points', html)
        self.assertNotIn('<svg', html)
        self.assertIn('not evidence of no tropical impacts', html)
        self.assertNotIn('Unavailable', html)

    def test_regional_boundaries_and_time_gaps(self):
        from kcdw.tropical_guidance import _north_america_point, _map
        p = source()['data']['points'][0]
        for lat, lon, expected in [(5,-60,True),(60,-55,True),(30,-100,True),
                                    (30,-45,True),(4.9,-60,False),(60.1,-55,False),
                                    (30,-100.1,False),(30,-44.9,False),(15,-95,False)]:
            with self.subTest(lat=lat,lon=lon):
                self.assertEqual(_north_america_point(dict(p,lat=lat,lon=lon)), expected)
        # Leaving and re-entering the region must not connect over hidden samples.
        s = source(HEADER + row(lead=6,lat=30,lon=-46)
                   + row(lead=12,lat=30,lon=-44)
                   + row(lead=18,lat=30,lon=-46))
        import xml.etree.ElementTree as ET
        html = _map(s['data']['points'])
        svg = ET.fromstring(html[:html.index('</svg>')+6])
        tracks = svg.find('.//*[@data-layer="tracks"]')
        assert tracks is not None
        self.assertEqual(len(tracks.findall('circle')), 2)
        self.assertEqual(len(tracks.findall('path')), 0)

    def test_map_has_real_land_outline_below_tracks_and_bounded_view(self):
        import xml.etree.ElementTree as ET
        html = render_wn3_cyclones(source(), RUN, RUN + timedelta(hours=12), NOW)
        svg = ET.fromstring(html[html.index('<svg '):html.index('</svg>')+6])
        self.assertIsNotNone(svg.find('.//*[@data-layer="land"]'))
        land = svg.find('.//*[@data-layer="land"]')
        assert land is not None
        self.assertTrue(land.findall('path'))
        self.assertGreater(sum(len(p.attrib['d']) for p in land.findall('path')), 1000)
        self.assertLess(html.index('data-layer="land"'), html.index('data-layer="tracks"'))
        self.assertIsNotNone(svg.find('.//clipPath'))
        self.assertIn('Natural Earth', html)
        self.assertLess(len(html), 90000)
        self.assertTrue(validate_wn3_cyclones(source(HEADER + row(track='AL072026')), NOW)['ok'])

    def test_member_and_point_deduplication(self):
        s = source(HEADER + row() + row() + row('2') + row('3', 1))
        self.assertEqual(len(s['data']['points']), 3)
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('2 / 64 unique members', html)
        self.assertNotIn('3 / 64 unique members', html)

    def test_malformed_fails_not_silently_dropped(self):
        for text in [HEADER, '<html>blocked</html>', HEADER + row(lat='nan'),
                     HEADER + row(sample=64), HEADER + row(lead=-6),
                     HEADER + row(lead=361), HEADER + row(lon=200),
                     HEADER + row() + row().replace(',990,', ',980,'),
                     HEADER + row().replace(',6,40.9', ',12,40.9')]:
            with self.subTest(text=text[:100]), self.assertRaises(ValueError):
                parse_wn3_cyclones(text, URL)

    def test_no_atlantic_track_is_not_unavailable_or_clear(self):
        s = source(HEADER + row(lat=20, lon=130))
        self.assertEqual(s['data']['points'], [])
        self.assertTrue(validate_wn3_cyclones(s, NOW)['ok'])
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('No North America / western Atlantic track points', html)
        self.assertNotIn('unavailable', html.lower())
        self.assertNotIn('all clear', html.lower())
        bad = render_wn3_cyclones({'ok': False, 'error': 'offline'}, RUN, RUN + timedelta(hours=12), NOW)
        self.assertIn('Unavailable', bad)

    def test_render_rechecks_stale_and_future_fetched(self):
        s = source()
        html = render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW + timedelta(days=3))
        self.assertIn('Stale', html)
        self.assertIn('2026-09-12T12:00:00Z', html)
        self.assertNotIn('1 / 64 unique members', html)
        s['fetched_at'] = (NOW + timedelta(days=1)).isoformat()
        self.assertFalse(validate_wn3_cyclones(s, NOW)['ok'])

    def test_out_of_window_not_used_for_proximity(self):
        s = source(HEADER + row(lead=6) + row('2', lead=18, lat=20, lon=-50))
        html = render_wn3_cyclones(s, RUN + timedelta(hours=18), RUN + timedelta(hours=24), NOW)
        self.assertIn('0 / 64 unique members', html)
        self.assertIn('Partial', html)
        outside = render_wn3_cyclones(s, RUN + timedelta(days=20), RUN + timedelta(days=21), NOW)
        self.assertIn('Out-of-range', outside)
        self.assertNotIn('0 / 64 unique members', outside)
        gap = render_wn3_cyclones(s, RUN + timedelta(hours=7), RUN + timedelta(hours=8), NOW)
        self.assertIn('No sampled track points', gap)
        self.assertNotIn('0 / 64 unique members', gap)

    def test_render_escaping_and_defensive_validation(self):
        bad = {'ok': False, 'error': '<script>alert("x")</script>'}
        html = render_wn3_cyclones(bad, RUN, RUN + timedelta(hours=12), NOW)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        for change in [{'model_id': 'OPER'}, {'ensemble_size': 65}, {'source_url': 'javascript:alert(1)'}]:
            s = source()
            s['data'].update(change)
            self.assertFalse(validate_wn3_cyclones(s, NOW)['ok'])
        s = source()
        s['data']['points'][0]['lat'] = float('nan')
        self.assertFalse(validate_wn3_cyclones(s, NOW)['ok'])
        self.assertIn('Unavailable', render_wn3_cyclones(None, RUN, RUN, NOW))

    def test_corrupt_cached_values_fail_closed(self):
        for value in ['40.9', None, True, {}, '<img src=x>']:
            s = source()
            s['data']['points'][0]['lat'] = value
            self.assertFalse(validate_wn3_cyclones(s, NOW)['ok'])
            self.assertIn('Unavailable', render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW))
        s = source()
        del s['data']['selection_method']
        self.assertIn('Unavailable', render_wn3_cyclones(s, RUN, RUN + timedelta(hours=12), NOW))

    def test_svg_is_self_contained_valid_xml(self):
        import xml.etree.ElementTree as ET
        html = render_wn3_cyclones(source(), RUN, RUN + timedelta(hours=12), NOW)
        svg = html[html.index('<svg '):html.index('</svg>') + len('</svg>')]
        ET.fromstring(svg)
        self.assertIn('KCDW / NYC', svg)
        self.assertNotIn('<script', html)
        self.assertNotIn('<image', html)

    def test_collector_bounded_candidates_and_response_init(self):
        class Client:
            def __init__(self): self.calls = []
            def get_text(self, url, maximum):
                self.calls.append((url, maximum))
                if url != URL: raise RuntimeError('404')
                return HEADER + row()
        client = Client()
        result = collect_wn3_cyclones(client, NOW)
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(len(client.calls), 8)
        self.assertTrue(all(maximum <= 16_000_000 for _, maximum in client.calls))
        class Bad:
            def get_text(self, url, maximum): return '<html>not CSV</html>'
        result = collect_wn3_cyclones(Bad(), NOW)
        self.assertFalse(result['ok'])
        self.assertIsNone(result['data'])


if __name__ == '__main__':
    unittest.main()
