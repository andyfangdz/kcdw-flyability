import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from kcdw import wn3_climatology as wn3

UTC = timezone.utc
NOW = datetime(2026, 9, 24, 18, tzinfo=UTC)


def rows(run, hours, speed=3.0):
    return [{'hours': h, 'wind_speed_10m_mean': speed, 'wind_speed_10m_p90': speed + 1, 'u_component_of_wind_10m_mean': -speed * 0.7071,
             'v_component_of_wind_10m_mean': -speed * 0.7071, 'low_cloud_cover_mean': 0.2, 'low_cloud_cover_p90': 0.6,
             'total_precipitation_1hr_mean': 0.0001} for h in hours]


class WN3ClimatologyTests(unittest.TestCase):
    def test_window_hours_follow_dst(self):
        self.assertEqual([t.hour for t in wn3.window_utc(date(2026, 10, 1), '14:00', '16:00')], [18, 19, 20])
        self.assertEqual([t.hour for t in wn3.window_utc(date(2026, 12, 1), '14:00', '16:00')], [19, 20, 21])
        run = wn3.run_for(date(2026, 10, 1))
        self.assertEqual(run, datetime(2026, 9, 24, 12, tzinfo=UTC))

    def test_run_and_cache_follow_lead(self):
        self.assertEqual(wn3.run_for(date(2026, 10, 1), 2), datetime(2026, 9, 29, 12, tzinfo=UTC))
        self.assertEqual(wn3.cache_path('var', '14:00').name, 'kcdw-1400.json')
        self.assertEqual(wn3.cache_path('var', '14:00', 2).name, 'kcdw-1400-d2.json')

    def test_summary_units_and_rows(self):
        s = wn3.summarize(rows(None, [174, 175, 176]), [174, 175, 176])
        self.assertAlmostEqual(s['sust_kt'], 3.0 * wn3.KNOTS, places=1)
        self.assertEqual((s['from_deg'], s['low_cloud'], s['low_cloud_p90'], s['rain_mm']), (45.0, 20.0, 60.0, 0.2))
        self.assertIsNone(wn3.summarize(rows(None, [174, 175]), [174, 175, 176]))

    def test_update_caches_and_enforces_cost_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            calls = []
            def runner(run, hours):
                calls.append(run)
                return rows(run, hours), 40 * 1024 ** 2
            cache = wn3.update(path, '14:00', '16:00', date(2026, 9, 1), date(2026, 9, 10), NOW, runner=runner)
            self.assertEqual(len(wn3.rows(cache)), 10)
            self.assertEqual(cache['billed_bytes'], 10 * 40 * 1024 ** 2)
            wn3.update(path, '14:00', '16:00', date(2026, 9, 1), date(2026, 9, 10), NOW, runner=runner)
            self.assertEqual(len(calls), 10)  # cached dates are never re-queried
            with self.assertRaises(RuntimeError):
                wn3.update(Path(tmp) / 'd.json', '14:00', '16:00', date(2026, 9, 1), date(2026, 9, 2), NOW,
                           runner=lambda run, hours: (rows(run, hours), wn3.MAX_QUERY_BYTES + 1))

    def test_dates_before_archive_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            wn3.update(Path(tmp) / 'c.json', '14:00', '16:00', date(2025, 12, 1), date(2026, 1, 10), NOW,
                       runner=lambda run, hours: (calls.append(run) or rows(run, hours), 1))
            self.assertTrue(all(run.date() >= wn3.FIRST_RUN for run in calls))


if __name__ == '__main__':
    unittest.main()
