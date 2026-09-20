import unittest

from kcdw.weathernext3_zarr import (
    nearest_index,
    run_name,
    validate_array_metadata,
)


class WeatherNext3ZarrTests(unittest.TestCase):
    def test_run_and_grid_coordinates(self):
        self.assertEqual(run_name("2026-09-19T12:00:00Z")[0], "20260919_12hr_01_preds")
        for value in ("2026-09-19T13:00:00Z", "2026-09-19T12:01:00Z", "2026-09-19T12:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_name(value)
        values = tuple(index / 10 for index in range(3600))
        self.assertEqual(nearest_index(values, (-74.2814) % 360), 2857)

    def test_array_contract_is_exact(self):
        metadata = {
            "shape": [360, 1801, 3600],
            "data_type": "float32",
            "chunk_grid": {"configuration": {"chunk_shape": [1, 1801, 3600]}},
            "dimension_names": ["lead_time", "lat_0p1", "lon_0p1"],
            "codecs": [
                {"name": "bytes", "configuration": {"endian": "little"}},
                {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
            ],
        }
        self.assertEqual(validate_array_metadata("low_cloud_cover_mean", metadata), (1801, 3600))
        for mutation in (
            lambda item: item.update(shape=[1]),
            lambda item: item.update(data_type="float64"),
            lambda item: item["codecs"].pop(),
        ):
            changed = {
                **metadata,
                "shape": list(metadata["shape"]),
                "codecs": list(metadata["codecs"]),
            }
            mutation(changed)
            with self.assertRaises(ValueError):
                validate_array_metadata("low_cloud_cover_mean", changed)
        with self.assertRaises(ValueError):
            validate_array_metadata("../../secret_mean", metadata)

if __name__ == "__main__":
    unittest.main()
