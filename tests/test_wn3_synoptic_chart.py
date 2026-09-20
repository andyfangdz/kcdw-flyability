import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HAS_CHARTS = all(importlib.util.find_spec(name) is not None for name in (
    "numpy", "matplotlib", "cartopy", "scipy", "zstandard",
))
if HAS_CHARTS:
    import numpy as np
    import zstandard
    from scripts import wn3_synoptic_chart as chart


@unittest.skipUnless(HAS_CHARTS, "Install requirements-charts.txt to test chart rendering")
class WN3SynopticChartTests(unittest.TestCase):
    RUN = "2026-09-20T00:00:00Z"
    VALID = "2026-09-23T00:00:00Z"

    def fields(self):
        return {
            "latitude": np.array([40.8, 40.9]),
            "longitude": np.array([-74.3, -74.2, -74.1]),
            "pressure_hpa": np.full((2, 3), 1020.),
            "speed_kt": np.full((2, 3), 6.),
            "u_kt": np.full((2, 3), 3.),
            "v_kt": np.full((2, 3), 4.),
        }

    def test_forecast_hours_and_timezone_normalization(self):
        init, valid, lead = chart.forecast_times("2026-09-19T20:00:00-04:00", self.VALID)
        self.assertEqual(init.isoformat(), "2026-09-20T00:00:00+00:00")
        self.assertEqual((valid - init).total_seconds(), 72 * 3600)
        self.assertEqual(lead, 72)
        for run, valid in (
            (self.RUN, "2026-09-23T00:00:00"),
            ("2026-09-20T13:00:00Z", self.VALID),
            (self.RUN, self.RUN),
            (self.RUN, "2026-10-05T01:00:00Z"),
            (self.RUN, "2026-09-23T00:30:00Z"),
        ):
            with self.subTest(run=run, valid=valid), self.assertRaises(ValueError):
                chart.forecast_times(run, valid)

    def test_regional_grid_preserves_geographic_indices(self):
        lat = np.linspace(-90, 90, 1801).astype("float32")
        lon = (np.arange(3600) / 10).astype("float32")
        fields, iy, ix = chart.regional_grid(lat, lon)
        row = np.argmin(abs(fields["latitude"] - 40.9))
        col = np.argmin(abs(fields["longitude"] + 74.3))
        self.assertEqual(iy[row], 1309)
        self.assertEqual(ix[col], 2857)
        np.testing.assert_allclose(fields["longitude"][[0, -1]], [-140, -47])
        for bad_lat, bad_lon in ((lat[::-1], lon), (lat, lon + .05)):
            with self.assertRaises(ValueError):
                chart.regional_grid(bad_lat, bad_lon)

    def test_decode_crops_reorders_and_converts_units(self):
        plane = np.array([[100000, 101000, 102000], [103000, 104000, 105000]], dtype="<f4")
        encoded = zstandard.ZstdCompressor().compress(plane.tobytes())
        actual = chart.decode_field(encoded, (2, 3), [1, 0], [2, 0], .01, 750, 1150)
        np.testing.assert_allclose(actual, [[1050, 1030], [1020, 1000]])
        encoded = zstandard.ZstdCompressor().compress(np.full((2, 3), 2, dtype="<f4").tobytes())
        actual = chart.decode_field(encoded, (2, 3), [0], [1], 3600 / 1852, 0, 312)
        self.assertAlmostEqual(float(actual[0, 0]), 3.8876889849, places=6)

    def test_decode_rejects_wrong_size_and_invalid_values(self):
        for raw in (b"short", np.full((2, 3), np.nan, dtype="<f4").tobytes(),
                    np.full((2, 3), 100000, dtype="<f4").tobytes()):
            with self.subTest(raw_size=len(raw)), self.assertRaises(ValueError):
                chart.decode_field(zstandard.ZstdCompressor().compress(raw), (2, 3), [0], [0], 1, 0, 312)

    def test_field_validation_catches_geographic_and_physical_errors(self):
        chart.validate_fields(self.fields())
        mutations = (
            ("longitude", np.array([-74.3, -74.1, -74.])),
            ("speed_kt", np.full((2, 3), 4.)),
            ("pressure_hpa", np.full((2, 3), 102000.)),
            ("u_kt", np.zeros((3, 2))),
            ("v_kt", np.full((2, 3), np.nan)),
        )
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                chart.validate_fields({**self.fields(), key: value})

    def test_cached_fields_require_matching_forecast_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(chart, "GrpcStore") as store:
            root = Path(temporary)
            data_path = root / "fields.npz"
            np.savez_compressed(data_path, **self.fields())
            proof = {"init": self.RUN, "valid": self.VALID,
                     "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest()}
            (root / "provenance.json").write_text(json.dumps(proof))
            fields, actual_proof = chart.read_fields(root, self.RUN, self.VALID, root / "cache")
            self.assertEqual(actual_proof, proof)
            np.testing.assert_array_equal(fields["speed_kt"], self.fields()["speed_kt"])
            with self.assertRaisesRegex(ValueError, "another forecast"):
                chart.read_fields(root, self.RUN, "2026-09-23T01:00:00Z", root / "cache")
            data_path.write_bytes(data_path.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                chart.read_fields(root, self.RUN, self.VALID, root / "cache")
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
