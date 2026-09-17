"""Paired wind evidence: source isolation, identities, units and event binding."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from kcdw.event_wind import collect_wind, validate_wind, wind_evidence

NOW = datetime(2026, 9, 17, 15, 30, tzinfo=timezone.utc)
FIELDS = ('wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m')


def snapshot():
    return {'collected_at': '2026-09-17T15:30:00Z',
            'event': {'slug': 'commercial-checkride', 'date': '2026-09-24', 'window': '08-17',
                      'title': 'Checkride', 'nav_label': 'Checkride', 'description': 'Flight'},
            'event_timing': {'slug': 'commercial-checkride', 'date': '2026-09-24',
                            'timezone': 'America/New_York', 'appointment_start': '08:00',
                            'appointment_status': 'confirmed', 'flight_start': '10:00',
                            'flight_status': 'expected', 'flight_end': '12:00',
                            'flight_duration_minutes': 120}}


class Client:
    def __init__(self):
        self.calls = []
        self.fail: str | None = None

    def get(self, url):
        self.calls.append(url)
        if self.fail and self.fail in url:
            raise RuntimeError('source unavailable')
        if 'meta.json' in url:
            return {'last_run_initialisation_time': datetime(2026, 9, 17, 6, tzinfo=timezone.utc).timestamp(),
                    'last_run_availability_time': NOW.timestamp() - 3600,
                    'data_end_time': datetime(2026, 9, 23, 9, tzinfo=timezone.utc).timestamp(),
                    'temporal_resolution_seconds': 10800}
        if 'weather.gov' in url:
            p = {'@id': url, 'gridId': 'OKX', 'gridX': 23, 'gridY': 48,
                 'updateTime': '2026-09-17T15:11:45Z'}
            for f, v in [('windSpeed', 11.112), ('windGust', 25.928), ('windDirection', 60)]:
                p[f] = {'uom': 'wmoUnit:degree_(angle)' if f == 'windDirection' else 'wmoUnit:km_h-1',
                        'values': [{'validTime': '2026-09-24T14:00:00Z/PT3H', 'value': v}]}
            return {'properties': p}
        q = parse_qs(urlsplit(url).query)
        model = q['models'][0]
        n = 31 if model == 'gfs05' else 51
        h: dict = {'time': [f'2026-09-24T{i:02}:00' for i in range(24)]}
        u = {'time': 'iso8601'}
        for f in FIELDS:
            for m in range(n):
                key = f + (f'_member{m:02}' if m else '')
                values = [10 if f == FIELDS[0] else 90 if f == FIELDS[1] else 20] * 24
                if f == FIELDS[2]:
                    values[14] = 100  # opening endpoint must not inflate two-hour maximum
                    values[16] = 30 if m == 0 else 25
                if model == 'ecmwf_aifs025' and f == FIELDS[2]:
                    values = [None] * 24
                h[key] = values
                u[key] = 'undefined' if all(v is None for v in values) else '°' if f == FIELDS[1] else 'kn'
        return {'latitude': 41, 'longitude': -74.25, 'timezone': 'GMT', 'utc_offset_seconds': 0,
                'hourly': h, 'hourly_units': u}


class WindTests(unittest.TestCase):
    def collect(self):
        s = snapshot(); c = Client(); s['event_wind'] = collect_wind(c, s, NOW)
        return s, c

    def test_paired_requests_maxima_crosswind_and_compact_evidence(self):
        s, c = self.collect(); e = wind_evidence(s, NOW)
        self.assertEqual(len(c.calls), 7)
        for url in c.calls:
            if '/v1/ensemble?' in url:
                q = parse_qs(urlsplit(url).query)
                self.assertEqual(set(q['hourly'][0].split(',')), set(FIELDS))
                self.assertEqual(q['wind_speed_unit'], ['kn'])
        g = e['ensembles']['gefs']
        self.assertEqual(g['gust_max'], {'n': 31, 'p50': 25.0, 'p90': 25.0, 'ge20': 31, 'ge25': 31, 'ge30': 1})
        self.assertEqual(g['crosswind']['04']['ge15'], 31)
        self.assertEqual(g['crosswind']['10']['ge15'], 0)
        self.assertEqual(g['samples'][0]['direction_counts'], {'E': 31})
        self.assertEqual(e['nws']['samples'][0]['wind_kt'], 6.0)
        self.assertIsNone(e['ensembles']['aifs_ens']['gust_max'])
        self.assertIsNone(e['ensembles']['aifs_ens']['crosswind']['04'])
        self.assertIn('unverified', e['ensembles']['ecmwf_ens']['binding'])
        self.assertLess(len(json.dumps(e).encode()), 5000)

    def test_missing_timing_old_archive_and_binding_rejected(self):
        s, _ = self.collect()
        for key, value in [('collected_at', '2026-09-17T15:31:00Z'), ('event_timing', None)]:
            b = copy.deepcopy(s); b[key] = value
            self.assertIsNone(validate_wind(b['event_wind'], b, NOW))
        b = copy.deepcopy(s); b['event_timing'].update(flight_start='11:00', flight_end='13:00')
        self.assertIsNone(validate_wind(b['event_wind'], b, NOW))
        self.assertIsNone(wind_evidence(snapshot(), NOW))
        b = snapshot(); del b['event_timing']; c = Client()
        self.assertIsNone(collect_wind(c, b, NOW)); self.assertFalse(c.calls)

    def test_independent_failures_and_expiration(self):
        s = snapshot(); c = Client(); c.fail = 'models=gfs05'
        s['event_wind'] = collect_wind(c, s, NOW)
        e = wind_evidence(s, NOW)
        self.assertIsNone(e['ensembles']['gefs']); self.assertIsNotNone(e['nws'])
        self.assertIsNone(wind_evidence(s, NOW + timedelta(hours=13)))

    def test_raw_revalidation_not_cached_summary(self):
        s, _ = self.collect()
        s['event_wind']['ensembles']['gefs']['summary'] = {'gust_max': {'n': 999}}
        self.assertEqual(wind_evidence(s, NOW)['ensembles']['gefs']['gust_max']['n'], 31)

    def test_model_units_members_axes_physical_values(self):
        mutations = [
            lambda r: r['hourly_units'].update(wind_speed_10m='km/h'),
            lambda r: r['hourly']['wind_direction_10m'].__setitem__(15, 361),
            lambda r: r['hourly']['wind_gusts_10m'].__setitem__(15, -1),
            lambda r: r['hourly']['wind_speed_10m'].__setitem__(15, True),
            lambda r: r['hourly']['wind_speed_10m'].__setitem__(15, float('nan')),
            lambda r: r['hourly'].update(wind_speed_10m_member00=[10]*24),
            lambda r: r['hourly'].pop('wind_direction_10m_member01'),
            lambda r: r['hourly']['time'].__setitem__(15, r['hourly']['time'][14]),
            lambda r: r.update(utc_offset_seconds=3600),
            lambda r: r.update(latitude=0),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                s, _ = self.collect(); mutate(s['event_wind']['ensembles']['gefs']['raw'])
                e = wind_evidence(s, NOW)
                self.assertIsNone(e['ensembles']['gefs']); self.assertIsNotNone(e['ensembles']['ecmwf_ens'])

    def test_missing_pair_excluded_not_zero_and_separate_denominators(self):
        s, _ = self.collect(); h = s['event_wind']['ensembles']['gefs']['raw']['hourly']
        h['wind_direction_10m'][16] = None
        h['wind_gusts_10m_member01'][15] = None
        g = wind_evidence(s, NOW)['ensembles']['gefs']
        self.assertEqual(g['gust_max']['n'], 30)
        self.assertEqual(g['crosswind']['04']['n'], 29)

    def test_nws_units_identity_issue_and_interval_coverage(self):
        mutations = [lambda p: p.update(gridX=24),
                     lambda p: p.update(updateTime='2026-09-16T00:00:00Z'),
                     lambda p: p.update(updateTime='2026-09-18T00:00:00Z'),
                     lambda p: p['windSpeed'].update(uom='m/s'),
                     lambda p: p['windGust']['values'][0].update(value=-1),
                     lambda p: p['windDirection']['values'][0].update(value=400),
                     lambda p: p['windGust']['values'][0].update(validTime='2026-09-24T14:00:00Z/PT1H'),
                     lambda p: p['windSpeed']['values'].append(copy.deepcopy(p['windSpeed']['values'][0]))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                s, _ = self.collect(); mutate(s['event_wind']['nws']['raw']['properties'])
                e = wind_evidence(s, NOW)
                self.assertIsNone(e['nws']); self.assertIsNotNone(e['ensembles']['gefs'])

    def test_likely_cycle_requires_recognized_short_horizon(self):
        s,_=self.collect()
        raw=s['event_wind']['ensembles']['ecmwf_ens']['metadata']['raw']
        raw['data_end_time']=NOW.timestamp()+3600
        evidence=wind_evidence(s,NOW)
        assert evidence is not None
        m=evidence['ensembles']['ecmwf_ens']
        self.assertIsNone(m['likely_init'])
        self.assertIsNotNone(m['gust_max'])

    def test_nws_subhourly_hazard_intervals_survive_evidence(self):
        s,_=self.collect()
        s['event_wind']['nws']['raw']['properties']['windGust']['values']=[
            {'validTime':'2026-09-24T14:00:00Z/PT15M','value':18.52},
            {'validTime':'2026-09-24T14:15:00Z/PT30M','value':74.08},
            {'validTime':'2026-09-24T14:45:00Z/PT2H15M','value':18.52}]
        evidence=wind_evidence(s,NOW)
        assert evidence is not None
        nws=evidence['nws']
        self.assertEqual([p['gust_kt'] for p in nws['samples']],[10,10,10])
        self.assertIn(['2026-09-24T14:15:00Z','2026-09-24T14:45:00Z',6.0,40.0,60],nws['interval_rows'])
        from kcdw.event_wind_view import render_wind
        page=render_wind(s,NOW)
        self.assertIn('10:15–10:45',page)
        self.assertIn('<td>40</td>',page)

    def test_metadata_failure_is_advertised_only_not_member_failure(self):
        s = snapshot(); c = Client(); c.fail = 'meta.json'
        s['event_wind'] = collect_wind(c, s, NOW)
        g = wind_evidence(s, NOW)['ensembles']['gefs']
        self.assertIsNone(g['advertised_init']); self.assertEqual(g['gust_max']['n'], 31)

    def test_sustained_is_member_max_and_direction_aggregate_is_return(self):
        s, _ = self.collect(); h = s['event_wind']['ensembles']['gefs']['raw']['hourly']
        h['wind_speed_10m'][14] = 50
        h['wind_direction_10m'][16] = 0
        g = wind_evidence(s, NOW)['ensembles']['gefs']
        self.assertEqual(g['sustained']['n'], 31)
        self.assertEqual(g['direction_at'], '2026-09-24T16:00:00Z')
        self.assertEqual(g['direction_counts'], {'N': 1, 'E': 30})
        self.assertEqual(g['samples'][0]['direction_counts'], {'E': 31})

    def test_crosswind_pairs_actual_members_not_marginal_quantiles(self):
        s, _ = self.collect(); h = s['event_wind']['ensembles']['gefs']['raw']['hourly']
        for member in range(31):
            suffix = f'_member{member:02}' if member else ''
            for i in (15, 16):
                h['wind_gusts_10m'+suffix][i] = 30 if member < 15 else 5
                h['wind_direction_10m'+suffix][i] = 30 if member < 15 else 120
        g = wind_evidence(s, NOW)['ensembles']['gefs']
        self.assertEqual(g['crosswind']['04']['ge15'], 0)
        self.assertEqual(g['crosswind']['04']['p90'], 5)
        self.assertEqual(g['gust_max']['ge30'], 15)

    def test_bad_metadata_removed_without_losing_members(self):
        for name, value in [('last_run_initialisation_time', NOW.timestamp()+86400),
                            ('temporal_resolution_seconds', 1),
                            ('last_run_availability_time', True)]:
            s, _ = self.collect()
            s['event_wind']['ensembles']['gefs']['metadata']['raw'][name] = value
            g = wind_evidence(s, NOW)['ensembles']['gefs']
            self.assertIsNone(g['advertised_init'])
            self.assertEqual(g['gust_max']['n'], 31)

    def test_nws_null_stays_null_and_hidden_interval_gap_rejected(self):
        s, _ = self.collect()
        p = s['event_wind']['nws']['raw']['properties']
        p['windGust']['values'][0]['value'] = None
        self.assertIsNone(wind_evidence(s, NOW)['nws']['samples'][0]['gust_kt'])
        p['windSpeed']['values'] = [
            {'validTime': '2026-09-24T14:00:00Z/PT30M', 'value': 10},
            {'validTime': '2026-09-24T15:00:00Z/PT2H', 'value': 10}]
        self.assertIsNone(wind_evidence(s, NOW)['nws'])

    def test_nonfinite_wrong_identity_length_and_fully_missing_field(self):
        for change in [lambda r: r.update(model='wrong'),
                       lambda r: r['hourly']['wind_speed_10m'].append(10),
                       lambda r: r['hourly']['wind_gusts_10m'].__setitem__(16, float('inf')),
                       lambda r: r['hourly_units'].update(wind_gusts_10m='undefined')]:
            s, _ = self.collect(); change(s['event_wind']['ensembles']['gefs']['raw'])
            self.assertIsNone(wind_evidence(s, NOW)['ensembles']['gefs'])
        s, _ = self.collect()
        s['event_wind']['ensembles']['aifs_ens']['raw']['hourly']['wind_gusts_10m'][16] = 10
        self.assertIsNone(wind_evidence(s, NOW)['ensembles']['aifs_ens'])

    def test_future_fetch_and_wrong_request_are_rejected(self):
        for key, value in [('fetched_at', '2026-09-18T00:00:00Z'), ('url', 'https://example.com')]:
            s, _ = self.collect(); s['event_wind']['ensembles']['gefs'][key] = value
            self.assertIsNone(wind_evidence(s, NOW)['ensembles']['gefs'])


if __name__ == '__main__':
    unittest.main()
