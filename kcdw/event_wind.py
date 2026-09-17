"""Snapshot-bound supplemental paired wind screens; never aviation readiness.

collect_wind(client, snapshot, now) -> envelope | None
validate_wind(envelope, snapshot, now) -> sanitized envelope | None
wind_evidence(snapshot, now) -> compact evidence | None

Envelope: version, snapshot_collected_at, event{slug,date,window}, window{start,end},
ensembles{gefs,ecmwf_ens,aifs_ens: record|None}, nws:record|None.
Records retain bounded original raw fields and request/fetch identity. Validation
recomputes summary; stored summaries are never authoritative. No archive/config
fallback. Missing timing/duration disables collection, not the parent report.
Evidence: window, ensembles{key:{label,fetched_at,advertised_init,data_end,
likely_init,binding,sustained,direction_at,direction_counts,samples,gust_max,
crosswind:{04,10}}|None}, nws:{issued_at,fetched_at,url,samples}|None, notes.
Sustained is member maximum across opening/hourly/closing samples; samples have
at,sustained,direction_counts. Direction aggregate is closing endpoint only.
Gust maxima use closing hourly endpoints; crosswind uses the SAME member's
same-timestamp mean direction, not actual peak-gust direction. Empty screens
are null, never zero; available screens have n,p50,p90 and ge20/ge25/ge30 or ge15.
All speeds are knots, all directions and runway headings true degrees.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
from typing import Any
from urllib.parse import urlencode

from .common import UTC, iso_z
from .event_timing import timing_evidence

MODELS = {
    'gefs': ('gfs05', 'ncep_gefs05', 31, 'GEFS'),
    'ecmwf_ens': ('ecmwf_ifs025', 'ecmwf_ifs025_ensemble', 51, 'IFS ENS'),
    'aifs_ens': ('ecmwf_aifs025', 'ecmwf_aifs025_ensemble', 51, 'AIFS ENS'),
}
FIELDS = ('wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m')
ENDPOINT = 'https://ensemble-api.open-meteo.com/v1/ensemble'
NWS_URL = 'https://api.weather.gov/gridpoints/OKX/23,48'
HEADINGS = {'04': 30, '10': 83}
ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError, IndexError)
NOTES = [
    'Knots; true degrees. Raw member screens, not pooled/calibrated probabilities or limits.',
    'Gust: closing-hour endpoints. Speed/direction: departure through return; interpolation adds no timing skill.',
    'Crosswind pairs member gust with same-time mean direction, only an approximation of peak-gust direction.',
    'True runway headings 04=030,10=083; availability/NOTAMs unchecked. Null is missing, not calm.',
    'Separate advertised metadata is not per-value run verification. Flight window is approximate.',
]


def _require(ok):
    if not ok:
        raise ValueError('invalid wind source')


def _aware(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)
    return value.astimezone(UTC)


def _time(value):
    _require(isinstance(value, str) and len(value) <= 40)
    return _aware(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _number(v, high: float = 300, nullable=False) -> Any:
    if nullable and v is None:
        return None
    _require(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= high)
    return v


def _fresh(t, now, hours):
    _require(-timedelta(minutes=5) <= now-t <= timedelta(hours=hours))


def _binding(snapshot):
    timing = timing_evidence(snapshot)
    if timing is None or timing.get('flight_duration_minutes') != 120:
        raise ValueError('expected two-hour flight timing required')
    start, end = _time(timing['flight_start_utc']), _time(timing['flight_end_utc'])
    _require(not start.minute and not start.second and end-start == timedelta(hours=2))
    collected = snapshot['collected_at']; _time(collected)
    return {'version': 1, 'snapshot_collected_at': collected,
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'window': {'start': iso_z(start), 'end': iso_z(end)}}


def _axis(binding):
    start = _time(binding['window']['start'])
    return [start+timedelta(hours=i) for i in range(3)]


def _url(spec, binding):
    day = _time(binding['window']['start']).date().isoformat()
    return ENDPOINT + '?' + urlencode(dict(latitude=40.8752, longitude=-74.2814,
        models=spec[0], hourly=','.join(FIELDS), wind_speed_unit='kn',
        timezone='UTC', start_date=day, end_date=day))


def _fetch(record, binding, now):
    fetched = _time(record['fetched_at']); _fresh(fetched, now, 12)
    _require(abs(fetched-_time(binding['snapshot_collected_at'])) <= timedelta(minutes=15))
    return fetched


def _members(raw, spec, binding):
    _require(isinstance(raw, dict))
    for key, target in [('latitude', 40.8752), ('longitude', -74.2814)]:
        value = raw[key]
        _require(type(value) in (int, float) and math.isfinite(value) and abs(value-target) <= .5)
    _require(raw.get('timezone') in ('GMT', 'UTC') and type(raw.get('utc_offset_seconds')) is int and raw['utc_offset_seconds'] == 0)
    for name in ('model', 'model_id'):
        _require(name not in raw or raw[name] == spec[0])
    h, units = raw['hourly'], raw['hourly_units']
    _require(isinstance(h, dict) and isinstance(units, dict) and units.get('time') == 'iso8601')
    start = _axis(binding)[0].replace(hour=0)
    expected = [(start+timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(24)]
    _require(h.get('time') == expected and set(units) == set(h))
    members = {f: {} for f in FIELDS}
    _require(len(h) == 1+len(FIELDS)*spec[2])
    for key, values in h.items():
        if key == 'time':
            continue
        match = re.fullmatch(r'(wind_speed_10m|wind_direction_10m|wind_gusts_10m)(?:_member(\d{2}))?', key)
        if match is None:
            raise ValueError('unknown member field')
        field, member = match[1], int(match[2] or 0)
        _require(member not in members[field] and 0 <= member < spec[2])
        _require(isinstance(values, list) and len(values) == 24)
        unit = '°' if field == FIELDS[1] else 'kn'
        _require(units[key] == unit or (units[key] in (None, 'undefined') and all(v is None for v in values)))
        members[field][member] = [_number(v, 360 if field == FIELDS[1] else 300, True) for v in values]
    _require(all(set(m) == set(range(spec[2])) for m in members.values()))
    return members


def _stats(values, thresholds=()):
    if not values:
        return None
    values = sorted(values)
    def quantile(p):
        x = (len(values)-1)*p
        return round(values[int(x)] + (values[math.ceil(x)]-values[int(x)])*(x-int(x)), 1)
    return {'n': len(values), 'p50': quantile(.5), 'p90': quantile(.9),
            **{f'ge{v}': sum(x >= v for x in values) for v in thresholds}}


def _compass(values):
    counts = {}
    for value in values:
        if value is not None:
            label = ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')[int((value+22.5) % 360 // 45)]
            counts[label] = counts.get(label, 0)+1
    return counts


def _metadata(meta, spec, binding, now):
    if meta is None:
        return None
    _require(meta['url'] == f'https://ensemble-api.open-meteo.com/data/{spec[1]}/static/meta.json')
    fetched = _fetch(meta, binding, now)
    raw = meta['raw']
    times = [datetime.fromtimestamp(_number(raw[k], 1e11), UTC) for k in
             ('last_run_initialisation_time', 'last_run_availability_time', 'data_end_time')]
    init, available, end = times
    _fresh(init, now, 36)
    _require(init <= available <= fetched+timedelta(minutes=5) and available < end <= init+timedelta(days=40))
    _require(type(raw['temporal_resolution_seconds']) is int and raw['temporal_resolution_seconds'] in (3600, 10800, 21600))
    return {'advertised_init': iso_z(init), 'data_end': iso_z(end),
            'advertised_available_at': iso_z(available), 'metadata_fetched_at': iso_z(fetched)}


def _model(record, spec, binding, now):
    _require(record['model_id'] == spec[0] and record['dataset'] == spec[1] and record['url'] == _url(spec, binding))
    _fetch(record, binding, now)
    m = _members(record['raw'], spec, binding)
    axis = _axis(binding); indices = [t.hour for t in axis]
    speeds, directions, gusts = [m[f] for f in FIELDS]
    sustained, maxima = [], []
    cross = {r: [] for r in HEADINGS}
    for member in range(spec[2]):
        vs = [speeds[member][i] for i in indices]
        gs = [gusts[member][i] for i in indices[1:]]
        ds = [directions[member][i] for i in indices[1:]]
        if all(v is not None for v in vs):
            sustained.append(max(vs))
        if all(g is not None for g in gs):
            maxima.append(max(gs))
            if all(d is not None for d in ds):
                for r, heading in HEADINGS.items():
                    cross[r].append(max(g*abs(math.sin(math.radians(d-heading))) for g, d in zip(gs, ds)))
    summary = {'label': spec[3], 'fetched_at': record['fetched_at'],
               'advertised_init': None, 'data_end': None, 'likely_init': None,
               'binding': 'Rolling response; advertised metadata is not response-bound.',
               'sustained': _stats(sustained), 'direction_at': iso_z(axis[-1]),
               'direction_counts': _compass([directions[m][indices[-1]] for m in directions]),
               'samples': [{'at': iso_z(t), 'sustained': _stats([v[i] for v in speeds.values() if v[i] is not None]),
                            'direction_counts': _compass([v[i] for v in directions.values()])} for t, i in zip(axis, indices)],
               'gust_max': _stats(maxima, (20, 25, 30)),
               'crosswind': {r: _stats(v, (15,)) for r, v in cross.items()}}
    clean = deepcopy(record)
    try:
        meta = _metadata(record.get('metadata'), spec, binding, now)
    except ERRORS:
        meta = None
        clean['metadata'] = None
    if meta:
        summary.update(meta)
        init = _time(meta['advertised_init'])
        if (spec[0] == 'ecmwf_ifs025' and init.hour in (6, 18)
                and init.minute == init.second == init.microsecond == 0
                and timedelta(hours=144) <= _time(meta['data_end'])-init <= timedelta(hours=147)
                and _time(meta['data_end']) < axis[0]
                and axis[-1] <= init-timedelta(hours=6)+timedelta(hours=360)):
            summary['likely_init'] = iso_z(init-timedelta(hours=6))
            summary['binding'] = 'Advertised short IFS cycle cannot cover flight; prior long cycle likely, explicitly unverified; not response-bound.'
    clean['summary'] = summary
    return clean


def _interval(value):
    start, duration = value.split('/')
    match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', duration)
    if match is None:
        raise ValueError('invalid interval duration')
    delta = timedelta(days=int(match[1] or 0), hours=int(match[2] or 0), minutes=int(match[3] or 0), seconds=int(match[4] or 0))
    _require(timedelta(0) < delta <= timedelta(days=32))
    start = _time(start)
    return start, start+delta


def _nws(record, binding, now):
    _require(record['url'] == NWS_URL)
    fetched = _fetch(record, binding, now)
    p = record['raw']['properties']
    _require(p.get('@id') == NWS_URL and p['gridId'] == 'OKX' and type(p['gridX']) is int and p['gridX'] == 23 and type(p['gridY']) is int and p['gridY'] == 48)
    issued = _time(p['updateTime']); _fresh(issued, now, 18)
    _require(issued <= fetched+timedelta(minutes=5))
    rows: list[dict[str, Any]] = [{'at': iso_z(t)} for t in _axis(binding)]
    field_intervals={}
    flight_start,flight_end=_axis(binding)[0],_axis(binding)[-1]
    boundaries={flight_start,flight_end}
    for field, name in [('windSpeed', 'wind_kt'), ('windGust', 'gust_kt'), ('windDirection', 'from_deg')]:
        f = p[field]; direction = field == 'windDirection'
        _require(f['uom'] == ('wmoUnit:degree_(angle)' if direction else 'wmoUnit:km_h-1'))
        _require(isinstance(f['values'], list) and 0 < len(f['values']) <= 512)
        intervals = []
        for item in f['values']:
            start, end = _interval(item['validTime'])
            value = _number(item['value'], 360 if direction else 555.6, True)
            _require(not intervals or start >= intervals[-1][1])
            intervals.append((start, end, value))
        for row, at in zip(rows, _axis(binding)):
            matches = [v for start, end, v in intervals if start <= at < end]
            _require(len(matches) == 1)
            row[name] = None if matches[0] is None else round(matches[0]/(1 if direction else 1.852), 1)
        # Require continuous interval coverage of the whole flight, not just samples.
        cursor = _axis(binding)[0]
        for start, end, _ in intervals:
            if start <= cursor < end:
                cursor = end
        _require(cursor > _axis(binding)[-1])
        field_intervals[name]=[(max(a,flight_start),min(b,flight_end),
            None if v is None else round(v/(1 if direction else 1.852),1))
            for a,b,v in intervals if a<flight_end and b>flight_start]
        for a,b,_ in field_intervals[name]:boundaries.update((a,b))
    edges=sorted(boundaries)
    _require(len(edges)<=25)  # Pathological sub-minute products are unavailable, not calm.
    interval_rows=[]
    for a,b in zip(edges,edges[1:]):
        values=[next(v for x,y,v in field_intervals[name] if x<=a and b<=y)
                for name in ('wind_kt','gust_kt','from_deg')]
        interval_rows.append([iso_z(a),iso_z(b),*values])
    clean = deepcopy(record)
    clean['summary'] = {'issued_at': iso_z(issued), 'fetched_at': record['fetched_at'], 'url': NWS_URL, 'samples': rows,
        'interval_columns':['start','end','wind_kt','gust_kt','from_deg'], 'interval_rows':interval_rows,
        'interval_note':'Source intervals intersected with the expected flight; hourly point samples alone can miss intervening hazards.'}
    return clean


def validate_wind(envelope, snapshot, now):
    """Fail outer binding closed; revalidate each independent source from raw."""
    try:
        now = _aware(now); binding = _binding(snapshot)
        _fresh(_time(binding['snapshot_collected_at']), now, 12)
        _require(isinstance(envelope, dict) and all(envelope.get(k) == v for k, v in binding.items()))
        out = deepcopy(binding); out['ensembles'] = {}
        for key, spec in MODELS.items():
            try:
                out['ensembles'][key] = _model(envelope['ensembles'][key], spec, binding, now)
            except ERRORS:
                out['ensembles'][key] = None
        try:
            out['nws'] = _nws(envelope.get('nws'), binding, now)
        except ERRORS:
            out['nws'] = None
        return out
    except ERRORS:
        return None


def collect_wind(client, snapshot, now):
    """Seven bounded public requests; one paired fields request per model.

    Supplied now is the collection clock (same convention as other collectors).
    The HTTP client owns response-byte limits, timeouts and retries.
    """
    try:
        now = _aware(now); out = _binding(snapshot)
        _fresh(_time(out['snapshot_collected_at']), now, .25)
    except ERRORS:
        return None
    out['ensembles'] = {}
    for key, spec in MODELS.items():
        url = _url(spec, out)
        try:
            raw = client.get(url)
            _members(raw, spec, out)
            # Retain only bounded original validation fields, not unused API extras.
            raw = {k: deepcopy(raw[k]) for k in ('latitude', 'longitude', 'timezone', 'utc_offset_seconds', 'hourly', 'hourly_units')}
            record = {'model_id': spec[0], 'dataset': spec[1], 'url': url,
                      'fetched_at': iso_z(now), 'raw': raw, 'metadata': None}
            try:
                meta_url = f'https://ensemble-api.open-meteo.com/data/{spec[1]}/static/meta.json'
                meta = client.get(meta_url)
                record['metadata'] = {'url': meta_url, 'fetched_at': iso_z(now), 'raw': {k: meta[k] for k in
                    ('last_run_initialisation_time', 'last_run_availability_time', 'data_end_time', 'temporal_resolution_seconds')}}
            except Exception:
                pass
            out['ensembles'][key] = record
        except Exception:
            out['ensembles'][key] = None
    try:
        p = client.get(NWS_URL)['properties']
        raw = {'properties': {k: deepcopy(p[k]) for k in ('@id', 'gridId', 'gridX', 'gridY', 'updateTime', 'windSpeed', 'windGust', 'windDirection')}}
        out['nws'] = {'url': NWS_URL, 'fetched_at': iso_z(now), 'raw': raw}
    except Exception:
        out['nws'] = None
    return validate_wind(out, snapshot, now)


def wind_evidence(snapshot, now):
    """Compact, freshly rederived screens; no raw members in narrative input."""
    envelope = validate_wind(snapshot.get('event_wind'), snapshot, now)
    if envelope is None:
        return None
    return {'window': envelope['window'],
            'ensembles': {key: record['summary'] if record else None for key, record in envelope['ensembles'].items()},
            'nws': envelope['nws']['summary'] if envelope['nws'] else None,
            'notes': list(NOTES)}
