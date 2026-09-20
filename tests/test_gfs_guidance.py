import copy
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

from kcdw.gfs_guidance import collect_gfs, validate_gfs, VARIABLES, UNITS

UTC = timezone.utc
NOW = datetime(2026, 9, 13, 16, tzinfo=UTC)
START = datetime(2026, 9, 22, 4, tzinfo=UTC)
END = datetime(2026, 9, 26, 4, tzinfo=UTC)


class FakeClient:
    def __init__(self):
        self.urls = []
        self.raw = {'latitude': 40.875, 'longitude': -74.25, 'timezone': 'GMT', 'utc_offset_seconds': 0,
                    'hourly_units': dict(UNITS), 'hourly': {'time': [int((START + timedelta(hours=i)).timestamp()) for i in range(96)]}}
        for name, value in zip(VARIABLES, (1012, 0.5, 10, 16, 40, 22)):
            self.raw['hourly'][name] = [value] * 96
        self.meta = {'last_run_initialisation_time': int(NOW.replace(hour=6).timestamp()),
                     'last_run_availability_time': int(NOW.replace(hour=11).timestamp()),
                     'last_run_modification_time': int(NOW.replace(hour=10).timestamp()),
                     'data_end_time': int((NOW.replace(hour=6) + timedelta(days=16)).timestamp()),
                     'temporal_resolution_seconds': 3600, 'update_interval_seconds': 21600}

    def get(self, url):
        self.urls.append(url)
        return copy.deepcopy(self.meta if '/static/meta.json' in url else self.raw)


class GFSTests(unittest.TestCase):
    def good(self):
        result = collect_gfs(FakeClient(), START, END, NOW)
        self.assertTrue(result['ok'], result['error'])
        return result

    def test_collect_pinned_axis_and_provenance(self):
        client = FakeClient()
        result = collect_gfs(client, START, END, NOW)
        self.assertTrue(result['ok'], result['error'])
        query = parse_qs(urlparse(next(u for u in client.urls if '/v1/forecast' in u)).query)
        self.assertEqual(query['models'], ['gfs_global'])
        self.assertEqual(query['wind_speed_unit'], ['kn'])
        data = result['data']
        self.assertEqual(data['model_id'], 'gfs_global')
        self.assertEqual(data['hourly']['time'][0], '2026-09-22T04:00:00Z')
        self.assertEqual(data['hourly']['time'][-1], '2026-09-26T03:00:00Z')
        self.assertEqual(data['metadata']['provenance'], 'latest-advertised; not response-bound')
        self.assertEqual(validate_gfs(result, NOW), {'available': True, 'error': ''})

    def test_nulls_are_gaps(self):
        client = FakeClient()
        client.raw['hourly']['precipitation'][3] = None
        result = collect_gfs(client, START, END, NOW)
        self.assertTrue(result['ok'], result['error'])
        self.assertIsNone(result['data']['hourly']['precipitation'][3])

    def test_raw_corruption_is_source_failure(self):
        for path, value in [(('latitude',), 0), (('longitude',), True), (('timezone',), 'America/New_York'),
                            (('hourly_units', 'precipitation'), 'inch'),
                            (('hourly', 'time'), [0]*96), (('hourly', 'temperature_2m'), [float('nan')]*96),
                            (('hourly', 'cloud_cover_low'), [101]*96), (('hourly', 'wind_speed_10m'), [True]*96)]:
            with self.subTest(path=path):
                client = FakeClient()
                target = client.raw
                for key in path[:-1]: target = target[key]
                target[path[-1]] = value
                self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])

    def test_persisted_tampering_rejected(self):
        mutations = [lambda d: d.update(model_id='gfs_seamless'), lambda d: d.update(endpoint='https://example.org'),
                     lambda d: d['hourly']['time'].__setitem__(0, '2026-09-22T05:00:00Z'),
                     lambda d: d['hourly']['precipitation'].__setitem__(0, float('inf')),
                     lambda d: d['hourly']['wind_gusts_10m'].__setitem__(0, True),
                     lambda d: d['hourly_units'].update(pressure_msl='Pa'),
                     lambda d: d['grid_point'].update(latitude=0),
                     lambda d: d.update(fetched_at='2026-09-14T00:00:00Z'),
                     lambda d: d['metadata'].update(provenance='response-bound')]
        for mutate in mutations:
            source = self.good()
            mutate(source['data'])
            self.assertFalse(validate_gfs(source, NOW)['available'])

    def test_render_time_staleness(self):
        self.assertFalse(validate_gfs(self.good(), NOW + timedelta(hours=25))['available'])
        self.assertFalse(validate_gfs(self.good(), NOW + timedelta(hours=12, seconds=1))['available'])

    def test_persisted_metadata_revalidated(self):
        for field, value in [('latest_advertised_init', '2026-09-11T06:00:00Z'),
                             ('latest_advertised_available_at', '2026-09-14T00:00:00Z'),
                             ('latest_advertised_modified_at', '2026-09-13T12:00:00Z'),
                             ('data_end_time', '2026-09-23T00:00:00Z'),
                             ('endpoint', 'https://ensemble-api.open-meteo.com/data/ncep_gefs05/static/meta.json'),
                             ('temporal_resolution_seconds', True),
                             ('fetched_at', '2026-09-12T00:00:00Z')]:
            with self.subTest(field=field):
                source = self.good()
                source['data']['metadata']['datasets']['ncep_gfs025'][field] = value
                self.assertFalse(validate_gfs(source, NOW)['available'])

    def test_missing_metadata_fails_closed(self):
        client = FakeClient()
        del client.meta['last_run_initialisation_time']
        self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])
        source = self.good()
        del source['data']['metadata']['datasets']['ncep_gfs013']
        self.assertFalse(validate_gfs(source, NOW)['available'])

    def test_wrong_identity_and_extra_members_rejected(self):
        for model in ['gfs_seamless', 'hrrr_conus', 'gfs05']:
            client = FakeClient()
            client.raw['model_id'] = model
            self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])
        client = FakeClient()
        client.raw['hourly']['precipitation_member01'] = [0]*96
        self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])

    def test_all_null_series_stays_unknown(self):
        client = FakeClient()
        client.raw['hourly']['cloud_cover_low'] = [None]*96
        source = collect_gfs(client, START, END, NOW)
        self.assertTrue(source['ok'], source['error'])
        self.assertEqual(source['data']['hourly']['cloud_cover_low'], [None]*96)
        self.assertIn('never clear weather', source['data']['metadata']['null_semantics'])

    def test_axis_truncation_shift_and_outside_horizon(self):
        for change in [lambda h: h['time'].pop(), lambda h: h['time'].__setitem__(1, h['time'][0]),
                       lambda h: h['precipitation'].pop()]:
            client = FakeClient()
            change(client.raw['hourly'])
            self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])
        source = self.good()
        source['data']['requested_start'] = '2026-09-22T03:00:00Z'
        self.assertFalse(validate_gfs(source, NOW)['available'])
        source = self.good()
        source['data']['grid_point']['requested_latitude'] = 40.0
        self.assertFalse(validate_gfs(source, NOW)['available'])


    def test_metadata_stale_future_missing_or_short(self):
        for key, value in [('last_run_initialisation_time', 0), ('last_run_availability_time', int((NOW+timedelta(hours=1)).timestamp())),
                           ('data_end_time', int(START.timestamp())), ('last_run_initialisation_time', True)]:
            client = FakeClient()
            client.meta[key] = value
            self.assertFalse(collect_gfs(client, START, END, NOW)['ok'])

    def test_nonthrowing_and_bad_window(self):
        for source in [None, [], {}, {'ok': True, 'data': None}, {'ok': 1, 'data': {}}]:
            self.assertFalse(validate_gfs(source, NOW)['available'])
        for start, end in [(END, START), (START.replace(tzinfo=None), END), (START+timedelta(minutes=1), END)]:
            self.assertFalse(collect_gfs(FakeClient(), start, end, NOW)['ok'])
        class Broken:
            def get(self, url): raise RuntimeError('offline')
        self.assertIn('offline', collect_gfs(Broken(), START, END, NOW)['error'])


if __name__ == '__main__':
    unittest.main()
