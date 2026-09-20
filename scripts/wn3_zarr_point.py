#!/usr/bin/env python3
"""Read selected KCDW values from an official WeatherNext 3 Zarr run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kcdw.weathernext3_zarr import GrpcStore, WeatherNext3Zarr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, help="synoptic initialization, for example 2026-09-19T12:00:00Z")
    parser.add_argument("--valid", action="append", required=True, help="hourly valid time; repeatable")
    parser.add_argument("--array", action="append", required=True, help="exact statistics array; repeatable")
    parser.add_argument("--project", default=None, help="quota/billing project")
    parser.add_argument("--cache-dir", default="var/wn3-zarr", help="generation/checksum-bound object cache")
    parser.add_argument("--direct-path", action="store_true", help="use only on same-region Google Compute Engine")
    args = parser.parse_args()

    source = WeatherNext3Zarr(
        GrpcStore(project=args.project, direct_path=args.direct_path, cache_dir=args.cache_dir), args.run
    )
    results = [source.point(array, valid) for valid in args.valid for array in args.array]
    print(json.dumps({"results": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
