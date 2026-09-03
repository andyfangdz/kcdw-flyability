from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kcdw.collector import report_dates
from kcdw.geometry import point_in_polygon
from kcdw.intervals import duration, expand_grid_values, expand_valid_time
from kcdw.prompt import build_prompt
from kcdw.renderer import render, window_state
from kcdw.validation import ValidationError, validate_analysis, validate_snapshot_readiness

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


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

    def test_prompt_defines_same_day_scoring(self):
        prompt = build_prompt(self.snapshot)
        self.assertIn("first report_date is today", prompt)
        self.assertIn("elapsed windows", prompt)

    def test_render_uses_valid_landmark_and_heading_semantics(self):
        rendered, _ = render(self.snapshot, self.analysis, datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc))
        self.assertIn('<div class="legend" role="group" aria-label="Score legend">', rendered)
        self.assertNotIn("<h3>", rendered)

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


if __name__ == "__main__": unittest.main()
