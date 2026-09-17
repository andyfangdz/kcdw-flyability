"""Hourly initialization-history maintenance; publish only changed or pending runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path

from .common import UTC, atomic_write, iso_z
from .event_update import update as update_forecasts
from .events import upcoming_events


def _publication_state(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                return {}
            data = json.loads(handle.read(65537))
        if not isinstance(data, dict) or not isinstance(data.get('events'), dict):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def update(var: Path, cloud_config: Path | None, now: datetime | None = None,
           events_path: Path | None = None, *, refresh=None) -> int:
    automatic_clock = now is None
    now = now or datetime.now(UTC)
    if refresh is None:
        from .run_history_update import refresh_run_history
        refresh = refresh_run_history
    root = var/'events'
    root.mkdir(parents=True, exist_ok=True)

    def record(message):
        with (root/'run-history-update.log').open('a', encoding='utf-8') as handle:
            handle.write(f'{iso_z(datetime.now(UTC))} {message}\n')

    state_path = root/'run-history-publication.json'
    state = _publication_state(state_path)
    mode = 'cloud' if cloud_config else 'local'
    previous = state.get('events', {}) if state.get('mode') == mode else {}
    pending = dict(previous)
    changed = False
    failures = 0
    for event in upcoming_events(now, events_path):
        if event.days_out(now) < 0:
            continue
        try:
            history = refresh(root/event.slug, event.as_dict(), now)
            if not history or not history.get('points'):
                record(f'event={event.slug} history=unavailable')
                continue
            digest = hashlib.sha256(json.dumps(history, sort_keys=True, allow_nan=False).encode()).hexdigest()
            pending[event.slug] = digest
            changed |= previous.get(event.slug) != digest
            record(f'event={event.slug} history=checked points={len(history["points"])}')
        except Exception as exc:
            failures += 1
            record(f'event={event.slug} history=failed error={type(exc).__name__}')
    if changed:
        # Recollect the current forecast rather than backdating a history-only
        # render or creating duplicate fetch-time snapshots. Both jobs share the
        # deployment's events-update.lock. Failed publication remains pending.
        try:
            published = update_forecasts(var, cloud_config,
                                         datetime.now(UTC) if automatic_clock else now,
                                         events_path)
            if published:
                failures += 1
                record('publication=failed retry=next-poll')
            else:
                atomic_write(state_path, json.dumps({'version': 1, 'mode': mode,
                             'events': pending, 'published_at': iso_z(datetime.now(UTC))},
                             sort_keys=True) + '\n')
                record('publication=success')
        except Exception as exc:
            failures += 1
            record(f'publication=failed error={type(exc).__name__} retry=next-poll')
    else:
        record('publication=unchanged')
    return int(bool(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--var', type=Path, default=Path('var'))
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('CLOUD_PUBLISH_CONFIG', 'var/cloudflare.json')))
    parser.add_argument('--no-publish', action='store_true')
    args = parser.parse_args(argv)
    return update(args.var, None if args.no_publish else args.config)


if __name__ == '__main__':
    raise SystemExit(main())
