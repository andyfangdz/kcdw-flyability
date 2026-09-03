from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kcdw.geometry import point_in_polygon
from kcdw.intervals import duration, expand_grid_values, expand_valid_time
from kcdw.renderer import render
from kcdw.validation import ValidationError, validate_analysis, validate_snapshot_readiness

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


class ProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((FIX / "sample_snapshot.json").read_text())
        cls.analysis = json.loads((FIX / "sample_analysis.json").read_text())

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
