from __future__ import annotations

import binascii
import copy
import json
import os
import struct
import subprocess
import tempfile
import unittest
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kcdw import collector
from kcdw.collector import report_dates
from kcdw.geometry import point_in_polygon
from kcdw.intervals import duration, expand_grid_values, expand_valid_time
from kcdw.prompt import build_prompt
from kcdw.renderer import render, score_class, window_state
from kcdw.validation import ValidationError, validate_analysis, validate_snapshot_readiness

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


def rgba_png(width: int, height: int, opaque_pixels: set[tuple[int, int]]) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray(width * 4)
        for x in range(width):
            if (x, y) in opaque_pixels:
                row[x * 4:x * 4 + 4] = b"\xff\x00\x00\xff"
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def snapshot_with_radar(snapshot: dict, frame_count: int = 2) -> dict:
    value = copy.deepcopy(snapshot)
    frames = []
    first_valid = datetime(2026, 9, 3, 11, 20, tzinfo=timezone.utc)
    for attachment in range(1, frame_count + 1):
        valid = first_valid + timedelta(minutes=attachment * 5)
        frames.append({
            "attachment": attachment,
            "valid_at": valid.isoformat().replace("+00:00", "Z"),
            "ingested_at": (valid + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "object_id": attachment,
            "name": f"fixture-{attachment}",
        })
    value["sources"]["radar_mosaic"] = {
        "ok": True,
        "fetched_at": value["collected_at"],
        "data": {
            "stale": False,
            "frame_order": "oldest_to_newest",
            "image_size": [900, 500],
            "frames": frames,
        },
        "error": None,
    }
    return value


def weather_next_payload(count: int = 192) -> dict:
    start = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    times = [int((start + timedelta(hours=index)).timestamp()) for index in range(count)]
    units = {
        "time": "unixtime",
        "temperature_2m": "°C",
        "temperature_2m_spread": "K",
        "precipitation": "inch",
        "precipitation_spread": "inch",
        "cloud_cover_low": "%",
        "cloud_cover_low_spread": "%",
        "cloud_cover_mid": "%",
        "cloud_cover_mid_spread": "%",
        "cloud_cover_high": "%",
        "cloud_cover_high_spread": "%",
        "wind_speed_10m": "kn",
        "wind_speed_10m_spread": "kn",
        "wind_direction_10m": "°",
        "pressure_msl": "hPa",
        "pressure_msl_spread": "hPa",
        "weather_code": "wmo code",
    }
    hourly: dict[str, list] = {"time": times}
    for name in units:
        if name != "time":
            hourly[name] = [0.0] * count
    hourly["pressure_msl"] = [1013.0] * count
    return {
        "latitude": 41.0,
        "longitude": -74.25,
        "timezone": "GMT",
        "utc_offset_seconds": 0,
        "hourly_units": units,
        "hourly": hourly,
    }


def ensemble_metadata(update_interval_seconds: int = 43_200) -> dict:
    return {
        "last_run_initialisation_time": 1_788_415_200,
        "last_run_modification_time": 1_788_433_200,
        "last_run_availability_time": 1_788_433_500,
        "temporal_resolution_seconds": 21_600,
        "update_interval_seconds": update_interval_seconds,
        "data_end_time": 1_789_711_200,
    }


class ProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((FIX / "sample_snapshot.json").read_text())
        cls.analysis = json.loads((FIX / "sample_analysis.json").read_text())

    def test_report_dates_include_current_local_day(self):
        before_midnight_utc = datetime(2026, 9, 4, 3, 59, tzinfo=timezone.utc)
        after_midnight_local = datetime(2026, 9, 4, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(
            report_dates(before_midnight_utc),
            ["2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09"],
        )
        self.assertEqual(report_dates(after_midnight_local)[0], "2026-09-04")
        self.assertEqual(len(report_dates(after_midnight_local)), 7)

    def test_window_state_boundaries_use_eastern_time(self):
        self.assertEqual(window_state("2026-09-03", "08-10", datetime(2026, 9, 3, 11, 59, tzinfo=timezone.utc)), "upcoming")
        self.assertEqual(window_state("2026-09-03", "08-10", datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)), "current")
        self.assertEqual(window_state("2026-09-03", "08-10", datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)), "elapsed")

    def test_interval_parsing_and_expansion(self):
        self.assertEqual(duration("PT3H").total_seconds(), 10800)
        values = expand_grid_values([{"validTime":"2026-09-03T12:00:00+00:00/PT3H","value":7}])
        self.assertEqual([x["value"] for x in values], [7, 7, 7])
        self.assertEqual(len(expand_valid_time("2026-09-03T12:00:00Z/P1DT2H")), 26)
        with self.assertRaises(ValueError): duration("P1M")

    def test_point_in_polygon_and_boundary(self):
        square = [[-75,40],[-74,40],[-74,41],[-75,41],[-75,40]]
        self.assertTrue(point_in_polygon(-74.5, 40.5, square))
        self.assertTrue(point_in_polygon(-75, 40.5, square))
        self.assertFalse(point_in_polygon(-73, 40.5, square))

    def test_collect_radar_loop_writes_fresh_frames_oldest_first(self):
        radar_images = [
            rgba_png(900, 500, {(400, 212)}),
            rgba_png(900, 500, {(450, 212)}),
        ]

        class RadarClient:
            def __init__(self):
                self.image_urls = []

            def get(self, url):
                self.query_url = url
                return {"features": [
                    {"attributes": {"objectid": 22, "name": "CONUS_L2_BREF_QCD_20260903_215500", "idp_validtime": 1788472500000, "idp_ingestdate": 1788472560000}},
                    {"attributes": {"objectid": 11, "name": "CONUS_L2_BREF_QCD_20260903_215000", "idp_validtime": 1788472200000, "idp_ingestdate": 1788472260000}},
                ]}

            def get_bytes(self, url, maximum):
                self.image_urls.append(url)
                self.maximum = maximum
                return radar_images[len(self.image_urls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            client = RadarClient()
            result = collector.collect_radar_loop(
                client,
                datetime(2026, 9, 3, 22, 0, tzinfo=timezone.utc),
                Path(tmp),
            )
            self.assertEqual([frame["valid_at"] for frame in result["frames"]], [
                "2026-09-03T21:50:00Z", "2026-09-03T21:55:00Z",
            ])
            self.assertEqual([frame["attachment"] for frame in result["frames"]], [1, 2])
            self.assertEqual(result["frame_order"], "oldest_to_newest")
            self.assertEqual(result["bbox"], [-82.0, 38.0, -73.0, 43.0])
            self.assertEqual(result["kcdw_pixel"], {"x": 772, "y": 212})
            self.assertFalse(result["stale"])
            self.assertEqual([frame["local_echo_within_25_nm"] for frame in result["frames"]], [False, False])
            self.assertGreater(result["frames"][0]["nearest_displayed_echo_nm"], result["frames"][1]["nearest_displayed_echo_nm"])
            self.assertGreater(result["frames"][1]["nearest_displayed_echo_nm"], 100)
            self.assertEqual(result["frames"][1]["nearest_displayed_echo_direction"], "W")
            self.assertEqual(sorted(path.name for path in Path(tmp).glob("*.png")), ["frame-01.png", "frame-02.png"])
            self.assertTrue(all("lockRasterIds" in url for url in client.image_urls))
            self.assertEqual(client.maximum, 1_000_000)

    def test_collect_radar_loop_rejects_stale_latest_frame(self):
        class StaleRadarClient:
            def get(self, _url):
                return {"features": [{"attributes": {
                    "objectid": 11,
                    "name": "CONUS_L2_BREF_QCD_20260903_210000",
                    "idp_validtime": 1788469200000,
                    "idp_ingestdate": 1788469260000,
                }}]}

            def get_bytes(self, _url, _maximum):
                raise AssertionError("stale radar must be rejected before image download")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "stale"):
                collector.collect_radar_loop(
                    StaleRadarClient(),
                    datetime(2026, 9, 3, 22, 0, tzinfo=timezone.utc),
                    Path(tmp),
                )

    def test_collect_weather_next2_uses_exact_bounded_model_contract(self):
        class WeatherNextClient:
            def get(self, url):
                if url.endswith("/static/meta.json"):
                    self.metadata_url = url
                    return ensemble_metadata()
                self.url = url
                return weather_next_payload()

        client = WeatherNextClient()
        now = datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc)
        result = collector.collect_weather_next(client, now)
        parsed = urllib.parse.urlparse(client.url)
        query = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path, collector.WEATHER_NEXT_ENDPOINT)
        self.assertEqual(query["models"], ["google_weathernext2_ensemble_mean"])
        self.assertEqual(query["forecast_hours"], ["192"])
        self.assertEqual(query["timezone"], ["GMT"])
        self.assertEqual(query["timeformat"], ["unixtime"])
        self.assertEqual(query["wind_speed_unit"], ["kn"])
        self.assertEqual(result["model"], "WeatherNext 2")
        self.assertEqual(result["model_id"], "google_weathernext2_ensemble_mean")
        self.assertEqual(result["ensemble_members"], 64)
        self.assertEqual(result["statistics"], ["mean", "spread"])
        self.assertEqual(result["native_timestep_hours"], 6)
        self.assertEqual(result["returned_timestep_hours"], 1)
        self.assertEqual(result["initialization_time"], "2026-09-03T06:00:00Z")
        self.assertEqual(result["availability_time"], "2026-09-03T11:05:00Z")
        self.assertIn("google_weathernext2_ensemble_mean", client.metadata_url)
        self.assertEqual(result["hourly"]["time"][0], "2026-09-03T12:00:00Z")
        self.assertEqual(len(result["hourly"]["time"]), 192)

    def test_collect_weather_next2_rejects_malformed_times_units_and_values(self):
        cases = []
        wrong_units = weather_next_payload()
        wrong_units["hourly_units"]["wind_speed_10m"] = "mph"
        cases.append(("units", wrong_units))
        wrong_time = weather_next_payload()
        wrong_time["hourly"]["time"][10] += 60
        cases.append(("hourly", wrong_time))
        missing_value = weather_next_payload()
        missing_value["hourly"]["cloud_cover_low"][20] = None
        cases.append(("numeric", missing_value))

        invalid_values = {
            "temperature_2m": 1e100,
            "temperature_2m_spread": 101.0,
            "precipitation": 51.0,
            "precipitation_spread": 51.0,
            "cloud_cover_low": 101.0,
            "cloud_cover_low_spread": 101.0,
            "wind_speed_10m": 301.0,
            "wind_speed_10m_spread": 301.0,
            "wind_direction_10m": 361.0,
            "pressure_msl": -1.0,
            "pressure_msl_spread": 151.0,
            "weather_code": 99.5,
        }
        for variable, value in invalid_values.items():
            payload = weather_next_payload()
            payload["hourly"][variable][0] = value
            cases.append((variable, payload))

        unsupported_weather_code = weather_next_payload()
        unsupported_weather_code["hourly"]["weather_code"][0] = 4
        cases.append(("unsupported weather code", unsupported_weather_code))

        for label, payload in cases:
            with self.subTest(label=label):
                client = mock.Mock()
                client.get.side_effect = [ensemble_metadata(), payload]
                with self.assertRaises(ValueError):
                    collector.collect_weather_next(
                        client,
                        datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc),
                    )

    def test_collect_aifs_ens_uses_exact_bounded_model_contract(self):
        class AifsClient:
            def get(self, url):
                if url.endswith("/static/meta.json"):
                    return ensemble_metadata(21_600)
                self.url = url
                return weather_next_payload()

        client = AifsClient()
        result = collector.collect_aifs_ens(
            client,
            datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc),
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(client.url).query)
        self.assertEqual(query["models"], ["ecmwf_aifs025_ensemble_mean"])
        self.assertEqual(query["forecast_hours"], ["192"])
        self.assertEqual(result["model"], "ECMWF AIFS-ENS")
        self.assertEqual(result["model_id"], "ecmwf_aifs025_ensemble_mean")
        self.assertEqual(result["ensemble_members"], 51)
        self.assertEqual(result["update_frequency_hours"], 6)
        self.assertEqual(result["forecast_horizon_days"], 15)

    def test_validation_rejects_post_8_pm_today_picks(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["collected_at"] = "2026-09-03T23:59:00Z"
        for field in ("best_day", "backup_day"):
            with self.subTest(field=field):
                value = copy.deepcopy(self.analysis)
                value["source_collected_at"] = snapshot["collected_at"]
                value["generated_at"] = "2026-09-04T00:00:00Z"
                value[field] = snapshot["local_date"]
                if field == "backup_day":
                    value["best_day"] = "2026-09-04"
                with self.assertRaisesRegex(ValidationError, "fully elapsed"):
                    validate_analysis(value, snapshot)

    def test_validation_allows_today_pick_before_8_pm(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["collected_at"] = "2026-09-03T23:58:00Z"
        value = copy.deepcopy(self.analysis)
        value["source_collected_at"] = snapshot["collected_at"]
        value["generated_at"] = "2026-09-03T23:59:00Z"
        value["best_day"] = snapshot["local_date"]
        value["backup_day"] = "2026-09-04"
        self.assertIs(validate_analysis(value, snapshot), value)

    def test_validation_rejects_score_duplicate_date_and_timestamp(self):
        bad = copy.deepcopy(self.analysis); bad["days"][0]["windows"][0]["score"] = 91
        with self.assertRaises(ValidationError): validate_analysis(bad, self.snapshot)
        bad = copy.deepcopy(self.analysis); bad["days"][1]["date"] = bad["days"][0]["date"]
        with self.assertRaises(ValidationError): validate_analysis(bad, self.snapshot)
        bad = copy.deepcopy(self.analysis); bad["source_collected_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(ValidationError): validate_analysis(bad, self.snapshot)

    def test_snapshot_readiness_allows_one_failure_but_rejects_sparse_data(self):
        partial = copy.deepcopy(self.snapshot); partial["sources"]["nws_points"]["ok"] = False
        validate_snapshot_readiness(partial)
        sparse = copy.deepcopy(self.snapshot)
        for source in sparse["sources"].values(): source["ok"] = False
        sparse["sources"]["nws_hourly"]["ok"] = True
        with self.assertRaises(ValidationError): validate_snapshot_readiness(sparse)

    def test_long_range_ensembles_cannot_satisfy_short_term_forecast_quorum(self):
        snapshot = copy.deepcopy(self.snapshot)
        for source in snapshot["sources"].values():
            source["ok"] = False
        now = datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc)
        for source_key, collect, update_interval in (
            ("weather_next", collector.collect_weather_next, 43_200),
            ("aifs_ens", collector.collect_aifs_ens, 21_600),
        ):
            client = mock.Mock()
            client.get.side_effect = [ensemble_metadata(update_interval), weather_next_payload()]
            snapshot["sources"][source_key] = {
                "ok": True,
                "fetched_at": snapshot["collected_at"],
                "data": collect(client, now),
                "error": None,
            }
        snapshot["sources"]["nws_alerts"]["ok"] = True
        snapshot["sources"]["nws_alerts"]["data"] = {"features": []}
        with self.assertRaisesRegex(ValidationError, "insufficient source coverage"):
            validate_snapshot_readiness(snapshot)

    def test_snapshot_readiness_rejects_radar_marked_available_without_fresh_frames(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["sources"]["radar_mosaic"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": {"stale": False, "frames": []},
            "error": None,
        }
        with self.assertRaisesRegex(ValidationError, "radar"):
            validate_snapshot_readiness(snapshot)
        with self.assertRaisesRegex(ValidationError, "radar"):
            validate_snapshot_readiness(snapshot_with_radar(self.snapshot, 7))

    def test_snapshot_readiness_validates_available_weather_next_source(self):
        snapshot = copy.deepcopy(self.snapshot)
        client = mock.Mock()
        client.get.side_effect = [ensemble_metadata(), weather_next_payload()]
        snapshot["sources"]["weather_next"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": collector.collect_weather_next(
                client,
                datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc),
            ),
            "error": None,
        }
        validate_snapshot_readiness(snapshot)
        bad_spread = copy.deepcopy(snapshot)
        bad_spread["sources"]["weather_next"]["data"]["hourly"]["precipitation_spread"][0] = -1
        with self.assertRaisesRegex(ValidationError, "WeatherNext"):
            validate_snapshot_readiness(bad_spread)
        snapshot["sources"]["weather_next"]["data"]["hourly_units"]["precipitation"] = "mm"
        with self.assertRaisesRegex(ValidationError, "WeatherNext"):
            validate_snapshot_readiness(snapshot)

    def test_snapshot_readiness_reapplies_all_ensemble_physical_bounds(self):
        snapshot = copy.deepcopy(self.snapshot)
        client = mock.Mock()
        client.get.side_effect = [ensemble_metadata(), weather_next_payload()]
        snapshot["sources"]["weather_next"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": collector.collect_weather_next(
                client,
                datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc),
            ),
            "error": None,
        }
        invalid_values = {
            "temperature_2m": 1e100,
            "temperature_2m_spread": 101.0,
            "precipitation": 51.0,
            "precipitation_spread": 51.0,
            "cloud_cover_low": 101.0,
            "cloud_cover_low_spread": 101.0,
            "wind_speed_10m": 301.0,
            "wind_speed_10m_spread": 301.0,
            "wind_direction_10m": 361.0,
            "pressure_msl": -1.0,
            "pressure_msl_spread": 151.0,
            "weather_code": 99.5,
        }
        for variable, value in invalid_values.items():
            with self.subTest(variable=variable):
                bad = copy.deepcopy(snapshot)
                bad["sources"]["weather_next"]["data"]["hourly"][variable][0] = value
                with self.assertRaisesRegex(ValidationError, "WeatherNext"):
                    validate_snapshot_readiness(bad)
        bad = copy.deepcopy(snapshot)
        bad["sources"]["weather_next"]["data"]["hourly"]["weather_code"][0] = 4
        with self.assertRaisesRegex(ValidationError, "WeatherNext"):
            validate_snapshot_readiness(bad)

    def test_prompt_defines_same_day_scoring(self):
        prompt = build_prompt(self.snapshot)
        self.assertIn("first report_date is today", prompt)
        self.assertIn("elapsed windows", prompt)

    def test_prompt_applies_aviation_decision_support_to_short_term_scoring(self):
        prompt = build_prompt(self.snapshot)
        required_guidance = (
            "Aviation Weather Decision Support",
            "Observed hazards outrank forecast prose",
            "Separate observed conditions now from forecast conditions",
            "Current local and upstream radar loops",
            "geometrically contains KCDW",
            "validTimeFrom <= collected_at <= validTimeTo",
            "Do not let severe weather hours away",
            "source freshness",
            "Forecast-only means",
            "Upstream developing means",
            "Regional observed means",
            "Local impact means",
            "updated proxy TAF timing",
            "return portion of the two-hour session",
            "Missing sources reduce confidence, not the score by themselves",
            "observation, advisory, forecast, or inference",
            "partially elapsed current block",
            "complete sentence within 300 characters",
            "Attached radar images are ordered oldest to newest",
            "deterministic spatial metrics",
        )
        for guidance in required_guidance:
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, prompt)

    def test_prompt_carries_dynamic_guidance_policy_without_relabeling_sources(self):
        prompt = build_prompt(self.snapshot)
        payload = json.loads(prompt.split("SOURCE_SNAPSHOT_JSON_BEGIN\n")[1].split("\nSOURCE_SNAPSHOT_JSON_END")[0])
        self.assertIsNone(payload["model_guidance_policy"]["preferred_after_48h"])
        self.assertEqual(payload["sources"]["weather_next"], self.snapshot["sources"]["weather_next"])
        self.assertEqual(payload["sources"]["aifs_ens"], self.snapshot["sources"]["aifs_ens"])

    def test_score_bands_match_prompt_and_renderer(self):
        self.assertEqual(
            [(score, score_class(score)) for score in (95, 80, 75, 65, 60, 45, 40, 25, 20, 0)],
            [(95, "strong"), (80, "strong"), (75, "probable"), (65, "probable"),
             (60, "tossup"), (45, "tossup"), (40, "unlikely"), (25, "unlikely"),
             (20, "nogo"), (0, "nogo")],
        )
        rendered, _ = render(self.snapshot, self.analysis, datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc))
        for label in (
            "80–95 · Strong go",
            "65–75 · Probably flyable",
            "45–60 · Toss-up",
            "25–40 · Probably not",
            "0–20 · Practical no-go",
        ):
            self.assertIn(label.split(" · ")[1], rendered)
            self.assertIn(label.lower().replace(" · ", " "), build_prompt(self.snapshot).lower())

    def test_window_reason_contract_allows_complete_short_term_rationale(self):
        value = copy.deepcopy(self.analysis)
        value["days"][0]["windows"][0]["reason"] = "x" * 300
        self.assertIs(validate_analysis(value, self.snapshot), value)
        schema = json.loads((ROOT / "schema" / "analysis.schema.json").read_text())
        self.assertEqual(schema["$defs"]["window"]["properties"]["reason"]["maxLength"], 300)

    def test_render_uses_valid_landmark_and_heading_semantics(self):
        rendered, _ = render(self.snapshot, self.analysis, datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc))
        self.assertIn('<div class="legend" role="group" aria-label="Score legend">', rendered)
        self.assertNotIn("<h3>", rendered)

    def test_render_lists_radar_source(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["sources"]["radar_mosaic"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": {"frames": [{
                "valid_at": "2026-09-03T12:05:00Z",
                "local_echo_within_25_nm": False,
                "local_echo_within_50_nm": False,
                "nearest_displayed_echo_nm": 57.0,
                "nearest_displayed_echo_direction": "N",
            }]},
            "error": None,
        }
        rendered, _ = render(snapshot, self.analysis, datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc))
        self.assertIn("NOAA/NWS MRMS radar loop", rendered)
        self.assertIn("Latest frame 2026-09-03T12:05:00Z", rendered)
        self.assertIn("no displayed echo within 50 NM; nearest displayed echo 57.0 NM N", rendered)

    def test_render_lists_long_range_ensembles_and_open_meteo_attribution(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["sources"]["weather_next"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": {"model": "WeatherNext 2"},
            "error": None,
        }
        snapshot["sources"]["aifs_ens"] = {
            "ok": True,
            "fetched_at": snapshot["collected_at"],
            "data": {"model": "ECMWF AIFS-ENS"},
            "error": None,
        }
        rendered, _ = render(snapshot, self.analysis, datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc))
        self.assertIn("Google WeatherNext 2 ensemble guidance", rendered)
        self.assertIn("ECMWF AIFS-ENS ensemble guidance", rendered)
        self.assertIn('href="https://open-meteo.com/"', rendered)
        self.assertIn("Weather data by Open-Meteo.com", rendered)

    def test_render_marks_today_and_elapsed_windows(self):
        rendered, _ = render(self.snapshot, self.analysis, datetime(2026, 9, 3, 20, 10, tzinfo=timezone.utc))
        self.assertIn("Today · Thursday", rendered)
        self.assertEqual(rendered.count('data-window-state="elapsed"'), 4)
        self.assertEqual(rendered.count('data-window-state="current"'), 1)

    def test_render_escapes_model_text_deterministically(self):
        value = copy.deepcopy(self.analysis); value["summary"] = '<script>alert("x")</script>'
        now = datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc)
        first, _ = render(self.snapshot, value, now); second, _ = render(self.snapshot, value, now)
        self.assertEqual(first, second)
        self.assertNotIn("<script>alert", first)
        self.assertIn("&lt;script&gt;", first)

    def test_stale_health_behavior(self):
        _, fresh = render(self.snapshot, self.analysis, datetime(2026,9,3,13,0,tzinfo=timezone.utc))
        _, stale = render(self.snapshot, self.analysis, datetime(2026,9,3,14,0,tzinfo=timezone.utc))
        self.assertEqual(fresh["status"], "ok")
        self.assertFalse(fresh["stale"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["stale_after"], 5400)

    def test_failed_codex_preserves_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); public = tmp / "public"; var = tmp / "var"; public.mkdir()
            index = public / "index.html"; index.write_text("known-good", encoding="utf-8")
            mock = tmp / "codex"
            mock.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo codex-test; exit 0; fi\nexit 42\n", encoding="utf-8")
            mock.chmod(0o755)
            env = os.environ | {"PUBLIC_DIR":str(public), "VAR_DIR":str(var), "SNAPSHOT_FIXTURE":str(FIX / "sample_snapshot.json"), "CODEX_BIN":str(mock)}
            result = subprocess.run([str(ROOT / "scripts" / "update_report.sh")], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 42, result.stderr.decode())
            feedback = json.loads((var / "agent-feedback.jsonl").read_text())
            self.assertEqual(feedback["feedback_status"], "missing")
            self.assertEqual(feedback["exit_code"], 42)
            self.assertEqual(index.read_text(), "known-good")
            self.assertIn("publication=preserved", (var / "update.log").read_text())

    def test_update_attaches_collected_radar_frames_to_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); public = tmp / "public"; var = tmp / "var"; radar = tmp / "radar"
            public.mkdir(); radar.mkdir()
            frame_paths = [radar / "frame-01.png", radar / "frame-02.png"]
            for frame in frame_paths:
                frame.write_bytes(rgba_png(900, 500, set()))
            snapshot = tmp / "snapshot.json"
            snapshot.write_text(json.dumps(snapshot_with_radar(self.snapshot)), encoding="utf-8")
            args_file = tmp / "args.txt"
            mock = tmp / "codex"
            mock.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = --version ]; then echo codex-test; exit 0; fi\n"
                "printf '%s\\n' \"$@\" > \"$MOCK_ARGS\"\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output-last-message ]; then cp \"$MOCK_ANALYSIS\" \"$2\"; exit 0; fi\n"
                "  shift\n"
                "done\n"
                "exit 2\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            env = os.environ | {
                "PUBLIC_DIR": str(public),
                "VAR_DIR": str(var),
                "RADAR_DIR": str(radar),
                "SNAPSHOT_FIXTURE": str(snapshot),
                "CODEX_BIN": str(mock),
                "MOCK_ARGS": str(args_file),
                "MOCK_ANALYSIS": str(FIX / "sample_analysis.json"),
            }
            result = subprocess.run([str(ROOT / "scripts" / "update_report.sh")], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            feedback = json.loads((var / "agent-feedback.jsonl").read_text())
            self.assertEqual(feedback["feedback_status"], "valid")
            self.assertEqual(feedback["exit_code"], 0)
            self.assertEqual(feedback["data_requests"], [])
            args = args_file.read_text().splitlines()
            self.assertIn("--model", args)
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
            self.assertIn('model_reasoning_effort="medium"', args)
            self.assertIn('web_search="live"', args)
            attached = [args[index + 1] for index, value in enumerate(args) if value == "--image"]
            self.assertEqual(attached, [str(path) for path in frame_paths])

    def test_update_rejects_unbound_or_malformed_radar_files(self):
        cases = (
            ("absent metadata", self.snapshot, {"frame-01.png": rgba_png(900, 500, set())}),
            ("wrong attachment name", snapshot_with_radar(self.snapshot), {
                "frame-01.png": rgba_png(900, 500, set()),
                "frame-99.png": rgba_png(900, 500, set()),
            }),
            ("malformed PNG", snapshot_with_radar(self.snapshot), {
                "frame-01.png": rgba_png(900, 500, set()),
                "frame-02.png": b"not a PNG",
            }),
        )
        for label, snapshot_value, files in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp_name:
                tmp = Path(tmp_name); public = tmp / "public"; var = tmp / "var"; radar = tmp / "radar"
                public.mkdir(); radar.mkdir()
                for name, content in files.items():
                    (radar / name).write_bytes(content)
                snapshot = tmp / "snapshot.json"
                snapshot.write_text(json.dumps(snapshot_value), encoding="utf-8")
                marker = tmp / "codex-called"
                mock = tmp / "codex"
                mock.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = --version ]; then echo codex-test; exit 0; fi\n"
                    "touch \"$MOCK_CALLED\"\n"
                    "exit 2\n",
                    encoding="utf-8",
                )
                mock.chmod(0o755)
                env = os.environ | {
                    "PUBLIC_DIR": str(public), "VAR_DIR": str(var), "RADAR_DIR": str(radar),
                    "SNAPSHOT_FIXTURE": str(snapshot), "CODEX_BIN": str(mock), "MOCK_CALLED": str(marker),
                }
                result = subprocess.run([str(ROOT / "scripts" / "update_report.sh")], cwd=ROOT, env=env, capture_output=True)
                self.assertEqual(result.returncode, 1, result.stderr.decode())
                self.assertFalse(marker.exists())
                self.assertIn("radar_attachments=failed", (var / "update.log").read_text())

    def test_success_without_radar_preserves_latest_radar_evidence(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name); public = tmp / "public"; var = tmp / "var"; latest = var / "latest-radar"
            public.mkdir(); latest.mkdir(parents=True)
            (latest / "frame-01.png").write_bytes(b"known-good")
            (latest / "manifest.json").write_text('{"known":"good"}', encoding="utf-8")
            mock = tmp / "codex"
            mock.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = --version ]; then echo codex-test; exit 0; fi\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --output-last-message ]; then cp \"$MOCK_ANALYSIS\" \"$2\"; exit 0; fi\n"
                "  shift\n"
                "done\n"
                "exit 2\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            env = os.environ | {
                "PUBLIC_DIR": str(public), "VAR_DIR": str(var),
                "SNAPSHOT_FIXTURE": str(FIX / "sample_snapshot.json"), "CODEX_BIN": str(mock),
                "MOCK_ANALYSIS": str(FIX / "sample_analysis.json"),
            }
            result = subprocess.run([str(ROOT / "scripts" / "update_report.sh")], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((latest / "frame-01.png").read_bytes(), b"known-good")
            self.assertEqual(json.loads((latest / "manifest.json").read_text()), {"known": "good"})



if __name__ == "__main__": unittest.main()
