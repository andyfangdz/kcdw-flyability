import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kcdw import hrrr_ceiling as hrrr
from kcdw import hrrr_ceiling_worker as worker

UTC = timezone.utc
NOW = datetime(2026, 9, 23, 14, 30, tzinfo=UTC)
SNAPSHOT = {'collected_at': '2026-09-23T14:28:00Z',
            'event': {'slug': 'checkride', 'title': 'Checkride', 'date': '2026-09-24', 'window': '08-17',
                      'nav_label': 'Checkride', 'description': 'Event'},
            'event_timing': {'flight_start': '14:00', 'flight_end': '16:00'}}
PROOF = {'start': 0, 'end': 99, 'total': 1000, 'bytes': 100, 'sha256': 'a' * 64}


def frame(init, lead, low_nearby=0):
    counts = {'25km': {'cells': 219, 'below_1000': 0, 'below_3000': low_nearby, 'below_7000': low_nearby + 5},
              '50nm': {'cells': 2996, 'below_1000': 40, 'below_3000': 100, 'below_7000': 600},
              '100nm': {'cells': 11991, 'below_1000': 280, 'below_3000': 350, 'below_7000': 2300}}
    return {'lead': lead, 'valid_at': hrrr.iso_z(init + timedelta(hours=lead)), 'source_url': hrrr.url_for(init, lead),
            'fields': {'ceiling': dict(PROOF), 'low_cloud': dict(PROOF)},
            'kcdw': {'ceiling_agl_ft': 9560.0, 'low_cloud_pct': 100.0},
            'within_10km': {'cells': 35, 'ceiling_cells': 35, 'min_ft': 8075.0, 'max_ft': 11291.0},
            'counts': counts, 'nearest_below_3000': {'distance_nm': 6.1, 'direction': 'SSE', 'ceiling_ft': 1504}}


def decoded(init, leads):
    return {'grid': {'rows': 133, 'cols': 134}, 'terrain': {'lead': leads[0], 'proof': dict(PROOF)},
            'frames': [frame(init, lead, 29 if lead == leads[2] else 0) for lead in leads]}


IMAGE = {'file': 'map.png', 'sha256': 'b' * 64, 'width': 1650, 'height': 1188}


class HrrrCeilingTests(unittest.TestCase):
    def collect(self, var, probe=lambda init, lead: True):
        init = datetime(2026, 9, 23, 12, tzinfo=UTC)
        with patch.object(hrrr, '_decode', side_effect=lambda folder, i, leads: decoded(i, leads)) as decode, \
             patch.object(hrrr, '_render_map', return_value=IMAGE):
            result = hrrr.collect(SNAPSHOT, var, NOW, probe=probe)
        return result, decode, init

    def test_frames_run_from_two_hours_before_the_flight_to_its_end(self):
        start, end, times = hrrr.frame_times(SNAPSHOT)
        self.assertEqual((hrrr.iso_z(start), hrrr.iso_z(end)), ('2026-09-24T18:00:00Z', '2026-09-24T20:00:00Z'))
        self.assertEqual([t.hour for t in times], [16, 17, 18, 19, 20])

    def test_candidates_respect_hourly_and_synoptic_reach(self):
        start, _, times = hrrr.frame_times(SNAPSHOT)
        found = hrrr.candidates(times, start, NOW)
        # 30+ hours out only the 48-hour synoptic runs reach 20Z tomorrow.
        self.assertEqual([hrrr.iso_z(i) for i, _ in found[:2]], ['2026-09-23T12:00:00Z', '2026-09-23T06:00:00Z'])
        self.assertEqual(found[0][1], [28, 29, 30, 31, 32])
        close = datetime(2026, 9, 24, 17, 10, tzinfo=UTC)
        init, leads = hrrr.candidates(times, start, close)[0]
        # Past context hours drop; the run must not start after the flight does.
        self.assertEqual((hrrr.iso_z(init), leads), ('2026-09-24T16:00:00Z', [0, 1, 2, 3, 4]))
        self.assertEqual(hrrr.candidates(times, start, datetime(2026, 9, 24, 20, 30, tzinfo=UTC))[0][0].hour, 18)

    def test_discover_skips_runs_that_are_not_yet_published(self):
        start, _, times = hrrr.frame_times(SNAPSHOT)
        seen = []
        def probe(init, lead):
            seen.append(init.hour)
            return init.hour == 6
        self.assertEqual(hrrr.discover(times, start, NOW, probe)[0].hour, 6)
        self.assertEqual(seen, [12, 6])
        self.assertIsNone(hrrr.discover(times, start, NOW, lambda i, l: False))

    def test_collect_builds_validates_and_reuses_the_run_cache(self):
        with tempfile.TemporaryDirectory() as var:
            first, decode, init = self.collect(var)
            self.assertEqual(first['init'], '2026-09-23T12:00:00Z')
            self.assertEqual([f['lead'] for f in first['frames']], [28, 29, 30, 31, 32])
            self.assertEqual(first['image']['url'], '/events/checkride/maps/' + 'b' * 64 + '.png')
            self.assertFalse(first['published'])
            second, decode, _ = self.collect(var)
            decode.assert_not_called()
            self.assertEqual(second, first)
            self.assertIsNone(self.collect(var, probe=lambda i, l: False)[0])

    def test_validation_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as var:
            good = self.collect(var)[0]
        self.assertEqual(hrrr.validate(good, SNAPSHOT, NOW), good)
        mutations = [
            lambda e: e.update(model='gfs'),
            lambda e: e.update(init='2026-09-23T13:00:00Z'),
            lambda e: e['frames'].pop(),
            lambda e: e['frames'][0].update(source_url='https://example.com/x'),
            lambda e: e['frames'][0]['counts']['25km'].update(below_1000=9),
            lambda e: e['frames'][0]['counts']['50nm'].update(cells=10),
            lambda e: e['frames'][0]['within_10km'].update(ceiling_cells=99),
            lambda e: e['frames'][0]['kcdw'].update(low_cloud_pct=float('nan')),
            lambda e: e['frames'][0].update(nearest_below_3000={'distance_nm': 5, 'direction': 'SSE', 'ceiling_ft': 3500}),
            lambda e: e['frames'][0]['fields']['ceiling'].update(sha256='zz'),
            lambda e: e['image'].update(url='https://example.com/map.png'),
            lambda e: e.update(extra=1),
        ]
        for mutate in mutations:
            bad = copy.deepcopy(good)
            mutate(bad)
            self.assertIsNone(hrrr.validate(bad, SNAPSHOT, NOW))
        self.assertIsNone(hrrr.validate(good, dict(SNAPSHOT, collected_at='2026-09-23T13:00:00Z'), NOW))
        self.assertIsNone(hrrr.validate(good, SNAPSHOT, NOW + timedelta(hours=25)))

    def test_render_and_evidence(self):
        with tempfile.TemporaryDirectory() as var:
            packet = self.collect(var)[0]
        snapshot = dict(SNAPSHOT, hrrr_ceiling=packet)
        html = hrrr.render(snapshot, NOW)
        self.assertIn('id="hrrr-ceiling"', html)
        self.assertIn('14:00 · flight', html)
        self.assertIn('6.1 nm SSE · 1,504 ft', html)
        self.assertIn('rendered locally', html)
        self.assertIn('Current HRRR ceiling unavailable', hrrr.render(dict(SNAPSHOT, hrrr_ceiling=None), NOW))
        self.assertEqual(hrrr.render(SNAPSHOT, NOW), '')
        evidence = hrrr.ceiling_evidence(snapshot, NOW)
        self.assertEqual(len(evidence['rows']), 5)
        self.assertEqual(len(evidence['rows'][0]), len(evidence['columns']))
        row = dict(zip(evidence['columns'], evidence['rows'][2]))
        self.assertEqual((row['kcdw_ceiling_ft'], row['cells_below_3000_25km']), (9560.0, 29))
        self.assertIsNone(hrrr.ceiling_evidence(SNAPSHOT, NOW))


class WorkerTests(unittest.TestCase):
    INIT = datetime(2026, 9, 30, 12, tzinfo=UTC)
    INDEX = ('62:41000000:d=2026093012:HGT:1000 mb:30 hour fcst:\n'
             '63:41888210:d=2026093012:HGT:surface:30 hour fcst:\n'
             '64:44041905:d=2026093012:TMP:surface:30 hour fcst:\n'
             '116:86341817:d=2026093012:LCDC:low cloud layer:30 hour fcst:\n'
             '117:87331149:d=2026093012:MCDC:middle cloud layer:30 hour fcst:\n'
             '120:89159125:d=2026093012:HGT:cloud ceiling:30 hour fcst:\n'
             '121:91219016:d=2026093012:HGT:cloud base:30 hour fcst:\n')

    def test_index_selects_exact_messages(self):
        index = '1:0:d=2026093012:REFC:entire atmosphere:30 hour fcst:\n' + self.INDEX
        ranges = worker.indexed_ranges(index, self.INIT, 30, ('ceiling', 'low_cloud', 'terrain'))
        self.assertEqual(ranges, {'terrain': (41888210, 44041904), 'low_cloud': (86341817, 87331148),
                                  'ceiling': (89159125, 91219015)})
        with self.assertRaises(ValueError):
            worker.indexed_ranges(index, self.INIT, 29, ('ceiling',))
        with self.assertRaises(ValueError):
            worker.indexed_ranges(index.replace('d=2026093012', 'd=2026093011'), self.INIT, 30, ('ceiling',))

    def test_reach_and_urls(self):
        self.assertEqual((worker.max_lead(self.INIT), worker.max_lead(self.INIT + timedelta(hours=1))), (48, 18))
        self.assertEqual(worker.url_for(self.INIT, 30), hrrr.url_for(self.INIT, 30))

    def test_statistics_on_a_synthetic_grid(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is only in the native-weather environment')
        lat, lon = np.meshgrid(np.arange(39.2, 42.6, 0.03), np.arange(-76.5, -72.0, 0.04), indexing='ij')
        km, bearing = worker.distance_bearing(lat, lon)
        agl = np.full(lat.shape, 9000.0)
        patch_cells = (km > 9) & (km < 14) & (bearing > 150) & (bearing < 165)
        agl[patch_cells] = 1500.0
        agl[km > 150] = np.nan
        stats = worker.statistics(agl, np.zeros(lat.shape), km, bearing)
        self.assertEqual(stats['kcdw']['ceiling_agl_ft'], 9000.0)
        self.assertEqual(stats['nearest_below_3000']['direction'], 'SSE')
        self.assertLess(stats['nearest_below_3000']['distance_nm'], 8)
        self.assertEqual(stats['counts']['25km']['below_3000'], int(patch_cells.sum()))
        self.assertEqual(stats['counts']['25km']['below_1000'], 0)


if __name__ == '__main__':
    unittest.main()
