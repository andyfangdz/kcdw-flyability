"""Weekly behavior tests; synthetic source fixtures, no network."""
import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from kcdw import weekly_guidance as weekly
from kcdw.common import UTC, iso_z
from test_events import NOW, FakeClient, synthetic_wn3


class WeeklyTests(unittest.TestCase):
    def test_axis_is_seven_eastern_dates_across_dst(self):
        for date, hours in [('2026-03-08', 167), ('2026-11-01', 169), ('2026-09-13', 168)]:
            now = datetime.fromisoformat(date + 'T16:00:00+00:00')
            start, end = weekly.weekly_range(now)
            self.assertEqual(len(weekly.hourly_axis(start, end)), hours)
            self.assertEqual(start.astimezone(weekly.TZ).hour, 0)
            self.assertEqual(end.astimezone(weekly.TZ).hour, 0)

    def test_collect_isolates_all_ensemble_failures_and_gfs(self):
        with patch.object(weekly, 'collect_model', side_effect=ValueError('broken')) as model, patch.object(weekly, 'collect_gfs', return_value={'ok': False, 'error': 'offline'}) as gfs:
            result = weekly.collect_weekly(None, NOW)
        self.assertEqual(model.call_count, 4)
        self.assertEqual(gfs.call_count, 1)
        self.assertEqual(len(result['models']), 4)
        self.assertTrue(all(not s['ok'] for s in result['models'].values()))
        self.assertIsNone(model.call_args.args[2])
        self.assertIn('display_range', model.call_args.kwargs)
        json.dumps(result, allow_nan=False)

    def test_wn3_survives_missing_old_and_malformed_weekly(self):
        for guidance in [None, {}, {'models': []}, {'collected_at': 'broken', 'models': {'gefs': {'ok': True}}}]:
            page = weekly.render_weekly({'weekly_guidance': guidance, 'sources': {'weather_next3': {'ok': True, 'fetched_at': iso_z(NOW), 'data': synthetic_wn3()}}}, NOW)
            self.assertIn('data-model="wn3"', page)
            self.assertNotIn('checkride', page.lower())
            self.assertNotIn('fill="#e8b84a"', page)
            self.assertIn('data-forecast-view="full"', page)
            self.assertIn('fixed-y-axis', page)
            self.assertEqual(page.count('data-comparison-field='), 6)

    def test_live_wn3_millisecond_timestamps_align_to_weekly_axis(self):
        data = synthetic_wn3()
        data['forecast']['valid_time_utc'] = [datetime.fromisoformat(t.replace('Z', '+00:00')).isoformat(timespec='milliseconds').replace('+00:00', 'Z') for t in data['forecast']['valid_time_utc']]
        page = weekly.render_weekly({'sources': {'weather_next3': {'ok': True, 'fetched_at': iso_z(NOW), 'data': data}}}, NOW)
        self.assertEqual(page.count('data-model="wn3"'), 5)

    def test_stale_and_malformed_wn3_fail_closed(self):
        source = {'ok': True, 'fetched_at': iso_z(NOW), 'data': synthetic_wn3()}
        for now, bad in [(NOW + timedelta(days=2), source), (NOW, dict(source, data={}))]:
            page = weekly.render_weekly({'sources': {'weather_next3': bad}}, now)
            self.assertNotIn('data-model="wn3"', page)
            self.assertIn('unavailable', page)

    def test_gfs_survives_conventional_failure_and_preserves_gaps(self):
        from test_gfs_guidance import FakeClient as GFSClient, NOW as GFS_NOW
        start, end = weekly.weekly_range(GFS_NOW)
        axis = weekly.hourly_axis(start, end)
        client = GFSClient()
        client.raw['hourly']['time'] = [int(t.timestamp()) for t in axis]
        for key in weekly.VARIABLES:
            client.raw['hourly'][key] = [client.raw['hourly'][key][0]] * len(axis)
        client.raw['hourly']['precipitation'][12] = None
        gfs = weekly.collect_gfs(client, start, end, GFS_NOW)
        self.assertTrue(gfs['ok'], gfs)
        page = weekly.render_weekly({'weekly_guidance': {'models': [], 'gfs': gfs}}, GFS_NOW)
        self.assertEqual(page.count('data-model="gfs"'), 6)
        self.assertNotIn('<polygon', page)
        self.assertIn('null', page)
        self.assertIn('Deterministic / no uncertainty band', page)
        gfs['data']['hourly']['precipitation'][0] = True
        self.assertNotIn('data-model="gfs"', weekly.render_weekly({'weekly_guidance': {'gfs': gfs}}, GFS_NOW))

    def test_evening_gfs_request_clips_only_past_utc_hours(self):
        now = datetime(2026, 9, 14, 2, tzinfo=UTC)
        with patch.object(weekly, 'collect_model', side_effect=ValueError('offline')), patch.object(weekly, 'collect_gfs', return_value={'ok': False}) as gfs:
            weekly.collect_weekly(None, now)
        self.assertEqual(gfs.call_args.args[1], now.replace(hour=0))
        self.assertEqual(gfs.call_args.args[2], weekly.weekly_range(now)[1])

    def test_render_after_midnight_keeps_the_report_week(self):
        collected = NOW.astimezone(weekly.TZ).replace(hour=23, minute=30).astimezone(UTC)
        guidance = weekly.collect_weekly(FakeClient(), collected)
        snapshot = {'collected_at': iso_z(collected), 'weekly_guidance': guidance}
        later = collected + timedelta(hours=1)
        page = weekly.render_weekly(snapshot, later)
        self.assertIn('data-axis-start="' + guidance['range']['start'] + '"', page)
        self.assertNotEqual(weekly.weekly_range(collected), weekly.weekly_range(later))

    def test_partial_member_hours_keep_other_samples_and_fields(self):
        class GappyClient(FakeClient):
            def get(self, url):
                raw = super().get(url)
                if 'v1/ensemble?' in url and 'models=gfs05' in url:
                    rain = [key for key in raw['hourly'] if key.startswith('precipitation')]
                    raw['hourly'][rain[0]][12] = None
                    for key in rain:
                        raw['hourly'][key][13] = None
                return raw
        result = weekly.collect_weekly(GappyClient(), NOW)
        gefs = result['models']['gefs']
        self.assertTrue(gefs['ok'], gefs)
        rain = gefs['data']['hourly']['precipitation']
        self.assertEqual(rain['sample_counts'][12], 30)
        self.assertEqual(rain['sample_counts'][13], 0)
        self.assertIsNotNone(rain['p50'][12])
        self.assertIsNone(rain['p50'][13])
        self.assertEqual(rain['sample_counts'][14], 31)
        self.assertIn('data-model="gefs"', weekly.render_weekly({'weekly_guidance': result}, NOW))
        # Event collection keeps its previous strict completeness contract.
        from test_events import EVENT
        with self.assertRaisesRegex(ValueError, 'partially missing'):
            weekly.collect_model(GappyClient(), weekly.MODELS[0], EVENT, NOW)

    def test_conventional_revalidated_individually(self):
        result = weekly.collect_weekly(FakeClient(), NOW)
        self.assertTrue(result['models']['gefs']['ok'], result['models']['gefs'])
        page = weekly.render_weekly({'weekly_guidance': result}, NOW)
        self.assertIn('data-model="gefs"', page)
        broken = copy.deepcopy(result)
        broken['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = float('nan')
        page = weekly.render_weekly({'weekly_guidance': broken}, NOW)
        self.assertNotIn('data-model="gefs"', page)
        self.assertIn('data-model="aifs_ens"', page)
        page = weekly.render_weekly({'weekly_guidance': result}, NOW + timedelta(hours=13))
        self.assertNotIn('data-model="gefs"', page)


if __name__ == '__main__':
    unittest.main()
