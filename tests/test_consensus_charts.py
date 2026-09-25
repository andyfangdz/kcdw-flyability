import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kcdw import consensus_charts as cc

NOW = datetime(2026, 9, 25, 20, tzinfo=timezone.utc)
EVENT = {'slug': 'commercial-checkride', 'date': '2026-10-01', 'title': 'Commercial checkride', 'window': '12-19',
         'nav_label': 'Checkride · Oct 1', 'description': 'Test'}
AIRPORT = {'icao': 'KCDW', 'latitude': 40.8752, 'longitude': -74.2814, 'timezone': 'America/New_York'}
TIMING = {'flight_start': '14:00', 'flight_end': '16:00'}
CYCLE = '2026-09-25T12:00:00Z'
VALID = [CYCLE, '2026-10-01T18:00:00Z']


def image(n):
    digest = f'{n:064x}'
    return {'file': f'{n}.png', 'sha256': digest, 'width': 2100, 'height': 1590}


def manifest():
    frames = []
    for i, valid in enumerate(VALID):
        lead = 0 if i == 0 else 150
        point = {'lat': 40.8752, 'lon': -74.2814, 'msl_hpa': 1016.0, 'gradient_hpa_per_100km': .68, 'geostrophic_kt': 12,
                 'models': {'gfs': {'msl_hpa': 1013.8, **({'precip6_mm': 1.2} if lead else {})},
                            'ifs': {'msl_hpa': 1016.8, **({'precip6_mm': 0.0} if lead else {})}}}
        frames.append({
            'valid': valid, 'analysis': lead == 0, 'precipitation': lead > 0, 'point': point,
            'models': {'gfs': {'init': CYCLE, 'lead': lead}, 'ifs': {'init': CYCLE, 'lead': lead}},
            # High ~555 km SSE; low ~185 km E with the two models ~55 km apart; front ~115 km N; far low and front ignored.
            'highs': [{'kind': 'H', 'hPa': 1020.0, 'lat': 36.0, 'lon': -73.0}],
            'lows': [{'kind': 'L', 'hPa': 998.0, 'lat': 40.9, 'lon': -72.1}, {'kind': 'L', 'hPa': 990.0, 'lat': 55.0, 'lon': -120.0}],
            'model_lows': {'gfs': [{'kind': 'L', 'hPa': 997.0, 'lat': 40.9, 'lon': -72.3}],
                           'ifs': [{'kind': 'L', 'hPa': 999.4, 'lat': 41.3, 'lon': -71.9}, {'kind': 'L', 'hPa': 1001, 'lat': 30, 'lon': -90}]},
            'fronts': [{'type': 'cold', 'cross_front_ms': 3.1, 'points': [[41.9, -76.0], [41.9, -74.3], [41.9, -72.0]]},
                       {'type': 'warm', 'cross_front_ms': -2.0, 'points': [[30.0, -90.0], [31.0, -89.0]]}],
            'images': {'conus': image(2 * i + 1), 'northeast': image(2 * i + 2)}})
    return {'version': 1, 'prepared_at': '2026-09-25T19:40:00Z', 'runs': {'gfs': CYCLE, 'ifs': CYCLE},
            'failures': [], 'frames': frames}


def snapshot():
    return {'event': dict(EVENT), 'airport': dict(AIRPORT), 'event_timing': dict(TIMING)}


class ConsensusChartTests(unittest.TestCase):
    def data(self, published=True):
        return cc.envelope(manifest(), snapshot(), NOW, published)

    def test_envelope_summarizes_nearby_features_and_validates(self):
        data = cc.validate(self.data(), EVENT)
        flight = data['frames'][1]
        self.assertEqual(flight['leads'], {'gfs': 150, 'ifs': 150})
        self.assertEqual(flight['images']['northeast']['url'], f"/events/commercial-checkride/maps/{4:064x}.png")
        self.assertEqual(set(flight['images']), {'conus', 'northeast'})
        near = flight['nearby']
        self.assertEqual((near['high']['hPa'], near['high']['direction']), (1020.0, 'SSE'))
        self.assertEqual((near['low']['hPa'], near['low']['direction']), (998.0, 'E'))
        self.assertAlmostEqual(near['low']['distance_km'], 183, delta=5)
        self.assertEqual(near['low_models']['count'], 2)
        self.assertEqual(near['low_models']['hPa'], [997, 999])
        self.assertAlmostEqual(near['low_models']['spread_km'], 55, delta=5)
        self.assertEqual((near['front']['type'], near['front']['direction']), ('cold', 'N'))
        self.assertAlmostEqual(near['front']['distance_km'], 114, delta=5)

    def test_validation_rejects_foreign_or_inconsistent_data(self):
        good = self.data()
        breaks = [
            lambda d: d['event'].update(slug='other'),
            lambda d: d.update(cycle='2026-09-25T06:00:00Z'),
            lambda d: d['frames'][1]['leads'].update(ifs=144),
            lambda d: d['frames'][0].update(analysis=False),
            lambda d: d['frames'][1]['images']['conus'].update(url='https://example.com/x.png'),
            lambda d: d['frames'][1]['images'].pop('conus'),
            lambda d: d['frames'].reverse(),
            lambda d: d.update(models=['gfs', 'nam']),
        ]
        for index, damage in enumerate(breaks):
            with self.subTest(index=index):
                bad = copy.deepcopy(good)
                damage(bad)
                with self.assertRaises(cc.ERRORS):
                    cc.validate(bad, EVENT)
                self.assertEqual(cc.render(bad, snapshot(), NOW), '')

    def test_summary_reports_agreement_and_rain_counts(self):
        data = self.data()
        flight = cc.summary(data['frames'][1])
        self.assertIn('models 1014–1017', flight[0])
        self.assertIn('Rain at KCDW in the 6 h ending then: 1 of 2 models (GFS).', flight)
        self.assertTrue(any('2 models place that low within 35 mi' in item for item in flight))
        analysis = cc.summary(data['frames'][0])
        self.assertFalse(any('Rain at KCDW' in item for item in analysis))
        self.assertTrue(analysis[-1].startswith('Nearest objective front: cold'))

    def test_render_shows_analysis_and_flight_cards_with_escaped_payload(self):
        html = cc.render(self.data(published=False), snapshot(), NOW)
        self.assertIn('id="consensus-analysis"', html)
        self.assertIn('<h3>Consensus surface analysis</h3>', html)
        self.assertIn('Checkride · expected flight', html)
        self.assertIn('F150', html)
        self.assertIn('Images were rendered locally and not published.', html)
        self.assertIn('<option value="1" selected>', html)
        payload = html.split('data-consensus="', 1)[1].split('"', 1)[0]
        self.assertNotIn('<', payload)
        self.assertEqual(cc.render(None, snapshot(), NOW), '')

    def run_collect(self, var, stdout, client=None):
        path = Path(var) / 'events/commercial-checkride/consensus-prog/20260925T12Z/manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest()))
        done = subprocess.CompletedProcess([], 0, stdout(path), 'private-detail')
        uploads = []
        with patch.object(cc.subprocess, 'run', return_value=done) as run, \
                patch('kcdw.coastal_publish.upload_frame', side_effect=lambda c, slug, f: uploads.append((slug, f['sha256']))):
            data = cc.collect(snapshot(), var, NOW, client)
        return data, run, uploads

    def test_collect_runs_common_cycle_uploads_new_images_once_and_saves_current(self):
        with tempfile.TemporaryDirectory() as var:
            stdout = lambda path: 'GFS: ...\n' + json.dumps({'manifest': str(path), 'reused': False}) + '\n'
            data, run, uploads = self.run_collect(var, stdout, client=object())
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--through') + 1], '2026-10-01T20:00:00Z')
            self.assertEqual(command[command.index('--cycle') + 1], 'common')
            self.assertEqual(command[command.index('--point') + 1], '40.8752,-74.2814')
            self.assertTrue(data['published'])
            self.assertEqual(len(uploads), 4)
            self.assertEqual({slug for slug, _ in uploads}, {'commercial-checkride'})
            _, _, again = self.run_collect(var, stdout, client=object())
            self.assertEqual(again, [])
            saved = json.loads((Path(var) / 'events/commercial-checkride/consensus-prog/current.json').read_text())
            self.assertEqual(saved['cycle'], CYCLE)

    def test_failed_refresh_carries_recent_cycle_then_expires(self):
        with tempfile.TemporaryDirectory() as var:
            ok = lambda path: json.dumps({'manifest': str(path)}) + '\n'
            self.run_collect(var, ok)
            data, _, _ = self.run_collect(var, lambda path: '')
            self.assertTrue(data['carried_over'])
            self.assertIn('could not refresh', cc.render(data, snapshot(), NOW))
            with patch.object(cc, 'MAX_CARRY', cc.timedelta(hours=1)), self.assertRaises(ValueError):
                self.run_collect(var, lambda path: '')


if __name__ == '__main__':
    unittest.main()
