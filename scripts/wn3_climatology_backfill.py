#!/usr/bin/env python3
"""One-time backfill of WN3 7-day forecasts for an event's flight window (BigQuery, ~46 MiB per date).

Usage: scripts/with-runtime.sh python scripts/wn3_climatology_backfill.py [--var var] [--slug commercial-checkride]
Safe to rerun; only missing dates are queried. Stops at the module's cost guard.
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kcdw.common import UTC  # noqa: E402
from kcdw.event_timing import load_event_timing  # noqa: E402
from kcdw.events import TZ, find_event  # noqa: E402
from kcdw.wn3_climatology import cache_path, rows, update  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--var', default='var')
    parser.add_argument('--slug', default='commercial-checkride')
    args = parser.parse_args()
    event = find_event(args.slug)
    timing = load_event_timing(event)
    start = timing['flight_start'] if timing else f'{event.start_hour:02d}:00'
    end = timing['flight_end'] if timing and timing.get('flight_end') else f'{event.end_hour:02d}:00'
    now = datetime.now(UTC)
    today = now.astimezone(TZ).date()
    path = cache_path(args.var, start)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = update(path, start, end, today - timedelta(days=730), today - timedelta(days=1), now)
    print(f'{path}: {len(rows(cache))} dates with values; {cache["billed_bytes"] / 1024 ** 3:.2f} GiB billed in total')


if __name__ == '__main__':
    main()
