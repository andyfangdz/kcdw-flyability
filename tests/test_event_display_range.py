"""Expanded collection axes must not alter the event-window statistics."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kcdw import event_ensemble as ee
from kcdw.common import iso_z
from tests.test_events import EVENT, NOW, FakeClient


class DisplayRangeTests(unittest.TestCase):
    def collect(self, client=None):
        with patch.object(ee, 'collect_weather_next3', side_effect=RuntimeError('offline')):
            return ee.collect_event(client or FakeClient(rain=(0, 2, 3)), EVENT, NOW)

    def test_eastern_midnight_legacy_and_far_future_bound(self):
        start, end = ee.event_range(EVENT, datetime(2026, 9, 13, 2, tzinfo=timezone.utc))
        self.assertEqual(iso_z(start), '2026-09-12T04:00:00Z')
        self.assertEqual(iso_z(end), '2026-09-26T04:00:00Z')
        self.assertEqual(iso_z(ee.event_range(EVENT)[0]), '2026-09-22T04:00:00Z')
        self.assertEqual(iso_z(ee.event_range(EVENT, NOW-timedelta(days=100))[0]), '2026-09-08T04:00:00Z')

    def test_all_members_full_axis_window_unchanged_and_missing_stays_null(self):
        snapshot = self.collect()
        expected = [iso_z(datetime(2026, 9, 12, 4, tzinfo=timezone.utc)+timedelta(hours=i)) for i in range(336)]
        for spec in ee.MODELS:
            data = snapshot['models'][spec.key]['data']
            self.assertEqual(data['hourly']['time'], expected)
            legacy = ee.collect_model(FakeClient(rain=(0, 2, 3)), spec, EVENT, NOW)
            self.assertEqual(data['window'], legacy['window'])
        cloud = snapshot['models']['gefs']['data']['hourly']['cloud_cover_low']
        self.assertEqual(cloud['p50'], [None]*336)
        self.assertEqual(cloud['sample_counts'], [0]*336)

    def test_old_and_expanded_archives_validate_without_wall_clock(self):
        snapshot = self.collect()
        ee.validate_snapshot(snapshot)
        broken = deepcopy(snapshot)
        broken['collected_at'] = iso_z(NOW+timedelta(days=1))
        with self.assertRaisesRegex(ValueError, 'range mismatch'):
            ee.validate_snapshot(broken)
        broken = deepcopy(snapshot)
        broken['models']['gefs']['data']['hourly']['time'][0] = '2026-09-12T05:00:00Z'
        with self.assertRaisesRegex(ValueError, 'identity/time mismatch'):
            ee.validate_snapshot(broken)
        start, end = ee.event_range(EVENT)
        snapshot['range'] = {'start': iso_z(start), 'end': iso_z(end)}
        for spec in ee.MODELS:
            snapshot['models'][spec.key]['data'] = ee.collect_model(FakeClient(), spec, EVENT, NOW)
        # An archive remains valid even if it was collected after its display.
        snapshot['collected_at'] = '2027-01-01T00:00:00Z'
        ee.validate_snapshot(snapshot)

    def test_comparator_requests_full_axis_and_gfs_receives_same_range(self):
        class ComparatorClient(FakeClient):
            def get(self, url):
                if 'models=google_weathernext2_ensemble_mean' not in url:
                    return super().get(url)
                self.urls.append(url)
                query = parse_qs(urlparse(url).query)
                start = datetime.fromisoformat(query['start_hour'][0]).replace(tzinfo=timezone.utc)
                end = datetime.fromisoformat(query['end_hour'][0]).replace(tzinfo=timezone.utc)
                n = int((end-start).total_seconds()/3600)+1
                fields = query['hourly'][0].split(',')
                hourly = {'time': [int((start+timedelta(hours=i)).timestamp()) for i in range(n)]}
                units = {'time': 'unixtime'}
                for field in fields:
                    hourly[field] = [1 if field.endswith('_spread') else (1020 if field == 'pressure_msl' else 0)]*n
                    units[field] = ee.UNITS[field.removesuffix('_spread')]
                return {'latitude': ee.LAT, 'longitude': ee.LON, 'timezone': 'GMT', 'utc_offset_seconds': 0,
                        'hourly': hourly, 'hourly_units': units}
        with patch('kcdw.ensemble_guidance._validate_metadata', return_value={'data_end_time': '2026-09-27T00:00:00Z'}), \
             patch.object(ee, 'collect_gfs', return_value={'ok': False}) as gfs:
            snapshot = self.collect(ComparatorClient())
        self.assertTrue(snapshot['weathernext2']['ok'], snapshot['weathernext2'])
        self.assertEqual(snapshot['weathernext2']['data']['hourly']['time'], snapshot['models']['gefs']['data']['hourly']['time'])
        self.assertEqual(gfs.call_args.args[1:3], ee.event_range(EVENT, NOW))
        ee.validate_snapshot(snapshot)

    def test_truncated_response_rejected_not_padded(self):
        class Truncated(FakeClient):
            def get(self, url):
                raw = super().get(url)
                hourly = raw.get('hourly')
                if isinstance(hourly, dict):
                    for key, values in hourly.items():
                        hourly[key] = values[-96:]
                return raw
        with self.assertRaisesRegex(RuntimeError, 'no ensemble model'):
            self.collect(Truncated())
