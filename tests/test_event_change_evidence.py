"""Fixed-window historical snapshot comparisons, never observations."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from kcdw.common import UTC, iso_z
from kcdw.event_change_evidence import build_event_changes, validate_event_changes
from test_ensemble_trends import snapshot

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=12)


def fixture(at=NOW):
    with patch('test_weathernext3.RUN', at-timedelta(hours=6)):
        data = snapshot(at)
    for source in data['models'].values():
        meta = source['data']['metadata']
        meta['initialization_time'] = iso_z(at-timedelta(hours=8))
        meta['availability_time'] = iso_z(at-timedelta(hours=2))
    from test_event_moisture import FakeClient
    from test_event_moisture_ensemble import Client
    from kcdw.event_moisture import collect_moisture
    from kcdw.event_moisture_ensemble import collect_moisture_ensemble
    start = datetime(2026, 9, 24, 4, tzinfo=UTC)
    end = start+timedelta(days=2)
    with patch('test_event_moisture.NOW', at):
        data['event_moisture'] = collect_moisture(FakeClient(), start, end, at)
    with patch('test_event_moisture_ensemble.NOW', at):
        data['event_moisture_ensemble'] = collect_moisture_ensemble(Client(), start, end, at)
    return data


class EventChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.current = fixture()
        self.old = fixture(OLD)

    def save(self, data, name='old'):
        path = self.root/name/'snapshot.json'
        path.parent.mkdir()
        path.write_text(json.dumps(data))
        return path

    def build(self):
        self.save(self.old)
        result = build_event_changes(self.current, self.root, NOW)
        self.assertIsNotNone(result)
        return result

    def test_saved_selection_numbers_roles_and_nonmutation(self):
        self.old['weathernext3']['data']['forecast']['fields']['precipitation_1h']['mean'] = [.5]*360
        before = copy.deepcopy(self.current)
        result = self.build()
        self.assertEqual(result['previous_collected_at'], iso_z(OLD))
        self.assertEqual(result['current_collected_at'], iso_z(NOW))
        self.assertEqual(result['guidance']['wn3']['alias'], 'WN3')
        pair = result['guidance']['wn3']
        self.assertEqual(pair['previous']['rain_window']['mean'], 4.5)
        self.assertEqual(pair['current']['rain_window']['mean'], 9)
        self.assertIsNone(pair['current']['rain_window']['p90'])
        self.assertEqual(result['guidance']['gefs']['previous']['low_cloud']['noon']['p50'], 40)
        self.assertEqual(result['moisture']['gefs']['previous']['samples']['noon']['surface']['p50'], 15)
        self.assertEqual(result['moisture']['ifs']['alias'], 'IFS')
        self.assertEqual(result['moisture']['aifs_single']['alias'], 'AIFS Single')
        self.assertEqual(result['moisture']['ifs']['current']['run_binding'], 'rolling; not response-bound')
        self.assertEqual(validate_event_changes(result, self.current, NOW), result)
        self.assertEqual(self.current, before)
        self.assertLessEqual(len(json.dumps(result).encode()), 16384)

    def test_closest_actual_clock_not_filename_or_forecast_history(self):
        self.save(fixture(OLD-timedelta(hours=1)), '20260915T000000Z.lie')
        self.save(self.old, 'unhelpful-name')
        self.current['forecast_history'] = {'secret': 'DO_NOT_FORWARD'}
        result = build_event_changes(self.current, self.root, NOW)
        self.assertEqual(result['previous_collected_at'], iso_z(OLD))
        self.assertNotIn('DO_NOT_FORWARD', json.dumps(result))

    def test_missing_invalid_current_and_historical_sources_are_isolated(self):
        self.old['models']['gefs']['ok'] = False
        self.current['models']['ecmwf_ens']['data']['hourly']['pressure_msl']['p50'][0] = True
        self.current['event_moisture']['models']['ifs']['ok'] = False
        # The normalized RH envelope uses available, not ok.
        self.current['event_moisture']['models']['ifs']['available'] = False
        result = self.build()
        self.assertNotIn('gefs', result['guidance'])
        self.assertNotIn('ecmwf_ens', result['guidance'])
        self.assertIn('wn3', result['guidance'])
        self.assertIn('gefs', result['moisture'])
        self.assertNotIn('ifs', result['moisture'])
        self.assertIn('aifs_single', result['moisture'])

    def test_nulls_remain_null(self):
        for data in (self.old, self.current):
            h = data['event_moisture']['models']['gfs']['data']['hourly']
            h['relative_humidity_925hPa'] = [None]*len(h['time'])
        result = self.build()
        self.assertIsNone(result['moisture']['gfs']['previous']['samples']['noon']['925hPa'])
        self.assertIsNone(result['guidance']['wn3']['previous']['low_cloud'])

    def test_unknown_fields_and_bad_historical_statistics_rejected(self):
        good = self.build()
        mutations = [
            lambda d: d.update(extra='untrusted'),
            lambda d: d['guidance']['wn3'].update(extra=0),
            lambda d: d['guidance']['wn3']['previous'].update(extra=0),
            lambda d: d['guidance']['gefs']['previous']['low_cloud']['noon'].update(extra=0),
            lambda d: d['guidance']['gefs']['previous']['pressure'].update(p10=1100),
            lambda d: d['guidance']['wn3']['previous']['rain_window'].update(p90=3),
            lambda d: d['moisture']['gefs']['previous']['samples']['noon']['surface'].update(members=True),
            lambda d: d['moisture']['ifs']['previous'].update(run_binding='response-bound'),
            lambda d: d['archive'].update(sha256='not-a-hash'),
        ]
        for value in (True, float('nan'), float('inf'), -1, 101, '50'):
            mutations.append(lambda d, v=value: d['guidance']['gefs']['previous']['low_cloud']['noon'].update(p50=v))
        for mutate in mutations:
            bad = copy.deepcopy(good)
            mutate(bad)
            self.assertIsNone(validate_event_changes(bad, self.current, NOW), str(bad)[:200])
        self.assertIsNotNone(validate_event_changes(good, self.current, NOW))

    def test_identity_window_clocks_and_current_packet_binding(self):
        good = self.build()
        for field, value in [('current_collected_at', iso_z(NOW-timedelta(minutes=1))),
                             ('previous_collected_at', iso_z(NOW-timedelta(hours=5))),
                             ('previous_collected_at', iso_z(NOW-timedelta(hours=19))),
                             ('previous_collected_at', iso_z(NOW+timedelta(hours=1)))]:
            bad = copy.deepcopy(good); bad[field] = value
            self.assertIsNone(validate_event_changes(bad, self.current, NOW))
        for mutate in [lambda d: d['event'].update(slug='other'),
                       lambda d: d['window'].update(end='2026-09-24T20:00:00Z'),
                       lambda d: d['guidance']['gefs']['current']['pressure'].update(p50=1021),
                       lambda d: d['guidance']['wn3']['previous'].update(fetched_at=iso_z(NOW))]:
            bad = copy.deepcopy(good); mutate(bad)
            self.assertIsNone(validate_event_changes(bad, self.current, NOW))
        damaged = copy.deepcopy(self.current)
        damaged['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = False
        self.assertIsNone(validate_event_changes(good, damaged, NOW))
        self.assertIsNone(validate_event_changes(good, self.current, NOW-timedelta(seconds=1)))
        self.assertIsNone(validate_event_changes(good, self.current, NOW+timedelta(hours=24)))
        self.assertIsNone(validate_event_changes(good, self.current, NOW.replace(tzinfo=None)))

    def test_no_baseline_wrong_event_ages_duplicates_and_bad_json(self):
        self.assertIsNone(build_event_changes(self.current, self.root, NOW))
        for i, hours in enumerate((5, 19, -1)):
            bad = copy.deepcopy(self.old); bad['collected_at'] = iso_z(NOW-timedelta(hours=hours))
            self.save(bad, 'age'+str(i))
        wrong = copy.deepcopy(self.old); wrong['event']['window'] = '09-17'
        self.save(wrong, 'wrong')
        self.assertIsNone(build_event_changes(self.current, self.root, NOW))
        self.save(self.old, 'duplicate1'); self.save(self.old, 'duplicate2')
        self.assertIsNone(build_event_changes(self.current, self.root, NOW))

    def test_comparison_survives_more_than_256_retained_runs(self):
        self.save(self.old, '20260915T000000Z.baseline')
        for i in range(256):
            (self.root / f'20260901T000000Z.{i}').mkdir()
        result = build_event_changes(self.current, self.root, NOW)
        self.assertIsNotNone(result)
        self.assertEqual(result['previous_collected_at'], iso_z(OLD))

    def test_scan_read_size_and_symlink_bounds(self):
        from kcdw import event_change_evidence as module
        path = self.save(self.old)
        link = self.root/'link'; link.mkdir(); (link/'snapshot.json').symlink_to(path)
        with patch.object(module, 'MAX_FILE_BYTES', 100):
            self.assertIsNone(build_event_changes(self.current, self.root, NOW))
        with patch.object(module, 'MAX_READ_BYTES', 100):
            self.assertIsNone(build_event_changes(self.current, self.root, NOW))
        with patch.object(module, 'MAX_SCAN_ENTRIES', 1):
            self.assertIsNone(build_event_changes(self.current, self.root, NOW))
        path.unlink()
        self.assertIsNone(build_event_changes(self.current, self.root, NOW))

    def test_historical_clock_not_now_and_pair_coverage_selection(self):
        # The older fresh RH packet is stale at NOW but valid at collection.
        self.save(self.old, 'complete')
        closer = fixture(OLD+timedelta(minutes=1))
        closer['event_moisture'] = None
        self.save(closer, 'partial')
        result = build_event_changes(self.current, self.root, NOW)
        self.assertEqual(result['previous_collected_at'], iso_z(OLD))
        self.assertIn('ifs', result['moisture'])


if __name__ == '__main__':
    unittest.main()
