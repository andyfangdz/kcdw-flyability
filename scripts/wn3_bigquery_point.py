#!/usr/bin/env python3
"""Bounded, opt-in WN3 BigQuery experiment; does not publish forecasts.

Uses google-auth/requests from the existing runtime. Default is a dry run.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kcdw.weathernext3 import FIELD_SPECS, STATISTICS
from kcdw.weathernext3_zarr import KCDW, parse_utc, run_name

DEFAULT_TABLE = "866962084172.WeatherNext_3.weathernext_3_0_0_0p1deg"
ARRAYS = {f"{s.array}_{stat}" for s in FIELD_SPECS.values() for stat in STATISTICS}


from kcdw.weathernext3_bigquery import request_body, validate_rows, query


def compare_zarr(rows, arrays, run, cache_dir):
    from kcdw.weathernext3_zarr import GrpcStore, WeatherNext3Zarr
    source = WeatherNext3Zarr(GrpcStore(cache_dir=cache_dir), run)
    comparisons = []
    for row in rows:
        for array in arrays:
            point = source.point(array, row["valid_time"])
            spec = next(s for s in FIELD_SPECS.values() if any(array == f"{s.array}_{stat}" for stat in STATISTICS))
            if point["unit"] != spec.source_unit:
                raise ValueError("unexpected Zarr source unit")
            grid = point["grid_point"]
            # Zarr's float32 0..360 longitude can differ from BigQuery's
            # decimal center by ~1.5e-5 degrees after wrapping to -180..180.
            # 5e-5 degrees accommodates rounding, far below a 0.1-degree cell.
            if abs(grid["latitude"]-row["latitude"]) > 5e-5 or abs(grid["longitude"]-row["longitude"]) > 5e-5:
                raise ValueError("BigQuery/Zarr grid mismatch")
            a, b = row[array], point["value"]
            comparisons.append(dict(valid_time=row["valid_time"], array=array,
                                    bigquery=a, zarr=b, absolute_difference=abs(a-b),
                                    matches=math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8)))
    return {"all_match": all(c["matches"] for c in comparisons), "comparisons": comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="BigQuery job/billing project")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--run", required=True)
    parser.add_argument("--valid", action="append", required=True)
    parser.add_argument("--array", action="append", choices=sorted(ARRAYS), help="default: all 39 current columns")
    parser.add_argument("--maximum-bytes-billed", type=int, default=1024**3)
    parser.add_argument("--execute", action="store_true", help="execute with byte cap; default is dry run")
    parser.add_argument("--compare-zarr", action="store_true", help="download matching global planes for parity")
    parser.add_argument("--cache-dir", default="var/wn3-zarr")
    args = parser.parse_args()
    if args.compare_zarr and not args.execute:
        parser.error("--compare-zarr requires --execute")
    arrays = sorted(set(args.array or ARRAYS))
    body = request_body(args.table, args.run, args.valid, arrays, args.maximum_bytes_billed, args.execute)
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    with AuthorizedSession(credentials) as session:
        result = query(session, args.project, body)
    result.update(table=args.table, run=args.run, arrays=arrays)
    if args.execute:
        validate_rows(result["rows"], args.run, args.valid, arrays)
        if args.compare_zarr:
            result["parity"] = compare_zarr(result["rows"], arrays, args.run, args.cache_dir)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    if result.get("parity", {}).get("all_match") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
