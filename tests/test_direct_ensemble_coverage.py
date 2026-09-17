"""Collection coverage policy must not invalidate historical sparse packets."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kcdw import direct_ensemble as d
from kcdw.event_ensemble import MODELS


class NativeCoverageTests(unittest.TestCase):
    init = datetime(2026, 9, 17, 0, tzinfo=timezone.utc)
    now = init + timedelta(hours=12, minutes=30)
    end = init + timedelta(hours=72)
    spec = MODELS[0]

    def packet(self, leads, omit=None):
        values = {'msl': 101000., '10u': 3., '10v': 4., 'tp': 6.,
                  'r2': 103., 'r850': 102., 'sp': 100000.}
        return {'model': 'gefs', 'init': d.iso_z(self.init),
                'offered_members': [f'{i:02}' for i in range(31)],
                'points': [dict(member='00', field=field, lead=lead, value=value,
                                collected_at=d.iso_z(self.now), fetched_at=d.iso_z(self.now))
                           for lead in leads for field, value in values.items()
                           if (field, lead) != omit]}

    def collect(self, packet, moisture=False, now=None):
        with patch.object(d, 'collect_native', return_value=packet):
            if moisture:
                return d.collect_rh(object(), self.spec, self.init, self.end, now or self.now)
            return d.collect_chart(object(), self.spec, None, self.init, self.end, now or self.now)

    def test_mission_only_candidate_returns_none(self):
        # Full mission-time membership cannot compensate for missing lead-up.
        packet = self.packet((30, 36, 42))
        packet['points'] = [dict(point, member=member)
                            for point in packet['points'] for member in packet['offered_members']]
        for moisture in (False, True):
            with self.subTest(moisture=moisture):
                self.assertIsNone(self.collect(packet, moisture))

    def test_sparse_event_candidate_returns_none_before_window_reduction(self):
        with patch.object(d, 'collect_native', return_value=self.packet((30, 36, 42))), \
                patch('kcdw.event_ensemble._window_scenarios') as window:
            self.assertIsNone(d.collect_chart(object(), self.spec, object(), self.init, self.end, self.now))
            window.assert_not_called()

    def test_whole_future_native_accepted_with_past_gaps_and_one_member(self):
        for moisture in (False, True):
            with self.subTest(moisture=moisture):
                data = self.collect(self.packet(range(12, 73, 6)), moisture)
                self.assertIsNotNone(data)
                field = 'relative_humidity_850hPa' if moisture else 'pressure_msl'
                self.assertEqual(data['hourly'][field]['sample_counts'][:12], [0]*12)
                self.assertEqual(data['hourly'][field]['sample_counts'][13:], [1]*59)
                self.assertEqual(data['members'], 31)
                d.validate_normalized(data, self.spec, self.now)
                if moisture:
                    self.assertEqual(data['hourly'][field]['p50'][13], 102.)
                else:
                    self.assertFalse(any(data['hourly']['wind_gusts_10m']['sample_counts']))
                    self.assertFalse(any(data['hourly']['cloud_cover_low']['sample_counts']))

    def test_each_required_field_rejects_internal_native_gap(self):
        for field, moisture in [('msl', False), ('10u', False), ('tp', False),
                                ('r2', True), ('r850', True), ('sp', True)]:
            with self.subTest(field=field):
                self.assertIsNone(self.collect(self.packet(range(12, 73, 6), (field, 48)), moisture))

    def test_original_now_is_ceiled_and_exact_hour_is_included(self):
        packet = self.packet(range(12, 73, 6), ('tp', 12))
        # Rain at 12Z is absent: the interval ending at 12Z was not fetched.
        self.assertIsNotNone(self.collect(packet))
        self.assertIsNone(self.collect(packet, now=self.init + timedelta(hours=12)))

    def test_missing_final_future_hour_is_rejected(self):
        for moisture in (False, True):
            with self.subTest(moisture=moisture):
                self.assertIsNone(self.collect(self.packet(range(12, 67, 6)), moisture))

    def test_sparse_historical_packet_remains_valid_offline(self):
        packet = self.packet((30, 36, 42))
        axis = [d.iso_z(self.init + timedelta(hours=i)) for i in range(72)]
        data = d.base_data(self.spec, packet, axis, self.now)
        d.seal(data)
        self.assertIs(d.validate_normalized(data, self.spec, self.now), data)
