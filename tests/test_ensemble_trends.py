"""Synthetic trend tests; no network or live archive dependency."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from kcdw.common import iso_z
from kcdw.event_ensemble import MODELS, collect_model, event_range
from kcdw.ensemble_trends import build_trends, validate_trends
from datetime import datetime
from kcdw.common import UTC
from kcdw.events import Event
from kcdw.event_ensemble import VARIABLES, UNITS

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)
EVENT = Event('commercial-checkride', 'Checkride', '2026-09-24', '08-17', 'Checkride', 'Test')

class FakeClient:
    def get(self, url):
        if 'meta.json' in url:
            return {'last_run_initialisation_time': int(NOW.replace(hour=12).timestamp()),
                    'last_run_availability_time': int(NOW.replace(hour=18).timestamp()),
                    'data_end_time': int((NOW+timedelta(days=16)).timestamp()),
                    'temporal_resolution_seconds': 10800}
        model_id = url.split('models=')[1].split('&')[0]
        spec = next(s for s in MODELS if s.model_id == model_id)
        start, _ = event_range(EVENT)
        hourly = {'time': [int((start+timedelta(hours=i)).timestamp()) for i in range(96)]}
        units = {'time': 'unixtime'}
        for variable, value in zip(VARIABLES, (1020, 1, 6, 10, 40, 20)):
            for member in range(spec.members):
                key = variable + (f'_member{member:02d}' if member else '')
                hourly[key] = [value]*96
                units[key] = UNITS[variable]
        return {'latitude': 41.0, 'longitude': -74.5, 'timezone': 'GMT', 'utc_offset_seconds': 0,
                'hourly_units': units, 'hourly': hourly}
from test_weathernext3 import fixture


def snapshot(at=NOW):
    start, end = event_range(EVENT)
    d = {'event': EVENT.as_dict(), 'collected_at': iso_z(at),
         'range': {'start': iso_z(start), 'end': iso_z(end)},
         'airport': {'icao': 'KCDW', 'latitude': 40.8752, 'longitude': -74.2814, 'timezone': 'America/New_York'},
         'models': {s.key: {'ok': True, 'data': collect_model(FakeClient(), s, EVENT, at)} for s in MODELS},
         'weathernext3': {'ok': True, 'data': fixture()}}
    return d


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, d, name='old'):
        p = self.root / name / 'snapshot.json'
        p.parent.mkdir()
        p.write_text(json.dumps(d))
        return p

    def gfs(self):
        from test_gfs_guidance import FakeClient as GFSClient, START, END
        from kcdw.gfs_guidance import collect_gfs
        client = GFSClient()
        for field, hour in [('last_run_initialisation_time',12), ('last_run_availability_time',18), ('last_run_modification_time',17)]:
            client.meta[field] = int(NOW.replace(hour=hour).timestamp())
        source = collect_gfs(client, START, END, NOW)
        self.assertTrue(source['ok'], source['error'])
        return source

    def test_duplicate_archive_timestamps_do_not_poison_history(self):
        old = snapshot(NOW-timedelta(hours=1))
        self.archive(old, 'duplicate-a')
        conflict = copy.deepcopy(old)
        for name in ('p10', 'p25', 'p50', 'p75', 'p90'):
            conflict['models']['gefs']['data']['hourly']['pressure_msl'][name] = [1000]*96
        self.archive(conflict, 'duplicate-b')
        current = snapshot()
        for name in ('p10', 'p25', 'p50', 'p75', 'p90'):
            current['models']['gefs']['data']['hourly']['pressure_msl'][name] = [1030]*96
        out = build_trends(current, self.root, NOW)
        self.assertTrue(validate_trends(out, EVENT.as_dict()))
        for model in out['models'].values():
            stamps = [p['collected_at'] for p in model['points']]
            self.assertEqual(len(stamps), len(set(stamps)))
        self.assertTrue(out['models']['gefs']['current_available'])

    def test_malformed_conventional_container_preserves_wn3_and_gfs(self):
        current = snapshot()
        current['gfs'] = self.gfs()
        current['models'] = None
        out = build_trends(current, self.root, NOW)
        self.assertTrue(validate_trends(out, EVENT.as_dict()))
        self.assertTrue(out['models']['wn3']['current_available'])
        self.assertTrue(out['models']['gfs']['current_available'])
        self.assertFalse(out['models']['gefs']['current_available'])

    def test_same_snapshot_pressure_pairs(self):
        old = snapshot(); old['gfs'] = self.gfs(); self.archive(old)
        current = copy.deepcopy(old); current['collected_at'] = iso_z(NOW+timedelta(hours=1))
        current['gfs']['data']['hourly']['pressure_msl'] = [1001]*96
        out = build_trends(current, self.root, NOW+timedelta(hours=1))
        pair = out['pressure_comparisons']['gefs']
        self.assertEqual(len(pair), 2)
        self.assertEqual([p['gfs'] for p in pair], [1012,1001])
        self.assertEqual(pair[-1]['center'], 1020)
        self.assertTrue(pair[-1]['is_current'])
        self.assertTrue(validate_trends(out, EVENT.as_dict()))
        current['gfs']['ok'] = False
        failed = build_trends(current, self.root, NOW+timedelta(hours=1))
        self.assertEqual(len(failed['pressure_comparisons']['gefs']), 1)
        self.assertFalse(failed['pressure_comparisons']['gefs'][-1]['is_current'])

    def test_gfs_null_rain_and_no_bands(self):
        d = snapshot(); d['gfs'] = self.gfs()
        h = d['gfs']['data']['hourly']
        i = h['time'].index('2026-09-24T13:00:00Z')
        h['precipitation'][i] = None
        out = build_trends(d, self.root, NOW)
        metrics = out['models']['gfs']['points'][0]['metrics']
        self.assertIsNone(metrics['rain'])
        self.assertEqual(metrics['pressure'], {'center':1012, 'low':None, 'high':None})

    def test_wn3_changed_actual_run_distinct_even_equal_metrics(self):
        d = snapshot(NOW-timedelta(hours=1)); self.archive(d)
        current = snapshot(); wn = current['weathernext3']['data']
        from kcdw.common import parse_time
        for obj, keys in [(wn['status'], ('actual_run_utc','requested_init_utc','attempted_init_utc','fetched_at')),
                          (wn['forecast'], ('requested_init_utc','response_init_utc'))]:
            for key in keys: obj[key] = iso_z(parse_time(obj[key])+timedelta(hours=6))
        wn['forecast']['valid_time_utc'] = [iso_z(parse_time(t)+timedelta(hours=6)) for t in wn['forecast']['valid_time_utc']]
        wn['forecast']['query']['retrieved_at'] = wn['status']['fetched_at']
        out = build_trends(current, self.root, NOW)
        self.assertEqual(len(out['models']['wn3']['points']), 2)

    def test_archive_file_and_directory_symlinks_and_size_are_ignored(self):
        d = snapshot(NOW-timedelta(hours=1))
        external = self.root / 'external.json'; external.write_text(json.dumps(d))
        symlink = self.root / 'link'; symlink.mkdir(); (symlink/'snapshot.json').symlink_to(external)
        large = self.root / 'large'; large.mkdir()
        with (large/'snapshot.json').open('wb') as f: f.truncate(8*1024*1024+1)
        (self.root/'directory-link').symlink_to(large, target_is_directory=True)
        current = snapshot(); current['models']['gefs']['ok'] = False
        self.assertEqual(build_trends(current, self.root, NOW)['models']['gefs']['points'], [])

    def test_bounded_reads_and_no_recursive_trends(self):
        from unittest.mock import patch
        from kcdw import ensemble_trends
        d = snapshot(); d['ensemble_trends'] = {'ignored': 'never retained'}
        for n in range(70): self.archive(d, str(n))
        original = ensemble_trends.json.loads
        with patch.object(ensemble_trends.json, 'loads', wraps=original) as loads:
            out = build_trends(d, self.root, NOW)
        self.assertLessEqual(loads.call_count, 64)
        self.assertNotIn('ignored', json.dumps(out))
        self.assertTrue(validate_trends(out, EVENT.as_dict()))
        del out['pressure_comparisons']
        self.assertTrue(validate_trends(out, EVENT.as_dict()))

    def test_pairs_plateau_latest_and_validator_caps(self):
        d = snapshot(); d['gfs'] = self.gfs(); self.archive(d)
        current = copy.deepcopy(d); current['collected_at'] = iso_z(NOW+timedelta(hours=1))
        out = build_trends(current, self.root, NOW+timedelta(hours=1))
        pair = out['pressure_comparisons']['gefs']
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0]['collected_at'], current['collected_at'])
        self.assertTrue(pair[0]['is_current'])
        out['pressure_comparisons']['gefs'] *= 9
        self.assertFalse(validate_trends(out, EVENT.as_dict()))

    def test_contract_and_fixed_instant(self):
        d = snapshot()
        h = d['models']['gefs']['data']['hourly']
        i = h['time'].index('2026-09-24T16:00:00Z')
        for p, value in [(10, 990), (25, 995), (50, 1000), (75, 1005), (90, 1010)]:
            h['pressure_msl']['p'+str(p)][i] = value
        out = build_trends(d, self.root, NOW)
        self.assertEqual(out['sample_time'], '2026-09-24T16:00:00Z')
        self.assertEqual(set(out['models']), {'gefs','ecmwf_ens','aifs_ens','geps','wn3','gfs'})
        self.assertEqual(out['models']['gefs']['points'][-1]['metrics']['pressure']['center'], 1000)
        rain = out['models']['wn3']['points'][-1]['metrics']['rain']
        self.assertEqual(rain, {'center': 9, 'low': None, 'high': None})
        self.assertTrue(validate_trends(out, d['event']))
        json.dumps(out, allow_nan=False)

    def test_dedupe_metadata_only_keeps_actual_latest_collection(self):
        old = snapshot(NOW-timedelta(hours=1)); self.archive(old)
        current = snapshot()
        current['models']['gefs']['data']['metadata']['initialization_time'] = '2026-09-12T18:00:00Z'
        current['models']['gefs']['data']['metadata']['availability_time'] = iso_z(NOW)
        out = build_trends(current, self.root, NOW)
        for key in ('gefs', 'wn3'):
            points = out['models'][key]['points']
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]['collected_at'], iso_z(NOW))
            self.assertTrue(points[0]['is_current'])

    def test_independent_failure_history_not_current(self):
        self.archive(snapshot(NOW-timedelta(hours=1)))
        d = snapshot(); d['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = True
        out = build_trends(d, self.root, NOW)
        self.assertFalse(out['models']['gefs']['current_available'])
        self.assertFalse(out['models']['gefs']['points'][-1]['is_current'])
        self.assertTrue(out['models']['aifs_ens']['current_available'])
        stale = build_trends(snapshot(), self.root, NOW+timedelta(hours=25))
        self.assertFalse(stale['models']['wn3']['current_available'])
        self.assertFalse(stale['models']['gefs']['current_available'])
        self.assertTrue(stale['models']['gefs']['points'])

    def test_mismatches_and_unsafe_archives_excluded(self):
        for n, mutate in enumerate((lambda d: d['event'].update(window='09-17'),
                                   lambda d: d['airport'].update(icao='KTEB'),
                                   lambda d: d['models']['gefs']['data']['hourly_units'].update(pressure_msl='Pa'),
                                   lambda d: d['models']['gefs']['data']['grid_point'].update(latitude=40.9))):
            d = snapshot(NOW-timedelta(hours=1)); mutate(d); self.archive(d, str(n))
        d = snapshot(); d['models']['gefs']['ok'] = False
        out = build_trends(d, self.root, NOW)
        self.assertEqual(out['models']['gefs']['points'], [])

    def test_changed_records_sorted_capped_and_old_rain_fans_allowed(self):
        for n in range(12):
            d = snapshot(NOW-timedelta(minutes=12-n))
            h = d['models']['gefs']['data']['hourly']; del h['precipitation']
            for p in (10,25,50,75,90): h['pressure_msl']['p'+str(p)] = [1000+n]*96
            self.archive(d, str(n))
        out = build_trends(snapshot(), self.root, NOW)
        pts = out['models']['gefs']['points']
        self.assertEqual(len(pts), 8)
        self.assertEqual([p['collected_at'] for p in pts], sorted(p['collected_at'] for p in pts))
        self.assertNotEqual(pts[-2]['metrics'], pts[-1]['metrics'])
        bad = copy.deepcopy(out); bad['models']['gefs']['points'] *= 2
        self.assertFalse(validate_trends(bad, EVENT.as_dict()))
        bad = copy.deepcopy(out); bad['models']['gefs']['points'][0]['metrics']['wind']['center'] = float('nan')
        self.assertFalse(validate_trends(bad, EVENT.as_dict()))
