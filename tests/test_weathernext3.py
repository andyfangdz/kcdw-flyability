"""Focused offline contracts for the official WN3 Zarr adapter."""
import unittest
from datetime import datetime, timedelta, timezone

from kcdw.weathernext3 import (
    FIELD_SPECS,
    SOURCE,
    collect_weather_next3,
    summarize_weather_next3,
    validate_weather_next3,
)

RUN = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
NOW = RUN + timedelta(hours=2)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def fixture(fallback=False):
    requested = RUN + timedelta(hours=6) if fallback else RUN
    values = {
        "temperature_2m": 20, "dewpoint_temperature_2m": 10,
        "precipitation_1h": 1, "imerg_precipitation_1h": 1,
        "experimental_precipitation_1h": 1, "sea_level_pressure": 101000,
        "wind_speed_10m": 5, "u_component_of_wind_10m": 2,
        "v_component_of_wind_10m": 3, "low_cloud_cover": 30,
        "medium_cloud_cover": 20, "high_cloud_cover": 10, "total_cloud_cover": 50,
    }
    fields = {}
    for name, spec in FIELD_SPECS.items():
        value = values[name]
        spread = 1000 if name == "sea_level_pressure" else max(abs(value) * .1, .1)
        fields[name] = {
            "unit": spec.unit, "source_unit": spec.source_unit,
            "source_array": spec.array, "step_type": spec.step_type,
            "mean": [value] * 360, "p10": [value - spread] * 360,
            "p90": [value + spread] * 360,
        }
    objects = len(FIELD_SPECS) * 3 * 360
    return {
        "explicit_last_good": False,
        "status": {
            "state": "degraded" if fallback else "ready", "available": True,
            "freshness": "fresh", "error_code": None,
            "authentication": "gcs_authenticated_read_succeeded",
            "transport": "GCS gRPC whole-object reads", "station": "KCDW",
            "model": "WeatherNext 3", "model_id": 12,
            "latitude": 40.8752, "longitude": -74.2814,
            "actual_run_utc": iso(RUN), "requested_init_utc": iso(requested),
            "attempted_init_utc": iso(RUN), "fetched_at": iso(requested),
            "fallback": fallback,
        },
        "forecast": {
            "source": SOURCE, "model": "WeatherNext 3", "model_id": 12,
            "latitude": 40.9, "longitude": -74.3,
            "grid_point": {"latitude": 40.9, "longitude": -74.3},
            "requested_init_utc": iso(requested), "response_init_utc": iso(RUN),
            "valid_time_utc": [iso(RUN + timedelta(hours=index)) for index in range(1, 361)],
            "fields": fields,
            "transfer": {"objects": objects, "network_objects": objects,
                         "cache_objects": 0, "object_bytes": objects,
                         "network_bytes": objects},
        },
    }


class FakeZarr:
    init = RUN

    def point(self, array, valid):
        base, statistic = array.rsplit("_", 1)
        spec = next(value for value in FIELD_SPECS.values() if value.array == base)
        source_value = {
            "K": 293.15, "m": .001, "Pa": 101000,
            "m s**-1": 5, "(0 - 1)": .25,
        }[spec.source_unit]
        source_value *= {"mean": 1, "p10": .99, "p90": 1.01}[statistic]
        return {
            "unit": spec.source_unit, "value": source_value,
            "grid_point": {"latitude": 40.9, "longitude": -74.3},
            "transfer": {"bytes_read": 10, "object_bytes": 10,
                         "fraction": 1.0, "cache_hit": False},
        }


class WeatherNext3Tests(unittest.TestCase):
    def test_full_relevant_field_contract_and_sparse_axis(self):
        data = fixture()
        self.assertIsNone(validate_weather_next3(data, NOW))
        self.assertEqual(set(data["forecast"]["fields"]), set(FIELD_SPECS))
        data["forecast"]["fields"].pop("low_cloud_cover")
        with self.assertRaises(ValueError):
            validate_weather_next3(data, NOW)

    def test_collection_normalizes_units_and_accounts_whole_objects(self):
        valid = RUN + timedelta(hours=3)
        data = collect_weather_next3(NOW, [valid], source=FakeZarr())
        fields = data["forecast"]["fields"]
        self.assertAlmostEqual(fields["temperature_2m"]["mean"][0], 20)
        self.assertAlmostEqual(fields["precipitation_1h"]["mean"][0], 1)
        self.assertAlmostEqual(fields["low_cloud_cover"]["mean"][0], 25)
        self.assertEqual(data["forecast"]["transfer"]["objects"], len(FIELD_SPECS) * 3)

    def test_summary_discloses_sparse_coverage_and_new_fields(self):
        summary = summarize_weather_next3(fixture(), NOW)
        day = summary["days"][1]
        self.assertEqual(day["fields"]["precipitation_1h"]["mean_sum_mm"], 12)
        self.assertIn("low_cloud_cover", summary["available_fields"])
        self.assertNotIn("cloud", summary["missing_fields"])


if __name__ == "__main__":
    unittest.main()
