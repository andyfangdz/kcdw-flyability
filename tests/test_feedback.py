import json
import tempfile
import unittest
from pathlib import Path

from kcdw.feedback import record, summarize, validate_requests


def request(priority="high"):
    return {"product_id": "upstream_afd", "product": "Upstream NWS AFDs",
            "priority": priority, "reason": "Upstream convective timing is uncertain.",
            "expected_benefit": "Clarify afternoon arrival timing.",
            "source_url": "", "retrieval": "not_attempted"}


class FeedbackTests(unittest.TestCase):
    def test_retains_runs_and_tallies_requests_without_counting_missing_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, snapshot, analysis = (root / name for name in ("log.jsonl", "snapshot.json", "analysis.json"))
            snapshot.write_text(json.dumps({"collected_at": "2026-09-08T12:00:00Z", "sources": {"radar": {"ok": False}}}))
            for index, requests in enumerate(([request()], [request("medium")], [])):
                analysis.write_text(json.dumps({"data_requests": requests}))
                record(log, str(index), snapshot, analysis, 0)
            analysis.write_text('{"data_requests": "bad"}')
            record(log, "invalid", snapshot, analysis, 1)
            record(log, "missing", snapshot, root / "absent.json", 42)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["source_status"], {"radar": False})
            self.assertEqual(rows[3]["feedback_status"], "invalid")
            self.assertIsNone(rows[4]["data_requests"])
            summary = summarize(log)
            self.assertEqual(summary["total_runs"], 5)
            self.assertEqual(summary["valid_feedback_runs"], 3)
            self.assertEqual(summary["no_requests_runs"], 1)
            self.assertEqual(summary["products"][0]["runs_requested"], 2)
            self.assertEqual(summary["products"][0]["priority_points"], 5)
            self.assertEqual(summary["products"][0]["high_priority_runs"], 1)

    def test_rejects_duplicates_bad_urls_and_bad_priorities(self):
        for requests in ([request(), request()], [request() | {"source_url": "javascript:alert(1)"}],
                         [request() | {"priority": []}], [request() | {"product_id": "Two Words"}],
                         [request() | {"reason": ""}]):
            with self.subTest(requests=requests), self.assertRaises(ValueError):
                validate_requests(requests)
