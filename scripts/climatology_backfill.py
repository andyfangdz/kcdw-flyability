#!/usr/bin/env python3
"""Daily: build the lead-matched ECMWF and WN3 forecast archives for upcoming events.

The event page ranks each model's current forecast against that model's own
forecasts issued the same number of days ahead. The lead shortens by one day
each day, and a new lead's WN3 archive takes ~260 BigQuery queries, too slow
for the hourly refresh. This job fills today's lead and tomorrow's in advance.
Everything is resumable; WN3 stays inside its cost guard and a time budget.

Usage: scripts/with-runtime.sh python scripts/climatology_backfill.py [--var var] [--minutes 50] [--leads 6 5]
"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kcdw import ecmwf_archive, wn3_climatology  # noqa: E402
from kcdw.common import UTC  # noqa: E402
from kcdw.event_climatology import MODEL_YEARS, lead_days, window  # noqa: E402
from kcdw.event_timing import load_event_timing  # noqa: E402
from kcdw.events import TZ, upcoming_events  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--var', default='var')
    parser.add_argument('--minutes', type=float, default=50)
    parser.add_argument('--leads', type=int, nargs='*', help='override the leads to build (days, 1-7)')
    args = parser.parse_args()
    deadline = time.monotonic() + 60 * args.minutes
    now = datetime.now(UTC)
    today = now.astimezone(TZ).date()
    first, last = today - timedelta(days=365 * MODEL_YEARS), today - timedelta(days=1)
    for event in [e for e in upcoming_events(now) if e.days_out(now) >= 0]:
        snapshot = {'event': event.as_dict(), 'event_timing': load_event_timing(event)}
        win = window(snapshot)
        start, end = win['label'][:5], win['label'][-5:]
        current = lead_days(snapshot, now)
        leads = args.leads or sorted({current, max(1, current - 1)}, reverse=True)
        for lead in leads:
            path = ecmwf_archive.cache_path(args.var, start, lead)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                ecmwf_archive.update(path, start, first, last, now, limit=20, lead_days=lead)
                filled = ecmwf_archive.fill_from_earth_engine(path, start, first, last, now, lead_days=lead)
                print(f'{event.slug} lead {lead}d ECMWF: {len(ecmwf_archive.rows(ecmwf_archive.load(path)))} dates ({filled} from Earth Engine)')
            except Exception as exc:
                print(f'{event.slug} lead {lead}d ECMWF: incomplete ({type(exc).__name__})')
            wpath = wn3_climatology.cache_path(args.var, start, lead)
            wpath.parent.mkdir(parents=True, exist_ok=True)
            try:
                cache = wn3_climatology.update(wpath, start, end, first, last, now, lead_days=lead, deadline=deadline)
                print(f'{event.slug} lead {lead}d WN3: {len(wn3_climatology.rows(cache))} dates; '
                      f'{cache["billed_bytes"] / 1024 ** 3:.2f} GiB billed for this lead so far')
            except Exception as exc:
                print(f'{event.slug} lead {lead}d WN3: stopped ({type(exc).__name__}: {exc})')


if __name__ == '__main__':
    main()
