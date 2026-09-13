"""Refresh every upcoming dated-event page: collect members, render, archive, publish.

Deterministic and independent of the Codex-backed main report. A failure for one
event never touches another event or the main publication.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .cloud_publish import Client, publish_event, publish_events_index
from .collector import Client as HttpClient
from .common import UTC, atomic_write, iso_z, load_json
from .event_ensemble import collect_event
from .event_renderer import render, summary
from .events import upcoming_events
from .runs import replace_link


def archive_run(var: Path, slug: str, run_id: str, snapshot: dict, html: str, health: dict, text: str) -> Path:
    destination = var / "events" / slug / "runs" / run_id
    staging = destination.with_name(f".{run_id}.tmp")
    if destination.exists() or staging.exists():
        raise FileExistsError(f"event run already archived: {run_id}")
    staging.mkdir(parents=True)
    atomic_write(staging / "snapshot.json", json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    atomic_write(staging / "index.html", html + "\n")
    atomic_write(staging / "health.json", json.dumps(health, indent=2, sort_keys=True) + "\n")
    atomic_write(staging / "manifest.json", json.dumps({"kind": "event", "slug": slug, "run_id": run_id, "summary": text,
                 "source_collected_at": snapshot["collected_at"], "archived_at": iso_z(datetime.now(UTC)), "status": "validated"}, indent=2) + "\n")
    os.replace(staging, destination)
    replace_link(var / "events" / slug / "current", destination)
    return destination


def update(var: Path, cloud_config: Path | None, now: datetime | None = None, events_path: Path | None = None) -> int:
    now = now or datetime.now(UTC)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}.{os.getpid()}"
    events = [e for e in upcoming_events(now, events_path) if e.days_out(now) >= 0]
    log = var / "events" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    def record(message: str) -> None:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{iso_z(datetime.now(UTC))} run={run_id} {message}\n")

    client = Client(load_json(cloud_config)) if cloud_config else None
    failures = 0
    for event in events:
        try:
            snapshot = collect_event(HttpClient(timeout=40), event, now)
            html, health = render(snapshot, now, events_path)
            text = summary(snapshot)
            archive = archive_run(var, event.slug, run_id, snapshot, html, health, text)
            usable = sum(1 for model in snapshot["models"].values() if model["ok"])
            record(f"event={event.slug} collector=success models={usable}/{len(snapshot['models'])} render=success archive=success")
            if client:
                publish_event(client, archive)
                record(f"event={event.slug} cloud_publication=success")
        except Exception as exc:
            failures += 1
            record(f"event={event.slug} failed error={type(exc).__name__}: {str(exc)[:200]}")
            print(f"{event.slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if client:
        try:
            publish_events_index(client, upcoming_events(now, events_path, horizon_days=3650), now)
            record("events_index=success")
        except Exception as exc:
            failures += 1
            record(f"events_index=failed error={type(exc).__name__}: {str(exc)[:200]}")
    if not events:
        record("events=none")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--var", type=Path, default=Path("var"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CLOUD_PUBLISH_CONFIG", "var/cloudflare.json")))
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args(argv)
    return update(args.var, None if args.no_publish else args.config)


if __name__ == "__main__":
    raise SystemExit(main())
