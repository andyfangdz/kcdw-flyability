import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kcdw import soundings
from test_event_model_matrix import NOW, SNAPSHOT

sys.path.insert(0, str(Path(soundings.__file__).parent))
import sounding_worker  # noqa: E402  (isolated worker; imported for its pure helpers)

UTC = timezone.utc
AT = datetime(2026, 9, 24, 12, tzinfo=UTC)
LEVELS = [[1012.0, 50.0, 26.0, 15.0, 220.0, 7.0], [1000.0, 150.0, 24.5, 14.5, 225.0, 10.0], [925.0, 820.0, 19.0, 11.0, 240.0, 15.0],
          [850.0, 1530.0, 14.0, 5.0, 262.0, 20.0], [700.0, 3150.0, 5.0, -10.0, 270.0, 27.0], [500.0, 5800.0, -12.0, -30.0, 275.0, 40.0],
          [400.0, 7400.0, -24.0, -40.0, 280.0, 50.0]]
ANALYSIS = {'surface': {'lcl_m': 1300, 'lfc_m': None, 'cape': 0, 'cin': 0}, 'mixed_layer': {'lcl_m': 1400, 'lfc_m': None, 'cape': 0, 'cin': 0},
            'parcel_trace': [[1012.0, 27.5], [850.0, 13.0], [700.0, 4.0]], 'mixing_top': {'pres': 880.0, 'hght_m': 1200},
            'mixed_layer_max_wind': {'wspd': 18.0, 'wdir': 255, 'hght_m': 1200}, 'mixed_layer_mean_wind': {'wspd': 13.0, 'wdir': 240},
            'lapse_rate_0_3km': 6.5, 'precip_water_in': 1.1}


def response(run, levels=LEVELS):
    start = run.replace(hour=0)
    times = [(start + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M') for h in range(240)]
    column = lambda value: [value] * 240
    surface = levels[0]
    hourly = {'time': times, 'surface_pressure': column(surface[0]), 'temperature_2m': column(surface[2]), 'dew_point_2m': column(surface[3]),
              'wind_direction_10m': column(surface[4]), 'wind_speed_10m': column(surface[5])}
    rows = {row[0]: row for row in levels[1:]}
    for p in soundings.LEVELS:
        row = rows.get(float(p), [p, round(44330 * (1 - (p / 1013.25) ** 0.19)), -3.0 - (600 - p) * 0.1, -40.0, 280.0, 60.0])
        for kind, value in zip(soundings.KINDS, (row[2], row[3], row[1], row[5], row[4])):
            hourly[f'{kind}_{p}hPa'] = column(value)
    return {'latitude': 40.875, 'longitude': -74.25, 'elevation': surface[1], 'utc_offset_seconds': 0, 'hourly': hourly}


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
    for _, _, model_id, _ in soundings.MODELS:
        for hour in (18, 12, 6):
            run = datetime(2026, 9, 18, hour, tzinfo=UTC)
            runs[(model_id, run.strftime('%Y-%m-%dT%H:%M'))] = response(run)
    return FakeClient(runs)


def fake_native(source, run, at):
    root = soundings.NOMADS + f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos' if source == 'gfs' else f'{soundings.AIGFS_ROOT}{run:%Y%m%d}/{run:%H}/x'
    return {'run': soundings.iso_z(run), 'source_url': root, 'grid': {'latitude': 41.0, 'longitude': -74.25}, 'levels': copy.deepcopy(LEVELS)}


def fake_analyse(entries):
    out = {}
    for e in entries:
        a = copy.deepcopy(ANALYSIS)
        if e['id'].startswith('ukmo'):
            a['mixed_layer_max_wind'] = {'wspd': 23.0, 'wdir': 229, 'hght_m': 1500}
            a['mixing_top'] = {'pres': 850.0, 'hght_m': 1500}
        out[e['id']] = dict(a, id=e['id'])
    return out


def collected(cache_path=None, analyse=fake_analyse):
    snapshot = copy.deepcopy(SNAPSHOT)
    with patch.object(soundings, '_native', side_effect=fake_native), patch.object(soundings, 'analyse', side_effect=analyse) as called:
        snapshot['model_soundings'] = soundings.collect_soundings(client(), snapshot, NOW, cache_path)
    return snapshot, called


class SoundingTests(unittest.TestCase):
    def test_open_meteo_profile_drops_levels_below_ground(self):
        run = datetime(2026, 9, 18, 18, tzinfo=UTC)
        high = copy.deepcopy(LEVELS)
        high[0][0], high[0][1] = 990.0, 250.0  # the 1000 hPa level is underground here
        entry = soundings._open_meteo(FakeClient({('icon_global', '2026-09-18T18:00'): response(run, high)}), 'icon_global', run, AT, SNAPSHOT['airport'])
        self.assertEqual([r[0] for r in entry['levels'][:3]], [990.0, 925.0, 850.0])
        self.assertLessEqual(len(max(entry['source_url'].split('?'), key=len)), 1500)  # one request fits the URL limit

    def test_levels_must_be_physical_and_ordered(self):
        soundings.check_levels(copy.deepcopy(LEVELS))
        for mutate in (lambda L: L[2].__setitem__(3, 25.0), lambda L: L.reverse(), lambda L: L[3].__setitem__(5, -1.0), lambda L: L[:] + [L[-1]]):
            levels = copy.deepcopy(LEVELS)
            result = mutate(levels)
            with self.assertRaises(ValueError):
                soundings.check_levels(result if isinstance(result, list) else levels)

    def test_collects_analyses_and_validates(self):
        snapshot, called = collected()
        packet = soundings.validate_soundings(snapshot['model_soundings'], snapshot)
        self.assertEqual(packet['sample_at'], '2026-09-24T12:00:00Z')
        self.assertTrue(all(r['ok'] and len(r['runs']) == 2 for r in packet['models']))
        self.assertEqual(called.call_count, 1)  # every profile in one SHARPpy call

    def test_cached_analyses_are_reused_and_keyed_by_method(self):
        with self.subTest('cache'):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'cache.json'
                collected(path)
                _, called = collected(path)
                self.assertEqual(called.call_count, 0)
                self.assertIn(f'analysis-{soundings.ANALYSIS_VERSION}', json.loads(path.read_text())['key'])

    def test_failed_analysis_is_not_published_or_cached(self):
        snapshot, _ = collected(analyse=lambda entries: (_ for _ in ()).throw(RuntimeError('worker failed')))
        self.assertIsNone(snapshot['model_soundings'])

    def test_tampered_packets_fail_closed(self):
        snapshot, _ = collected()
        for mutate in (lambda p: p['models'][0]['runs'][0]['analysis']['mixing_top'].update(hght_m=99999),
                       lambda p: p['models'][0]['runs'][0].update(source_url='https://example.com/gfs'),
                       lambda p: p['models'][2]['runs'][0]['levels'][1].__setitem__(3, 40.0),
                       lambda p: p.update(sample_at='2026-09-24T18:00:00Z')):
            bad = copy.deepcopy(snapshot)
            mutate(bad['model_soundings'])
            with self.assertRaises(ValueError):
                soundings.validate_soundings(bad['model_soundings'], bad)
            self.assertEqual(soundings.render_soundings(bad, NOW), '')
            self.assertIsNone(soundings.sounding_evidence(bad, NOW))

    def test_render_and_evidence(self):
        snapshot, _ = collected()
        html = soundings.render_soundings(snapshot, NOW)
        self.assertIn('id="soundings"', html)
        self.assertIn('How the mixing top is found', html)
        self.assertEqual(html.count('class="clim-panel skewt"'), 6)
        self.assertEqual(len(set(__import__('re').findall(r'id="(skewt-clip-\d+)"', html))), 6)
        self.assertIn('from 18 kt (NCEP GFS) to 23 kt (UKMO Global)', html)
        self.assertIn('mixing top 3,900 ft', html)
        evidence = soundings.sounding_evidence(snapshot, NOW)
        ukmo = next(m for m in evidence['models'] if m['model'] == 'UKMO Global')
        self.assertEqual((ukmo['mixing_height_ft'], ukmo['mixed_layer_max_wind']), (4900, '229° 23 kt'))
        self.assertEqual(ukmo['previous_run']['run'], '2026-09-18T12:00:00Z')

    def test_moist_layers_and_barbs(self):
        levels = copy.deepcopy(LEVELS)
        levels[2][3] = levels[2][2] - 1  # saturated at 925 hPa
        self.assertEqual(soundings._moist_layers(levels), [[2500, 2500]])
        self.assertIn('<circle', soundings._barb(10, 10, 0, 1))
        west = soundings._barb(100, 100, 270, 25)
        self.assertTrue(west.startswith('<path class="barb" d="M100.0,100.0L78.0,100.0'))  # staff points west

    def test_worker_moisture_and_profile_order(self):
        self.assertAlmostEqual(sounding_worker.rh_dewpoint(20.0, 100.0), 20.0, places=1)
        self.assertAlmostEqual(sounding_worker.rh_dewpoint(20.0, 50.0), 9.3, places=1)
        self.assertAlmostEqual(sounding_worker.q_dewpoint(20.0, 0.00726, 1000.0), 9.3, delta=0.3)
        surface = {'pres': 1005.0, 'hght': 60.0, 'tmpc': 25.0, 'dwpc': 14.0, 'u': -3.0, 'v': -3.0}
        levels = [{'pres': float(p), 'hght': h, 'tmpc': t, 'dwpc': t - 8, 'u': -5.0, 'v': -2.0}
                  for p, h, t in ((1000, 40.0, 25.0), (925, 800.0, 19.0), (850, 1500.0, 14.0), (700, 3100.0, 5.0),
                                  (600, 4300.0, -3.0), (500, 5800.0, -12.0), (400, 7400.0, -24.0))]
        rows = sounding_worker.profile(surface, levels)
        self.assertEqual(rows[0][:2], [1005.0, 60.0])
        self.assertEqual(rows[1][0], 925.0)  # 1000 hPa lies below this surface
        self.assertEqual(rows[0][4:], [45.0, round(18 ** .5 * sounding_worker.KNOTS, 2)])


@unittest.skipUnless(soundings.SHARPPY_PYTHON.exists(), 'Install requirements-sharppy.txt into var/sharppy-venv')
class SharppyWorkerTests(unittest.TestCase):
    def test_worker_resamples_and_reports_mixed_layer(self):
        request = {'profiles': [{'id': 'x', **{k: [row[i] for row in LEVELS] for i, k in
                                               enumerate(('pres', 'hght', 'tmpc', 'dwpc', 'wdir', 'wspd'))}}]}
        result = subprocess.run([str(soundings.SHARPPY_PYTHON), str(Path(soundings.__file__).with_name('sharppy_worker.py'))],
                                input=json.dumps(request), text=True, capture_output=True, check=True, timeout=60)
        [a] = json.loads(result.stdout)
        soundings.check_analysis(dict(a), LEVELS)
        self.assertNotIn(a['mixing_top']['pres'], (925.0, 850.0))  # placed between the coarse levels
        self.assertLessEqual(a['mixed_layer_max_wind']['wspd'], 20.0)
        self.assertGreater(a['surface']['lcl_m'], 500)

    def test_profile_without_a_mixing_top_is_dropped_alone(self):
        # Warmer than any level above: pbl_top finds no top, prints a warning and returns the profile top.
        hot = [list(row) for row in LEVELS]
        hot[0][2], hot[0][3] = 55.0, 5.0
        profiles = [{'id': name, **{k: [row[i] for row in levels] for i, k in enumerate(('pres', 'hght', 'tmpc', 'dwpc', 'wdir', 'wspd'))}}
                    for name, levels in (('ok', LEVELS), ('hot', hot))]
        result = subprocess.run([str(soundings.SHARPPY_PYTHON), str(Path(soundings.__file__).with_name('sharppy_worker.py'))],
                                input=json.dumps({'profiles': profiles}), text=True, capture_output=True, check=True, timeout=60)
        self.assertEqual([a['id'] for a in json.loads(result.stdout)], ['ok'])


if __name__ == '__main__':
    unittest.main()
