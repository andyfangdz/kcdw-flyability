from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .collector import _decode_rgba_png
from .validation import ValidationError, validate_snapshot_readiness

MAX_RADAR_BYTES = 1_000_000
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_radar_evidence(snapshot: dict, radar_dir: Path) -> tuple[str, list[Path]]:
    """Bind exact, decoded radar files to validated snapshot attachments."""
    sources = snapshot.get("sources")
    if not isinstance(sources, dict):
        raise ValidationError("snapshot sources must be an object")
    source = sources.get("radar_mosaic")
    if radar_dir.is_symlink():
        raise ValidationError("radar attachment directory must not be a symlink")
    entries = sorted(radar_dir.iterdir(), key=lambda path: path.name) if radar_dir.exists() else []

    if source is None:
        if entries:
            raise ValidationError("radar files exist without snapshot metadata")
        return "absent", []
    if not isinstance(source, dict) or not source.get("ok"):
        if entries:
            raise ValidationError("radar files exist for an unavailable radar source")
        return "failed", []

    validate_snapshot_readiness(snapshot)
    data = source.get("data")
    if not isinstance(data, dict):
        raise ValidationError("radar attachment metadata must be an object")
    frames = data.get("frames")
    if not isinstance(frames, list) or not 2 <= len(frames) <= 6:
        raise ValidationError("radar attachments require 2..6 metadata frames")
    attachments = [frame.get("attachment") if isinstance(frame, dict) else None for frame in frames]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in attachments):
        raise ValidationError("radar attachment numbers must be integers")
    if attachments != list(range(1, len(frames) + 1)):
        raise ValidationError("radar attachment metadata must be consecutive and ordered")

    expected_names = [f"frame-{attachment:02d}.png" for attachment in attachments]
    actual_names = [entry.name for entry in entries]
    if actual_names != expected_names:
        raise ValidationError(
            f"radar files do not match metadata: expected {expected_names}, found {actual_names}"
        )

    expected_size = data.get("image_size")
    if expected_size != [900, 500]:
        raise ValidationError("radar metadata has unexpected image dimensions")
    paths = []
    for name in expected_names:
        path = radar_dir / name
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"radar attachment is not a regular file: {name}")
        size = path.stat().st_size
        if not 0 < size <= MAX_RADAR_BYTES:
            raise ValidationError(f"radar attachment has invalid size: {name}")
        try:
            width, height, _rows = _decode_rgba_png(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"invalid radar attachment {name}: {exc}") from exc
        if [width, height] != expected_size:
            raise ValidationError(f"radar attachment dimensions do not match metadata: {name}")
        paths.append(path)
    return "ok", paths


def _rename_exchange(first: Path, second: Path) -> bool:
    """Atomically exchange two Linux paths when renameat2 is available."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except AttributeError:
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
        return False
    raise OSError(error, os.strerror(error), str(first), str(second))


def _switch_latest_link(latest: Path, target: Path, run_id: str) -> None:
    temporary_link = latest.parent / f".latest-radar.{run_id}.tmp"
    if os.path.lexists(temporary_link):
        raise FileExistsError(f"temporary radar link already exists: {temporary_link}")
    temporary_link.symlink_to(target.name, target_is_directory=True)
    legacy: Path | None = None
    try:
        if not os.path.lexists(latest) or latest.is_symlink():
            os.replace(temporary_link, latest)
            return
        if not latest.is_dir():
            raise ValidationError("latest-radar exists but is not a directory or symlink")
        if _rename_exchange(temporary_link, latest):
            legacy = temporary_link
        else:
            legacy = latest.parent / f"radar-published.legacy.{run_id}"
            os.replace(latest, legacy)
            try:
                os.replace(temporary_link, latest)
            except Exception:
                os.replace(legacy, latest)
                raise
    finally:
        if temporary_link.is_symlink():
            temporary_link.unlink()
    if legacy is not None and legacy.exists() and not legacy.is_symlink():
        shutil.rmtree(legacy, ignore_errors=True)


def _cleanup_old_publications(var_dir: Path, current: Path) -> None:
    """Best-effort cleanup after the new evidence pointer is committed."""
    try:
        candidates = list(var_dir.glob("radar-published.*"))
    except OSError:
        return
    for candidate in candidates:
        try:
            if candidate != current and candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            continue


def publish_radar_evidence(snapshot: dict, radar_dir: Path, var_dir: Path, run_id: str) -> bool:
    """Publish a complete radar evidence set behind an atomically replaced pointer."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValidationError("invalid radar publication run id")
    state, paths = validate_radar_evidence(snapshot, radar_dir)
    if state != "ok" or not paths:
        return False
    if radar_dir.parent.resolve() != var_dir.resolve():
        raise ValidationError("managed radar directory must be directly under VAR_DIR")

    manifest = {
        "snapshot_collected_at": snapshot["collected_at"],
        "radar_mosaic": snapshot["sources"]["radar_mosaic"],
    }
    manifest_tmp = radar_dir / ".manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(manifest_tmp, radar_dir / "manifest.json")

    target = var_dir / f"radar-published.{run_id}"
    if os.path.lexists(target):
        raise FileExistsError(f"radar publication target already exists: {target}")
    os.replace(radar_dir, target)
    latest = var_dir / "latest-radar"
    try:
        _switch_latest_link(latest, target, run_id)
    except Exception:
        os.replace(target, radar_dir)
        raise

    _cleanup_old_publications(var_dir, target)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("snapshot", type=Path)
    validate_parser.add_argument("radar_dir", type=Path)
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("snapshot", type=Path)
    publish_parser.add_argument("radar_dir", type=Path)
    publish_parser.add_argument("var_dir", type=Path)
    publish_parser.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if args.command == "validate":
            state, paths = validate_radar_evidence(snapshot, args.radar_dir)
            print(state, len(paths))
        else:
            print("published" if publish_radar_evidence(snapshot, args.radar_dir, args.var_dir, args.run_id) else "preserved")
    except (OSError, ValueError, ValidationError) as exc:
        print(f"radar evidence validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
