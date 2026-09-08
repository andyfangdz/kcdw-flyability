from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .collector import _decode_rgba_png
from .validation import ValidationError, validate_snapshot_readiness

MAX_RADAR_BYTES = 1_000_000


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("snapshot", type=Path)
    validate_parser.add_argument("radar_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        state, paths = validate_radar_evidence(snapshot, args.radar_dir)
        print(state, len(paths))
    except (OSError, ValueError, ValidationError) as exc:
        print(f"radar evidence validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
