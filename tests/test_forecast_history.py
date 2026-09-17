"""Saved-value history tests; archives are local synthetic provider contracts."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from kcdw.common import iso_z, parse_time
from kcdw.event_ensemble import MODELS, collect_model, event_range
from kcdw.forecast_history import build_forecast_history, validate_forecast_history
from test_events import EVENT, NOW, FakeClient


def archived(at=NOW):
    start, end = event_range(EVENT, at)
    return {'event': EVENT.as_dict(), 'collected_at': iso_z(at),
            'range': {'start': iso_z(start), 'end': iso_z(end)},
            'models': {s.key: {'ok': True, 'data': collect_model(FakeClient(), s, EVENT, at, (start, end))} for s in MODELS}}


class ForecastHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = archived()
        self.now = NOW + timedelta(days=2)
        start, end = event_range(EVENT, self.now)
        self.current = {'event': EVENT.as_dict(), 'collected_at': iso_z(self.now),
                        'range': {'start': iso_z(start), 'end': iso_z(end)},
                        'models': {}, 'readiness': {'ready': False}}

    def save(self, data, name='run'):
        path = self.root / name / 'snapshot.json'
        path.parent.mkdir()
        path.write_text(json.dumps(data))
        return path

    def build(self):
        return build_forecast_history(self.current, self.root, self.now)

    def test_early_history_survives_256_newer_archives_and_read_budget(self):
        import os
        from unittest.mock import patch
        early=self.save(self.old,'20260912T200000Z.1')
        os.utime(early,(1,1))
        recent={'event':EVENT.as_dict(),'collected_at':iso_z(NOW+timedelta(days=1)),
                'range':self.current['range'],'models':{}}
        for i in range(256):
            path=self.save(recent,f'20260913T200000Z.{i+2}')
            os.utime(path,(i+2,i+2))
        # Enough budget for the early file and a few tiny newer files, not all.
        with patch('kcdw.forecast_history.MAX_READ_BYTES',early.stat().st_size+1000):
            result=self.build()
        self.assertIsNotNone(result)
        self.assertEqual(result['start'],self.old['range']['start'])
        self.assertTrue(result['provenance']['truncated'])

    def test_valid_times_and_boundaries_must_be_whole_hours(self):
        self.save(self.old)
        good=self.build()
        for offset in (timedelta(minutes=30),timedelta(seconds=1)):
            with self.subTest(offset=offset):
                bad=copy.deepcopy(good)
                bad['start']=iso_z(parse_time(bad['start'])+offset)
                for source in bad['sources'].values():
                    source['hourly']['time'][0]=bad['start']
                self.assertIsNone(validate_forecast_history(bad,EVENT.as_dict(),self.now))
                bad=copy.deepcopy(good)
                bad['cutoff']=iso_z(parse_time(bad['cutoff'])+offset)
                self.assertIsNone(validate_forecast_history(bad,EVENT.as_dict(),self.now))
                bad=copy.deepcopy(good)
                axis=bad['sources']['gefs']['hourly']['time']
                axis[3]=iso_z(parse_time(axis[3])+offset)
                self.assertIsNone(validate_forecast_history(bad,EVENT.as_dict(),self.now))
        good['collected_at']=iso_z(self.now-timedelta(seconds=17))
        self.assertIsNotNone(validate_forecast_history(good,EVENT.as_dict(),self.now))

    def test_editorial_description_change_preserves_saved_history(self):
        self.save(self.old)
        self.current['event']['description'] = 'Confirmed appointment; expected flight later.'
        result = self.build()
        self.assertIsNotNone(result)
        self.assertEqual(result['start'], self.old['range']['start'])
        self.assertEqual(result['event'], self.current['event'])
        self.assertIsNotNone(validate_forecast_history(result, self.current['event'], self.now))

    def test_changed_mission_window_still_rejects_history(self):
        self.save(self.old)
        self.current['event']['window'] = '10-17'
        self.assertIsNone(self.build())

    def test_missing_history(self):
        self.assertIsNone(self.build())
        self.assertIsNone(build_forecast_history(self.current, self.root/'absent', self.now))

    def test_actual_earliest_values_not_archive_collection_and_no_mutation(self):
        self.save(self.old)
        before = copy.deepcopy(self.current)
        result = self.build()
        self.assertEqual(result['start'], self.old['range']['start'])
        self.assertNotEqual(result['start'], self.old['collected_at'])
        self.assertEqual(result['provenance']['state'], 'historical')
        self.assertEqual(result, validate_forecast_history(result, EVENT.as_dict(), self.now))
        self.assertEqual(self.current, before)
        for source in result['sources'].values():
            self.assertTrue(all(t < result['cutoff'] for t in source['hourly']['time']))

    def test_latest_saved_collection_wins_per_field_and_time(self):
        self.save(self.old, 'z-older-name')
        newer = copy.deepcopy(self.old)
        newer['collected_at'] = iso_z(NOW + timedelta(hours=1))
        fan = newer['models']['gefs']['data']['hourly']['wind_speed_10m']
        for p in ('p10', 'p25', 'p50', 'p75', 'p90'):
            fan[p] = [12] * len(fan[p])
        self.save(newer, 'a-newer-name')
        result = self.build()
        self.assertEqual(result['sources']['gefs']['hourly']['wind_speed_10m']['center'][0], 12)
        self.assertIn('pressure_msl', result['sources']['gefs']['hourly'])

    def test_corrupt_provider_and_file_do_not_remove_independent_sources(self):
        self.old['models']['gefs']['data']['hourly']['pressure_msl']['p50'][0] = 'bad'
        self.save(self.old)
        self.save({}, 'corrupt').write_text('{broken')
        result = self.build()
        self.assertNotIn('gefs', result['sources'])
        self.assertIn('ecmwf_ens', result['sources'])

    def test_wrong_event_future_collection_and_no_earlier_points(self):
        wrong = copy.deepcopy(self.old)
        wrong['event']['date'] = '2026-09-25'
        self.save(wrong, 'wrong')
        future = copy.deepcopy(self.old)
        future['collected_at'] = iso_z(self.now + timedelta(hours=1))
        self.save(future, 'future')
        self.assertIsNone(self.build())
        self.save(self.old)
        self.current['range']['start'] = self.old['range']['start']
        self.assertIsNone(self.build())

    def test_persisted_contract_rejections(self):
        self.save(self.old)
        good = self.build()
        mutations = [lambda x: x.update(version=True),
                     lambda x: x.update(cutoff='2026-09-25T00:00:00Z'),
                     lambda x: x.update(start='2026-01-01T00:00:00Z'),
                     lambda x: x['event'].update(slug='wrong'),
                     lambda x: x.update(collected_at=iso_z(self.now+timedelta(hours=1))),
                     lambda x: x['sources']['gefs'].update(statistic='mean'),
                     lambda x: x['sources']['gefs']['hourly']['time'].append(x['cutoff']),
                     lambda x: x['sources']['gefs']['hourly']['pressure_msl']['center'].__setitem__(0, float('nan')),
                     lambda x: x['sources']['gefs']['hourly']['pressure_msl']['low'].pop(),
                     lambda x: x['provenance'].update(extra='x'*400001)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(good)
                mutate(bad)
                self.assertIsNone(validate_forecast_history(bad, EVENT.as_dict(), self.now))
        self.assertIsNone(validate_forecast_history(good, EVENT.as_dict(), self.now+timedelta(hours=13)))

    def test_wn2_sd_negative_boundaries_not_rh(self):
        axis = self.old['models']['gefs']['data']['hourly']['time']
        self.old['weathernext2'] = {'ok': True, 'data': {'model_id': 'google_weathernext2_ensemble_mean',
            'hourly': {'time': axis, 'precipitation': [0.2]*len(axis), 'precipitation_spread': [1]*len(axis)}}}
        self.save(self.old)
        result = self.build()
        self.assertEqual(result['sources']['wn2']['hourly']['precipitation']['low'][0], -0.8)
        self.assertEqual(result['sources']['wn2']['statistic'], 'mean')

    def test_wn3_uses_own_clock_normalizes_units_and_fractional_iso(self):
        from test_weathernext3 import fixture
        self.old['models'] = {}
        self.old['weathernext3'] = {'ok': True, 'data': fixture()}
        self.save(self.old)
        result = self.build()
        source = result['sources']['wn3']
        self.assertEqual(source['statistic'], 'mean')
        self.assertEqual(source['hourly']['pressure_msl']['center'][0], 1010)
        self.assertAlmostEqual(source['hourly']['wind_speed_10m']['center'][0], 5*3600/1852)
        self.assertEqual(result['start'], '2026-09-12T13:00:00Z')
        self.assertEqual(set(result['sources']), {'wn3'})

    def test_rh_real_validation_shared_aliases_sparse_axis_and_no_sd(self):
        from test_event_moisture_ensemble import Client, NOW as RH_NOW, START, END
        from kcdw.event_moisture_ensemble import collect_moisture_ensemble
        moisture = collect_moisture_ensemble(Client(), START, END, RH_NOW)
        self.old['collected_at'] = iso_z(RH_NOW)
        self.old['models'] = {}
        self.old['event_moisture_ensemble'] = moisture
        self.now = RH_NOW + timedelta(days=1)
        self.current['collected_at'] = iso_z(self.now)
        self.current['range']['start'] = iso_z(START+timedelta(days=1))
        self.save(self.old)
        result = self.build()
        source = result['sources']['gefs']
        self.assertEqual(len(source['hourly']['time']), 3)
        fan = source['hourly']['relative_humidity_2m']
        self.assertEqual(fan, {'center': [15]*3, 'low': [3]*3, 'high': [27]*3})
        bad = copy.deepcopy(result)
        bad['sources']['wn2'] = bad['sources'].pop('gefs')
        bad['sources']['wn2'].update(label='WeatherNext 2', statistic='mean')
        self.assertIsNone(validate_forecast_history(bad, EVENT.as_dict(), self.now))

    def test_latest_null_does_not_erase_an_earlier_saved_value(self):
        self.save(self.old, 'old')
        newer = copy.deepcopy(self.old)
        newer['collected_at'] = iso_z(NOW+timedelta(hours=1))
        fan = newer['models']['ecmwf_ens']['data']['hourly']['cloud_cover_low']
        for p in ('p10', 'p25', 'p50', 'p75', 'p90'):
            fan[p][0] = None
        fan['sample_counts'][0] = 0
        self.save(newer, 'new')
        result = self.build()
        self.assertEqual(result['sources']['ecmwf_ens']['hourly']['cloud_cover_low']['center'][0],
                         self.old['models']['ecmwf_ens']['data']['hourly']['cloud_cover_low']['p50'][0])

    def test_deterministic_rh_has_no_band_and_keeps_extra_aliases(self):
        from test_event_moisture import FakeClient as RHClient, NOW as RH_NOW, START, END
        from kcdw.event_moisture import collect_moisture
        self.old['models'] = {}
        self.old['collected_at'] = iso_z(RH_NOW)
        self.old['event_moisture'] = collect_moisture(RHClient(), START, END, RH_NOW)
        self.now = RH_NOW+timedelta(days=1)
        self.current['collected_at'] = iso_z(self.now)
        self.current['range']['start'] = iso_z(event_range(EVENT, self.now)[0])
        self.save(self.old)
        result = self.build()
        self.assertEqual(set(result['sources']), {'gfs', 'ifs', 'aifs_single'})
        for source in result['sources'].values():
            self.assertEqual(source['statistic'], 'deterministic')
            self.assertTrue(all(v is None for v in source['hourly']['relative_humidity_2m']['low']))
        bad = copy.deepcopy(result)
        bad['sources']['gfs']['hourly']['relative_humidity_2m']['high'][0] = 90
        self.assertIsNone(validate_forecast_history(bad, EVENT.as_dict(), self.now))

    def test_small_byte_budget_preserves_earliest_real_point(self):
        from unittest.mock import patch
        self.save(self.old)
        with patch('kcdw.forecast_history.MAX_BYTES', 8000):
            result = self.build()
            self.assertLessEqual(len(json.dumps(result, separators=(',', ':')).encode()), 8000)
            self.assertTrue(result['provenance']['truncated'])
            self.assertEqual(result['start'], self.old['range']['start'])
            self.assertIsNotNone(validate_forecast_history(result, EVENT.as_dict(), self.now))


if __name__ == '__main__':
    unittest.main()
