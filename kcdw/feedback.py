"""Durable per-run product requests and a frequency/priority tally."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PRIORITY = {"high": 3, "medium": 2, "low": 1}


def validate_requests(requests) -> None:
    if not isinstance(requests, list) or len(requests) > 5:
        raise ValueError("data_requests must contain 0..5 items")
    seen = set()
    for item in requests:
        fields = {"product_id", "product", "priority", "reason", "expected_benefit", "source_url", "retrieval"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid data request fields")
        for key, limit in (("product_id", 80), ("product", 120), ("reason", 300), ("expected_benefit", 300), ("source_url", 500)):
            value = item[key]
            if not isinstance(value, str) or len(value) > limit or (key != "source_url" and not value.strip()):
                raise ValueError(f"invalid data request {key}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", item["product_id"]) or item["product_id"] in seen:
            raise ValueError("invalid or duplicate product_id")
        seen.add(item["product_id"])
        if not isinstance(item["priority"], str) or item["priority"] not in PRIORITY or item["retrieval"] not in ("retrieved", "unavailable", "not_attempted"):
            raise ValueError("invalid request priority or retrieval")
        if item["source_url"]:
            url = urlsplit(item["source_url"])
            if url.scheme not in ("http", "https") or not url.netloc:
                raise ValueError("invalid request source URL")


def record(log: Path, run_id: str, snapshot: Path, analysis: Path, exit_code: int) -> None:
    entry = {"schema_version": 1, "run_id": run_id,
             "recorded_at": datetime.now(timezone.utc).isoformat(),
             "exit_code": exit_code, "source_collected_at": None,
             "source_status": {}, "feedback_status": "missing", "data_requests": None}
    if snapshot.exists():
        try:
            data = json.loads(snapshot.read_text())
            entry["source_collected_at"] = data.get("collected_at")
            entry["source_status"] = {key: value.get("ok") for key, value in data.get("sources", {}).items()}
        except (ValueError, AttributeError):
            pass
    if analysis.exists():
        try:
            requests = json.loads(analysis.read_text())["data_requests"]
            validate_requests(requests)
            entry.update(feedback_status="valid", data_requests=requests)
        except (ValueError, KeyError, TypeError):
            entry["feedback_status"] = "invalid"
    # The updater holds update.lock throughout this append and cleanup.
    payload = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def summarize(log: Path) -> dict:
    products = {}
    total = valid = no_requests = 0
    for line in log.read_text().splitlines():
        entry = json.loads(line)
        total += 1
        if entry["feedback_status"] != "valid":
            continue
        valid += 1
        no_requests += not entry["data_requests"]
        for request in entry["data_requests"]:
            row = products.setdefault(request["product_id"], {
                "product_id": request["product_id"], "runs_requested": 0,
                "high_priority_runs": 0, "priority_points": 0, "retrieved_runs": 0})
            row["runs_requested"] += 1
            row["high_priority_runs"] += request["priority"] == "high"
            row["priority_points"] += PRIORITY[request["priority"]]
            row["retrieved_runs"] += request["retrieval"] == "retrieved"
            row["latest_request"] = request
    return {"total_runs": total, "valid_feedback_runs": valid, "no_requests_runs": no_requests,
            "products": sorted(products.values(), key=lambda row: (-row["runs_requested"], -row["priority_points"], row["product_id"]))}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    append = sub.add_parser("record")
    for name in ("log", "run_id", "snapshot", "analysis"):
        append.add_argument(name)
    append.add_argument("exit_code", type=int)
    report = sub.add_parser("summary")
    report.add_argument("log", nargs="?", default="var/agent-feedback.jsonl")
    args = parser.parse_args()
    if args.command == "record":
        record(Path(args.log), args.run_id, Path(args.snapshot), Path(args.analysis), args.exit_code)
    else:
        print(json.dumps(summarize(Path(args.log)), indent=2))


if __name__ == "__main__":
    main()
