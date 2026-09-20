"""Focused offline contracts for the official WN3 BigQuery adapter."""
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
    return {
        "explicit_last_good": False,
        "status": {
            "state": "degraded" if fallback else "ready", "available": True,
            "freshness": "fresh", "error_code": None,
            "authentication": "bigquery_authenticated_query_succeeded",
            "transport": "BigQuery REST", "station": "KCDW",
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
            "query": {"table": "866962084172.WeatherNext_3.weathernext_3_0_0_0p1deg",
                      "project": "aviation-486817", "job_id": "fixture_job", "location": "US",
                      "bytes_processed": 100, "bytes_billed": 10485760, "query_cache_hit": False,
                      "local_cache_hit": False, "retrieved_at": iso(requested)},
        },
    }


class FakeStore:
    def candidates(self, now):
        return [RUN]

    def fetch(self, run):
        f=fixture()['forecast']
        rows=[]
        for i, valid in enumerate(f['valid_time_utc']):
            row=dict(init_time=iso(RUN), valid_time=valid, hours=i+1, latitude=40.9, longitude=-74.3)
            for name, spec in FIELD_SPECS.items():
                for stat in ('mean','p10','p90'):
                    row[f'{spec.array}_{stat}']=(f['fields'][name][stat][i]-spec.offset)/spec.factor
            rows.append(row)
        return dict(rows=rows, provenance=f['query'])


class WeatherNext3Tests(unittest.TestCase):
    def test_full_relevant_field_contract_and_sparse_axis(self):
        data = fixture()
        self.assertIsNone(validate_weather_next3(data, NOW))
        self.assertEqual(set(data["forecast"]["fields"]), set(FIELD_SPECS))
        data["forecast"]["fields"].pop("low_cloud_cover")
        with self.assertRaises(ValueError):
            validate_weather_next3(data, NOW)

    def test_collection_normalizes_units_and_preserves_query_provenance(self):
        valid = RUN + timedelta(hours=3)
        data = collect_weather_next3(NOW, [valid], store=FakeStore())
        fields = data["forecast"]["fields"]
        self.assertAlmostEqual(fields["temperature_2m"]["mean"][0], 20)
        self.assertAlmostEqual(fields["precipitation_1h"]["mean"][0], 1)
        self.assertAlmostEqual(fields["low_cloud_cover"]["mean"][0], 30)
        self.assertEqual(data["forecast"]["query"]["job_id"], "fixture_job")
        self.assertNotIn("transfer", data["forecast"])

    def test_summary_discloses_sparse_coverage_and_new_fields(self):
        summary = summarize_weather_next3(fixture(), NOW)
        day = summary["days"][1]
        self.assertEqual(day["fields"]["precipitation_1h"]["mean_sum_mm"], 12)
        self.assertIn("low_cloud_cover", summary["available_fields"])
        self.assertNotIn("cloud", summary["missing_fields"])

class WeeklyNativeTests(unittest.TestCase):
    def test_week_covers_every_hour_and_dst(self):
        from kcdw.weathernext3 import relevant_valid_times, EASTERN
        for now in (NOW, datetime(2026, 10, 31, 14, tzinfo=timezone.utc)):
            init = now.replace(hour=12)
            end = datetime.combine(now.astimezone(EASTERN).date() + timedelta(days=7),
                                   datetime.min.time(), EASTERN).astimezone(timezone.utc)
            selected = set(relevant_valid_times(now, init))
            t = init + timedelta(hours=1)
            while t <= end:
                self.assertIn(t, selected)
                t += timedelta(hours=1)

    def test_direction_units_and_missing_gust(self):
        from kcdw.weathernext3 import mean_hourly, wind_direction
        self.assertEqual([wind_direction(u, v) for u, v in ((0,-1),(-1,0),(0,1),(1,0))], [0,90,180,270])
        self.assertIsNone(wind_direction(0, 0))
        h = mean_hourly(fixture(), NOW)['hourly']
        self.assertAlmostEqual(h['wind_speed_10m'][0], 5*3600/1852)
        self.assertEqual(h['pressure_msl'][0], 1010)
        self.assertTrue(all(v is None for v in h['wind_gusts_10m']))
        self.assertAlmostEqual(h['wind_direction_10m'][0], 213.6900675)


if __name__ == "__main__":
    unittest.main()
