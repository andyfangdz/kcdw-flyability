import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from kcdw.collector import _source
from kcdw.nbm_guidance import MAX_BULK_BYTES, collect_nbm, parse_card, product_url
from kcdw.renderer import source_rows
from kcdw.validation import ValidationError, validate_snapshot_readiness

FIX = Path(__file__).parent / "fixtures"
CYCLE = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
NOW = CYCLE + timedelta(hours=1, minutes=30)


class NBMTests(unittest.TestCase):
    def card(self, product):
        return (FIX / f"{product.lower()}_kcdw.txt").read_text()

    def test_real_cards_preserve_cells_and_align_midnight(self):
        for product, count, end in (("NBH", 25, "2026-09-09T13:00:00Z"),
                                    ("NBS", 23, "2026-09-11T12:00:00Z")):
            with self.subTest(product=product):
                raw = self.card(product)
                data = parse_card(raw + "\n" + raw.replace("KCDW", "KCEC"), product, CYCLE, NOW)
                self.assertEqual(data["raw_text"], raw.rstrip())
                self.assertEqual(len(data["valid_times"]), count)
                self.assertEqual(data["valid_times"][-1], end)
                self.assertIn("-88-88", data["raw_text"])
                self.assertNotIn("KCEC", data["raw_text"])

    def test_rejects_wrong_station_product_cycle_stale_and_truncated(self):
        raw = self.card("NBH")
        cases = [raw.replace("KCDW", "KTEB"), raw.replace("NBH", "NBS"),
                 raw.replace("1200 UTC", "1100 UTC"), raw.replace(" UTC  13", " UTC  14"),
                 "\n".join(line for line in raw.splitlines() if not line.startswith(" VIS"))]
        for text in cases:
            with self.subTest(text=text[:70]), self.assertRaises(ValueError):
                parse_card(text, "NBH", CYCLE, NOW)
        for now in (CYCLE - timedelta(minutes=1), CYCLE + timedelta(hours=7)):
            with self.assertRaises(ValueError):
                parse_card(raw, "NBH", CYCLE, now)

    def test_collect_falls_back_to_previous_cycle(self):
        client = Mock()
        client.get_text.side_effect = [RuntimeError("not published yet"), self.card("NBS")]
        data = collect_nbm(client, NOW, "NBS")
        self.assertEqual(data["cycle_time"], "2026-09-08T12:00:00Z")
        self.assertEqual(client.get_text.call_args.args[0], product_url("NBS", CYCLE))
        self.assertEqual(client.get_text.call_args.kwargs, {"maximum": MAX_BULK_BYTES})

    def test_failure_is_isolated_and_attempts_are_bounded(self):
        client = Mock()
        client.get_text.side_effect = RuntimeError("offline")
        source = _source(lambda: collect_nbm(client, NOW, "NBH"), NOW)
        self.assertFalse(source["ok"])
        self.assertEqual(client.get_text.call_count, 6)

    def test_validation_binds_metadata_and_renderer_shows_cycles(self):
        snapshot = json.loads((FIX / "sample_snapshot.json").read_text())
        snapshot["collected_at"] = "2026-09-08T13:30:00Z"
        snapshot["sources"]["nws_hourly"]["data"][0].update(startTime="2026-09-08T13:00:00Z", endTime="2026-09-08T16:00:00Z")
        snapshot["sources"]["okx_afd"]["data"]["issuanceTime"] = "2026-09-08T12:00:00Z"
        for product in ("NBH", "NBS"):
            snapshot["sources"][f"nbm_{product.lower()}"] = _source(
                lambda: parse_card(self.card(product), product, CYCLE, NOW), NOW)
        validate_snapshot_readiness(snapshot)
        html = source_rows(snapshot)
        self.assertIn("24h / NBH", html)
        self.assertIn("72h / NBS", html)
        self.assertIn("Cycle 2026-09-08T12:00:00Z", html)
        bad = copy.deepcopy(snapshot)
        bad["sources"]["nbm_nbs"]["data"]["valid_times"][0] = "2026-09-08T17:00:00Z"
        with self.assertRaisesRegex(ValidationError, "NBS"):
            validate_snapshot_readiness(bad)


if __name__ == "__main__":
    unittest.main()
