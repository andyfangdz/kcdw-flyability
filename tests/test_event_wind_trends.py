"""Snapshot-bound wind history contract; no network."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_event_change_evidence import fixture, NOW
from kcdw.common import iso_z
from kcdw.event_wind_trends import build_wind_trends, validate_wind_trends, wind_trend_evidence


def windy(at=NOW, value=10):
    data = fixture(at)
    for source in data['models'].values():
        for p in ('p10', 'p25', 'p50', 'p75', 'p90'):
            source['data']['hourly']['wind_gusts_10m'][p] = [value]*96
    return data


class WindTrendsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.current = windy()

    def save(self, data, name='old'):
        p = self.root/name/'snapshot.json'
        p.parent.mkdir()
        p.write_text(json.dumps(data))

    def build(self):
        self.save(windy(NOW-timedelta(hours=24), 20))
        result = build_wind_trends(self.current, self.root, NOW)
        self.assertIsNotNone(result)
        return result

    def test_history_validated_at_own_clock_no_render_io(self):
        out = self.build()
        rows = out['models']['gefs']['states']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][out['columns'].index('gust_center')], 20)
        self.assertEqual(out['sample_at'], '2026-09-24T16:00:00Z')
        self.current['wind_trends'] = out
        with patch('kcdw.event_wind_trends._archives', side_effect=AssertionError('render IO')):
            self.assertEqual(validate_wind_trends(out, self.current, NOW), out)
            self.assertIsNotNone(wind_trend_evidence(self.current, NOW))

    def test_duplicate_clocks_and_identical_values_collapse(self):
        old = windy(NOW-timedelta(hours=12), 20)
        self.save(old, 'one'); self.save(old, 'two')
        self.save(windy(NOW-timedelta(hours=6)), 'same')
        out = build_wind_trends(self.current, self.root, NOW)
        self.assertEqual(len(out['models']['gefs']['states']), 1)

    def test_keep_last_four_changes_plus_current(self):
        for i in range(8):
            self.save(windy(NOW-timedelta(hours=16-i), 11+i), str(i))
        out = build_wind_trends(self.current, self.root, NOW)
        self.assertEqual(len(out['models']['gefs']['states']), 5)

    def test_clock_window_and_current_mismatch(self):
        out = self.build()
        for now in (NOW.replace(tzinfo=None), NOW-timedelta(seconds=1), NOW+timedelta(hours=13)):
            self.assertIsNone(validate_wind_trends(out, self.current, now))
        bad = copy.deepcopy(out); bad['sample_at'] = '2026-09-24T17:00:00Z'
        self.assertIsNone(validate_wind_trends(bad, self.current, NOW))
        changed = windy(value=15)
        validated = validate_wind_trends(out, changed, NOW)
        self.assertNotIn('gefs', validated['models'])

    def test_bad_source_is_local_and_raw_fields_gate(self):
        out = self.build()
        self.current['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = True
        validated = validate_wind_trends(out, self.current, NOW)
        self.assertNotIn('gefs', validated['models'])
        self.assertIn('ecmwf_ens', validated['models'])
        for value in (True, -1, 400, float('nan'), '10'):
            bad = copy.deepcopy(out)
            bad['models']['gefs']['states'][0][out['columns'].index('gust_center')] = value
            result = validate_wind_trends(bad, windy(), NOW)
            self.assertTrue(result is None or 'gefs' not in result['models'])

    def test_future_stale_wrong_window_history_rejected(self):
        for i, age in enumerate((-1, 37)):
            self.save(windy(NOW-timedelta(hours=age), 20), str(i))
        wrong = windy(NOW-timedelta(hours=12), 20); wrong['event']['window'] = '09-17'
        self.save(wrong, 'wrong')
        out = build_wind_trends(self.current, self.root, NOW)
        self.assertEqual(len(out['models']['gefs']['states']), 1)

    def test_archive_bounds_reused(self):
        self.save(windy(NOW-timedelta(hours=12), 20))
        from kcdw import event_change_evidence as safe
        for bound in ('MAX_FILE_BYTES', 'MAX_READ_BYTES', 'MAX_SCAN_ENTRIES'):
            with patch.object(safe, bound, 0):
                out = build_wind_trends(self.current, self.root, NOW)
                self.assertEqual(len(out['models']['gefs']['states']), 1)

    def test_byte_budget_reserves_time_spread_before_recent_repeats(self):
        from kcdw import event_change_evidence as safe
        for age in [*range(1,14),18,24,30,36]:
            at=NOW-timedelta(hours=age)
            self.save(windy(at,10+age),at.strftime('%Y%m%dT%H%M%SZ'))
        sizes=[p.stat().st_size for p in self.root.glob('*/snapshot.json')]
        with patch.object(safe,'MAX_READ_BYTES',max(sizes)*4+1):
            out=build_wind_trends(self.current,self.root,NOW)
        clocks=[r[0] for r in out['models']['gefs']['states']]
        self.assertLessEqual(min(clocks),iso_z(NOW-timedelta(hours=24)))

    def test_explicit_flight_end_and_legacy_archive(self):
        self.current['event_timing'] = {'slug': 'commercial-checkride', 'date': '2026-09-24',
            'timezone': 'America/New_York', 'appointment_start': '08:00',
            'appointment_status': 'confirmed', 'flight_start': '10:00', 'flight_status': 'expected',
            'flight_end': '12:00', 'flight_duration_minutes': 120}
        out = self.build()
        self.assertEqual(out['sample_kind'], 'expected_flight_end')
        self.assertEqual(len(out['models']['gefs']['states']), 2)
        self.assertIn('not historical flight timing', ' '.join(out['notes']))

    def test_wn3_conversion_unknown_gusts_and_raw_units(self):
        before = copy.deepcopy(self.current)
        out = self.build()
        self.assertEqual(self.current, before)
        row = out['models']['wn3']['states'][-1]
        forecast = self.current['weathernext3']['data']['forecast']
        from kcdw.ensemble_trends import _time
        index = [_time(t) for t in forecast['valid_time_utc']].index(_time(out['sample_at']))
        self.assertAlmostEqual(row[out['columns'].index('wind_center')],
            forecast['fields']['wind_speed_10m']['mean'][index]*3600/1852)
        self.assertIsNone(row[out['columns'].index('gust_center')])
        self.current['models']['gefs']['data']['hourly_units']['wind_gusts_10m'] = 'm/s'
        result = validate_wind_trends(out, self.current, NOW)
        self.assertNotIn('gefs', result['models'])
        self.assertIn('wn3', result['models'])

    def test_historical_provenance_order_units(self):
        out = self.build()
        for field, value in [('fetched_at', iso_z(NOW)), ('wind_p10', 300),
                             ('collected_at', iso_z(NOW)), ('archive', None)]:
            bad = copy.deepcopy(out)
            bad['models']['gefs']['states'][0][out['columns'].index(field)] = value
            result = validate_wind_trends(bad, self.current, NOW)
            self.assertNotIn('gefs', result['models'])
        bad = copy.deepcopy(out); bad['unit'] = 'm/s'
        self.assertIsNone(validate_wind_trends(bad, self.current, NOW))

if __name__ == '__main__':
    unittest.main()
