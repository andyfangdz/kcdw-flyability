import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kcdw.changes import compare
from kcdw.evidence import compact_grid, decode_nbm, ensemble_summary, prepare
from kcdw.nbm_guidance import collect_nbm, parse_card
from kcdw.readiness import usable_forecast
from kcdw.renderer import render
from kcdw.runs import finish, publish
from kcdw.scoring import planning_windows
from kcdw.server import live_health, current_public
from kcdw.validation import validate_analysis, validate_snapshot_readiness, ValidationError

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def modern_analysis(snapshot):
    analysis = json.loads((FIX / "sample_analysis.json").read_text())
    for day in analysis["days"]:
        day["outlook"] = "Probably flyable"
        day["windows"] = [w for w in day["windows"] if w["window"] in planning_windows(snapshot, day["date"])]
        for window in day["windows"]:
            window.pop("label", None)
    return analysis


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((FIX / "sample_snapshot.json").read_text())

    def test_empty_successes_cannot_pass_readiness(self):
        self.snapshot["sources"] = {key: {"ok": True, "data": data} for key, data in (
            ("nws_hourly", []), ("nws_alerts", []), ("nws_points", {}))}
        with self.assertRaisesRegex(ValidationError, "insufficient source coverage"):
            validate_snapshot_readiness(self.snapshot)

    def test_forecast_must_cover_whole_session_without_gaps(self):
        period = {"startTime": "2026-09-03T12:00:00Z", "endTime": "2026-09-03T13:00:00Z", "shortForecast": "Clear"}
        self.assertFalse(usable_forecast("nws_hourly", [period], NOW))
        second = period | {"startTime": "2026-09-03T13:00:00Z", "endTime": "2026-09-03T14:00:00Z"}
        self.assertTrue(usable_forecast("nws_hourly", [second, period], NOW))
        self.assertFalse(usable_forecast("nws_hourly", [period, second | {"startTime": "2026-09-03T13:30:00Z"}], NOW))

    def test_horizon_contract_omits_long_range_and_elapsed_slots(self):
        self.snapshot["collected_at"] = "2026-09-03T15:00:00Z"
        analysis = modern_analysis(self.snapshot)
        analysis["source_collected_at"] = self.snapshot["collected_at"]
        analysis["generated_at"] = self.snapshot["collected_at"]
        validate_analysis(analysis, self.snapshot)
        self.assertEqual(analysis["days"][0]["windows"][0]["window"], "10-12")
        self.assertTrue(all(not d["windows"] for d in analysis["days"][3:]))
        analysis["days"][3]["windows"] = copy.deepcopy(analysis["days"][1]["windows"])
        with self.assertRaisesRegex(ValidationError, "horizon"):
            validate_analysis(analysis, self.snapshot)

    def test_renderer_derives_labels_and_sorts_windows(self):
        analysis = modern_analysis(self.snapshot)
        day = analysis["days"][0]
        day["windows"][0].update(score=0, label="Strong go")
        day["windows"].reverse()
        html, _ = render(self.snapshot, analysis, NOW)
        first = html.split('data-window="08-10"')[1].split('</li>')[0]
        self.assertIn("Practical no-go", first)
        self.assertNotIn("Strong go", first)
        self.assertLess(html.index('data-window="08-10"'), html.index('data-window="10-12"'))
        self.assertNotIn('95%</strong>', html)
        self.assertEqual(html.count('data-window="'), 18)

    def test_health_ages_without_rendering_again(self):
        data = {"generated_at": "2026-09-03T12:00:00Z", "stale_after": 5400, "status": "ok"}
        self.assertFalse(live_health(data, NOW + timedelta(minutes=89))["stale"])
        self.assertTrue(live_health(data, NOW + timedelta(minutes=91))["stale"])
        self.assertEqual(data["status"], "ok")

    def test_changes_ignore_elapsed_slots_and_small_same_band_moves(self):
        old = modern_analysis(self.snapshot)
        new = copy.deepcopy(old)
        new["days"][0]["windows"][0]["score"] = 0
        new["days"][1]["windows"][0]["score"] = 45
        result = compare(new, old, NOW + timedelta(hours=3))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["date"], "2026-09-04")
        self.assertEqual(compare(old, old, NOW)["items"], [])

    def test_grid_compaction_is_lossless_and_does_not_fill_gaps(self):
        grid = {"windSpeed": {"uom": "kn", "values": [
            {"time": "2026-09-03T12:00:00Z", "value": 5},
            {"time": "2026-09-03T13:00:00Z", "value": 5},
            {"time": "2026-09-03T15:00:00Z", "value": 5},
            {"time": "2026-09-03T16:00:00Z", "value": None}]}}
        compact_grid(grid)
        self.assertEqual(grid["windSpeed"]["values"], [
            {"validTime": "2026-09-03T12:00:00Z/PT2H", "value": 5},
            {"validTime": "2026-09-03T15:00:00Z/PT1H", "value": 5},
            {"validTime": "2026-09-03T16:00:00Z/PT1H", "value": None}])

    def test_nbm_decode_and_cached_card_avoid_repeat_bulk_download(self):
        raw = (FIX / "nbh_kcdw.txt").read_text()
        now = datetime(2026, 9, 8, 12, 45, tzinfo=timezone.utc)
        client = Mock()
        client.get_text.return_value = raw
        with tempfile.TemporaryDirectory() as root:
            a = collect_nbm(client, now, "NBH", Path(root))
            b = collect_nbm(client, now, "NBH", Path(root))
        self.assertEqual(a, b)
        client.get_text.assert_called_once()
        fields = decode_nbm(a)["fields"]
        self.assertEqual(fields["ceiling_feet"][0], "unlimited")
        self.assertEqual(fields["visibility_miles"][0], 10)
        self.assertEqual(fields["wind_direction_degrees"][0], 320)
        self.assertIsNone(fields["precip_probability_6h_pct"][0])
        self.assertEqual(decode_nbm(a | {"model_version": "6.0"})["status"], "unsupported_version")

    def test_ensemble_arithmetic_is_within_model_spread_not_hourly_variation(self):
        times = ["2026-09-03T12:00:00Z", "2026-09-03T13:00:00Z"]
        data = {"model": "test", "hourly": {"time": times}, "hourly_units": {}}
        for field, unit in (("precipitation", "inch"), ("cloud_cover_low", "%"), ("wind_speed_10m", "kn")):
            data["hourly"][field] = [0, 100]
            data["hourly"][field + "_spread"] = [2, 3]
            data["hourly_units"][field] = unit
        summary = ensemble_summary(data, self.snapshot)["days"][0]["variables"]["wind_speed_10m"]
        self.assertEqual(summary["variance_max"], 9)
        self.assertEqual(summary["sd_max"], 3)
        self.assertEqual(summary["max_sd_at"], times[1])

    def test_run_commit_failure_preserves_previous_and_archives_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            var, public = root / "var", root / "public"
            var.mkdir(); public.mkdir()
            snapshot, analysis, prompt, radar, log, html, health = (root / p for p in ("snapshot.json", "analysis.json", "prompt.txt", "radar", "codex.log", "index.html", "health.json"))
            snapshot.write_text(json.dumps(self.snapshot))
            analysis.write_text(json.dumps(modern_analysis(self.snapshot)))
            prompt.write_text("exact input")
            log.write_text("agent output")
            html.write_text("first report")
            health.write_text('{"generated_at":"2026-09-03T12:00:00Z"}')
            publish(var, public, "first", snapshot, analysis, prompt, radar, log, html, health)
            self.assertEqual(current_public(var, public), var / "runs" / "first" / "public")
            html.write_text("second report")
            with patch("kcdw.runs.replace_link", side_effect=OSError("commit failed")):
                with self.assertRaises(OSError):
                    publish(var, public, "second", snapshot, analysis, prompt, radar, log, html, health)
            self.assertEqual((current_public(var, public) / "index.html").read_text(), "first report")
            finish(var, "failed", snapshot, root / "missing", prompt, radar, log, 42)
            self.assertEqual(json.loads((var / "runs/failed/outcome.json").read_text())["exit_code"], 42)
            self.assertEqual((var / "runs/failed/prompt.txt").read_text(), "exact input")


if __name__ == "__main__":
    unittest.main()
