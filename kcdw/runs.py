"""Archive each run and publish one pointer to a complete successful run."""
import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .changes import compare
from .common import atomic_write, load_json


def run_path(var, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", run_id):
        raise ValueError("invalid run ID")
    return var / "runs" / run_id


def copy_typesafe(source, destination):
    """Private diagnostics stay outside public/ and never follow symlinks."""
    if source is None or not source.is_dir() or source.is_symlink():
        return
    target = destination / 'typesafe'
    target.mkdir(mode=0o700, exist_ok=True)
    for path in source.glob('*.json'):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 4_000_000:
            shutil.copyfile(path, target / path.name)
            (target / path.name).chmod(0o600)


def copy_inputs(destination, snapshot, analysis, prompt, radar, log, typesafe=None):
    destination.mkdir(parents=True, exist_ok=True)
    for source, name in ((snapshot, "snapshot.json"), (analysis, "analysis.json"), (prompt, "prompt.txt"), (log, "codex.log")):
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, destination / name)
    if radar.is_dir() and not radar.is_symlink():
        for source in radar.glob("frame-*.png"):
            if source.is_file() and not source.is_symlink() and source.stat().st_size <= 1_000_000:
                (destination / "radar").mkdir(exist_ok=True)
                shutil.copyfile(source, destination / "radar" / source.name)
    copy_typesafe(typesafe, destination)


def replace_link(link, target):
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    temporary.symlink_to(os.path.relpath(target, link.parent), target_is_directory=target.is_dir())
    try:
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def publish(var, public, run_id, snapshot, analysis, prompt, radar, log, html, health, previous=None, typesafe=None):
    destination = run_path(var, run_id)
    staging = destination.with_name(f".{run_id}.tmp")
    if destination.exists() or staging.exists():
        raise FileExistsError(f"run already archived: {run_id}")
    copy_inputs(staging, snapshot, analysis, prompt, radar, log, typesafe)
    (staging / "public").mkdir()
    shutil.copyfile(html, staging / "public" / "index.html")
    shutil.copyfile(health, staging / "public" / "health.json")
    data = load_json(snapshot)
    current = load_json(analysis)
    if (staging / "radar").exists():
        atomic_write(staging / "radar" / "manifest.json", json.dumps({
            "snapshot_collected_at": data["collected_at"],
            "radar_mosaic": data["sources"].get("radar_mosaic"),
        }, indent=2) + "\n")
    changes = compare(current, load_json(previous) if previous and previous.exists() else None, datetime.now(timezone.utc))
    atomic_write(staging / "changes.json", json.dumps(changes, indent=2) + "\n")
    atomic_write(staging / "manifest.json", json.dumps({"run_id": run_id, "source_collected_at": data["collected_at"],
                 "archived_at": datetime.now(timezone.utc).isoformat(), "status": "validated"}, indent=2) + "\n")
    os.replace(staging, destination)
    # The web server resolves this once per request. This is the publication commit.
    replace_link(var / "current", destination)
    # Compatibility exports are not the served source of truth. Failure after commit
    # must not misreport a valid publication or delete its retained evidence.
    try:
        for source, target in ((destination / "snapshot.json", var / "latest-snapshot.json"),
                               (destination / "analysis.json", var / "latest-analysis.json"),
                               (destination / "public" / "index.html", public / "index.html"),
                               (destination / "public" / "health.json", public / "health.json")):
            atomic_write(target, source.read_text())
        if (destination / "radar").exists():
            latest = var / "latest-radar"
            if latest.is_dir() and not latest.is_symlink():
                os.replace(latest, var / f"legacy-radar.{run_id}")
            replace_link(latest, destination / "radar")
    except OSError as exc:
        print(f"publication committed; compatibility export failed: {exc}")


def finish(var, run_id, snapshot, analysis, prompt, radar, log, exit_code, typesafe=None):
    destination = run_path(var, run_id)
    if not destination.exists():
        copy_inputs(destination, snapshot, analysis, prompt, radar, log, typesafe)
    atomic_write(destination / "outcome.json", json.dumps({"exit_code": exit_code,
                 "finished_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("publish", "finish"))
    p.add_argument("var", type=Path)
    p.add_argument("run_id")
    for name in ("snapshot", "analysis", "prompt", "radar", "log"):
        p.add_argument(name, type=Path)
    p.add_argument("--public", type=Path)
    p.add_argument("--html", type=Path)
    p.add_argument("--health", type=Path)
    p.add_argument("--previous", type=Path)
    p.add_argument("--typesafe", type=Path)
    p.add_argument("--exit-code", type=int, default=0)
    a = p.parse_args()
    if a.command == "publish":
        publish(a.var, a.public, a.run_id, a.snapshot, a.analysis, a.prompt, a.radar, a.log, a.html, a.health, a.previous, a.typesafe)
    else:
        finish(a.var, a.run_id, a.snapshot, a.analysis, a.prompt, a.radar, a.log, a.exit_code, a.typesafe)


if __name__ == "__main__":
    main()
