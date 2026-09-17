"""Bounded same-event historical snapshot changes for narration only.

Read one directory level, no symlinks, at most 4096 entries/256 files, 32 MiB total
and 6 MiB/file. Directory timestamps prioritize reads only; actual collected_at
controls eligibility. Prefer the most independently comparable source pairs,
then the collection nearest twelve hours earlier (eligible interval 6–18 hours).
No forecast_history or previously persisted change evidence is ever reused.

Compact historical packets retain original fetch/run/grid provenance and the
archive byte digest. The digest is an audit reference, not authentication: the
persisted validator validates historical shape, not an inaccessible raw archive.
Current packets are always recomputed from independently validated raw sources.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timedelta
from typing import Any

from .common import UTC, iso_z
from .ensemble_trends import ADVERTISED, AIRPORT, BOUND, _direct_provenance, _event, _grid, _source, _time
from .events import _event as validate_event

MAX_SCAN_ENTRIES = 4096
MAX_FILES = 256
MAX_READ_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 6 * 1024 * 1024
MAX_ENVELOPE_BYTES = 16_384
MAX_V2_ENVELOPE_BYTES = 24_576  # Native per-pair provenance; narrative remains 60k.
MIN_AGE = timedelta(hours=6)
MAX_AGE = timedelta(hours=18)
TARGET_AGE = timedelta(hours=12)
ALIASES = {'wn3': 'WN3', 'gfs': 'GFS', 'gefs': 'GEFS',
           'ecmwf_ens': 'ECMWF ENS', 'aifs_ens': 'AIFS-ENS'}
RH_ALIASES = {k: v for k, v in ALIASES.items() if k != 'wn3'} | {
    'ifs': 'IFS', 'aifs_single': 'AIFS Single'}
ROLLING = 'rolling; not response-bound'
NOTES = [
    'Historical snapshot changes for the same fixed event window; not observations, independent confirmations, votes or calibrated probabilities.',
    'Wind and pressure are at the fixed noon sample (clamped inside the event window); cloud and RH are morning/noon/afternoon samples. Low cloud and RH are not ceiling probabilities.',
    'Ensembles retain p10/p50/p90; WN3 retains means, which need not lie within p10/p90. WN3 rain sums hourly means without a summed quantile band; ensemble rain is the member-window total distribution.',
    'RH is rolling, not response-bound; changes between collections do not establish an initialization trend. AIFS Single is separate from AIFS-ENS. Missing means unknown; pairs require both sources available.',
    'Within bounded archive reads, prefer the greatest number of comparable source pairs, then nearest 12 hours earlier within 6–18 hours. Archive SHA-256 is an audit reference, not authentication.',
]
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError)
NOTES_V2 = NOTES[:3] + ['Validated direct-native RH and guidance are response-bound; legacy rolling RH remains unverified. AIFS Single is separate from AIFS-ENS. Missing means unknown; pairs require both sources available.'] + NOTES[4:] + ['Source/provider/grid/sampling changes can affect these same-valid-time snapshot differences; inspect each original grid and sampling. Native-versus-rolling comparisons are not pure initialization trends.']
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')


def _require(condition):
    if not condition:
        raise ValueError('invalid snapshot change evidence')


def _keys(value, expected):
    _require(type(value) is dict and set(value) == set(expected))


def _stamp(value):
    stamp = _time(value)
    _require(stamp.tzinfo is not None and iso_z(stamp) == value)
    return stamp


def _source_stamp(value):
    # Keep original fractional fetch precision, rather than silently binding a
    # changed subsecond source clock to the same condensed packet.
    stamp = _time(value)
    canonical = {stamp.isoformat(timespec=p).replace('+00:00', 'Z')
                 for p in ('seconds', 'milliseconds', 'microseconds')}
    _require(value in canonical)
    return stamp


def _encoded(value):
    return json.dumps(value, allow_nan=False, sort_keys=True)


def _context(snapshot, now):
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    event = validate_event(snapshot['event'])
    identity, _, rain_times = _event(event.as_dict())
    collected = _stamp(snapshot['collected_at'])
    _require(timedelta(0) <= now-collected <= timedelta(hours=12))
    _require(snapshot['airport'] == AIRPORT)
    opening = rain_times[0]-timedelta(hours=1)
    noon = opening+timedelta(hours=max(event.start_hour, min(12, event.end_hour-1))-event.start_hour)
    moments = {'morning': iso_z(opening), 'noon': iso_z(noon),
               'afternoon': iso_z(rain_times[-1]-timedelta(hours=1))}
    window = {'start': iso_z(opening), 'end': iso_z(rain_times[-1]), 'samples': moments,
              'instant_sampling': 'start inclusive; end exclusive',
              'rain_sampling': 'preceding-hour totals; opening excluded; closing included'}
    return identity, collected, noon, rain_times, window


def _number(value, low, high):
    _require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def _stats(value, kind, low, high, members=None):
    """Validate roles, missing bands, and median ordering (not mean ordering)."""
    if value is None:
        return
    keys = ('value',) if kind == 'value' else ('mean', 'p10', 'p90') if kind == 'mean' else ('p10', 'p50', 'p90')
    _keys(value, (*keys, 'members') if members else keys)
    values = [value[k] for k in keys]
    for number in values:
        if number is not None:
            _number(number, low, high)
    if kind != 'value':
        lower, upper = value['p10'], value['p90']
        _require((lower is None) == (upper is None))
        if lower is not None:
            _require(lower <= upper)
        if kind == 'p50':
            _require(all(v is None for v in values) or all(v is not None for v in values))
            if lower is not None:
                _require(lower <= value['p50'] <= upper)
        else:
            _require(value['mean'] is not None)
    if members:
        count = value['members']
        _require(type(count) is int and 0 <= count <= members)
        _require((count == 0) == all(v is None for v in values))


def _packet(packet, key, group, clock, hours, version=1):
    provenance = {'fetched_at', 'grid', 'run_binding'}
    direct = None
    if version == 2 and 'source_provenance' in packet:
        provenance.add('source_provenance')
        direct = _direct_provenance({'metadata': packet['source_provenance']})
        if direct is None or direct != packet['source_provenance']:
            raise ValueError('invalid native provenance')
        _require(packet['run_binding'] == BOUND and packet['run_time'] == direct['initialization_time'])
        run = _stamp(packet['run_time'])
        _require(run <= _source_stamp(packet['fetched_at']) and timedelta(0) <= clock-run <= timedelta(hours=24))
        if group == 'moisture':
            provenance.add('run_time')
    if group == 'guidance':
        _keys(packet, provenance | {'run_time', 'pressure', 'wind', 'rain_window', 'low_cloud'})
    else:
        _keys(packet, provenance | {'samples'})
    fetched = _source_stamp(packet['fetched_at'])
    ttl = timedelta(hours=18 if key == 'wn3' else 12)
    _require(timedelta(0) <= clock-fetched <= ttl)
    _keys(packet['grid'], ('latitude', 'longitude'))
    _number(packet['grid']['latitude'], 39.8752, 41.8752)
    _number(packet['grid']['longitude'], -75.2814, -73.2814)
    if group == 'moisture':
        _require(packet['run_binding'] == (BOUND if direct else ROLLING))
        _keys(packet['samples'], ('morning', 'noon', 'afternoon'))
        ensemble = key in ('gefs', 'ecmwf_ens', 'aifs_ens')
        for row in packet['samples'].values():
            _keys(row, ('surface', '925hPa'))
            for value in row.values():
                if ensemble:
                    _stats(value, 'p50', 0, 100, 31 if key == 'gefs' else 51)
                elif value is not None:
                    _number(value, 0, 100)
        return
    _require(packet['run_binding'] == (BOUND if key == 'wn3' or direct else ADVERTISED))
    run = packet['run_time']
    _require(run is not None or key != 'wn3')
    if run is not None:
        run = _stamp(run)
        _require(run <= fetched and timedelta(0) <= clock-run <= timedelta(hours=18 if key == 'wn3' else 24))
    kind = 'mean' if key == 'wn3' else 'value' if key == 'gfs' else 'p50'
    for field, unit, low, high in (('pressure', 'hPa', 750, 1150), ('wind', 'kt', 0, 320),
                                  ('rain_window', 'mm', 0, 1500*hours)):
        metric = packet[field]
        if metric is None:
            continue
        _keys(metric, ('unit', kind, 'p10', 'p90'))
        _require(metric['unit'] == unit)
        _require(metric[kind] is not None)
        if kind == 'value':
            _require(metric['p10'] is None and metric['p90'] is None)
            _stats({'value': metric['value']}, kind, low, high)
        else:
            _stats({k: v for k, v in metric.items() if k != 'unit'}, kind, low, high)
        if key == 'wn3' and field == 'rain_window':
            _require(metric['p10'] is None and metric['p90'] is None)
        elif key == 'wn3':
            _require(metric['p10'] is not None and metric['p90'] is not None)
    cloud = packet['low_cloud']
    if key == 'wn3':
        _require(cloud is None)
    else:
        _keys(cloud, ('morning', 'noon', 'afternoon'))
        for value in cloud.values():
            _require(type(value) is dict)
            _stats(value, kind, 0, 100)


def _packets(snapshot, clock, sample, rain_times, window):
    # Lazy imports avoid the narrative evidence -> changes -> evidence cycle.
    from .event_narrative_evidence import _current
    from .event_moisture_view import moisture_evidence
    result = {'guidance': {}, 'moisture': {}}
    for key in ALIASES:
        try:
            raw = _current(snapshot, key, clock, sample, rain_times)
            data = _source(snapshot, key)['data']
            fetched = data['status']['fetched_at'] if key == 'wn3' else data['fetched_at']
            lat, lon = _grid(_source(snapshot, key), key)
            packet = {k: copy.deepcopy(raw[k]) for k in ('run_time', 'run_binding', 'pressure', 'wind', 'rain_window')}
            packet.update(fetched_at=fetched, grid={'latitude': lat, 'longitude': lon}, low_cloud=None)
            if 'source_provenance' in raw:
                packet['source_provenance'] = copy.deepcopy(raw['source_provenance'])
            if key == 'gfs':
                packet['low_cloud'] = {label: {'value': row['value']} for label, row in zip(window['samples'], raw['low_cloud']['samples'])}
            elif key != 'wn3':
                packet['low_cloud'] = {label: {p: raw['low_cloud'][old][p] for p in ('p10', 'p50', 'p90')}
                                       for label, old in (('morning', 'morning'), ('noon', 'midday'), ('afternoon', 'afternoon'))}
            _packet(packet, key, 'guidance', clock, len(rain_times), version=2)
            result['guidance'][key] = packet
        except ERRORS:
            continue
    # Isolate malformed RH families/models before calling the existing validator;
    # a corrupt deterministic family must not suppress independent ensemble RH.
    for key in RH_ALIASES:
        try:
            family = 'event_moisture_ensemble' if key in ('gefs', 'ecmwf_ens', 'aifs_ens') else 'event_moisture'
            envelope = snapshot[family]
            isolated = {'event': snapshot['event'], family: dict(envelope, models={key: envelope['models'][key]})}
            row = next(m for m in moisture_evidence(isolated, clock)['models'] if m['key'] == key)
            _require(row['available'] is True)
            data = envelope['models'][key]['data']
            grid = data['grid_point']
            axis = {r['at']: r for r in row['samples']}
            packet = {'fetched_at': row['fetched_at'], 'run_binding': ROLLING,
                      'grid': {k: grid[k] for k in ('latitude', 'longitude')}, 'samples': {}}
            # moisture_evidence above has already run the family's validator.
            direct = _direct_provenance(data)
            if direct:
                packet.update(source_provenance=direct, run_binding=BOUND, run_time=direct['initialization_time'])
            for label, at in window['samples'].items():
                r = axis[at]
                packet['samples'][label] = {'surface': copy.deepcopy(r['surface_RH_percent']),
                    '925hPa': copy.deepcopy(next(v['RH_percent'] for v in r['levels'] if v['pressure_hPa'] == 925))}
            _packet(packet, key, 'moisture', clock, len(rain_times), version=2)
            result['moisture'][key] = packet
        except (*ERRORS, StopIteration):
            continue
    return result


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _archives(root, target, *, spread_hours=()):
    """Bound scan AND bytes, and refuse symlink/FIFO/directory substitution."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root_fd = os.open(root, flags | os.O_DIRECTORY)
    except (OSError, TypeError):
        return
    try:
        names = []
        with os.scandir(root_fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_SCAN_ENTRIES:
                    return  # No arbitrary subset of an overfull directory.
                if NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
        def priority(name):
            try:
                hint = datetime.strptime(name[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
                return (abs(hint-target), name)
            except ValueError:
                return (timedelta(0), name)
        ordered = sorted(names, key=priority)
        if spread_hours:
            # Reserve age-spread candidates before dense recent rerenders consume
            # the unchanged byte budget. Filename clocks only prioritize reads;
            # consumers still validate actual collection times and identity.
            anchors = [target-timedelta(hours=h) for h in spread_hours]
            buckets = [[] for _ in anchors]
            unknown = []
            for name in names:
                try:
                    hint = datetime.strptime(name[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
                    if not min(anchors) <= hint <= max(anchors):
                        unknown.append(name)
                        continue
                    distance, index = min((abs(hint-at), i) for i, at in enumerate(anchors))
                    buckets[index].append((distance,name))
                except ValueError:
                    unknown.append(name)
            ranked = [(rank, i, name) for i,bucket in enumerate(buckets)
                      for rank,(_,name) in enumerate(sorted(bucket))]
            ordered = [name for _,_,name in sorted(ranked)] + sorted(unknown,key=priority)
        budget = MAX_READ_BYTES
        for name in ordered[:MAX_FILES]:
            try:
                directory_fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=root_fd)
                try:
                    fd = os.open('snapshot.json', flags, dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
                with os.fdopen(fd, 'rb') as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES or info.st_size >= budget:
                        continue
                    raw = file.read(min(MAX_FILE_BYTES+1, budget))
                    budget -= len(raw)
                if len(raw) > MAX_FILE_BYTES or not raw:
                    continue
                data = json.loads(raw, object_pairs_hook=_no_duplicates,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                if type(data) is dict:
                    yield data, {'name': name, 'sha256': hashlib.sha256(raw).hexdigest()}
            except (OSError, UnicodeError, *ERRORS):
                continue
    except OSError:
        return
    finally:
        os.close(root_fd)


def build_event_changes(snapshot, runs_dir, now) -> dict | None:
    """Build one detached comparison, or None when no eligible pairs exist."""
    try:
        identity, collected, sample, rain_times, window = _context(snapshot, now)
        current = _packets(snapshot, now, sample, rain_times, window)
        if not any(current.values()):
            return None
        candidates, counts = [], {}
        for previous, provenance in _archives(runs_dir, collected-TARGET_AGE):
            try:
                stamp = _stamp(previous['collected_at'])
                _require(MIN_AGE <= collected-stamp <= MAX_AGE)
                past_identity, _, _, _, past_window = _context(previous, stamp)
                _require(past_identity == identity and past_window == window)
                counts[stamp] = counts.get(stamp, 0)+1
                packets = _packets(previous, stamp, sample, rain_times, window)
                paired = {}
                for group, aliases in (('guidance', ALIASES), ('moisture', RH_ALIASES)):
                    paired[group] = {key: {'alias': aliases[key], 'previous': packet, 'current': current[group][key]}
                                     for key, packet in packets[group].items() if key in current[group]
                                     and (packet['grid'] == current[group][key]['grid']
                                          or 'source_provenance' in packet or 'source_provenance' in current[group][key])}
                coverage = sum(len(pairs) for pairs in paired.values())
                if coverage:
                    candidates.append((coverage, stamp, provenance, paired))
            except ERRORS:
                continue
        candidates = [row for row in candidates if counts[row[1]] == 1]
        if not candidates:
            return None
        _, stamp, provenance, paired = min(candidates, key=lambda row: (-row[0], abs((collected-row[1])-TARGET_AGE), row[1]))
        result = {'version': 1, 'kind': 'historical_snapshot_change', 'event': identity,
                  'current_collected_at': iso_z(collected), 'previous_collected_at': iso_z(stamp),
                  'window': window, 'archive': provenance, **paired, 'notes': list(NOTES)}
        if any('source_provenance' in pair[side] for group in paired.values() for pair in group.values() for side in ('previous', 'current')):
            result.update(version=2, notes=list(NOTES_V2))
        return validate_event_changes(result, snapshot, now)
    except ERRORS:
        return None


def validate_event_changes(envelope, snapshot, now) -> dict | None:
    """Strict persisted contract; return a detached validated packet or None.

    Unknown fields, malformed history, changed clocks/windows, or a current
    packet not exactly reproducible at assessment time invalidate the envelope.
    The builder independently skips unavailable sources, never substitutes old
    values for failed current guidance and never mutates narrative/quorum state.
    """
    try:
        _keys(envelope, ('version', 'kind', 'event', 'current_collected_at',
                         'previous_collected_at', 'window', 'archive', 'guidance', 'moisture', 'notes'))
        _require(type(envelope['version']) is int and envelope['version'] in (1, 2))
        version = envelope['version']
        _require(envelope['kind'] == 'historical_snapshot_change' and envelope['notes'] == (NOTES if version == 1 else NOTES_V2))
        _require(len(_encoded(envelope).encode()) <= (MAX_ENVELOPE_BYTES if version == 1 else MAX_V2_ENVELOPE_BYTES))
        identity, collected, sample, rain_times, window = _context(snapshot, now)
        _require(_encoded(envelope['event']) == _encoded(identity) and _encoded(envelope['window']) == _encoded(window))
        _require(envelope['current_collected_at'] == iso_z(collected))
        past = _stamp(envelope['previous_collected_at'])
        _require(MIN_AGE <= collected-past <= MAX_AGE and past <= now)
        _keys(envelope['archive'], ('name', 'sha256'))
        _require(isinstance(envelope['archive']['name'], str) and NAME.fullmatch(envelope['archive']['name']))
        _require(isinstance(envelope['archive']['sha256'], str) and re.fullmatch('[0-9a-f]{64}', envelope['archive']['sha256']))
        current = _packets(snapshot, now, sample, rain_times, window)
        count = 0
        for group, aliases in (('guidance', ALIASES), ('moisture', RH_ALIASES)):
            pairs: dict[str, Any] = envelope[group]
            _require(type(pairs) is dict and set(pairs) <= set(aliases))
            for key in aliases:
                if key not in pairs:
                    continue
                pair: dict[str, Any] = pairs[key]
                _keys(pair, ('alias', 'previous', 'current'))
                _require(pair['alias'] == aliases[key])
                _packet(pair['previous'], key, group, past, len(rain_times), version)
                _packet(pair['current'], key, group, now, len(rain_times), version)
                _require(pair['previous']['grid'] == pair['current']['grid']
                         or version == 2 and ('source_provenance' in pair['previous'] or 'source_provenance' in pair['current']))
                _require(key in current[group] and _encoded(pair['current']) == _encoded(current[group][key]))
                count += 1
        _require(count > 0)
        return copy.deepcopy(envelope)
    except ERRORS:
        return None
