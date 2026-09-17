"""Bounded, fixed-instant wind snapshot evolution, not independent model cycles.

Archive digests are audit references, not authentication. Persisted history is
shape/provenance checked; current packets are reproduced from primary sources.
Rendering never reads archives or current timing configuration.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .common import iso_z
from .ensemble_trends import AIRPORT, ADVERTISED, BOUND, SOURCE_CHANGE, _event, _grid, _point, _source, _time
from .event_change_evidence import (_archives, _stamp, _source_stamp, _require,
                                    _keys, _number, NAME, ERRORS)
from .event_timing import timing_evidence
from .events import _event as validate_event

LABELS = {'gefs': 'GEFS', 'ecmwf_ens': 'ECMWF ENS', 'aifs_ens': 'AIFS-ENS', 'gfs': 'GFS', 'wn3': 'WN3'}
COLUMNS = ['collected_at', 'fetched_at', 'run_time', 'wind_center', 'wind_p10', 'wind_p90',
           'gust_center', 'gust_p10', 'gust_p90', 'archive']
LOOKBACK = timedelta(hours=36)
MAX_STATES = 5
MAX_BYTES = 24576
NOTES = [
    'Same valid instant, not flight-period maxima. Historical weather at this sample is not historical flight timing.',
    'Previous 36h; latest four distinct value states plus current. Consecutive identical values and duplicate collections are not independent updates.',
    'Rolling advertised runs are not response-bound; WN3 alone has a response-bound run. Snapshot evolution is not a verified initialization trend.',
    'kt; ensemble centers are medians, WN3 mean, GFS deterministic. p10/p90 are marginal quantiles, not ranges or probabilities. Missing gusts are unknown.',
    'Archive SHA-256 is an audit reference, not authentication; bounded scan may omit history.'
]
NOTES_V2 = NOTES[:2] + ['Validated direct-native and WN3 runs are response-bound; rolling advertised runs remain unverified. History with different provider/sampling is omitted.'] + NOTES[3:] + [SOURCE_CHANGE]


def _context(snapshot, now):
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    event = validate_event(snapshot['event'])
    identity, _, rain = _event(event.as_dict())
    stamp = _stamp(snapshot['collected_at'])
    _require(timedelta(0) <= now-stamp <= timedelta(hours=12) and snapshot['airport'] == AIRPORT)
    timing = timing_evidence(snapshot)
    opening = rain[0]-timedelta(hours=1)
    sample = opening+timedelta(hours=max(event.start_hour, min(12, event.end_hour))-event.start_hour)
    kind = 'noon_clamped_to_event'
    if timing and timing.get('flight_end_utc'):
        sample, kind = _time(timing['flight_end_utc']), 'expected_flight_end'
    return identity, stamp, sample, rain, kind


def _statistic(key):
    return 'mean' if key == 'wn3' else 'deterministic' if key == 'gfs' else 'median'


def _validate_row(row, key, clock, current, archives):
    _require(type(row) is list and len(row) == len(COLUMNS))
    p = dict(zip(COLUMNS, row))
    collected = _stamp(p['collected_at'])
    fetched = _source_stamp(p['fetched_at'])
    ttl = timedelta(hours=18 if key == 'wn3' else 12)
    _require(timedelta(0) <= clock-fetched <= ttl and fetched <= collected)
    run = p['run_time']
    _require(run is not None or key != 'wn3')
    if run is not None:
        run = _stamp(run)
        _require(run <= fetched and timedelta(0) <= clock-run <= timedelta(hours=18 if key == 'wn3' else 24))
    for prefix in ('wind', 'gust'):
        center, low, high = [p[prefix+s] for s in ('_center', '_p10', '_p90')]
        for v in (center, low, high):
            if v is not None:
                _number(v, 0, 320)
        if key == 'gfs':
            _require(low is None and high is None)
        else:
            _require(all(v is None for v in (center, low, high)) or all(v is not None for v in (center, low, high)))
            if center is not None:
                _require(low <= high)
                if key != 'wn3':
                    _require(low <= center <= high)
        if prefix == 'gust' and key == 'wn3':
            _require(center is None)
    _require(p['wind_center'] is not None or p['gust_center'] is not None)
    if current:
        _require(p['archive'] is None)
    else:
        index = p['archive']
        _require(type(index) is int and 0 <= index < len(archives))
    return p


def _packet(snapshot, key, clock, sample, rain, archive=None, with_provenance=False) -> Any:
    point = _point(snapshot, key, clock, sample, rain)  # Gate ALL raw fields.
    d = _source(snapshot, key)['data']
    wind = point['metrics']['wind']
    gust = None
    if key != 'wn3':
        h = d['hourly']; i = h['time'].index(iso_z(sample))
        g = h.get('wind_gusts_10m')
        if key == 'gfs':
            gust = {'center': g[i], 'low': None, 'high': None} if g is not None else None
        elif g is not None:
            gust = dict(zip(('center', 'low', 'high'), (g[p][i] for p in ('p50', 'p10', 'p90'))))
    fetched = d['status']['fetched_at'] if key == 'wn3' else d['fetched_at']
    row = [snapshot['collected_at'], fetched, point['run_time']]
    for metric in (wind, gust):
        row.extend([metric[p] for p in ('center', 'low', 'high')] if metric else [None]*3)
    row.append(archive)
    _validate_row(row, key, clock, archive is None, [None]*(archive+1) if archive is not None else [])
    grid = list(_grid(_source(snapshot, key), key))
    _number(grid[0], 39.8752, 41.8752); _number(grid[1], -75.2814, -73.2814)
    return (row, grid, point.get('source_provenance')) if with_provenance else (row, grid)


def build_wind_trends(snapshot, runs_dir, now):
    """Collect independently validated history within bounded archive I/O."""
    try:
        identity, collected, sample, rain, kind = _context(snapshot, now)
        out = {'version': 1, 'event': identity, 'current_collected_at': iso_z(collected),
               'sample_at': iso_z(sample), 'sample_kind': kind, 'unit': 'kt',
               'columns': list(COLUMNS), 'archives': [], 'models': {}, 'notes': list(NOTES)}
        history = []
        for past, audit in _archives(runs_dir, collected, spread_hours=(0,12,24,36,6,18,30)):
            try:
                stamp = _stamp(past['collected_at'])
                _require(timedelta(0) < collected-stamp <= LOOKBACK)
                past_identity, _, _, _, _ = _context(past, stamp)
                _require(past_identity == identity)
                history.append((stamp, past, audit))
            except ERRORS:
                continue
        counts = Counter(stamp for stamp, _, _ in history)
        history = sorted((r for r in history if counts[r[0]] == 1), key=lambda r: r[0])
        # Preserve original archive clocks and hashes, but share audit records.
        audits = {}
        for key, label in LABELS.items():
            try:
                current, grid, direct = _packet(snapshot, key, now, sample, rain, with_provenance=True)
            except ERRORS:
                continue
            states = []
            for stamp, past, audit in history:
                try:
                    index = audits.setdefault((audit['name'], audit['sha256']), len(audits))
                    row, old_grid, old_direct = _packet(past, key, stamp, sample, rain, index, with_provenance=True)
                    _require(old_grid == grid)
                    # Do not label rolling archive rows as native bound cycles.
                    identity = lambda p: {k: v for k, v in p.items() if k != 'initialization_time'} if p else None
                    _require(identity(old_direct) == identity(direct))
                    if states and states[-1][3:9] == row[3:9]:
                        states[-1] = row
                    else:
                        states.append(row)
                except ERRORS:
                    continue
            if states and states[-1][3:9] == current[3:9]:
                states[-1] = current
            else:
                states.append(current)
            out['models'][key] = {'label': label, 'statistic': _statistic(key),
                'run_binding': BOUND if key == 'wn3' or direct else ADVERTISED, 'grid': grid,
                'states': states[-MAX_STATES:]}
            if direct:
                out['models'][key]['source_provenance'] = direct
                out.update(version=2, notes=list(NOTES_V2))
        # Retain only audit records referenced by the capped state lists.
        used = sorted({r[-1] for m in out['models'].values() for r in m['states'] if r[-1] is not None})
        lookup = {old: new for new, old in enumerate(used)}
        entries = list(audits)
        out['archives'] = [dict(zip(('name', 'sha256'), entries[i])) for i in used]
        for model in out['models'].values():
            for row in model['states']:
                if row[-1] is not None:
                    row[-1] = lookup[row[-1]]
        return validate_wind_trends(out, snapshot, now)
    except ERRORS:
        return None


def validate_wind_trends(envelope, snapshot, now):
    """Reproduce current packets; invalid individual models fail locally."""
    try:
        _keys(envelope, ('version', 'event', 'current_collected_at', 'sample_at', 'sample_kind',
                         'unit', 'columns', 'archives', 'models', 'notes'))
        _require(type(envelope['version']) is int and envelope['version'] in (1, 2))
        version = envelope['version']
        _require(len(json.dumps(envelope).encode()) <= MAX_BYTES)
        identity, collected, sample, rain, kind = _context(snapshot, now)
        _require(envelope['event'] == identity and envelope['current_collected_at'] == iso_z(collected))
        _require(envelope['sample_at'] == iso_z(sample) and envelope['sample_kind'] == kind)
        _require(envelope['unit'] == 'kt' and envelope['columns'] == COLUMNS and envelope['notes'] == (NOTES if version == 1 else NOTES_V2))
        archives: list[dict[str, Any]] = envelope['archives']
        _require(type(archives) is list and len(archives) <= 20)
        for audit in archives:
            _keys(audit, ('name', 'sha256'))
            _require(isinstance(audit['name'], str) and NAME.fullmatch(audit['name']))
            _require(isinstance(audit['sha256'], str) and re.fullmatch('[0-9a-f]{64}', audit['sha256']))
        models: dict[str, Any] = envelope['models']
        _require(type(models) is dict and set(models) <= set(LABELS))
        result = copy.deepcopy(envelope); result['models'] = {}
        for key in LABELS:
            if key not in models:
                continue
            model: dict[str, Any] = models[key]
            try:
                fields = {'label', 'statistic', 'run_binding', 'grid', 'states'}
                if version == 2 and 'source_provenance' in model:
                    fields.add('source_provenance')
                _keys(model, fields)
                _require(model['label'] == LABELS[key] and model['statistic'] == _statistic(key))
                current, grid, direct = _packet(snapshot, key, now, sample, rain, with_provenance=True)
                _require(model.get('source_provenance') == direct)
                _require(model['run_binding'] == (BOUND if key == 'wn3' or direct else ADVERTISED))
                _require(model['grid'] == grid)
                states = model['states']
                _require(type(states) is list and 1 <= len(states) <= MAX_STATES)
                previous = None
                for i, row in enumerate(states):
                    stamp = _stamp(row[0])
                    is_current = i == len(states)-1
                    _require(timedelta(0) <= collected-stamp <= LOOKBACK)
                    _require((stamp == collected) == is_current)
                    _validate_row(row, key, now if is_current else stamp, is_current, archives)
                    if direct:
                        _require(row[2] is not None)
                    if previous is not None:
                        _require(previous[0] < stamp and previous[1] != row[3:9])
                    previous = stamp, row[3:9]
                _require(states[-1] == current)
                result['models'][key] = copy.deepcopy(model)
            except ERRORS:
                continue
        return result if result['models'] else None
    except ERRORS:
        return None


def wind_trend_evidence(snapshot, now):
    """Compact narrative projection; validation uses persisted provenance only."""
    valid = validate_wind_trends(snapshot.get('wind_trends'), snapshot, now)
    if valid is None:
        return None
    columns = ['collected_at', 'run_time', 'wind_center', 'wind_p90', 'gust_center', 'gust_p90']
    indices = [COLUMNS.index(k) for k in columns]
    return {'sample_at': valid['sample_at'], 'sample_kind': valid['sample_kind'], 'unit': 'kt',
            'columns': columns, 'models': {key: {'label': m['label'], 'statistic': m['statistic'],
                'run_binding': m['run_binding'], 'states': [[r[i] for i in indices] for r in m['states']]}
                for key, m in valid['models'].items()}, 'notes': list(valid['notes'])}
