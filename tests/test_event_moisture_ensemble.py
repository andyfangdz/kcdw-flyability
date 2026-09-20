"""Synthetic members exercise RH aggregation without network access."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
import unittest
from kcdw.event_moisture_ensemble import collect_moisture_ensemble, validate_moisture_ensemble
UTC = timezone.utc
NOW = datetime(2026, 9, 14, 22, tzinfo=UTC)
START = NOW.replace(hour=0)
END = START + timedelta(hours=3)
FIELDS = ('relative_humidity_2m', 'relative_humidity_1000hPa', 'relative_humidity_925hPa', 'relative_humidity_850hPa')

class Client:

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.queries = []

    def get(self, url):
        if '/static/meta.json' in url:
            return {'last_run_initialisation_time': (NOW - timedelta(hours=6)).timestamp(), 'last_run_availability_time': (NOW - timedelta(hours=3)).timestamp(), 'data_end_time': (NOW + timedelta(hours=24)).timestamp(), 'temporal_resolution_seconds': 10800}
        q = parse_qs(urlparse(url).query)
        self.queries.append(q)
        model = q['models'][0]
        count = 31 if model == 'gfs05' else 51
        start = datetime.fromisoformat(q['start_hour'][0]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(q['end_hour'][0]).replace(tzinfo=UTC)
        n = int((end - start).total_seconds() / 3600) + 1
        h = {'time': [(start + timedelta(hours=i)).timestamp() for i in range(n)]}
        units = {'time': 'unixtime'}
        for field in (*FIELDS, 'surface_pressure'):
            for member in range(count):
                key = field + (f'_member{member:02}' if member else '')
                h[key] = [1010 if field == 'surface_pressure' else member for _ in range(n)]
                units[key] = 'hPa' if field == 'surface_pressure' else '%'
        raw = {'latitude': 40.8752, 'longitude': -74.2814, 'timezone': 'GMT', 'utc_offset_seconds': 0, 'hourly': h, 'hourly_units': units}
        if self.mutate:
            self.mutate(raw, model)
        return raw

class MoistureEnsembleTests(unittest.TestCase):

    def test_real_member_quantiles_and_no_model_pooling(self):
        client = Client()
        envelope = collect_moisture_ensemble(client, START, END, NOW)
        view = validate_moisture_ensemble(envelope, NOW)
        self.assertTrue(set(view['models']) == {'gefs', 'ecmwf_ens', 'aifs_ens'})
        for alias, median, low, high, count in [('gefs', 15, 3, 27, 31), ('ecmwf_ens', 25, 5, 45, 51), ('aifs_ens', 25, 5, 45, 51)]:
            source = view['models'][alias]
            self.assertTrue(source['available'], source['error'])
            data = source['data']
            fan = data['hourly'][FIELDS[0]]
            self.assertTrue(fan == {'p10': [low] * 3, 'p50': [median] * 3, 'p90': [high] * 3, 'sample_counts': [count] * 3})
            self.assertTrue(not any('_member' in key for key in data['hourly']))
        self.assertTrue({q['models'][0] for q in client.queries} == {'gfs05', 'ecmwf_ifs025', 'ecmwf_aifs025'})
        self.assertTrue(all(q['timeformat'] == ['unixtime'] for q in client.queries))

    def test_terrain_and_missing_pressure_are_memberwise_not_mean_mask(self):

        def mutate(raw, model):
            raw['hourly']['surface_pressure'] = [999, None, 1000]
            for i in range(1, 31 if model == 'gfs05' else 51):
                raw['hourly'][f'surface_pressure_member{i:02}'] = [900, None, 800]
        data = collect_moisture_ensemble(Client(mutate), START, END, NOW)['models']['gefs']['data']
        self.assertTrue(data['hourly'][FIELDS[1]]['sample_counts'] == [0, 0, 1])
        self.assertTrue(data['hourly'][FIELDS[1]]['p50'] == [None, None, 0])
        self.assertTrue(data['hourly'][FIELDS[2]]['sample_counts'] == [1, 0, 1])
        self.assertTrue(data['hourly'][FIELDS[3]]['sample_counts'] == [31, 0, 1])
        self.assertTrue(data['hourly'][FIELDS[0]]['sample_counts'] == [31, 31, 31])

    def test_partial_nulls_and_missing_fields_are_unknown_not_zero(self):

        def mutate(raw, model):
            for key in list(raw['hourly']):
                if key.startswith(FIELDS[1]):
                    del raw['hourly'][key]
                    del raw['hourly_units'][key]
                elif key.startswith(FIELDS[0]):
                    raw['hourly'][key][1] = None
        env = collect_moisture_ensemble(Client(mutate), START, END, NOW)
        d = validate_moisture_ensemble(env, NOW)['models']['gefs']['data']
        self.assertTrue(d['hourly'][FIELDS[1]]['p50'] == [None] * 3)
        self.assertTrue(d['hourly'][FIELDS[1]]['sample_counts'] == [0] * 3)
        self.assertTrue(d['hourly'][FIELDS[0]]['sample_counts'] == [31, 0, 31])

    def test_undefined_units_only_preserve_entirely_missing_member_fields(self):
        for unit in (None, 'undefined'):
            with self.subTest(unit=unit):
                def mutate(raw, model):
                    for key in raw['hourly']:
                        if key.startswith(FIELDS[1]):
                            raw['hourly'][key] = [None] * 3
                            raw['hourly_units'][key] = unit
                env = collect_moisture_ensemble(Client(mutate), START, END, NOW)
                view = validate_moisture_ensemble(env, NOW)
                for alias, count in (('gefs',31),('ecmwf_ens',51),('aifs_ens',51)):
                    source = view['models'][alias]
                    self.assertTrue(source['available'], source['error'])
                    hourly = source['data']['hourly']
                    self.assertEqual(hourly[FIELDS[1]]['sample_counts'],[0]*3)
                    self.assertEqual(hourly[FIELDS[1]]['p50'],[None]*3)
                    for field in (FIELDS[0],FIELDS[2],FIELDS[3]):
                        self.assertEqual(hourly[field]['sample_counts'],[count]*3)

    def test_undefined_units_with_any_real_value_still_reject_source(self):
        for unit in (None, 'undefined'):
            with self.subTest(unit=unit):
                def mutate(raw, model):
                    if model == 'gfs05':
                        raw['hourly'][FIELDS[1]] = [None, 80, None]
                        raw['hourly_units'][FIELDS[1]] = unit
                env = collect_moisture_ensemble(Client(mutate), START, END, NOW)
                view = validate_moisture_ensemble(env, NOW)
                self.assertFalse(view['models']['gefs']['available'])
                self.assertTrue(view['models']['ecmwf_ens']['available'])

    def test_bad_provider_is_isolated_and_errors_are_safe(self):
        for bad in ['units', 'member', 'duplicate', 'axis', 'length', 'bounds', 'nan', 'grid', 'model']:
            with self.subTest(bad=bad):

                def mutate(raw, model):
                    if model != 'gfs05':
                        return
                    if bad == 'units':
                        raw['hourly_units'][FIELDS[0]] = 'secret-token'
                    if bad == 'member':
                        raw['hourly'][FIELDS[0] + '_member31'] = [1] * 3
                    if bad == 'duplicate':
                        raw['hourly'][FIELDS[0] + '_member00'] = [1] * 3
                    if bad == 'axis':
                        raw['hourly']['time'][1] += 3600
                    if bad == 'length':
                        raw['hourly'][FIELDS[0]].pop()
                    if bad == 'bounds':
                        raw['hourly'][FIELDS[0]][0] = 101
                    if bad == 'nan':
                        raw['hourly'][FIELDS[0]][0] = float('nan')
                    if bad == 'grid':
                        raw['latitude'] = 0
                    if bad == 'model':
                        raw['model'] = 'wrong'
                env = collect_moisture_ensemble(Client(mutate), START, END, NOW)
                view = validate_moisture_ensemble(env, NOW)
                self.assertTrue(not view['models']['gefs']['available'])
                self.assertTrue(view['models']['ecmwf_ens']['available'])
                self.assertTrue('secret-token' not in str(env))

    def test_utc_midnight_clip_keeps_original_range(self):
        start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=-4)))
        now = datetime(2026, 9, 14, 0, 30, tzinfo=UTC)
        client = Client()
        original = client.get

        def get(url):
            if '/static/meta.json' in url:
                return {'last_run_initialisation_time': (now - timedelta(hours=6)).timestamp(), 'last_run_availability_time': (now - timedelta(hours=3)).timestamp(), 'data_end_time': (now + timedelta(hours=24)).timestamp(), 'temporal_resolution_seconds': 10800}
            return original(url)
        client.get = get
        env = collect_moisture_ensemble(client, start, END, now)
        self.assertTrue(env['range']['start'] == '2026-09-13T04:00:00Z')
        self.assertTrue(client.queries[0]['start_hour'] == ['2026-09-14T00:00'])
        self.assertTrue(validate_moisture_ensemble(env, now)['models']['gefs']['available'])
        self.assertTrue(env['models']['gefs']['data']['hourly']['time'][0] == '2026-09-14T00:00:00Z')

    def test_persisted_contract_revalidated(self):
        for bad in ['stale_fetch', 'stale_meta', 'order', 'count', 'zero_count', 'units', 'identity', 'axis', 'provenance']:
            with self.subTest(bad=bad):
                env = collect_moisture_ensemble(Client(), START, END, NOW)
                d = env['models']['gefs']['data']
                fan = d['hourly'][FIELDS[0]]
                if bad == 'stale_fetch':
                    d['fetched_at'] = '2026-09-13T00:00:00Z'
                if bad == 'stale_meta':
                    d['metadata']['initialization_time'] = '2026-09-12T00:00:00Z'
                if bad == 'order':
                    fan['p10'][0] = 99
                if bad == 'count':
                    fan['sample_counts'][0] = 32
                if bad == 'zero_count':
                    fan['sample_counts'][0] = 0
                if bad == 'units':
                    d['hourly_units'][FIELDS[0]] = 'fraction'
                if bad == 'identity':
                    d['members'] = 51
                if bad == 'axis':
                    d['hourly']['time'][0] = '2026-09-13T00:00:00Z'
                if bad == 'provenance':
                    d['metadata']['binding_note'] = 'exact immutable run'
                view = validate_moisture_ensemble(env, NOW)
                self.assertTrue(not view['models']['gefs']['available'])
                self.assertTrue(view['models']['aifs_ens']['available'])

    def test_short_latest_cycle_is_not_a_false_exact_run_binding(self):
        start, end = (START + timedelta(days=5), END + timedelta(days=5))
        env = collect_moisture_ensemble(Client(), start, end, NOW)
        view = validate_moisture_ensemble(env, NOW)
        d = view['models']['ecmwf_ens']['data']
        self.assertTrue(view['models']['ecmwf_ens']['available'])
        self.assertTrue(not d['metadata']['covers_display'])
        self.assertTrue('not an immutable run binding' in d['metadata']['binding_note'])

    def test_absent_pressure_preserves_surface_and_subset_members_count_honestly(self):

        def mutate(raw, model):
            for key in list(raw['hourly']):
                if key.startswith('surface_pressure') or '_member' in key:
                    del raw['hourly'][key]
                    del raw['hourly_units'][key]
        env = collect_moisture_ensemble(Client(mutate), START, END, NOW)
        source = validate_moisture_ensemble(env, NOW)['models']['gefs']
        self.assertTrue(source['available'])
        h = source['data']['hourly']
        self.assertTrue(h[FIELDS[0]]['sample_counts'] == [1] * 3)
        self.assertTrue(h[FIELDS[0]]['p50'] == [0] * 3)
        for field in FIELDS[1:]:
            self.assertTrue(h[field]['sample_counts'] == [0] * 3)
            self.assertTrue(h[field]['p50'] == [None] * 3)

    def test_unusable_metadata_is_not_persisted(self):
        for bad in ['stale', 'missing', 'secret', 'future']:
            with self.subTest(bad=bad):
                client = Client()
                original = client.get

                def get(url):
                    if 'ncep_gefs05/static/meta.json' in url:
                        if bad == 'missing':
                            return {}
                        if bad == 'secret':
                            raise RuntimeError('https://private/?apikey=secret-token')
                        raw = original(url)
                        delta = timedelta(hours=-25 if bad == 'stale' else 1)
                        raw['last_run_initialisation_time'] = (NOW + delta).timestamp()
                        return raw
                    return original(url)
                client.get = get
                env = collect_moisture_ensemble(client, START, END, NOW)
                self.assertTrue(env['models']['gefs']['data'] is None)
                self.assertTrue('secret-token' not in str(env))
                self.assertTrue(validate_moisture_ensemble(env, NOW)['models']['ecmwf_ens']['available'])

    def test_persisted_untrusted_extras_members_and_numeric_types(self):
        for mutate in [lambda d: d.update(raw_members={'secret-token': [1, 2]}), lambda d: d['member_ids'][FIELDS[0]].append('99'), lambda d: d['hourly'][FIELDS[0]]['sample_counts'].__setitem__(0, True), lambda d: d['hourly'][FIELDS[0]]['p50'].__setitem__(0, float('inf')), lambda d: d['hourly'][FIELDS[0]]['p90'].pop()]:
            with self.subTest(mutate=mutate):
                env = collect_moisture_ensemble(Client(), START, END, NOW)
                mutate(env['models']['gefs']['data'])
                view = validate_moisture_ensemble(env, NOW)
                self.assertTrue(not view['models']['gefs']['available'])
                self.assertTrue('secret-token' not in str(view))
                self.assertTrue(view['models']['ecmwf_ens']['available'])

    def test_later_render_does_not_rebase_collection_axis(self):
        env = collect_moisture_ensemble(Client(), START, END, NOW)
        self.assertTrue(validate_moisture_ensemble(env, NOW + timedelta(hours=3))['models']['gefs']['available'])
        self.assertTrue(not validate_moisture_ensemble(env, NOW + timedelta(hours=13))['models']['gefs']['available'])
