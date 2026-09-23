import copy
import unittest
from datetime import datetime, timedelta, timezone

from kcdw import synoptic_pattern as pattern
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc
ANALYSIS = '''000
ASUS01 KWBC 181835
CODSUS

CODED SURFACE FRONTAL POSITIONS
VALID 091818Z
HIGHS 1037 4978 1040 60120
LOWS 1014 3373 990 5550 1016
3769
STNRY 3881 3783
'''
FORECAST = '''000
FSUS02 KWBC {stamp}
CODSRP

CODED SURFACE FRONTAL POSITIONS FORECAST
12HR PROG VALID 190600Z
HIGHS 1036 4874
LOWS {low} 3571
COLD WK 3784 3585
24HR PROG VALID 241200Z
HIGHS 1036 4774
LOWS 1008 3568
'''


def record(text, issued, uuid):
    return {'id': uuid, 'url': f'{pattern.ROOT}/{uuid}', 'productCode': 'COD', 'issuingOffice': 'KWBC',
            'issuanceTime': issued, 'fetched_at': '2026-09-18T21:30:00Z', 'productText': text}


def envelope(forecasts=None):
    return {'version': 1, 'snapshot_collected_at': SNAPSHOT['collected_at'],
            'event': {k: SNAPSHOT['event'][k] for k in ('slug', 'date')},
            'analysis': record(ANALYSIS, '2026-09-18T18:35:00Z', '00000000-0000-0000-0000-000000000001'),
            'forecasts': forecasts if forecasts is not None else [
                record(FORECAST.format(stamp='181751', low=1012), '2026-09-18T17:51:00Z', '00000000-0000-0000-0000-000000000002')]}


class PatternTests(unittest.TestCase):
    def test_decodes_wrapped_centers_and_both_time_formats(self):
        issued = datetime(2026, 9, 18, 18, 35, tzinfo=UTC)
        [(valid, hours, highs, lows)] = pattern.decode(ANALYSIS, issued, True)
        self.assertEqual((valid, hours), (datetime(2026, 9, 18, 18, tzinfo=UTC), None))
        self.assertEqual(highs[0], (1037, 49, -78))
        self.assertEqual(lows[-1], (1016, 37, -69))
        blocks = pattern.decode(FORECAST.format(stamp='181751', low=1012), issued, False)
        self.assertEqual([(b[0], b[1]) for b in blocks], [(datetime(2026, 9, 19, 6, tzinfo=UTC), 12),
                                                          (datetime(2026, 9, 24, 12, tzinfo=UTC), 24)])

    def test_month_rollover(self):
        valid = pattern._valid('011200Z', datetime(2026, 9, 30, 17, tzinfo=UTC), False)
        self.assertEqual(valid, datetime(2026, 10, 1, 12, tzinfo=UTC))

    def test_selects_nearby_centers_and_derives_gradient(self):
        rows = pattern.validate_pattern(envelope(), SNAPSHOT, NOW)['rows']
        now = rows[0]
        self.assertEqual(now['high']['hPa'], 1037)  # 1040 hPa over the Pacific is out of range
        self.assertEqual(now['low']['hPa'], 1014)   # the 990 hPa low near Labrador is too far
        self.assertEqual(now['difference_hPa'], 23)
        self.assertEqual(now['high']['direction'], 'NNW')
        self.assertEqual(rows[-1]['difference_hPa'], 28)
        self.assertGreater(rows[-1]['gradient_hPa_per_100km'], now['gradient_hPa_per_100km'])

    def test_newest_issuance_wins_each_valid_time(self):
        older = record(FORECAST.format(stamp='180359', low=1002), '2026-09-18T03:59:00Z', '00000000-0000-0000-0000-000000000003')
        newer = record(FORECAST.format(stamp='181751', low=1012), '2026-09-18T17:51:00Z', '00000000-0000-0000-0000-000000000002')
        rows = pattern.validate_pattern(envelope([older, newer]), SNAPSHOT, NOW)['rows']
        self.assertEqual([r['low']['hPa'] for r in rows if r['valid'] == '2026-09-19T06:00:00Z'], [1012])

    def test_stale_or_foreign_products_fail_closed(self):
        stale = envelope()
        stale['analysis']['issuanceTime'] = '2026-09-18T01:00:00Z'
        other = envelope()
        other['event'] = {'slug': 'other', 'date': '2026-09-24'}
        bad = envelope()
        bad['analysis']['productText'] = bad['analysis']['productText'].replace('4978', '49X8')
        for packet in (stale, other, bad):
            with self.assertRaises(ValueError):
                pattern.validate_pattern(packet, SNAPSHOT, NOW)
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot['synoptic_pattern'] = stale
        self.assertIsNone(pattern.pattern_evidence(snapshot, NOW))
        self.assertEqual(pattern.render_pattern(snapshot, NOW), '')

    def test_render_story_marks_flight_row(self):
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot['synoptic_pattern'] = envelope()
        html = pattern.render_pattern(snapshot, NOW)
        self.assertIn('the gradient tightens', html)
        self.assertEqual(html.count('<tr data-flight>'), 1)
        self.assertIn('nearest the flight', html)


if __name__ == '__main__':
    unittest.main()
