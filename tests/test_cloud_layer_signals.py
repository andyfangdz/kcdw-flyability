import copy
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from kcdw import cloud_layer_signals as layers

NOW = datetime(2026, 9, 15, 22, tzinfo=timezone.utc)
SNAPSHOT = {'collected_at': '2026-09-15T22:00:00Z',
            'event': {'slug': 'checkride', 'title': 'Checkride', 'date': '2026-09-24',
                      'window': '08-17', 'nav_label': 'Checkride', 'description': 'Event'},
            'range': {'start': '2026-09-22T04:00:00Z', 'end': '2026-09-26T04:00:00Z'}}


class Client:
    def __init__(self, mutate=None):
        self.urls = []
        self.mutate = mutate

    def get(self, url):
        self.urls.append(url)
        query = parse_qs(urlsplit(url).query)
        fields = query['hourly'][0].split(',')
        times = [int(datetime(2026, 9, 24, hour, tzinfo=timezone.utc).timestamp()) for hour in range(12, 21)]
        raw = {'latitude': 41.0, 'longitude': -74.5, 'timezone': 'GMT', 'utc_offset_seconds': 0,
               'hourly': {'time': times}, 'hourly_units': {'time': 'unixtime'}}
        ensemble = 'ensemble-api' in url
        for field in fields:
            for member in range(3 if ensemble else 1):
                key = field + (f'_member{member:02}' if member else '')
                value = (1010 if field == 'surface_pressure' else 180 if field == 'wind_direction_10m' else
                         10 if field == 'wind_speed_10m' else 95 if 'humidity' in field else
                         80 if field == 'cloud_cover_low' else
                         {1000: 120, 925: 780, 850: 1500}[int(field.split('_')[-1][:-3])] if field.startswith('geopotential') else 10)
                raw['hourly'][key] = [value] * len(times)
                raw['hourly_units'][key] = layers.FIELD_UNITS[field]
        if self.mutate:
            self.mutate(raw, query, ensemble)
        return raw


class LayerTests(unittest.TestCase):
    def collect(self, client=None):
        return layers.collect_layer_signals(client or Client(), SNAPSHOT, NOW)

    def test_collect_profiles_and_members(self):
        client = Client()
        result = self.collect(client)
        self.assertIsNotNone(result)
        self.assertEqual(len(client.urls), 6)
        self.assertEqual(result['ensembles']['gefs']['data']['all_three']['joint_low_cloud_rh925'], {'count': 3, 'denominator': 3})
        point = result['profiles']['gfs']['data']['points'][0]
        self.assertEqual(point['levels']['925']['temperature'], 10)
        self.assertEqual(point['thermal']['925_850']['lapse_c_per_km'], 0)
        self.assertFalse(result['profiles']['gfs']['data']['metadata']['model_init_is_response_bound'])
        self.assertIsNotNone(layers.validate_layer_signals(result, SNAPSHOT, NOW))

    def test_same_member_persistence_not_marginal_minimum(self):
        def mutate(raw, query, ensemble):
            if ensemble:
                raw['hourly']['cloud_cover_low'][0] = 0
                raw['hourly']['cloud_cover_low_member01'][4] = 0
                raw['hourly']['cloud_cover_low_member02'][8] = 0
        data = self.collect(Client(mutate))['ensembles']['gefs']['data']
        self.assertEqual([p['screens']['low_cloud']['count'] for p in data['points']], [2, 2, 2])
        self.assertEqual(data['all_three']['low_cloud'], {'count': 0, 'denominator': 3})

    def test_below_ground_missing_and_strict_pressure_gate(self):
        def mutate(raw, query, ensemble):
            for key in list(raw['hourly']):
                if key.startswith('surface_pressure'):
                    raw['hourly'][key] = [925] * 9
        result = self.collect(Client(mutate))
        point = result['profiles']['gfs']['data']['points'][0]
        self.assertEqual(point['levels']['1000']['status'], 'below_ground')
        self.assertIsNone(point['levels']['1000']['temperature'])
        self.assertIsNone(point['thermal']['1000_925']['lapse_c_per_km'])
        self.assertEqual(result['ensembles']['gefs']['data']['points'][0]['screens']['rh925'], {'count': None, 'denominator': 0})

    def test_failure_isolated_and_no_upstream_error(self):
        def mutate(raw, query, ensemble):
            if query['models'] == ['gfs05']:
                raise RuntimeError('secret token')
        result = self.collect(Client(mutate))
        self.assertFalse(result['ensembles']['gefs']['ok'])
        self.assertTrue(result['profiles']['gfs']['ok'])
        self.assertNotIn('secret', str(result))

    def test_validation_tampering_source_isolated(self):
        result = self.collect()
        mutations = [lambda d: d.update(endpoint='https://evil.test'),
                     lambda d: d['points'][0]['screens']['low_cloud'].update(count=True),
                     lambda d: d['points'][0]['screens']['low_cloud'].update(count=32),
                     lambda d: d.update(ceiling_ft=1000),
                     lambda d: d['all_three']['low_cloud'].update(denominator=4)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                bad = copy.deepcopy(result)
                mutate(bad['ensembles']['gefs']['data'])
                valid = layers.validate_layer_signals(bad, SNAPSHOT, NOW)
                self.assertFalse(valid['ensembles']['gefs']['ok'])
                self.assertTrue(valid['profiles']['gfs']['ok'])

    def test_invalid_clock_event_range_and_unknown_outer_fields(self):
        result = self.collect()
        for key, value in [('collected_at', '2026-09-14T00:00:00Z'), ('version', True),
                           ('range', {'start': '2026-09-24T00:00:00Z', 'end': '2026-09-25T00:00:00Z'}),
                           ('event', dict(SNAPSHOT['event'], date='2026-09-25')), ('ceiling', 800)]:
            bad = copy.deepcopy(result)
            bad[key] = value
            self.assertIsNone(layers.validate_layer_signals(bad, SNAPSHOT, NOW))

    def test_missing_surface_pressure_and_fields_remain_unknown(self):
        def mutate(raw, query, ensemble):
            for key in list(raw['hourly']):
                if key.startswith('surface_pressure') or key.startswith('relative_humidity_2m'):
                    raw['hourly'][key] = [None] * 9
                    raw['hourly_units'][key] = 'undefined'
        result = self.collect(Client(mutate))
        data = result['ensembles']['gefs']['data']
        self.assertEqual(data['points'][0]['screens']['joint_surface_rh925'], {'count': None, 'denominator': 0})
        self.assertEqual(data['points'][0]['screens']['low_cloud']['denominator'], 3)
        point = result['profiles']['gfs']['data']['points'][0]
        self.assertEqual(point['levels']['925']['status'], 'unknown_ground')
        self.assertIsNone(point['levels']['925']['relative_humidity'])

    def test_rejects_raw_axis_duplicate_member_and_grid(self):
        mutations = [lambda r: r['hourly']['time'].__setitem__(0, r['hourly']['time'][1]),
                     lambda r: r['hourly'].update(cloud_cover_low_member00=[80]*9),
                     lambda r: r.update(latitude=0)]
        for mutate in mutations:
            def change(raw, query, ensemble):
                if ensemble:
                    mutate(raw)
            self.assertFalse(self.collect(Client(change))['ensembles']['gefs']['ok'])

    def test_detached_copy_and_profile_derived_values_revalidated(self):
        result = self.collect()
        valid = layers.validate_layer_signals(result, SNAPSHOT, NOW)
        valid['profiles']['gfs']['data']['points'][0]['thermal']['925_850']['inversion'] = True
        self.assertFalse(layers.validate_layer_signals(valid, SNAPSHOT, NOW)['profiles']['gfs']['ok'])
        self.assertFalse(result['profiles']['gfs']['data']['points'][0]['thermal']['925_850']['inversion'])

    def test_profile_tampering_and_units(self):
        result = self.collect()
        for value in [float('nan'), True, 200]:
            bad = copy.deepcopy(result)
            bad['profiles']['gfs']['data']['points'][0]['surface']['temperature_2m'] = value
            valid = layers.validate_layer_signals(bad, SNAPSHOT, NOW)
            self.assertFalse(valid['profiles']['gfs']['ok'])
        def mutate(raw, query, ensemble):
            if not ensemble:
                raw['hourly_units']['temperature_2m'] = 'F'
        self.assertFalse(self.collect(Client(mutate))['profiles']['gfs']['ok'])


if __name__ == '__main__':
    unittest.main()
