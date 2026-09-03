from __future__ import annotations

import binascii
import copy
import json
import os
import struct
import subprocess
import tempfile
import unittest
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
            self.assertIn(label, rendered)
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
            args = args_file.read_text().splitlines()
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

    def test_publish_radar_evidence_writes_manifest_and_switches_latest_atomically(self):
        from kcdw.radar_evidence import publish_radar_evidence

        with tempfile.TemporaryDirectory() as tmp_name:
            var = Path(tmp_name); radar = var / "radar.run"; latest = var / "latest-radar"
            radar.mkdir(); latest.mkdir()
            (latest / "old").write_text("old", encoding="utf-8")
            for attachment in (1, 2):
                (radar / f"frame-{attachment:02d}.png").write_bytes(rgba_png(900, 500, set()))
            snapshot = snapshot_with_radar(self.snapshot)
            publish_radar_evidence(snapshot, radar, var, "test-run")
            self.assertTrue(latest.is_symlink())
            self.assertEqual(sorted(path.name for path in latest.glob("frame-*.png")), ["frame-01.png", "frame-02.png"])
            manifest = json.loads((latest / "manifest.json").read_text())
            self.assertEqual(manifest["snapshot_collected_at"], snapshot["collected_at"])
            self.assertEqual(manifest["radar_mosaic"], snapshot["sources"]["radar_mosaic"])

    def test_post_switch_cleanup_failure_does_not_report_publication_failure(self):
        from kcdw.radar_evidence import publish_radar_evidence

        with tempfile.TemporaryDirectory() as tmp_name:
            var = Path(tmp_name); old = var / "radar-published.old"; radar = var / "radar.run"
            old.mkdir(); radar.mkdir()
            (var / "latest-radar").symlink_to(old.name, target_is_directory=True)
            for attachment in (1, 2):
                (radar / f"frame-{attachment:02d}.png").write_bytes(rgba_png(900, 500, set()))
            with mock.patch("kcdw.radar_evidence.Path.glob", side_effect=OSError("injected cleanup failure")):
                self.assertTrue(publish_radar_evidence(snapshot_with_radar(self.snapshot), radar, var, "cleanup"))
            latest = var / "latest-radar"
            self.assertTrue(latest.is_symlink())
            self.assertEqual(os.readlink(latest), "radar-published.cleanup")
            self.assertTrue((latest / "manifest.json").is_file())


if __name__ == "__main__": unittest.main()
