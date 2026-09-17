"""Synthetic NHC products: never operational weather evidence."""
from datetime import datetime, timedelta, timezone
import unittest
from kcdw.nhc_guidance import collect_nhc, render_nhc, parse_outlook, parse_advisory

NOW = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
TWO = '''<pre>000
ABNT20 KNHC 131134
TWOAT
Tropical Weather Outlook
NWS National Hurricane Center Miami FL
800 AM EDT Sun Sep 13 2026

For the North Atlantic...Caribbean Sea and the Gulf of America:

Central Atlantic:
A disturbance could develop later this week.
* Formation chance through 48 hours...low...near 0 percent.
* Formation chance through 7 days...low...20 percent.
$$
Forecaster Example</pre>'''
TCM = '''<pre>WTNT21 KNHC 131430
TCMAT1
HURRICANE EXAMPLE FORECAST/ADVISORY NUMBER 1
NWS NATIONAL HURRICANE CENTER MIAMI FL AL012026
1500 UTC SUN SEP 13 2026
HURRICANE CENTER LOCATED NEAR 20.0N 60.0W AT 13/1500Z
MAX SUSTAINED WINDS 65 KT WITH GUSTS TO 80 KT.
FORECAST VALID 14/0000Z 21.0N 61.0W
MAX WIND 70 KT...GUSTS 85 KT.
OUTLOOK VALID 18/1200Z 30.0N 65.0W
MAX WIND 50 KT...GUSTS 60 KT.
NEXT ADVISORY AT 13/2100Z
$$</pre>'''
RSS = '<rss><channel><pubDate>Sun, 13 Sep 2026 15:31:20 GMT</pubDate></channel></rss>'
STORM = {'id':'al012026','name':'Example','classification':'HU','lastUpdate':'2026-09-13T15:00:00Z','forecastAdvisory':{'issuance':'2026-09-13T15:00:00Z','url':'https://www.nhc.noaa.gov/text/MIATCMAT1.shtml'}}

class Client:
    def __init__(self, storms=None, fail=None):
        self.storms = [] if storms is None else storms
        self.fail = fail
        self.calls = []
    def get(self, url):
        self.calls.append(url)
        if self.fail == 'inventory': raise RuntimeError('offline <script>')
        return {'activeStorms': self.storms}
    def get_text(self, url, maximum):
        self.calls.append(url)
        if self.fail == 'outlook' and 'TWO' in url: raise RuntimeError('outlook offline')
        if url.endswith('.xml'): return RSS
        return TWO if 'TWO' in url else TCM

class NHCTest(unittest.TestCase):
    def test_active_track_and_percentages(self):
        s = collect_nhc(Client([STORM]), NOW)
        self.assertTrue(s['ok'], s['error'])
        self.assertEqual(s['data']['outlook']['areas'][0]['chance_7day'], '20 percent')
        a = s['data']['storms'][0]['advisory']
        self.assertEqual(a['track'][-1]['valid_at'], '2026-09-18T12:00:00Z')
        self.assertEqual(a['track'][0]['wind_kt'], 70)
        h = render_nhc(s,NOW,NOW+timedelta(hours=2),NOW)
        self.assertIn('70 kt',h)
        self.assertIn('fresh',h)
    def test_none_is_inventory_not_formation(self):
        s = collect_nhc(Client(), NOW)
        self.assertTrue(s['ok'])
        h=render_nhc(s,NOW,NOW+timedelta(hours=2),NOW)
        self.assertIn('no active Atlantic cyclones',h)
        self.assertIn('20 percent',h)
    def test_failure_never_means_none(self):
        s=collect_nhc(Client(fail='inventory'),NOW)
        self.assertFalse(s['ok'])
        h=render_nhc(s,NOW,NOW,NOW)
        self.assertNotIn('no active Atlantic cyclones',h)
        self.assertIn('unavailable',h)
        self.assertIn('20 percent',h)
    def test_expired_at_render(self):
        s=collect_nhc(Client([STORM]),NOW)
        later=NOW+timedelta(days=1)
        h=render_nhc(s,later,later+timedelta(hours=2),later)
        self.assertIn('expired',h)
        self.assertNotIn('— fresh',h)
    def test_out_of_window(self):
        s=collect_nhc(Client([STORM]),NOW)
        start=datetime(2026,9,24,12,tzinfo=timezone.utc)
        h=render_nhc(s,start,start+timedelta(hours=8),NOW)
        self.assertIn('not yet covered',h)
        self.assertIn('Mission: Sep 24 08:00 EDT',h)
        self.assertIn('WN3',h)
        self.assertIn('does not rule out',h)
    def test_timestamp_failclosed(self):
        with self.assertRaises(ValueError): parse_outlook(TWO.replace('800 AM EDT Sun Sep 13 2026','bad date'))
        s=collect_nhc(Client(),NOW)
        s['data']['outlook']['issued_at']='bad'
        self.assertIn('unavailable',render_nhc(s,NOW,NOW,NOW))
    def test_html_escaping_and_url_allowlist(self):
        storm=dict(STORM,name='<img src=x onerror=alert(1)>',forecastAdvisory={'issuance':STORM['lastUpdate'],'url':'https://www.nhc.noaa.gov.evil.test/text/x'})
        c=Client([storm]); s=collect_nhc(c,NOW)
        self.assertFalse(s['ok'])
        self.assertFalse(any('evil.test' in u for u in c.calls))
        h=render_nhc(s,NOW,NOW,NOW)
        self.assertNotIn('<img',h)
        self.assertIn('&lt;img',h)
    def test_month_rollover_and_dissipation(self):
        text=TCM.replace('SEP 13','DEC 31').replace('13/1500','31/1500').replace('14/0000','01/0000').replace('18/1200Z 30.0N 65.0W\nMAX WIND 50 KT...GUSTS 60 KT.','03/1200Z...DISSIPATED').replace('13/2100','31/2100')
        a=parse_advisory(text,'al012026')
        self.assertEqual(a['track'][0]['valid_at'],'2027-01-01T00:00:00Z')
        self.assertEqual(a['valid_end'],'2027-01-03T12:00:00Z')
        self.assertIn('DISSIPATED',a['track'][-1]['status'])
    def test_invalid_inventory_and_future_product(self):
        self.assertFalse(collect_nhc(Client([{'id':'bad'}]),NOW)['ok'])
        s=collect_nhc(Client(),NOW-timedelta(days=1))
        self.assertFalse(s['ok'])
    def test_no_formation_and_partial_outlook_failure(self):
        plain = TWO[:TWO.index('Central Atlantic:')] + 'Tropical cyclone formation is not expected during the next 7 days.\n$$</pre>'
        self.assertTrue(parse_outlook(plain)['no_formation_expected'])
        s = collect_nhc(Client([STORM], fail='outlook'), NOW)
        self.assertFalse(s['ok'])
        self.assertIsNotNone(s['data']['storms'][0]['advisory'])
        self.assertIn('70 kt', render_nhc(s,NOW,NOW,NOW))
    def test_malformed_track_fails_before_fresh_label(self):
        s = collect_nhc(Client([STORM]), NOW)
        s['data']['storms'][0]['advisory']['track'][0]['valid_at'] = 'invalid'
        h = render_nhc(s,NOW,NOW,NOW)
        storm_section = h.split('Example (al012026)', 1)[1]
        self.assertNotIn('— fresh', storm_section)
        self.assertIn('unavailable', storm_section)
    def test_partial_window_and_stale_inventory(self):
        s = collect_nhc(Client(), NOW)
        h = render_nhc(s,NOW,NOW+timedelta(days=8),NOW)
        self.assertIn('only partly covered',h)
        h = render_nhc(s,NOW,NOW,NOW+timedelta(hours=4))
        self.assertNotIn('no active Atlantic cyclones',h)
    def test_rss_failure_preserves_independently_verified_advisory(self):
        class RSSFailure(Client):
            def get_text(self, url, maximum):
                if url.endswith('.xml'):
                    self.calls.append(url)
                    raise RuntimeError('RSS offline')
                return super().get_text(url, maximum)
        c = RSSFailure([STORM])
        s = collect_nhc(c, NOW)
        self.assertTrue(any(url.endswith('CurrentStorms.json') for url in c.calls))
        self.assertFalse(s['ok'])
        self.assertEqual(s['data']['storms'][0]['advisory']['track'][0]['wind_kt'], 70)
        h = render_nhc(s, NOW, NOW + timedelta(hours=2), NOW)
        self.assertIn('70 kt', h)
        self.assertNotIn('no active Atlantic cyclones', h)
        self.assertIn('— fresh', h)

    def test_bounded_fetches(self):
        c=Client([dict(STORM,id=f'al{i:02d}2026') for i in range(1,12)])
        s=collect_nhc(c,NOW)
        self.assertFalse(s['ok'])
        self.assertLessEqual(len(c.calls),11)

if __name__ == '__main__': unittest.main()
