import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kcdw import ecmwf_archive as arch

UTC = timezone.utc
NOW = datetime(2026, 9, 24, 18, tzinfo=UTC)


class Runner:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, items):
        self.calls.append(items)
        return [({'init': i['init'], 'lead': i['lead'], 'error': 'ValueError'} if i['init'] in self.fail else
                 {'init': i['init'], 'lead': i['lead'], 'sust_kt': 8.0, 'from_deg': 50.0, 'gust_kt': 20.0}) for i in items]


class ArchiveTests(unittest.TestCase):
    def test_seven_day_run_matches_flight_time_and_dst(self):
        self.assertEqual(arch.seven_day_item(date(2026, 10, 1), '14:00'), {'init': '2026-09-24T12:00:00Z', 'lead': 174})
        self.assertEqual(arch.sample_time(date(2026, 12, 1), '14:00'), datetime(2026, 12, 1, 18, tzinfo=UTC))  # 19Z EST floors to 18Z
        self.assertEqual(arch.seven_day_item(date(2026, 9, 24), '10:00'), {'init': '2026-09-17T12:00:00Z', 'lead': 168})

    def test_item_for_any_lead(self):
        self.assertEqual(arch.item_for(date(2026, 10, 1), '14:00', 6), {'init': '2026-09-25T12:00:00Z', 'lead': 150})
        self.assertEqual(arch.item_for(date(2026, 10, 1), '14:00', 1), {'init': '2026-09-30T12:00:00Z', 'lead': 30})
        self.assertEqual(arch.cache_path('var', '14:00', 7).name, 'kcdw-1400.json')
        self.assertEqual(arch.cache_path('var', '14:00', 3).name, 'kcdw-1400-d3.json')

    def test_update_fills_newest_first_and_retries_errors_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            first, last = date(2026, 9, 1), date(2026, 9, 10)
            bad = arch.seven_day_item(date(2026, 9, 5), '14:00')['init']
            runner = Runner(fail={bad})
            cache = arch.update(path, '14:00', first, last, NOW, runner=runner)
            self.assertEqual(len(cache['days']), 10)
            self.assertEqual(runner.calls[0][0]['init'], arch.seven_day_item(last, '14:00')['init'])
            self.assertEqual(len(arch.rows(cache)), 9)
            again = Runner()
            arch.update(path, '14:00', first, last, NOW + timedelta(hours=1), runner=again)
            self.assertEqual(again.calls, [])
            arch.update(path, '14:00', first, last, NOW + arch.RETRY_AFTER, runner=again)
            self.assertEqual([i['init'] for batch in again.calls for i in batch], [bad])
            [row] = [r for r in arch.rows(arch.load(path)) if r[0] == '2026-09-01']
            self.assertEqual(row[1:3], [8.0, 20.0])

    def test_failed_batch_does_not_stop_later_batches(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def runner(items):
                calls.append(items)
                if len(calls) == 1:
                    raise subprocess.CalledProcessError(2, 'worker')
                return Runner()(items)
            cache = arch.update(Path(tmp) / 'c.json', '14:00', date(2026, 8, 1), date(2026, 9, 10), NOW, runner=runner)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(cache['days']), 41 - arch.BATCH)

    def test_dates_before_gust_archive_and_unpublished_runs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner()
            arch.update(Path(tmp) / 'c.json', '14:00', date(2024, 11, 1), date(2024, 11, 25), NOW, runner=runner)
            dates = [i['init'][:10] for b in runner.calls for i in b]
            self.assertTrue(all(d >= '2024-11-12' for d in dates))
            runner = Runner()
            arch.update(Path(tmp) / 'd.json', '14:00', date(2026, 10, 1), date(2026, 10, 2), NOW, runner=runner)
            self.assertEqual(runner.calls, [])  # runs for these dates are still in the future

    def ee_runner(self, offset=0.0, days=40):
        def run(request):
            out = []
            for d in range(days):
                day = date(2026, 9, 10) - timedelta(days=d)
                item = arch.seven_day_item(day, '14:00')
                ms = int(datetime.fromisoformat(item['init'].replace('Z', '+00:00')).timestamp() * 1000)
                # 8 kt from 050 with a 20 kt gust, matching Runner(), plus an optional offset
                speed = (8.0 + offset) / arch.KNOTS
                out.append({'created_ms': ms, 'u': -speed * math.sin(math.radians(50)), 'v': -speed * math.cos(math.radians(50)),
                            'gust': (20.0 + offset) / arch.KNOTS})
            return out
        return run

    def test_earth_engine_fills_gaps_only_after_matching_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            first, last = date(2026, 8, 2), date(2026, 9, 10)
            fail = {arch.seven_day_item(date(2026, 9, 1), '14:00')['init']}
            arch.update(path, '14:00', first, last, NOW, runner=Runner(fail=fail))
            with self.assertRaises(ValueError):
                arch.fill_from_earth_engine(path, '14:00', first, last, NOW, runner=self.ee_runner(offset=0.5))
            self.assertEqual(arch.fill_from_earth_engine(path, '14:00', first, last, NOW, runner=self.ee_runner()), 1)
            self.assertEqual(arch.load(path)['days']['2026-09-01']['source'], 'earth-engine')

    def test_current_falls_back_to_verified_earth_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            first, last = date(2026, 8, 2), date(2026, 9, 10)
            arch.update(path, '14:00', first, last, NOW, runner=Runner())
            arch.fill_from_earth_engine(path, '14:00', first, last, NOW, runner=self.ee_runner())
            init = datetime(2026, 9, 24, 0, tzinfo=UTC)
            ms = int(init.timestamp() * 1000)
            ee = lambda request: [{'created_ms': ms, 'u': 0.0, 'v': -3.0, 'gust': 6.0}]
            result = arch.current(date(2026, 10, 1), '14:00', NOW, runner=Runner(fail={arch.iso_z(init)}), path=path, ee_runner=ee)
            self.assertEqual(result['init'], '2026-09-24T00:00:00Z')
            self.assertAlmostEqual(result['gust'], 6.0 * arch.KNOTS)

    def test_current_reads_each_run_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            runner = Runner()
            result = arch.current(date(2026, 10, 1), '14:00', NOW, runner=runner, path=path)
            self.assertEqual((result['init'], result['lead']), ('2026-09-24T00:00:00Z', 186))
            again = Runner()
            self.assertEqual(arch.current(date(2026, 10, 1), '14:00', NOW + timedelta(minutes=30), runner=again, path=path), result)
            self.assertEqual(again.calls, [])


if __name__ == '__main__':
    unittest.main()
