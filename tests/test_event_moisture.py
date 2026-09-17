"""Supplemental RH contracts; fake payloads are not live weather evidence."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from kcdw.event_moisture import collect_moisture, validate_moisture as validate_envelope, MODELS, VARIABLES, UNITS

def validate_moisture(envelope, now):
    return validate_envelope(envelope, now)['models']

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 1, tzinfo=UTC)
START = datetime(2026, 9, 14, 4, tzinfo=UTC)
END = datetime(2026, 9, 26, 4, tzinfo=UTC)


class FakeClient:
    def __init__(self, mutate=None, fail=None):
        self.urls = []
        self.mutate = mutate
        self.fail = fail

    def get(self, url):
        self.urls.append(url)
        if self.fail and self.fail in url:
            raise RuntimeError('https://secret.example/?apikey=DO_NOT_LEAK')
        if '/static/meta.json' in url:
            dataset = url.split('/data/')[1].split('/')[0]
            init = NOW.replace(hour=0) - timedelta(hours=6)
            raw = {'last_run_initialisation_time': int(init.timestamp()),
                   'last_run_modification_time': int((init + timedelta(hours=4)).timestamp()),
                   'last_run_availability_time': int((init + timedelta(hours=5)).timestamp()),
                   'data_end_time': int((init + timedelta(days=6 if dataset == 'ecmwf_ifs025' else 16)).timestamp()),
                   'temporal_resolution_seconds': 10800 if dataset == 'ecmwf_ifs025' else 21600 if dataset == 'ecmwf_aifs025_single' else 3600,
                   'update_interval_seconds': 21600}
            if self.mutate:
                self.mutate(dataset, raw)
            return raw
        q = parse_qs(urlparse(url).query)
        start = datetime.fromisoformat(q['start_hour'][0]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(q['end_hour'][0]).replace(tzinfo=UTC)
        n = int((end-start).total_seconds()/3600) + 1
        raw = {'latitude': 40.875, 'longitude': -74.25, 'timezone': 'GMT', 'utc_offset_seconds': 0,
               'hourly_units': dict(UNITS, time='unixtime'),
               'hourly': {'time': [int((start+timedelta(hours=i)).timestamp()) for i in range(n)]}}
        for field in VARIABLES:
            raw['hourly'][field] = [1005 if field == 'surface_pressure' else 500 if field.startswith('geopotential') else 85] * n
        if self.mutate:
            self.mutate(q['models'][0], raw)
        return raw


class MoistureTests(unittest.TestCase):
    def good(self):
        envelope = collect_moisture(FakeClient(), START, END, NOW)
        statuses = validate_moisture(envelope, NOW)
        self.assertTrue(all(s['available'] for s in statuses.values()), statuses)
        return envelope

    def test_live_clock_fractional_seconds_survive_timestamp_serialization(self):
        now = NOW + timedelta(microseconds=123456)
        envelope = collect_moisture(FakeClient(), START, END, now)
        statuses = validate_moisture(envelope, now)
        self.assertTrue(all(s['available'] for s in statuses.values()), statuses)

    def test_fresh_explicit_models_exact_axis_and_display_range(self):
        client = FakeClient()
        envelope = collect_moisture(client, START, END, NOW)
        statuses = validate_moisture(envelope, NOW)
        self.assertEqual(set(statuses), set(MODELS))
        self.assertTrue(all(s['available'] for s in statuses.values()), statuses)
        queries = [parse_qs(urlparse(u).query) for u in client.urls if '/v1/forecast?' in u]
        self.assertEqual({q['models'][0] for q in queries}, {m['model_id'] for m in MODELS.values()})
        for q in queries:
            self.assertEqual(q['start_hour'], ['2026-09-15T00:00'])
            self.assertEqual(q['end_hour'], ['2026-09-26T03:00'])
            self.assertEqual(q['hourly'][0].split(','), list(VARIABLES))
        self.assertEqual(envelope['range']['start'], '2026-09-14T04:00:00Z')
        self.assertEqual(envelope['range']['end'], '2026-09-26T04:00:00Z')
        self.assertEqual(envelope['models']['gfs']['data']['requested_start'], '2026-09-15T00:00:00Z')
        self.assertEqual(len([u for u in client.urls if '/static/meta.json' in u]), 4)

    def test_short_ifs_metadata_horizon_does_not_reject_rolling_rh(self):
        data = validate_moisture(self.good(), NOW)['ifs']['data']
        self.assertFalse(data['metadata']['model_init_is_response_bound'])
        self.assertEqual(data['metadata']['provenance'], 'latest-advertised; not response-bound')
        self.assertIn('unbound beyond metadata horizon', data['metadata']['hourly_provenance'])
        self.assertEqual(data['hourly']['relative_humidity_2m'][-1], 85)

    def test_gaps_below_surface_and_missing_pressure(self):
        def mutate(model, raw):
            if 'hourly' in raw:
                h = raw['hourly']
                h['relative_humidity_2m'][2] = None
                h['surface_pressure'][0] = 990
                h['surface_pressure'][1] = None
                h['relative_humidity_925hPa'][3] = None
        env = collect_moisture(FakeClient(mutate), START, END, NOW)
        for status in validate_moisture(env, NOW).values():
            self.assertTrue(status['available'], status)
            h = status['data']['hourly']
            self.assertIsNone(h['relative_humidity_1000hPa'][0])
            self.assertIsNone(h['geopotential_height_1000hPa'][0])
            self.assertEqual(h['relative_humidity_925hPa'][0], 85)
            self.assertIsNone(h['relative_humidity_850hPa'][1])
            self.assertIsNone(h['relative_humidity_2m'][2])
            self.assertIsNone(h['relative_humidity_925hPa'][3])

    def test_undefined_units_only_when_entire_field_null(self):
        for unit in (None, 'undefined'):
            def mutate(model, raw):
                if 'hourly' in raw:
                    raw['hourly_units']['relative_humidity_925hPa'] = unit
                    raw['hourly']['relative_humidity_925hPa'] = [None] * len(raw['hourly']['time'])
            env = collect_moisture(FakeClient(mutate), START, END, NOW)
            self.assertTrue(all(s['available'] for s in validate_moisture(env, NOW).values()))
        env = self.good()
        env['models']['gfs']['data']['hourly_units']['relative_humidity_2m'] = 'undefined'
        self.assertFalse(validate_moisture(env, NOW)['gfs']['available'])

    def test_provider_failures_isolated_and_sanitized(self):
        for failure in ('models=gfs_global', '/ncep_gfs025/', 'models=ecmwf_ifs025'):
            env = collect_moisture(FakeClient(fail=failure), START, END, NOW)
            statuses = validate_moisture(env, NOW)
            self.assertEqual(sum(s['available'] for s in statuses.values()), 2)
            self.assertNotIn('DO_NOT_LEAK', repr(env))
            self.assertNotIn('secret.example', repr(statuses))

    def test_stale_fetch_and_aware_input(self):
        env = self.good()
        self.assertTrue(all(not s['available'] for s in validate_moisture(env, NOW + timedelta(hours=12, seconds=1)).values()))
        for start, end, now in ((START.replace(tzinfo=None), END, NOW), (START, END, NOW.replace(tzinfo=None)),
                                (START, END + timedelta(days=10), NOW), (END, START, NOW)):
            client = FakeClient()
            env = collect_moisture(client, start, end, now)
            self.assertTrue(all(not s['available'] for s in validate_moisture(env, NOW).values()))
            self.assertEqual(client.urls, [])

    def test_malformed_raw_isolated(self):
        mutations = [lambda r: r.update(latitude=0), lambda r: r.update(utc_offset_seconds=True),
                     lambda r: r.update(model_id='best_match'),
                     lambda r: r['hourly']['time'].__setitem__(0, 0),
                     lambda r: r['hourly']['relative_humidity_2m'].__setitem__(0, 101),
                     lambda r: r['hourly']['surface_pressure'].__setitem__(0, float('nan')),
                     lambda r: r['hourly']['relative_humidity_2m'].__setitem__(0, True),
                     lambda r: r['hourly_units'].update(geopotential_height_925hPa='ft'),
                     lambda r: r['hourly']['relative_humidity_850hPa'].pop()]
        for change in mutations:
            def mutate(model, raw):
                if model == 'gfs_global':
                    change(raw)
            status = validate_moisture(collect_moisture(FakeClient(mutate), START, END, NOW), NOW)
            self.assertFalse(status['gfs']['available'])
            self.assertTrue(status['ifs']['available'])

    def test_metadata_stale_future_chronology_and_bad_horizon(self):
        for field, value in [('latest_advertised_init', '2026-09-12T18:00:00Z'),
                             ('latest_advertised_available_at', '2026-09-16T00:00:00Z'),
                             ('latest_advertised_modified_at', '2026-09-15T00:00:00Z'),
                             ('data_end_time', '2026-09-13T00:00:00Z'),
                             ('data_end_time', '2026-10-30T00:00:00Z'),
                             ('temporal_resolution_seconds', True),
                             ('fetched_at', '2026-09-13T00:00:00Z')]:
            env = self.good()
            env['models']['ifs']['data']['metadata']['datasets']['ecmwf_ifs025'][field] = value
            statuses = validate_moisture(env, NOW)
            self.assertFalse(statuses['ifs']['available'], field)
            self.assertTrue(statuses['gfs']['available'])

    def test_persisted_tampering_and_no_mutation(self):
        env = self.good()
        original = copy.deepcopy(env)
        validate_moisture(env, NOW)
        self.assertEqual(env, original)
        for change in (lambda d: d.update(model_id='best_match'),
                       lambda d: d.update(endpoint='https://bad.example'),
                       lambda d: d['hourly']['time'].reverse(),
                       lambda d: d['metadata'].update(model_init_is_response_bound=True)):
            corrupt = copy.deepcopy(env)
            change(corrupt['models']['gfs']['data'])
            self.assertFalse(validate_moisture(corrupt, NOW)['gfs']['available'])
        self.assertTrue(all(not s['available'] for s in validate_moisture(None, NOW).values()))


if __name__ == '__main__':
    unittest.main()
