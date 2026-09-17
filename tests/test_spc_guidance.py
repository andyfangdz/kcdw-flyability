"""Synthetic SPC fixtures; not operational evidence."""
import copy
from datetime import datetime, timedelta, timezone
import unittest
from kcdw.spc_guidance import collect_spc, render_spc, GEO_URLS

NOW = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
def fixture(day, dn=None):
    start = NOW.replace(hour=12) + timedelta(days=day-1)
    p = {'ISSUE': '202609130820', 'VALID': start.strftime('%Y%m%d%H%M'),
         'EXPIRE': (start+timedelta(days=1)).strftime('%Y%m%d%H%M'),
         'DN': dn if dn is not None else (2 if day<4 else 0),
         'LABEL': 'TSTM' if day<4 else 'Potential Too Low'}
    g = {'type':'Polygon','coordinates':[[[-75,40],[-73,40],[-73,42],[-75,42],[-75,40]]]}
    if day>=4 and not dn: g={'type':'GeometryCollection','geometries':[]}
    return {'type':'FeatureCollection','features':[{'type':'Feature','properties':p,'geometry':g}]}
class Client:
    def __init__(self):
        self.rows: dict = {u:fixture(d) for d,u in GEO_URLS.items()}
        self.calls=[]
    def get(self,url):
        self.calls.append(url)
        r=self.rows[url]
        if isinstance(r,Exception): raise r
        return copy.deepcopy(r)
    def get_text(self,url,maximum): raise RuntimeError('text unavailable <script>')
class SPCTest(unittest.TestCase):
    def render(self,s,now=NOW,start=NOW): return render_spc(s,start,start+timedelta(days=7),now)
    def test_categories_and_explicit_low(self):
        s=collect_spc(Client(),NOW); h=self.render(s)
        self.assertIn('General thunder',h); self.assertIn('Potential Too Low',h)
        self.assertIn('within 25 miles',h); self.assertIn('not a point rain',h)
        self.assertEqual(len(s['data']['products']),8)
    def test_independent_failure_and_escape(self):
        c=Client(); c.rows[GEO_URLS[1]]=RuntimeError('<script>')
        h=self.render(collect_spc(c,NOW))
        self.assertNotIn('<script>',h); self.assertIn('unknown',h); self.assertIn('Potential Too Low',h)
    def test_empty_not_all_clear(self):
        c=Client(); c.rows[GEO_URLS[1]]['features']=[]
        h=self.render(collect_spc(c,NOW)); self.assertIn('Day 1',h); self.assertIn('unknown',h)
    def test_malformed_geometry(self):
        c=Client(); c.rows[GEO_URLS[1]]['features'][0]['geometry']['coordinates']=[[[999,40]]]
        s=collect_spc(c,NOW); self.assertIsNotNone(s['data']['products'][0]['error'])
    def test_outside_polygon_not_clear(self):
        c=Client(); c.rows[GEO_URLS[1]]['features'][0]['geometry']['coordinates']=[[[0,0],[1,0],[1,1],[0,1],[0,0]]]
        h=self.render(collect_spc(c,NOW)); self.assertIn('outside the depicted',h); self.assertNotIn('all clear',h)
    def test_stale_and_future_fetch(self):
        s=collect_spc(Client(),NOW)
        self.assertNotIn('KCDW: General thunder',self.render(s,now=NOW+timedelta(days=2)))
        s['fetched_at']=(NOW+timedelta(hours=1)).isoformat()
        self.assertIn('unknown',self.render(s))
    def test_out_of_window_and_persisted_tamper(self):
        s=collect_spc(Client(),NOW)
        self.assertIn('not covered',self.render(s,start=NOW+timedelta(days=10)))
        s['data']['products'][0]['geojson']['features'][0]['properties']['ISSUE']='bogus'
        self.assertIn('unknown',self.render(s))
    def test_severe_probability_and_hole(self):
        c=Client(); c.rows[GEO_URLS[4]]=fixture(4,30)
        h=self.render(collect_spc(c,NOW)); self.assertIn('30%',h)
        g=c.rows[GEO_URLS[1]]['features'][0]['geometry']
        g['coordinates'].append([[-74.5,40.5],[-74,40.5],[-74,41],[-74.5,41],[-74.5,40.5]])
        h=self.render(collect_spc(c,NOW)); self.assertIn('outside the depicted',h)
    def test_uncertain_is_not_potential_low_and_unknown_is_rejected(self):
        c=Client(); p=c.rows[GEO_URLS[4]]['features'][0]['properties']
        p['LABEL']='Predictability Too Low'
        h=self.render(collect_spc(c,NOW)); self.assertIn('uncertainty prevents delineation',h)
        p['LABEL']='No storms guaranteed'
        s=collect_spc(c,NOW); self.assertIsNotNone(s['data']['products'][3]['error'])
    def test_missing_and_invalid_sources(self):
        for s in (None, {}, {'fetched_at':NOW.isoformat(),'data':None}):
            self.assertIn('unknown',self.render(s))
    def test_stale_issuance_with_fresh_fetch(self):
        c=Client(); c.rows[GEO_URLS[1]]['features'][0]['properties']['ISSUE']='202609110820'
        s=collect_spc(c,NOW); self.assertIsNotNone(s['data']['products'][0]['error'])
    def test_adjacent_day_substitution_is_rejected(self):
        for target, wrong in [(d, neighbor) for d in range(1, 9) for neighbor in (d-1, d+1) if 1 <= neighbor <= 8]:
            with self.subTest(target=target, wrong=wrong):
                c = Client(); c.rows[GEO_URLS[target]] = fixture(wrong)
                s = collect_spc(c, NOW)
                self.assertIsNotNone(s['data']['products'][target-1]['error'])

    def test_overnight_day_one_and_month_boundary_day_two(self):
        from kcdw.spc_guidance import _parse
        for day, issued, first, last, at in [
            (1, '202609140055', '202609140100', '202609141200', '2026-09-14T02:00:00+00:00'),
            (2, '202601311730', '202602011200', '202602021200', '2026-01-31T18:00:00+00:00')]:
            raw = fixture(day)
            raw['features'][0]['properties'].update(ISSUE=issued, VALID=first, EXPIRE=last)
            self.assertEqual(_parse(raw, day, datetime.fromisoformat(at))['feature_count'], 1)

    def test_long_valid_discussion_survives_render_revalidation(self):
        discussion = ('SPC AC 130820\nDay 4-8 Convective Outlook\n'
                      'Valid 161200Z - 211200Z\n' + 'National discussion. ' * 600 +
                      '\n..Forecaster.. 09/13/2026\n')
        class DiscussionClient(Client):
            def get_text(self, url, maximum):
                return discussion
        source = collect_spc(DiscussionClient(), NOW)
        self.assertTrue(source['ok'], source['error'])
        self.assertGreater(len(source['data']['discussion']['text']), 9000)
        html = self.render(source)
        self.assertIn('SPC national Day 4–8 discussion (not a KCDW forecast)', html)
        self.assertNotIn('National discussion unavailable', html)
        self.assertLess(len(html), 20000)

    def test_timestamp_mismatch_and_wrong_day(self):
        for key,value in [('ISSUE_ISO','2026-09-12T08:20:00Z'),('VALID','202609191200')]:
            c=Client(); c.rows[GEO_URLS[1]]['features'][0]['properties'][key]=value
            s=collect_spc(c,NOW); self.assertIsNotNone(s['data']['products'][0]['error'])

if __name__=='__main__': unittest.main()
