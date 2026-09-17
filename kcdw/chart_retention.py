"""Retain complete, recent chart packets when a new source loses coverage.

Collection-time archive I/O only. Never splice different runs into one series or
renew a retained packet's clock. Rendering uses only persisted, bound notes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from html import escape

from .common import parse_time
from .events import TZ
from .event_change_evidence import _archives
from .event_ensemble import validate_snapshot, VARIABLES
from .event_moisture_ensemble import validate_moisture_ensemble

MAX_AGE = timedelta(hours=12)
CHART_FIELDS = ('pressure_msl', 'wind_speed_10m', 'precipitation')
RH_FIELDS = ('relative_humidity_2m', 'relative_humidity_850hPa')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _covers(hourly, fields, display, now):
    try:
        start, end = (parse_time(display[k]) for k in ('start', 'end'))
        first = max(start, now.replace(minute=0, second=0, microsecond=0))
        if first < now:
            first += timedelta(hours=1)
        indexes = {parse_time(t): i for i, t in enumerate(hourly['time'])}
        if first >= end:
            return False
        while first < end:
            index = indexes[first]
            if any(hourly[field]['sample_counts'][index] <= 0 for field in fields):
                return False
            first += timedelta(hours=1)
        return True
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def _chart_ok(source, display, now):
    return isinstance(source, dict) and source.get('ok') is True and _covers(source['data']['hourly'], CHART_FIELDS, display, now)


def _rh_ok(envelope, display, now):
    status = validate_moisture_ensemble(envelope, now)
    return all(item['available'] and _covers(item['data']['hourly'], RH_FIELDS, display, now)
               for item in status['models'].values())


def _origin(candidate, scope, model, value, now):
    stamp = parse_time(candidate['collected_at'])
    original = value['collected_at'] if scope == 'event_moisture_ensemble' else candidate['collected_at']
    for row in candidate.get('chart_retention', []):
        if row['scope'] == scope and row['model'] == model:
            if row['sha256'] != digest(value):
                raise ValueError('retained chart binding mismatch')
            original = row['source_collected_at']
            break
    source_time = parse_time(original)
    if not timedelta(0) <= stamp-source_time <= MAX_AGE or not timedelta(0) <= now-source_time <= MAX_AGE:
        raise ValueError('retained chart stale or future')
    return {'scope': scope, 'model': model, 'source_collected_at': original, 'sha256': digest(value)}


def retain_chart_coverage(snapshot, runs_dir, now):
    """Mutate only incomplete chart sources, preserving entire source packets."""
    display = snapshot['range']
    needed = {key for key, source in snapshot['models'].items() if not _chart_ok(source, display, now)}
    native = {key for key, source in snapshot['models'].items()
              if (source.get('data') or {}).get('metadata', {}).get('direct_native') is True}
    need_rh = not _rh_ok(snapshot.get('event_moisture_ensemble'), display, now)
    if not needed and not native and not need_rh:
        return
    clock = parse_time(snapshot.get('collection_started_at', snapshot['collected_at']))
    for candidate, _ in _archives(runs_dir, now):
        try:
            stamp = parse_time(candidate['collected_at'])
            if not timedelta(0) <= now-stamp <= MAX_AGE or stamp > clock:
                continue
            if candidate['event'] != snapshot['event'] or candidate['range'] != display:
                continue
            validate_snapshot(candidate)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        for key in sorted(needed | native):
            try:
                source = candidate['models'][key]
                if not _chart_ok(source, display, now):
                    continue
                if key not in needed:
                    current = snapshot['models'][key]['data']['hourly']
                    previous = source['data']['hourly']
                    if not any(_covers(previous, (field,), display, now) and
                               not _covers(current, (field,), display, now) for field in VARIABLES):
                        continue
                note = _origin(candidate, 'models', key, source, now)
                if source['data'].get('metadata', {}).get('direct_native') is True:
                    from .direct_ensemble import validate_normalized
                    from .event_ensemble import MODELS
                    validate_normalized(source['data'], next(spec for spec in MODELS if spec.key == key), now)
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            snapshot['models'][key] = copy.deepcopy(source)
            snapshot.setdefault('chart_retention', []).append(note)
            needed.discard(key)
            native.discard(key)
        if need_rh:
            try:
                envelope = candidate['event_moisture_ensemble']
                if _rh_ok(envelope, display, now):
                    note = _origin(candidate, 'event_moisture_ensemble', 'all', envelope, now)
                    snapshot['event_moisture_ensemble'] = copy.deepcopy(envelope)
                    snapshot.setdefault('chart_retention', []).append(note)
                    need_rh = False
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
        if not needed and not native and not need_rh:
            break


def retention_evidence(snapshot):
    """Allowlisted, content-bound notes; no archive access at render time."""
    out = []
    for row in snapshot.get('chart_retention', []):
        try:
            scope, model = row['scope'], row['model']
            source = snapshot['models'][model] if scope == 'models' else snapshot['event_moisture_ensemble']
            if scope not in ('models', 'event_moisture_ensemble') or row['sha256'] != digest(source):
                continue
            parse_time(row['source_collected_at'])
            data = source.get('data') or {}
            provider = data.get('metadata', {}).get('source_provider') if data.get('metadata', {}).get('direct_native') else 'Open-Meteo'
            if scope == 'event_moisture_ensemble':
                provider = 'Original per-model sources'
            out.append({'scope': scope, 'model': model, 'source_collected_at': row['source_collected_at'], 'provider': provider or 'Native'})
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return out


def render_retention(snapshot):
    rows = retention_evidence(snapshot)
    if not rows:
        return ''
    labels = []
    for row in rows:
        label = row['model'].upper()+' charts' if row['scope'] == 'models' else 'Ensemble humidity charts'
        stamp = parse_time(row['source_collected_at']).astimezone(TZ).strftime('%b %-d, %H:%M %Z')
        labels.append(escape(f'{label}: {row["provider"]}, {stamp}'))
    return ('<p class="comparison-intro">Full-range data retained from earlier saved snapshots because the new collection was incomplete: '
            + '; '.join(labels) + '. Original source/run attribution and collection clocks are unchanged; these are not new retrievals.</p>')
