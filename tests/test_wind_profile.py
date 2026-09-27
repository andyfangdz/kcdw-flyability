import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kcdw import aigfs_worker, wind_profile as profile
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc
AT = datetime(2026, 9, 24, 12, tzinfo=UTC)  # flight 10:00-12:00 EDT; 15Z middle ties 12Z/18Z, earlier wins


def response(run, surface=(8.0, 220), low=(15.0, 240), aloft=(22.0, 265), hours=240):
    start = run.replace(hour=0)
    times = [(start + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M') for h in range(hours)]
    column = lambda value: [value] * hours
    hourly = {'time': times, 'pressure_msl': column(1015.0)}
    for (speed, direction), suffix in zip((surface, low, aloft), ('10m', '925hPa', '850hPa')):
        hourly[f'wind_speed_{suffix}'], hourly[f'wind_direction_{suffix}'] = column(speed), column(direction)
    return {'latitude': 40.875, 'longitude': -74.25, 'utc_offset_seconds': 0, 'hourly': hourly}


class FakeClient:
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
    for _, _, model_id, _ in profile.MODELS:
        for hour, aloft in ((18, 24.0), (12, 18.0), (6, 20.0), (0, 21.0)):
            run = datetime(2026, 9, 18, hour, tzinfo=UTC)
            runs[(model_id, run.strftime('%Y-%m-%dT%H:%M'))] = response(run, aloft=(aloft, 265))
    runs[('gfs_global', '2026-09-18T18:00')] = response(datetime(2026, 9, 18, 18, tzinfo=UTC), surface=(7.0, 262), aloft=(16.0, 277))
    return FakeClient(runs)


def fake_aigfs(run, at):
    values = {'wind10_kt': 7.9, 'from10_deg': 220.1, 'wind925_kt': 14.0, 'from925_deg': 233.2,
              'wind850_kt': 17.2 if run.hour == 18 else 15.0, 'from850_deg': 250.9, 'mslp_hpa': 1016.9}
    return {'run': profile.iso_z(run), 'grid': {'latitude': 41.0, 'longitude': -74.25}, 'values': values,
            'source_url': f'{profile.AIGFS_ROOT}{run:%Y%m%d}/{run:%H}/model/atmos/grib2/aigfs.t{run:%H}z.sfc.f000.grib2'}


def collected(fake=None, now=NOW):
    snapshot = copy.deepcopy(SNAPSHOT)
    with patch.object(profile, '_aigfs', side_effect=fake_aigfs):
        snapshot['wind_profile'] = profile.collect_profile(fake or client(), snapshot, now)
    return snapshot


class WindProfileTests(unittest.TestCase):
    def test_sample_time_is_the_synoptic_hour_nearest_mid_flight(self):
        self.assertEqual(profile.sample_time(SNAPSHOT), AT)
        afternoon = dict(SNAPSHOT, event_timing=dict(SNAPSHOT['event_timing'], flight_start='14:00', flight_end='16:00'))
        self.assertEqual(profile.sample_time(afternoon), datetime(2026, 9, 24, 18, tzinfo=UTC))

    def test_collects_newest_runs_and_validates(self):
        snapshot = collected()
        packet = profile.validate_profile(snapshot['wind_profile'], snapshot)
        self.assertEqual(packet['sample_at'], '2026-09-24T12:00:00Z')
        rows = {r['key']: r for r in packet['models']}
        self.assertTrue(all(r['ok'] for r in rows.values()))
        self.assertEqual([r['run'] for r in rows['ifs']['runs']],
                         ['2026-09-18T18:00:00Z', '2026-09-18T12:00:00Z', '2026-09-18T06:00:00Z'])
        self.assertEqual(rows['gfs']['runs'][0]['values']['from10_deg'], 262)
        self.assertTrue(rows['aigfs']['ai'] and rows['aifs']['ai'] and not rows['gfs']['ai'])

    def test_tampered_or_foreign_packets_fail_closed(self):
        snapshot = collected()
        for mutate in (lambda p: p['models'][0]['runs'][0]['values'].update(wind850_kt=900),
                       lambda p: p['models'][3]['runs'][0].update(source_url='https://example.com/x'),
                       lambda p: p.update(sample_at='2026-09-24T18:00:00Z'),
                       lambda p: p['models'].reverse()):
            bad = copy.deepcopy(snapshot)
            mutate(bad['wind_profile'])
            with self.assertRaises(ValueError):
                profile.validate_profile(bad['wind_profile'], bad)
            self.assertEqual(profile.render_profile(bad, NOW), '')
            self.assertIsNone(profile.profile_evidence(bad, NOW))

    def test_only_settled_misses_are_cached(self):
        fake = client()
        del fake.runs[('icon_global', '2026-09-18T18:00')]
        cache = {}
        with patch.object(profile, '_aigfs', side_effect=fake_aigfs):
            profile.collect_model(fake, profile.MODELS[4], AT, SNAPSHOT['airport'], NOW, float('inf'), cache)
        self.assertNotIn('2026-09-18T18:00:00Z', cache)  # upstream error: retried next refresh
        fake.runs[('icon_global', '2026-09-18T12:00')]['hourly']['wind_speed_850hPa'] = [None] * 240
        cache = {}
        profile.collect_model(fake, profile.MODELS[4], AT, SNAPSHOT['airport'], NOW, float('inf'), cache)
        self.assertNotIn('2026-09-18T12:00:00Z', cache)  # incomplete 9.5 h after the run: may still fill in
        profile.collect_model(fake, profile.MODELS[4], AT, SNAPSHOT['airport'], NOW + timedelta(hours=3), float('inf'), cache)
        self.assertIsNone(cache['2026-09-18T12:00:00Z'])  # settled

    def test_render_and_evidence_explain_aloft_and_ai_disagreement(self):
        snapshot = collected()
        html = profile.render_profile(snapshot, NOW)
        self.assertIn('id="wind-profile"', html)
        self.assertIn('NOAA AIGFS · AI', html)
        self.assertIn('from 16 kt (NCEP GFS) to 24 kt', html)
        self.assertIn('NCEP GFS and its AI counterpart NOAA AIGFS disagree on surface direction by about 42°', html)
        self.assertIn('+6 kt aloft', html)
        evidence = profile.profile_evidence(snapshot, NOW)
        gfs = next(m for m in evidence['models'] if m['model'] == 'NCEP GFS')
        self.assertEqual(gfs['surface'], '262° 7 kt')
        self.assertEqual(gfs['change_since_previous_run']['surface_turn_deg'], 42)

    def test_direction_spread_wraps_through_north(self):
        rows = [{'ok': True, 'key': k, 'label': k, 'runs': [{'values': {'from10_deg': d, 'wind850_kt': 20.0}}]}
                for k, d in (('a', 350.0), ('b', 10.0), ('c', 5.0))]
        self.assertIn('span about 20° across the models, from 350° to 010°', ' '.join(profile._story(rows, AT)))

    def test_no_covering_run_returns_none(self):
        snapshot = copy.deepcopy(SNAPSHOT)
        with patch.object(profile, '_aigfs', return_value=None):
            self.assertIsNone(profile.collect_profile(FakeClient({}), snapshot, NOW))

    def test_worker_reads_only_expected_index_rows(self):
        init = datetime(2026, 9, 26, 18, tzinfo=UTC)
        idx = '\n'.join(f'{i + 1}:{i * 1000}:d=2026092618:{var}:{level}:120 hour fcst:' for i, (var, level) in enumerate(
            [('UGRD', '100 m above ground'), ('UGRD', '10 m above ground'), ('VGRD', '10 m above ground'),
             ('PRMSL', 'mean sea level'), ('TMP', 'surface')]))
        self.assertEqual(aigfs_worker.ranges(idx, init, 120, 'sfc'),
                         {'u10': (1000, 1999), 'v10': (2000, 2999), 'mslp': (3000, 3999)})
        with self.assertRaises(ValueError):
            aigfs_worker.ranges(idx.replace('d=2026092618', 'd=2026092612'), init, 120, 'sfc')
        with self.assertRaises(ValueError):
            aigfs_worker.ranges(idx, init, 114, 'sfc')


if __name__ == '__main__':
    unittest.main()
