"""Forecast-domain tests: synthetic values, real source availability boundaries."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kcdw.forecast_domain import forecast_times

UTC = timezone.utc
NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


def at(hour):
    return NOW + timedelta(hours=hour)


def hourly(hours, **fields):
    return {'time': [at(h).isoformat() for h in hours], **fields}


def fan(values):
    return {**{p: list(values) for p in ('p10', 'p50', 'p90')},
            'members_with_data': 31, 'sample_counts': [31] * len(values)}


class ForecastDomainTests(unittest.TestCase):
    def setUp(self):
        self.original = [at(h) for h in range(9)]

    def run_domain(self, snapshot=None, models=None, window_start=None):
        result = forecast_times(snapshot or {}, models or [], self.original, NOW,
                                window_start or at(6))
        self.assertEqual(result[-1], self.original[-1])
        self.assertTrue(all(t.tzinfo is UTC for t in result))
        self.assertTrue(all(b - a == timedelta(hours=1) for a, b in zip(result, result[1:])))
        return result

    def test_null_padding_trims_to_first_plotted_value(self):
        models = [{'hourly': hourly(range(9), precipitation=fan([None]*3 + [1]*6))}]
        self.assertEqual(self.run_domain(models=models), self.original[3:])

    def test_true_zero_counts_and_only_plotted_statistics_count(self):
        raw = fan([None, None, 0, None])
        raw['p25'] = [99]*4
        raw['metadata'] = [99]*4
        models = [{'hourly': hourly(range(4), precipitation=raw, unplotted=[99]*4)}]
        self.assertEqual(self.run_domain(models=models)[0], at(2))

    def test_earlier_wn3_extends_with_full_hourly_axis(self):
        snapshot = {'weathernext3': {'ok': True, 'data': {'forecast': {
            'valid_time_utc': [at(h).isoformat() for h in (-3, 0, 3)],
            'fields': {'precipitation_1h': {'mean': [0, 1, 2], 'p10': [0]*3, 'p90': [1]*3}}
        }}}}
        with patch('kcdw.event_ensemble.weathernext3_diagnostic', return_value={'available': True}) as gate:
            result = self.run_domain(snapshot)
        gate.assert_called_once_with(snapshot, NOW)
        self.assertEqual(result, [at(h) for h in range(-3, 9)])

    def test_wn2_requires_mean_not_spread_or_metadata_alone(self):
        snapshot = {'weathernext2': {'ok': True, 'data': {'hourly': hourly(
            [-3, -2, -1, 0], precipitation=[None, None, 0, 1],
            precipitation_spread=[1]*4, metadata=[99]*4)}}}
        self.assertEqual(self.run_domain(snapshot)[0], at(-1))
        snapshot['weathernext2']['ok'] = False
        self.assertEqual(self.run_domain(snapshot), self.original)

    def test_gfs_uses_validation_and_only_plotted_fields(self):
        snapshot = {'gfs': {'ok': True, 'data': {'hourly': hourly(
            [-3, -2], precipitation=[None, 0], metadata=[1, 1])}}}
        with patch('kcdw.gfs_guidance.validate_gfs', return_value={'available': True}) as gate:
            self.assertEqual(self.run_domain(snapshot)[0], at(-2))
        gate.assert_called_once_with(snapshot['gfs'], NOW)
        with patch('kcdw.gfs_guidance.validate_gfs', return_value={'available': False}):
            self.assertEqual(self.run_domain(snapshot), self.original)

    def test_moisture_scalars_and_fans_not_heights_or_counts(self):
        for values in ([None, 0], fan([None, 0])):
            status = {'models': {'gefs': {'available': True, 'data': {'hourly': hourly(
                [-4, -3], relative_humidity_925hPa=values, geopotential_height_925hPa=[800]*2)}}}}
            with patch('kcdw.event_moisture_view.validated', return_value=status) as gate:
                self.assertEqual(self.run_domain()[0], at(-3))
            gate.assert_called_once_with({}, NOW)
            status['models']['gefs']['available'] = False
            with patch('kcdw.event_moisture_view.validated', return_value=status):
                self.assertEqual(self.run_domain(), self.original)

    def test_real_gates_reject_incomplete_supplements(self):
        source = {'ok': True, 'data': {'hourly': hourly([-6], precipitation=[1]),
                                     'forecast': {'valid_time_utc': [at(-6).isoformat()],
                                                  'fields': {'precipitation_1h': {'mean': [1]}}}}}
        snapshot = {key: copy.deepcopy(source) for key in
                    ('gfs', 'weathernext3', 'event_moisture', 'event_moisture_ensemble')}
        self.assertEqual(self.run_domain(snapshot), self.original)

    def test_real_wn3_gate_allows_fresh_and_rejects_stale_run(self):
        from dataclasses import asdict
        from kcdw.common import parse_time
        from test_events import EVENT, NOW as fixture_now, synthetic_wn3
        envelope = synthetic_wn3()
        snapshot = {'event': asdict(EVENT), 'collected_at': fixture_now.isoformat(),
                    'weathernext3': {'ok': True, 'data': envelope}}
        original = [fixture_now + timedelta(hours=h) for h in range(300)]
        mission = datetime(2026, 9, 24, 12, tzinfo=UTC)
        fresh = forecast_times(snapshot, [], original, fixture_now, mission)
        self.assertEqual(fresh[0], parse_time(envelope['forecast']['valid_time_utc'][0]))
        self.assertEqual(fresh[-1], original[-1])
        stale = forecast_times(snapshot, [], original, fixture_now + timedelta(hours=72), mission)
        self.assertEqual(stale, original)

    def test_empty_evidence_falls_back_and_does_not_read_archives(self):
        snapshot = {'history': [{'hourly': hourly([-10], precipitation=[1])}]}
        models = [{'hourly': hourly([-5, -4], precipitation=fan([None, None]))}]
        self.assertEqual(self.run_domain(snapshot, models), self.original)

    def test_mission_start_retained_even_when_first_data_is_later(self):
        models = [{'hourly': hourly([7, 8], precipitation=fan([1, 1]))}]
        self.assertEqual(self.run_domain(models=models), self.original[6:])
        self.assertEqual(self.run_domain(window_start=at(-2))[0], at(-2))

    def test_values_after_original_end_do_not_change_fallback_or_end(self):
        models = [{'hourly': hourly([9, 10], precipitation=fan([1, 1]))}]
        self.assertEqual(self.run_domain(models=models), self.original)

    def test_non_numeric_and_nonfinite_are_not_evidence(self):
        models = [{'hourly': hourly(range(5), precipitation=fan([True, '1', float('nan'), float('inf'), 0]))}]
        self.assertEqual(self.run_domain(models=models)[0], at(4))

    def test_no_mutation_and_new_list(self):
        snapshot = {'weathernext2': {'ok': True, 'data': {'hourly': hourly(
            [1, 2], precipitation=[0, 1], precipitation_spread=[0, 0])}}}
        models = [{'hourly': hourly([0, 1], precipitation=fan([None, 0]))}]
        before = copy.deepcopy((snapshot, models, self.original))
        result = self.run_domain(snapshot, models)
        self.assertEqual((snapshot, models, self.original), before)
        self.assertIsNot(result, self.original)

    def test_empty_original_axis(self):
        self.assertEqual(forecast_times({}, [], [], NOW, at(6)), [])


if __name__ == '__main__':
    unittest.main()
