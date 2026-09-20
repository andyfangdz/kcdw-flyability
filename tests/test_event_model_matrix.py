import copy
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from kcdw import event_model_matrix as matrix
from kcdw.event_model_matrix_view import ceiling_card, headline, render_matrix, summary

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
SNAPSHOT = {
    'collected_at': '2026-09-18T21:30:00Z',
    'airport': {'icao': 'KCDW', 'latitude': 40.8752, 'longitude': -74.2814, 'timezone': 'America/New_York'},
    'event': {'slug': 'commercial-checkride', 'title': 'Commercial checkride', 'date': '2026-09-24', 'window': '08-17',
              'nav_label': 'Checkride · Sep 24', 'description': 'Practical test.'},
    'event_timing': {'slug': 'commercial-checkride', 'date': '2026-09-24', 'timezone': 'America/New_York',
                     'appointment_start': '08:00', 'appointment_status': 'confirmed', 'flight_start': '10:00',
                     'flight_status': 'expected', 'flight_end': '12:00', 'flight_duration_minutes': 120},
}


def response(run, *, low=100, spread=4.0, gust=20.0, rain=0.0, hours=240, missing=()):
    start = run.replace(hour=0)
    times = [(start + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M') for h in range(hours)]
    covered = [start + timedelta(hours=h) >= run for h in range(hours)]
    column = lambda value: [value if ok else None for ok in covered]
    hourly = {'time': times, 'cloud_cover_low': column(low), 'cloud_cover_mid': column(10), 'temperature_2m': column(15.0),
              'dew_point_2m': column(15.0 - spread), 'wind_speed_10m': column(8.0), 'wind_direction_10m': column(40),
              'wind_gusts_10m': column(gust), 'precipitation': column(rain), 'relative_humidity_925hPa': column(90),
              'pressure_msl': column(1025.0)}
    for key in missing:
        hourly[key] = [None] * hours
    return {'latitude': 40.875, 'longitude': -74.25, 'utc_offset_seconds': 0, 'hourly': hourly}


class FakeClient:
    direct_native = False

    def __init__(self, runs):
        self.runs, self.urls = runs, []

    def get(self, url):
        self.urls.append(url)
        query = parse_qs(urlparse(url).query)
        key = (query['models'][0], query['run'][0])
        if key not in self.runs:
            raise RuntimeError('HTTP Error 400: Bad Request')
        return self.runs[key]


def client():
    runs = {}
    for _, _, model_id in matrix.MODELS:
        runs[(model_id, '2026-09-18T12:00')] = response(datetime(2026, 9, 18, 12, tzinfo=UTC), low=30, spread=9.0)
        runs[(model_id, '2026-09-18T00:00')] = response(datetime(2026, 9, 18, 0, tzinfo=UTC), low=100, spread=3.0)
    runs[('gfs_global', '2026-09-18T12:00')] = response(datetime(2026, 9, 18, 12, tzinfo=UTC), low=100, spread=4.0, gust=29.0)
    runs[('icon_global', '2026-09-18T18:00')] = response(datetime(2026, 9, 18, 18, tzinfo=UTC), hours=120)
    del runs[('ukmo_global_deterministic_10km', '2026-09-18T12:00')], runs[('ukmo_global_deterministic_10km', '2026-09-18T00:00')]
    return FakeClient(runs)


class ModelMatrixTests(unittest.TestCase):
    def packet(self):
        return matrix.collect_matrix(client(), SNAPSHOT, NOW)

    def test_flight_window_prefers_expected_flight_and_falls_back_to_event_window(self):
        start, end, kind = matrix.flight_window(SNAPSHOT)
        self.assertEqual((matrix.iso_z(start), matrix.iso_z(end), kind), ('2026-09-24T14:00:00Z', '2026-09-24T16:00:00Z', 'expected flight'))
        bare = {k: v for k, v in SNAPSHOT.items() if k != 'event_timing'}
        start, end, kind = matrix.flight_window(bare)
        self.assertEqual((matrix.iso_z(start), matrix.iso_z(end), kind), ('2026-09-24T12:00:00Z', '2026-09-24T21:00:00Z', 'event window'))

    def test_candidate_runs_are_bounded_newest_first_synoptic_cycles(self):
        runs = matrix.candidate_runs(NOW)
        self.assertEqual(len(runs), matrix.MAX_CANDIDATES)
        self.assertEqual(runs[0], datetime(2026, 9, 18, 18, tzinfo=UTC))
        self.assertTrue(all(a - b == timedelta(hours=6) for a, b in zip(runs, runs[1:])))

    def test_summarize_computes_window_statistics_and_rejects_uncovered_runs(self):
        start, end, _ = matrix.flight_window(SNAPSHOT)
        values = matrix.summarize(response(datetime(2026, 9, 18, 12, tzinfo=UTC), low=100, spread=4.0, rain=0.6), start, end)['values']
        self.assertEqual((values['low_cloud_pct'], values['base_ft'], values['wind_dir_deg'], values['gust_kt']), (100, 1600, 40, 20.0))
        self.assertEqual(values['rain_mm'], 1.2)
        self.assertEqual((values['read'], values['tone']), ('rain', 'poor'))
        self.assertIsNone(matrix.summarize(response(datetime(2026, 9, 18, 18, tzinfo=UTC), hours=120), start, end))
        self.assertIsNone(matrix.summarize(response(datetime(2026, 9, 18, 12, tzinfo=UTC), missing=('cloud_cover_low',)), start, end))
        shifted = response(datetime(2026, 9, 18, 12, tzinfo=UTC))
        shifted['utc_offset_seconds'] = -14400
        self.assertIsNone(matrix.summarize(shifted, start, end))
        optional = matrix.summarize(response(datetime(2026, 9, 18, 12, tzinfo=UTC), missing=('wind_gusts_10m',)), start, end)
        self.assertIsNone(optional['values']['gust_kt'])

    def test_screens_use_fixed_thresholds(self):
        base = {'rain_mm': 0.0, 'gust_kt': 10.0}
        for low, feet, read, tone in ((100, 1800, 'low_overcast', 'poor'), (90, 3200, 'high_overcast', 'marginal'),
                                      (60, 1500, 'broken', 'marginal'), (49, 900, 'scattered', 'good')):
            values = dict(base, low_cloud_pct=low, base_ft=feet)
            values['read'] = matrix.classify(values)
            self.assertEqual((values['read'], matrix.tone(values)), (read, tone))
        gusty = {'rain_mm': 0.0, 'gust_kt': 25.0, 'low_cloud_pct': 10, 'base_ft': 4000, 'read': 'scattered'}
        self.assertEqual(matrix.tone(gusty), 'marginal')

    def test_collect_binds_each_row_to_its_run_and_previous_covering_run(self):
        fake = client()
        packet = matrix.collect_matrix(fake, SNAPSHOT, NOW)
        matrix.validate_matrix(packet, SNAPSHOT)
        rows = {row['key']: row for row in packet['models']}
        self.assertEqual(rows['icon']['current']['run'], '2026-09-18T12:00:00Z')
        self.assertEqual(rows['icon']['previous']['run'], '2026-09-18T00:00:00Z')
        self.assertEqual(rows['icon']['change'], 'better')
        self.assertEqual((rows['gfs']['current']['values']['read'], rows['gfs']['change']), ('low_overcast', 'steady'))
        self.assertFalse(rows['ukmo']['ok'])
        self.assertTrue(all(url.startswith(matrix.API + '?') and 'run=2026-09-1' in url for url in fake.urls))
        self.assertIn('run=2026-09-18T12%3A00', rows['ifs']['current']['source_url'])

    def test_collect_returns_none_without_any_model_after_event_or_beyond_horizon(self):
        self.assertIsNone(matrix.collect_matrix(FakeClient({}), SNAPSHOT, NOW))
        self.assertIsNone(matrix.collect_matrix(client(), SNAPSHOT, datetime(2026, 9, 24, 17, tzinfo=UTC)))
        self.assertIsNone(matrix.collect_matrix(client(), SNAPSHOT, datetime(2026, 9, 1, tzinfo=UTC)))

    def test_cache_reuses_immutable_runs_survives_outage_and_resets_on_new_window(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model-matrix-cache.json'
            first = client()
            packet = matrix.collect_matrix(first, SNAPSHOT, NOW, path)
            cached = json.loads(path.read_text())['entries']
            self.assertEqual(sorted(k for k, v in cached['ifs'].items() if v), ['2026-09-18T00:00:00Z', '2026-09-18T12:00:00Z'])
            # ICON 18Z answered but stops short of the window: only 3.5 h old, so it is not yet negatively cached.
            self.assertNotIn('2026-09-18T18:00:00Z', cached['icon'])
            second = client()
            again = matrix.collect_matrix(second, SNAPSHOT, NOW, path)
            self.assertEqual(again['models'], packet['models'])
            refetched = [u for u in second.urls if 'ukmo' not in u and ('run=2026-09-18T12%3A00' in u or 'run=2026-09-18T00%3A00' in u)]
            self.assertEqual(refetched, [])
            outage = matrix.collect_matrix(FakeClient({}), SNAPSHOT, NOW, path)
            self.assertEqual(outage['models'], packet['models'])
            matrix.validate_matrix(outage, SNAPSHOT)
            later = NOW + timedelta(hours=9)
            settled = client()
            matrix.collect_matrix(settled, dict(SNAPSHOT, collected_at=matrix.iso_z(later)), later, path)
            self.assertIsNone(json.loads(path.read_text())['entries']['icon']['2026-09-18T18:00:00Z'])
            moved = dict(SNAPSHOT, event_timing=dict(SNAPSHOT['event_timing'], flight_start='11:00', flight_end='13:00'))
            self.assertIsNone(matrix.collect_matrix(FakeClient({}), moved, NOW, path))
            path.write_text('not json')
            self.assertIsNotNone(matrix.collect_matrix(client(), SNAPSHOT, NOW, path))

    def test_rate_limited_rows_pause_once_then_retry_and_report_upstream_errors(self):
        class Limited(FakeClient):
            def __init__(self, runs, recover):
                super().__init__(runs)
                self.recover, self.limited = recover, True

            def get(self, url):
                if self.limited and 'ecmwf_ifs' in url:
                    self.urls.append(url)
                    raise RuntimeError('HTTP Error 429: Too Many Requests')
                return super().get(url)

        pauses = []
        limited = Limited(client().runs, recover=True)

        def pause(seconds):
            pauses.append(seconds)
            limited.limited = False

        packet = matrix.collect_matrix(limited, SNAPSHOT, NOW, sleep=pause)
        self.assertEqual(pauses, [matrix.RETRY_PAUSE_SECONDS])
        rows = {row['key']: row for row in packet['models']}
        self.assertTrue(rows['ifs']['ok'])
        self.assertTrue(all('retry' not in row for row in packet['models']))
        matrix.validate_matrix(packet, SNAPSHOT)
        stuck = matrix.collect_matrix(Limited(client().runs, recover=False), SNAPSHOT, NOW, sleep=lambda seconds: None)
        rows = {row['key']: row for row in stuck['models']}
        self.assertEqual((rows['ifs']['ok'], rows['ifs']['error']), (False, 'upstream rate limit'))
        # A cycle that is simply not published yet (HTTP 400) never triggers the pause.
        calm = []
        matrix.collect_matrix(client(), SNAPSHOT, NOW, sleep=calm.append)
        self.assertEqual(calm, [])

    def test_validation_rejects_tampered_packets(self):
        packet = self.packet()
        for mutate in (lambda p: p.update(version=2),
                       lambda p: p.update(snapshot_collected_at='2026-09-18T20:30:00Z'),
                       lambda p: p['thresholds'].update(base_ft=1000),
                       lambda p: p['models'][1]['current']['values'].update(read='rain'),
                       lambda p: p['models'][1]['current']['values'].update(low_cloud_pct=140),
                       lambda p: p['models'][1].update(change='worse'),
                       lambda p: p['models'][1]['current'].update(run='2026-09-19T12:00:00Z'),
                       lambda p: p['models'].reverse()):
            bad = copy.deepcopy(packet)
            mutate(bad)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                matrix.validate_matrix(bad, SNAPSHOT)

    def test_view_is_dense_escaped_and_fails_closed(self):
        snapshot = dict(SNAPSHOT, model_matrix=self.packet())
        markup = render_matrix(snapshot, NOW)
        self.assertIn('id="model-matrix"', markup)
        self.assertIn('expected flight 10:00–12:00 EDT', markup)
        self.assertIn('4 models split: 3 favorable, 1 unfavorable', markup)
        self.assertIn('Unavailable: no recent run covers the window.', markup)
        self.assertIn('▲ Better', markup)
        self.assertIn('Estimated base is not a ceiling', markup)
        self.assertIn('not probabilities, votes or a go/no-go decision', markup)
        rows = [row for row in snapshot['model_matrix']['models'] if row['ok']]
        self.assertIn('Cloud is the split: 30–100% cover', summary(rows))
        self.assertEqual(headline(rows[:1]), 'One model available: favorable')
        tampered = copy.deepcopy(snapshot)
        tampered['model_matrix']['models'][0]['label'] = '<script>alert(1)</script>'
        self.assertEqual(render_matrix(tampered, NOW), '')
        self.assertEqual(render_matrix(SNAPSHOT, NOW), '')

    def test_ceiling_card_summarizes_low_overcast_models(self):
        card = ceiling_card(dict(SNAPSHOT, model_matrix=self.packet()))
        self.assertEqual((card['value'], card['tone'], card['low']), ('1 of 4 models low overcast', 'watch', 1))
        self.assertIn('Estimates, not ceilings', card['detail'])
        self.assertIsNone(ceiling_card(SNAPSHOT))

class NativeWN3MatrixTests(unittest.TestCase):
    def snapshot(self):
        from test_weathernext3 import fixture, RUN
        data = fixture()
        shift = timedelta(days=6)
        for key in ('actual_run_utc', 'requested_init_utc', 'attempted_init_utc', 'fetched_at'):
            data['status'][key] = matrix.iso_z(matrix.parse_time(data['status'][key]) + shift)
        for key in ('requested_init_utc', 'response_init_utc'):
            data['forecast'][key] = matrix.iso_z(matrix.parse_time(data['forecast'][key]) + shift)
        data['forecast']['valid_time_utc'] = [matrix.iso_z(matrix.parse_time(t)+shift) for t in data['forecast']['valid_time_utc']]
        data['forecast']['query']['retrieved_at'] = data['status']['fetched_at']
        return dict(SNAPSHOT, weathernext3={'ok': True, 'data': data})

    def test_native_peer_preferred_with_direction_and_unknown_gust(self):
        snapshot = self.snapshot()
        packet = matrix.collect_matrix(client(), snapshot, NOW)
        matrix.validate_matrix(packet, snapshot)
        row = packet['models'][0]
        self.assertEqual(row['key'], 'wn3')
        self.assertTrue(row['ok'])
        self.assertEqual(row['current']['values']['wind_dir_deg'], 214)
        self.assertIsNone(row['current']['values']['gust_kt'])
        markup = render_matrix(dict(snapshot, model_matrix=packet), NOW)
        self.assertIn('WN3 · preferred', markup)
        self.assertIn('Gust unavailable', markup)
        self.assertIn('5 models split', markup)
        bad = copy.deepcopy(packet)
        bad['models'][0]['current']['values']['wind_kt'] = 20
        with self.assertRaises(ValueError):
            matrix.validate_matrix(bad, snapshot)

    def test_stale_and_incomplete_native_sources_do_not_count(self):
        for kind in ('stale', 'incomplete', 'unavailable'):
            snapshot = self.snapshot()
            if kind == 'stale':
                snapshot['weathernext3']['data']['status']['actual_run_utc'] = '2026-09-16T12:00:00Z'
            elif kind == 'incomplete':
                snapshot['weathernext3']['data']['forecast']['fields'].pop('temperature_2m')
            else:
                snapshot['weathernext3']['ok'] = False
            packet = matrix.collect_matrix(client(), snapshot, NOW)
            self.assertFalse(packet['models'][0]['ok'])
            matrix.validate_matrix(packet, snapshot)

    def test_calm_direction_is_unknown_without_dropping_model(self):
        snapshot = self.snapshot()
        fields = snapshot['weathernext3']['data']['forecast']['fields']
        for name in ('u_component_of_wind_10m', 'v_component_of_wind_10m'):
            fields[name]['mean'] = [0] * 360
        packet = matrix.collect_matrix(client(), snapshot, NOW)
        matrix.validate_matrix(packet, snapshot)
        self.assertTrue(packet['models'][0]['ok'])
        self.assertIsNone(packet['models'][0]['current']['values']['wind_dir_deg'])
        self.assertIn('Variable', render_matrix(dict(snapshot, model_matrix=packet), NOW))

    def test_distinct_native_runs_drive_history_not_repeated_refreshes(self):
        import tempfile
        from pathlib import Path
        snapshot = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'matrix.json'
            first = matrix.collect_matrix(client(), snapshot, NOW, path)
            repeated = matrix.collect_matrix(client(), snapshot, NOW, path)
            self.assertIsNone(repeated['models'][0]['previous'])
            data = snapshot['weathernext3']['data']
            for key in ('actual_run_utc', 'attempted_init_utc', 'requested_init_utc', 'fetched_at'):
                data['status'][key] = '2026-09-18T18:00:00Z'
            data['forecast']['query']['retrieved_at'] = data['status']['fetched_at']
            for key in ('response_init_utc', 'requested_init_utc'):
                data['forecast'][key] = '2026-09-18T18:00:00Z'
            data['forecast']['valid_time_utc'] = [matrix.iso_z(matrix.parse_time(t)+timedelta(hours=6)) for t in data['forecast']['valid_time_utc']]
            newer = matrix.collect_matrix(client(), snapshot, NOW, path)
            matrix.validate_matrix(newer, snapshot)
            self.assertEqual(newer['models'][0]['previous'], first['models'][0]['current'])
            self.assertEqual(newer['models'][0]['change'], 'steady')


if __name__ == "__main__":
    unittest.main()
