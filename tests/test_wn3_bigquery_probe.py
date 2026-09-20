"""Integrity and cost-bound checks for the optional BigQuery experiment."""
import copy
import unittest
from unittest.mock import patch

from scripts.wn3_bigquery_point import DEFAULT_TABLE, compare_zarr, request_body, validate_rows

RUN = '2026-09-19T12:00:00Z'
VALID = ['2026-09-20T12:00:00Z']
ARRAYS = ['temperature_2m_mean', 'temperature_2m_p10', 'temperature_2m_p90']


class ProbeTests(unittest.TestCase):
    def test_parity_accepts_float32_coordinates_but_rejects_adjacent_cell(self):
        row = dict(valid_time=VALID[0], latitude=40.9, longitude=-74.3,
                   temperature_2m_mean=289.37384033203125)
        point = dict(unit='K', value=row['temperature_2m_mean'],
                     grid_point=dict(latitude=40.900001525878906, longitude=-74.29998779296875))
        with patch('kcdw.weathernext3_zarr.GrpcStore'), patch('kcdw.weathernext3_zarr.WeatherNext3Zarr') as source:
            source.return_value.point.return_value = point
            self.assertTrue(compare_zarr([row], ['temperature_2m_mean'], RUN, '/tmp')['all_match'])
            point['grid_point']['longitude'] = -74.2
            with self.assertRaises(ValueError):
                compare_zarr([row], ['temperature_2m_mean'], RUN, '/tmp')

    def test_reject_invalid_requests_before_network(self):
        for table, run, valid, arrays, cap in [
            ('a.b.c`', RUN, VALID, ARRAYS, 1),
            (DEFAULT_TABLE, RUN, [RUN], ARRAYS, 1),
            (DEFAULT_TABLE, RUN, ['2026-09-20T12:30:00Z'], ARRAYS, 1),
            (DEFAULT_TABLE, RUN, VALID, ['injected_column'], 1),
            (DEFAULT_TABLE, RUN, VALID, ARRAYS, 0),
        ]:
            with self.subTest(table=table, valid=valid, arrays=arrays, cap=cap), self.assertRaises(ValueError):
                request_body(table, run, valid, arrays, cap)

    def test_default_dry_run_and_execution_cap(self):
        for execute in [False, True]:
            body = request_body(DEFAULT_TABLE, RUN, VALID, ARRAYS, 123, execute)
            self.assertEqual(body['maximumBytesBilled'], '123')
            self.assertEqual(body['dryRun'], not execute)
            self.assertIn('t.init_time = @init', body['query'])
            self.assertNotIn('SELECT *', body['query'])

    def test_reject_incomplete_corrupt_and_misaligned_rows(self):
        row = dict(init_time=RUN, valid_time=VALID[0], hours=24, latitude=40.9,
                   longitude=-74.3, temperature_2m_mean=290,
                   temperature_2m_p10=288, temperature_2m_p90=292)
        validate_rows([row], RUN, VALID, ARRAYS)
        for key, value in [('init_time', VALID[0]), ('hours', 25), ('latitude', 40.8),
                           ('temperature_2m_mean', None), ('temperature_2m_mean', float('nan')),
                           ('temperature_2m_mean', 1000), ('temperature_2m_p10', 300)]:
            bad = copy.deepcopy(row)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_rows([bad], RUN, VALID, ARRAYS)
        for rows in [[], [row, row]]:
            with self.assertRaises(ValueError):
                validate_rows(rows, RUN, VALID, ARRAYS)


if __name__ == '__main__':
    unittest.main()
