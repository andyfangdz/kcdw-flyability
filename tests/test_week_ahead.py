import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kcdw import event_gusts as gusts
from kcdw import synoptic_pattern as pattern
from kcdw import week_ahead as week
from test_event_gusts import FakeClient, available, fake_rrfs
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc
EXTENDED = '''000
FXUS02 KWBC 181932
PMDEPD

Extended Forecast Discussion
Valid 12Z Mon Sep 21 2026 - 12Z Fri Sep 25 2026

...Overview...

A coastal low lingers near the Northeast before exiting.

Second overview paragraph.

...Guidance/Predictability Assessment...

Models disagree on the Plains trough.

Timing of the front into the Northeast is uncertain.

...Weather/Hazards Highlights...

Heavy rain in the Southwest.

$$
'''


def record(text, issued, uuid, code):
    return {'id': uuid, 'url': f'{pattern.ROOT}/{uuid}', 'productCode': code, 'issuingOffice': 'KWNH',
            'issuanceTime': issued, 'fetched_at': '2026-09-18T21:30:00Z', 'productText': text}


class WeekAheadTests(unittest.TestCase):
    def snapshot(self):
        s = copy.deepcopy(SNAPSHOT)
        with patch.object(gusts, '_rrfs_series', side_effect=fake_rrfs):
            s['event_gusts'] = gusts.collect_gusts(FakeClient(available(range(0, 30))), s, NOW)
        return s

    def test_rows_are_flight_aligned_and_bounded(self):
        times = week.anchors(self.snapshot(), NOW)
        self.assertIn(datetime(2026, 9, 24, 14, tzinfo=UTC), times)
        self.assertTrue(all((t - times[0]) % timedelta(hours=12) == timedelta(0) for t in times))
        self.assertLessEqual(len(times), week.MAX_ROWS)
        self.assertGreaterEqual(times[0], NOW.replace(minute=0))

    def test_columns_keep_missing_values_missing(self):
        s = self.snapshot()
        times, cols = week.columns(s, NOW)
        nbm = next(c for c in cols if c[0] == 'nbm')
        cell = nbm[3]['2026-09-24T14:00:00Z']
        self.assertEqual((cell['dir'], cell['wind'], cell['gust']), (40, 10.0, 24.0))
        self.assertNotIn('pressure', cell)  # fixture responses carry no pressure
        self.assertEqual(week._sum12([0.5] * 11 + [None], 11), None)
        self.assertEqual(week._sum12([0.25] * 12, 11), 3.0)
        self.assertIsNone(week._sum12([0.1] * 5, 4))

    def test_extended_discussion_is_verbatim_regional_and_bound(self):
        s = self.snapshot()
        s['synoptic_pattern'] = {'snapshot_collected_at': s['collected_at'],
                                 'extended': record(EXTENDED, '2026-09-18T19:32:00Z', '00000000-0000-0000-0000-00000000000e', 'PMD')}
        wpc = week.extended(s, NOW)
        self.assertEqual(wpc['overview'], ['A coastal low lingers near the Northeast before exiting.', 'Second overview paragraph.'])
        self.assertEqual([r['text'] for r in wpc['regional']], ['Timing of the front into the Northeast is uncertain.'])
        s['synoptic_pattern']['snapshot_collected_at'] = '2026-09-18T20:00:00Z'
        self.assertIsNone(week.extended(s, NOW))
        stale = copy.deepcopy(s['synoptic_pattern'])
        stale['extended']['issuanceTime'] = '2026-09-16T00:00:00Z'
        with self.assertRaises(ValueError):
            pattern.extended_discussion(stale, NOW)

    def test_render_and_evidence(self):
        s = self.snapshot()
        html = week.render_week(s, NOW)
        self.assertIn('flight start', html)
        self.assertEqual(html.count('<tr data-flight>'), 1)
        packet = week.week_evidence(s, NOW)
        self.assertIn('2026-09-24T14:00:00Z', packet['rows'])
        self.assertIn('nbm', packet['rows']['2026-09-24T14:00:00Z'])
        self.assertEqual(week.render_week({**SNAPSHOT}, NOW), '')


if __name__ == '__main__':
    unittest.main()
