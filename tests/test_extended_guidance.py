"""Synthetic dated bulletins; live smoke is deliberately separate."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from kcdw.extended_guidance import (parse_cpc, parse_wpc, parse_cpc_point,
                                    collect_extended, render_extended, point_url)

UTC = timezone.utc
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
CPC = '''<pre>Prognostic Discussion for 6 to 10 and 8 to 14 day outlooks
NWS Climate Prediction Center College Park, MD
300 PM EDT Sat September 12 2026
6-10 DAY OUTLOOK FOR SEP 18 - 22 2026
Near normal temperatures are favored for the Northeast.
8-14 DAY OUTLOOK FOR SEP 20 - 26 2026
Above normal precipitation is favored for the Northeast.
6-10 DAY OUTLOOK TABLE
Outlook for Sep 18 - 22 2026
NEW JERSEY N A
8-14 DAY OUTLOOK TABLE
Outlook for Sep 20 - 26 2026
NEW JERSEY N A
</pre>'''
WPC = '''<pre>Extended Forecast Discussion
NWS Weather Prediction Center College Park MD
344 AM EDT Sun Sep 13 2026
Valid 12Z Tue Sep 15 2026 - 12Z Sat Sep 19 2026
...Weather/Hazards Highlights...
Showers and thunderstorms work into the Eastern U.S. Thursday.
</pre>'''
ERO = '''<pre>Excessive Rainfall Discussion
NWS Weather Prediction Center College Park MD
346 AM EDT Sun Sep 13 2026
Day 1
Valid 12Z Sun Sep 13 2026 - 12Z Mon Sep 14 2026
...Northeast...
A slight risk of excessive rainfall remains over southern New England.
Day 2
Valid 12Z Mon Sep 14 2026 - 12Z Tue Sep 15 2026
Rainfall over the Central Plains.
</pre>'''

class ExtendedTests(unittest.TestCase):
    def client(self):
        c = Mock()
        c.get_text.side_effect = lambda url, maximum: CPC if 'cpc.' in url else ERO if 'qpferd' in url else WPC
        c.get.side_effect = RuntimeError('GIS offline')
        return c

    def test_issue_windows_and_state_categories(self):
        p = parse_cpc(CPC)
        self.assertEqual(p[0]['issued_at'], '2026-09-12T19:00:00Z')
        self.assertEqual(p[1]['valid_end'], '2026-09-27T04:00:00Z')
        self.assertEqual(p[0]['regional_categories'], {'temperature': 'N', 'precipitation': 'A'})
        w = parse_wpc(WPC, 'medium_range')
        self.assertEqual(w[0]['valid_start'], '2026-09-15T12:00:00Z')
        self.assertEqual(len(parse_wpc(ERO, 'excessive_rainfall')), 2)

    def test_month_and_year_rollover(self):
        t = CPC.replace('September 12 2026', 'December 25 2026').replace('SEP 18 - 22 2026', 'DEC 31 - JAN 4 2027').replace('SEP 20 - 26 2026', 'JAN 2 - 8 2027')
        self.assertEqual(parse_cpc(t)[0]['valid_start'], '2026-12-31T05:00:00Z')

    def test_combined_ero_days_and_substantive_excerpt(self):
        t = ERO.replace('Day 2\nValid', 'Day 4 and Day 5\n\nValid')
        self.assertIn('Day 4 and Day 5', parse_wpc(t, 'excessive_rainfall')[1]['title'])
        p = parse_wpc(ERO.replace('A slight risk', '...Northeast...\n\nA slight risk'), 'excessive_rainfall')[0]
        self.assertIn('southern New England', p['excerpt'])

    def test_missing_dates_rejected(self):
        for text in ['<html>service unavailable</html>', CPC.replace('300 PM EDT Sat September 12 2026', '')]:
            with self.assertRaises(ValueError): parse_cpc(text)
        with self.assertRaises(ValueError): parse_wpc(WPC.replace('Valid 12Z', 'Missing 12Z'), 'medium_range')

    def test_partial_outages_and_bounded_fetch(self):
        c = self.client()
        def fetch(url, maximum):
            if 'cpc.' in url: raise RuntimeError('CPC offline')
            return ERO if 'qpferd' in url else WPC
        c.get_text.side_effect = fetch
        s = collect_extended(c, NOW)
        self.assertTrue(s['ok'])
        self.assertFalse(s['data']['cpc']['ok'])
        self.assertTrue(s['data']['wpc']['ok'])
        self.assertEqual(c.get_text.call_count, 3)
        self.assertEqual(c.get.call_count, 4)
        c = self.client()
        c.get_text.side_effect = lambda url, maximum: CPC if 'cpc.' in url else (_ for _ in ()).throw(RuntimeError('WPC offline'))
        s = collect_extended(c, NOW)
        self.assertTrue(s['data']['cpc']['ok'])
        self.assertFalse(s['data']['wpc']['ok'])

    def test_render_semantics_stale_and_future(self):
        s = collect_extended(self.client(), NOW)
        start = datetime(2026, 9, 24, 14, tzinfo=UTC)
        out = render_extended(s, start, start + timedelta(hours=4), NOW)
        for label in ['not probability of rain', 'not ordinary rain chance', 'not a site-specific forecast', 'does not yet cover Sep 24', 'full mission overlap', 'New Jersey']:
            self.assertIn(label, out)
        out = render_extended(s, start, start + timedelta(hours=4), NOW + timedelta(days=4))
        self.assertIn('stale', out)
        self.assertNotIn('full mission overlap', out)
        out = render_extended(s, start, start + timedelta(hours=4), NOW - timedelta(days=2))
        self.assertIn('future', out)

    def test_half_open_coverage_and_invalid_missing(self):
        s = collect_extended(self.client(), NOW)
        end = datetime(2026, 9, 27, 4, tzinfo=UTC)
        self.assertNotIn('full mission overlap', render_extended(s, end, end + timedelta(hours=1), NOW))
        self.assertIn('partial mission overlap', render_extended(s, end - timedelta(hours=1), end + timedelta(hours=1), NOW))
        self.assertIn('unavailable', render_extended({}, NOW, NOW + timedelta(hours=1), NOW))
        self.assertIn('invalid mission window', render_extended(s, NOW, NOW, NOW))

    def test_escaping_and_safe_links(self):
        s = collect_extended(self.client(), NOW)
        p = s['data']['cpc']['products'][0]
        p['excerpt'] = '<script>alert("x")</script> & rain'
        p['source_url'] = 'javascript:alert(1)'
        out = render_extended(s, NOW, NOW + timedelta(hours=1), NOW)
        self.assertNotIn('<script>', out)
        self.assertNotIn('href="javascript:', out)
        self.assertIn('&lt;script&gt;', out)

    def test_relevant_categories_and_wpc_horizon_visible_before_details(self):
        s = collect_extended(self.client(), NOW)
        start = datetime(2026, 9, 24, 12, tzinfo=UTC)
        out = render_extended(s, start, start + timedelta(hours=9), NOW)
        visible = out.split('<details>', 1)[0]
        self.assertIn('CPC 8-14 day', visible)
        self.assertIn('precipitation: above normal', visible)
        self.assertIn('full mission overlap', visible)
        self.assertIn('WPC', visible)
        self.assertIn('does not yet cover Sep 24', visible)
        expired = render_extended(s, start, start + timedelta(hours=9), NOW + timedelta(days=4)).split('<details>', 1)[0]
        self.assertNotIn('precipitation: above normal', expired)

    def test_regional_excerpt_does_not_bury_northeast_after_other_regions(self):
        text = WPC.replace('Showers and thunderstorms work into the Eastern U.S. Thursday.',
            'Hot weather persists over the Southern Plains. ' * 50 +
            'Rain spreads across the Northeast Thursday. Cooler conditions reach New England Friday.')
        excerpt = parse_wpc(text, 'medium_range')[0]['excerpt']
        self.assertIn('Rain spreads across the Northeast Thursday.', excerpt)
        self.assertNotIn('Southern Plains', excerpt)

    def test_point_schema_geometry_query_and_ambiguity(self):
        def ms(d): return int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp() * 1000)
        j = {'features': [{'attributes': {'fcst_date': ms('2026-09-12'), 'start_date': ms('2026-09-18'), 'end_date': ms('2026-09-22'), 'prob': 33.0, 'cat': 'Above'}}]}
        p = parse_cpc_point(j, '6-10', 'precipitation')
        self.assertEqual(p['probability_label'], 'Above normal; CPC map probability contour 33%')
        self.assertIn('esriSpatialRelIntersects', point_url('6-10', 'precipitation'))
        for bad in [{'features': []}, {'features': j['features'] * 2}, {'error': {'message': 'offline'}}]:
            with self.assertRaises(ValueError): parse_cpc_point(bad, '6-10', 'precipitation')
        j['features'][0]['attributes']['prob'] = float('nan')
        with self.assertRaises(ValueError): parse_cpc_point(j, '6-10', 'precipitation')

if __name__ == '__main__': unittest.main()
