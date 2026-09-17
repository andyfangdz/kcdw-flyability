"""Downstream contract tests: native proofs belong to source validators."""
import copy
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from test_ensemble_trends import snapshot, NOW, EVENT
import test_ensemble_trends as legacy
from kcdw.ensemble_trends import _point, _event, build_trends, validate_trends, BOUND
from kcdw.event_narrative_evidence import _current


def direct(data, provider='NOAA'):
    data['metadata'] = {'ok': True, 'provenance': 'direct-native',
        'model_init_is_response_bound': True, 'initialization_time': '2026-09-12T12:00:00Z',
        'source_provider': provider, 'sampling': 'Nearest native grid; hourly interpolation.'}
    return data


class DirectProvenanceTests(unittest.TestCase):
    def test_ensemble_binding_without_fabricated_availability(self):
        d = snapshot(); direct(d['models']['gefs']['data'])
        _, sample, rain = _event(EVENT.as_dict())
        with patch('kcdw.ensemble_trends.validate_snapshot') as validator:
            p = _point(d, 'gefs', NOW, sample, rain)
            validator.assert_called_once()
        self.assertEqual(p['run_binding'], BOUND)
        self.assertEqual(p['source_provenance']['source_provider'], 'NOAA')
        self.assertNotIn('availability_time', p['source_provenance'])

    def test_direct_metadata_cannot_bypass_source_validation(self):
        d = snapshot(); direct(d['models']['gefs']['data'])
        _, sample, rain = _event(EVENT.as_dict())
        with patch('kcdw.ensemble_trends.validate_snapshot', side_effect=ValueError('bad native proof')):
            with self.assertRaises(ValueError):
                _point(d, 'gefs', NOW, sample, rain)

    def test_direct_gfs_needs_no_openmeteo_datasets(self):
        d = snapshot(); d['gfs'] = legacy.TrendTests().gfs(); direct(d['gfs']['data'])
        _, sample, rain = _event(EVENT.as_dict())
        with patch('kcdw.ensemble_trends.validate_gfs', return_value={'available': True}):
            p = _current(d, 'gfs', NOW, sample, rain)
        self.assertEqual(p['run_binding'], BOUND)
        self.assertIn('sampling', p['source_provenance'])

    def test_native_trends_versioned_and_legacy_stays_v1(self):
        d = snapshot()
        with tempfile.TemporaryDirectory() as root:
            old = build_trends(d, Path(root), NOW)
            self.assertEqual(old['version'], 1)
            direct(d['models']['gefs']['data'])
            with patch('kcdw.ensemble_trends.validate_snapshot'):
                new = build_trends(d, Path(root), NOW)
            self.assertEqual(new['version'], 2)
            self.assertTrue(validate_trends(new, EVENT.as_dict()))
            self.assertEqual(new['models']['gefs']['provenance'], BOUND)
            downgraded = copy.deepcopy(new); downgraded['version'] = 1
            self.assertFalse(validate_trends(downgraded, EVENT.as_dict()))

    def test_change_packet_accepts_bound_moisture_only_in_v2(self):
        from kcdw.event_change_evidence import _packet
        meta = direct({})['metadata']; meta.pop('ok')
        packet = {'fetched_at': '2026-09-12T20:00:00Z', 'grid': {'latitude': 41., 'longitude': -74.5},
            'run_binding': BOUND, 'run_time': meta['initialization_time'], 'source_provenance': meta,
            'samples': {p: {'surface': 50, '925hPa': 70} for p in ('morning','noon','afternoon')}}
        _packet(packet, 'ifs', 'moisture', NOW, 9, version=2)
        with self.assertRaises(ValueError):
            _packet(packet, 'ifs', 'moisture', NOW, 9)

    def test_renderer_keeps_native_binding(self):
        from kcdw.trend_renderer import _models
        d = snapshot(); direct(d['models']['gefs']['data'])
        with tempfile.TemporaryDirectory() as root, patch('kcdw.ensemble_trends.validate_snapshot'):
            trends = build_trends(d, Path(root), NOW)
        models = _models(trends['models'], NOW, NOW)
        self.assertIsNotNone(models['gefs']['points'][-1]['run'])

    def test_wind_native_packet_version(self):
        from kcdw.event_wind_trends import build_wind_trends, validate_wind_trends
        d = snapshot(); direct(d['models']['gefs']['data'])
        with tempfile.TemporaryDirectory() as root, patch('kcdw.ensemble_trends.validate_snapshot'):
            result = build_wind_trends(d, Path(root), NOW)
            self.assertEqual(result['version'], 2)
            self.assertEqual(result['models']['gefs']['run_binding'], BOUND)
            self.assertEqual(validate_wind_trends(result, d, NOW), result)

    def test_mixed_snapshot_preserves_old_grid_and_cloud_rh(self):
        import json
        from test_event_change_evidence import fixture, NOW as now, OLD
        from kcdw.common import iso_z
        from kcdw.event_change_evidence import build_event_changes, validate_event_changes
        from kcdw.event_narrative_evidence import build_event_evidence
        old, current = fixture(OLD), fixture(now)
        for source in current['models'].values():
            direct(source['data'])
            source['data']['metadata']['initialization_time'] = iso_z(now.replace(hour=6))
            source['data']['grid_point']['latitude'] = 40.75
        saved = copy.deepcopy(old)
        with tempfile.TemporaryDirectory() as root, patch('kcdw.ensemble_trends.validate_snapshot'):
            path = Path(root)/'old'; path.mkdir(); (path/'snapshot.json').write_text(json.dumps(old))
            changes = build_event_changes(current, Path(root), now)
            self.assertIsNotNone(changes)
            self.assertEqual(changes['version'], 2)
            pair = changes['guidance']['gefs']
            self.assertNotEqual(pair['previous']['grid'], pair['current']['grid'])
            self.assertNotIn('source_provenance', pair['previous'])
            self.assertIn('low_cloud', pair['current'])
            self.assertTrue(changes['moisture'])
            self.assertEqual(validate_event_changes(changes, current, now), changes)
            current['event_changes'] = changes
            evidence = build_event_evidence(current, now)
            sources = {s['id']: s for s in evidence['sources']}
            self.assertEqual(sources['snapshot_changes']['status'], 'available')
            self.assertEqual(sources['low_level_rh']['status'], 'available')
            self.assertLessEqual(len(json.dumps(evidence).encode()), 60000)
        self.assertEqual(old, saved)

    def test_all_native_pairs_fit_without_dropping_cloud_or_rh(self):
        import json
        from test_event_change_evidence import fixture, NOW as now, OLD
        from kcdw.common import iso_z
        from kcdw.event_change_evidence import build_event_changes
        from kcdw.event_narrative_evidence import build_event_evidence
        def full(at):
            d = fixture(at); d['gfs'] = legacy.TrendTests().gfs()
            sources = [d['gfs'], *d['models'].values(),
                       *d['event_moisture']['models'].values(), *d['event_moisture_ensemble']['models'].values()]
            for source in sources:
                direct(source['data'])
                source['data']['fetched_at'] = iso_z(at)
                from datetime import timedelta
                source['data']['metadata']['initialization_time'] = iso_z(at-timedelta(hours=6))
            return d
        # Isolate downstream sizing from the native-proof validators tested by collectors.
        def validated(snap, at):
            return {'models': {k: {'available': True, 'data': v['data']}
                for family in ('event_moisture','event_moisture_ensemble')
                for k,v in snap.get(family,{}).get('models',{}).items()}}
        old, current = full(OLD), full(now)
        with tempfile.TemporaryDirectory() as root, patch('kcdw.ensemble_trends.validate_snapshot'), patch('kcdw.ensemble_trends.validate_gfs', return_value={'available': True}), patch('kcdw.event_moisture_view.validated', side_effect=validated):
            path = Path(root)/'old'; path.mkdir(); (path/'snapshot.json').write_text(json.dumps(old))
            changes = build_event_changes(current, Path(root), now)
            self.assertIsNotNone(changes)
            self.assertEqual(len(changes['guidance']), 5)
            self.assertEqual(len(changes['moisture']), 6)
            current['event_changes'] = changes
            evidence = build_event_evidence(current, now)
            self.assertLessEqual(len(json.dumps(evidence).encode()), 60000)
            self.assertEqual(next(s for s in evidence['sources'] if s['id']=='snapshot_changes')['status'], 'available')

if __name__ == '__main__':
    unittest.main()
