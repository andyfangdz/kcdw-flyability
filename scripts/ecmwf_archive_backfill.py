#!/usr/bin/env python3
"""One-time backfill of ECMWF 7-day forecasts for an event's flight window (~3 MB per date).

Usage: scripts/with-runtime.sh python scripts/ecmwf_archive_backfill.py [--var var] [--slug commercial-checkride]
Safe to rerun; only missing dates are fetched.
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kcdw.common import UTC  # noqa: E402
from kcdw.ecmwf_archive import cache_path, rows, update  # noqa: E402
from kcdw.event_timing import load_event_timing  # noqa: E402
from kcdw.events import TZ, find_event  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--var', default='var')
    parser.add_argument('--slug', default='commercial-checkride')
    parser.add_argument('--retry-errors', action='store_true', help='re-read dates that failed, without waiting')
    args = parser.parse_args()
    event = find_event(args.slug)
    timing = load_event_timing(event)
    start = timing['flight_start'] if timing else f'{event.start_hour:02d}:00'
    now = datetime.now(UTC)
    today = now.astimezone(TZ).date()
    path = cache_path(args.var, start)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = update(path, start, today - timedelta(days=730), today - timedelta(days=1), now, retry_errors=args.retry_errors)
    good = rows(cache)
    print(f'{path}: {len(good)} dates with values, {len(cache["days"]) - len(good)} unavailable')


if __name__ == '__main__':
    main()
