"""Refresh every upcoming dated-event page: collect members, render, archive, publish.

Independent of the main report; a bounded agent narrative explains each fresh
event snapshot. Narrative failures leave the deterministic charts available.
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
from .event_ensemble import collect_event as collect_base_event, event_range
from .ensemble_trends import build_trends
from .run_history import load_run_history
from .event_renderer import render, summary
from .events import upcoming_events
from .event_timing import load_event_timing
from .runs import replace_link


def collect_moisture(client, start, end, now):
    from .event_moisture import collect_moisture as collect
    return collect(client, start, end, now)


def collect_moisture_ensemble(client, start, end, now):
    from .event_moisture_ensemble import collect_moisture_ensemble as collect
    return collect(client, start, end, now)


def collect_afds(client, snapshot, now):
    from .event_afd import collect_afds as collect
    return collect(client, snapshot, now)


def collect_wind(client, snapshot, now):
    from .event_wind import collect_wind as collect
    return collect(client, snapshot, now)


def collect_native_wind(snapshot, cache_dir, now):
    from .native_wind import collect_native_wind as collect
    return collect(snapshot, cache_dir, now)


def build_wind_trends(snapshot, runs_dir, now):
    from .event_wind_trends import build_wind_trends as build
    return build(snapshot, runs_dir, now)


def collect_ceiling(snapshot, cache_dir, now):
    from .cloud_ceiling import collect_ceiling as collect
    return collect(snapshot, cache_dir, now)


def collect_layer_signals(client, snapshot, now):
    from .cloud_layer_signals import collect_layer_signals as collect
    return collect(client, snapshot, now)


def collect_model_matrix(client, snapshot, cache_path, now):
    from .event_model_matrix import collect_matrix as collect
    return collect(client, snapshot, now, cache_path)


def collect_event_gusts(client, snapshot, cache_path, now):
    from .event_gusts import collect_gusts as collect
    return collect(client, snapshot, now, cache_path)


def collect_synoptic_pattern(client, snapshot, now):
    from .synoptic_pattern import collect_pattern as collect
    return collect(client, snapshot, now)


def collect_wn3_100m_wind(snapshot, now):
    from .wn3_surface_context import collect_wind as collect
    return collect(snapshot, now)


def collect_wn3_hourly(now):
    from .wn3_hourly import collect
    return collect(now)


def collect_wn2_members(client, snapshot, now):
    from .event_wn2_members import collect_members as collect
    return collect(client, snapshot, now)


def _collection_clock(client, fallback):
    return datetime.now(UTC) if getattr(client, 'direct_native', False) is True else fallback


def collect_event(client, event, now):
    options = {"allow_empty": True} if getattr(client, "direct_native", False) is True else {}
    snapshot = collect_base_event(client, event, now, **options)
    start, end = event_range(event, now)
    for key, collector in (("event_moisture", collect_moisture),
                           ("event_moisture_ensemble", collect_moisture_ensemble)):
        try:
            snapshot[key] = collector(client, start, end, _collection_clock(client, now))
        except Exception:
            # Supplemental RH cannot suppress the independent forecast sources.
            snapshot[key] = None
    if getattr(client, 'direct_native', False) is True:
        snapshot.setdefault('collection_started_at', snapshot['collected_at'])
        snapshot['collected_at'] = iso_z(_collection_clock(client, now))
        snapshot['direct_native_version'] = 1
    return snapshot


def build_forecast_history(snapshot, runs_dir, now):
    from .forecast_history import build_forecast_history as build
    return build(snapshot, runs_dir, now)


def build_event_changes(snapshot, runs_dir, now):
    from .event_change_evidence import build_event_changes as build
    return build(snapshot, runs_dir, now)


def generate_event_narrative(snapshot, work_dir, now):
    from .event_narrative import generate_event_narrative as generate
    return generate(snapshot, work_dir, now, typesafe_var=work_dir.parents[3])


def archive_run(var: Path, slug: str, run_id: str, snapshot: dict, html: str, health: dict, text: str) -> Path:
    destination = var / "events" / slug / "runs" / run_id
    staging = destination.with_name(f".{run_id}.tmp")
    if destination.exists() or staging.exists():
        raise FileExistsError(f"event run already archived: {run_id}")
    staging.mkdir(parents=True)
    atomic_write(staging / "snapshot.json", json.dumps(snapshot, separators=(',', ':'), sort_keys=True) + "\n")
    atomic_write(staging / "index.html", html + "\n")
    atomic_write(staging / "health.json", json.dumps(health, indent=2, sort_keys=True) + "\n")
    atomic_write(staging / "manifest.json", json.dumps({"kind": "event", "slug": slug, "run_id": run_id, "summary": text,
                 "source_collected_at": snapshot["collected_at"], "archived_at": iso_z(datetime.now(UTC)), "status": "validated"}, indent=2) + "\n")
    from .runs import copy_typesafe
    copy_typesafe(var / 'events' / slug / 'narratives' / run_id / 'typesafe', staging)
    os.replace(staging, destination)
    replace_link(var / "events" / slug / "current", destination)
    return destination


def update(var: Path, cloud_config: Path | None, now: datetime | None = None, events_path: Path | None = None) -> int:
    live_clock = now is None
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
            snapshot = collect_event(HttpClient(timeout=40, direct_native=True, direct_ensembles=True), event, now)
            now = max(now, datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00")))
            from .chart_retention import retain_chart_coverage
            retain_chart_coverage(snapshot, var / "events" / event.slug / "runs", now)
            if not any(source["ok"] for source in snapshot["models"].values()):
                raise RuntimeError("no ensemble model was usable after full-range recovery")
            snapshot["event_timing"] = load_event_timing(event, events_path)
            snapshot["initialization_provenance_version"] = 1
            snapshot["narrative_sampling_dictionary_version"] = 1
            snapshot["native_ensemble_evidence_version"] = 1
            snapshot["narrative_priority_version"] = 1
            from .coastal_maps import load as load_coastal_maps
            for key, collect in (
                ("coastal_maps", lambda: load_coastal_maps(var / "events" / event.slug / "coastal-maps.json", snapshot['event'])),
                ("event_wind", lambda: collect_wind(HttpClient(timeout=15, retries=0, direct_native=True), snapshot, now)),
                ("event_gusts", lambda: collect_event_gusts(HttpClient(timeout=12, retries=0, direct_native=True), snapshot, var / "events" / event.slug / "gust-guidance-cache.json", now)),
                ("native_wind", lambda: collect_native_wind(snapshot, var / "events" / event.slug / "native-wind-cache", now)),
                ("wind_trends", lambda: build_wind_trends(snapshot, var / "events" / event.slug / "runs", now)),
                ("synoptic_pattern", lambda: collect_synoptic_pattern(HttpClient(timeout=10, retries=1), snapshot, now)),
                ("event_afds", lambda: collect_afds(HttpClient(timeout=10, retries=1), snapshot, now)),
                ("cloud_ceiling", lambda: collect_ceiling(snapshot, var / "events" / event.slug / "native-ceiling-cache", now)),
                ("cloud_layer_signals", lambda: collect_layer_signals(HttpClient(timeout=15, retries=0, direct_native=True), snapshot, now)),
                ("wn3_100m_wind", lambda: collect_wn3_100m_wind(snapshot, now)),
                ("wn3_hourly", lambda: collect_wn3_hourly(now)),
                ("weathernext2_members", lambda: collect_wn2_members(None, snapshot, now)),
                ("model_matrix", lambda: collect_model_matrix(HttpClient(timeout=12, retries=0, direct_native=True), snapshot, var / "events" / event.slug / "model-matrix-cache.json", now)),
            ):
                try:
                    snapshot[key] = collect()
                    record(f"event={event.slug} {key}={'available' if snapshot[key] else 'unavailable'}")
                except Exception as exc:
                    snapshot[key] = None
                    record(f"event={event.slug} {key}=unavailable error={type(exc).__name__}")
            if live_clock:
                now = datetime.now(UTC)
            try:
                snapshot["ensemble_trends"] = build_trends(snapshot, var / "events" / event.slug / "runs", now)
            except Exception:
                snapshot["ensemble_trends"] = None
                record(f"event={event.slug} trends=unavailable")
            try:
                snapshot["forecast_history"] = build_forecast_history(snapshot, var / "events" / event.slug / "runs", now)
            except Exception:
                snapshot["forecast_history"] = None
                record(f"event={event.slug} forecast_history=unavailable")
            try:
                snapshot["event_changes"] = build_event_changes(snapshot, var / "events" / event.slug / "runs", now)
                if snapshot["event_changes"] is None:
                    record(f"event={event.slug} event_changes=none")
            except Exception:
                snapshot["event_changes"] = None
                record(f"event={event.slug} event_changes=unavailable")
            from .wn3_cloud_analysis import collect_previous as collect_previous_clouds
            try:
                snapshot['wn3_cloud_previous'] = collect_previous_clouds(snapshot, var / 'events' / event.slug / 'runs', now, backfill=live_clock)
            except Exception:
                snapshot['wn3_cloud_previous'] = None
                record(f'event={event.slug} wn3_cloud_previous=unavailable')
            snapshot["ensemble_run_history"] = load_run_history(var / "events" / event.slug / "backfill.json", snapshot["event"], now)
            try:
                work_dir = var / "events" / event.slug / "narratives" / run_id
                snapshot["event_narrative"] = generate_event_narrative(snapshot, work_dir, now)
                writer = (snapshot.get('event_narrative') or {}).get('provider', 'unknown')
                record(f"event={event.slug} narrative=success provider={writer}")
            except Exception as exc:
                snapshot["event_narrative"] = None
                record(f"event={event.slug} narrative=unavailable error={type(exc).__name__}")
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
