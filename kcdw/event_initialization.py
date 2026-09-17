"""Bounded, snapshot-only initialization provenance; no collection or archive I/O.

initialization_evidence(snapshot, now) returns six mandatory compact dict rows,
plus at most two separate ensemble RH rows when their metadata differs, and
three independent native wind rows for direct-discovery (v2) snapshots. `binding`
is response-bound, latest-advertised, or unavailable. `init` is qualified by that
binding; `likely_init` is always an explicitly unverified inference. Optional
available_at is upstream publication time, NEVER substituted with fetch time.

IFS policy: recognized 06/18Z cycles are short (144h). ECMWF open-data docs
https://www.ecmwf.int/en/forecasts/datasets/open-data now advertise 360h for
00/12Z; deliberately retain a conservative 240h inference ceiling here. The
exact upstream data_end is preserved (it may include a native-step boundary).
No assertion that metadata binds any rolling value, even inside its horizon.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from .common import UTC, iso_z
from .events import TZ
from .event_moisture import validate_moisture
from .event_moisture_ensemble import validate_moisture_ensemble
from .event_ensemble import MODELS, validate_snapshot, UNITS, ENDPOINT
from .weathernext3 import validate_weather_next3
from .cloud_ceiling import validate_ceiling
from .native_wind import native_wind_evidence
from .source_presentation import is_direct

LABELS = {'wn3': 'WN3', 'gfs_native': 'Native GFS', 'ifs': 'ECMWF IFS',
          'aifs_single': 'AIFS Single', 'ecmwf_ens': 'ECMWF ENS', 'aifs_ens': 'AIFS ENS'}
WIND_LABELS = {'gfs_native_wind': 'Native GFS', 'ifs_native_wind': 'Native IFS',
               'aifs_single_native_wind': 'Native AIFS Single'}
EXTRA_LABELS = {'gfs_chart': 'GFS operational', 'gfs_rh': 'GFS operational',
                'gefs': 'NCEP GEFS', 'geps': 'CMC GEPS'}
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _time(value):
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError('timestamp')
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('timestamp timezone')
    return t.astimezone(UTC)


def _fresh(value, now, hours):
    t = _time(value)
    if not timedelta(0) <= now-t <= timedelta(hours=hours):
        raise ValueError('stale/future')
    return t


def _target(snapshot):
    try:
        event = snapshot['event']
        day = datetime.strptime(event['date'], '%Y-%m-%d').replace(tzinfo=TZ)
        a, b = map(int, event['window'].split('-'))
        if not 0 <= a < b <= 24:
            raise ValueError('window')
        return day+timedelta(hours=a), day+timedelta(hours=b)
    except ERRORS:
        return None


def _advertised(meta, now, fetched, moisture=False):
    init_key, avail_key = (('latest_advertised_init', 'latest_advertised_available_at')
                           if moisture else ('initialization_time', 'availability_time'))
    init = _fresh(meta[init_key], now, 24)
    available, end = _time(meta[avail_key]), _time(meta['data_end_time'])
    if not init <= available <= _time(fetched)+timedelta(minutes=5) or end <= available:
        raise ValueError('metadata chronology')
    return dict(binding='latest-advertised', init=iso_z(init),
                available_at=iso_z(available), data_end=iso_z(end))


def _direct(data, now, collected=None):
    if not is_direct(data):
        raise ValueError('not a direct packet')
    meta = data['metadata']
    init = _fresh(meta['initialization_time'], now, 24)
    fetched = _fresh(data['fetched_at'], now, 12)
    if not init <= fetched <= (_time(collected) if collected else now):
        raise ValueError('direct chronology')
    # A successful retrieval does not establish the upstream publication time.
    return dict(binding='response-bound', init=iso_z(init))


def _ensemble(snapshot, key, now):
    source = snapshot['models'][key]
    if source.get('ok') is not True:
        raise ValueError('unavailable')
    isolated = {k: snapshot[k] for k in ('event', 'range', 'collected_at', 'collection_started_at', 'direct_native_version') if k in snapshot}
    isolated['models'] = {s.key: source if s.key == key else {'ok': False} for s in MODELS}
    validate_snapshot(isolated)
    data = source['data']; spec = next(s for s in MODELS if s.key == key)
    if (data['model_id'] != spec.model_id or (not is_direct(data) and data['endpoint'] != ENDPOINT) or
            data['hourly_units'] != dict(UNITS, time='iso8601 UTC') or
            data.get('explicit_last_good') or source.get('explicit_last_good')):
        raise ValueError('identity')
    _fresh(data['fetched_at'], now, 12)
    _fresh(snapshot['collected_at'], now, 12)
    if is_direct(data):
        return _direct(data, now, snapshot['collected_at'])
    if data['metadata'].get('ok') is not True or data['metadata'].get('fresh') is not True:
        raise ValueError('metadata unavailable')
    return _advertised(data['metadata'], now, data['fetched_at'])


def initialization_evidence(snapshot, now):
    """Bounded allowlisted rows, independently validated, no raw errors."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    scopes = ('point weather', 'native ceiling/low-cloud samples only',
              'RH/height packet only', 'RH/height packet only',
              'primary weather packet', 'primary weather packet')
    rows = [dict(model=key, scope=scope, binding='unavailable')
            for key, scope in zip(LABELS, scopes)]
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return rows
    target = _target(snapshot)
    try:
        moisture = validate_moisture(snapshot.get('event_moisture'), now)['models']
    except ERRORS:
        moisture = {}
    for row in rows:
        key = row['model']
        try:
            if key == 'wn3':
                source = snapshot['weathernext3']
                if source.get('ok') is not True:
                    raise ValueError('unavailable')
                data = source['data']; validate_weather_next3(data, now)
                row.update(binding='response-bound', init=iso_z(_time(data['forecast']['response_init_utc'])))
            elif key == 'gfs_native':
                data = validate_ceiling(snapshot.get('cloud_ceiling'), snapshot, now)
                if not data:
                    raise ValueError('unavailable')
                row.update(binding='response-bound', init=iso_z(_time(data['model_init'])))
            elif key in ('ifs', 'aifs_single'):
                status = moisture[key]
                if not status['available']:
                    raise ValueError('unavailable')
                data = status['data']
                if is_direct(data):
                    row.update(_direct(data, now, snapshot.get('collected_at')))
                    row['scope'] += ' · ' + data['metadata']['source_provider'] + ' direct'
                    continue
                dataset = 'ecmwf_ifs025' if key == 'ifs' else 'ecmwf_aifs025_single'
                metadata = data['metadata']['datasets'][dataset]
                row.update(_advertised(metadata, now, data['fetched_at'], True))
                if key == 'ifs' and target:
                    init, end = _time(row['init']), _time(row['data_end'])
                    previous = init-timedelta(hours=6)
                    # Require the whole target after the exact advertised boundary,
                    # plus actual non-null RH evidence somewhere in that window.
                    has_values = any(target[0] <= _time(t) < target[1] and v is not None
                                     for t, v in zip(data['hourly']['time'], data['hourly']['relative_humidity_2m']))
                    if (init.hour in (6, 18) and init.minute == init.second == 0 and
                            timedelta(hours=144) <= end-init <= timedelta(hours=147) and
                            target[0] > end and target[1] <= previous+timedelta(hours=240) and has_values):
                        row.update(likely_init=iso_z(previous), note='Inferred/unverified preceding long cycle; actual source unknown.')
            else:
                row.update(_ensemble(snapshot, key, now))
                if row['binding'] == 'response-bound':
                    row['scope'] += ' · ' + snapshot['models'][key]['data']['metadata']['source_provider'] + ' direct'
        except ERRORS:
            # Do not leave partially populated current provenance on failure.
            row.clear(); row.update(model=key, scope=scopes[list(LABELS).index(key)], binding='unavailable')
    try:
        rh = validate_moisture_ensemble(snapshot.get('event_moisture_ensemble'), now)['models']
    except ERRORS:
        rh = {}
    for key in ('ecmwf_ens', 'aifs_ens'):
        primary = next(r for r in rows if r['model'] == key)
        try:
            status = rh[key]
            if not status['available']:
                raise ValueError('unavailable')
            data = status['data']
            info = (_direct(data, now, snapshot.get('collected_at')) if is_direct(data) else
                    _advertised(data['metadata'], now, data['fetched_at']))
            if all(primary.get(k) == v for k, v in info.items()):
                primary['scope'] = ('primary + separate RH packet (same verified run)' if is_direct(data) else
                                    'primary + separate RH packet (same advertised metadata)')
            else:
                rows.append(dict(model=key, scope='separate RH packet only', **info))
        except ERRORS:
            pass
    # New source-migration snapshots expose GFS chart/RH and GEFS/GEPS clocks
    # separately; an archive without those packets retains its original rows.
    for key, source_key in (('gfs_chart', 'gfs'),):
        try:
            from .gfs_guidance import validate_gfs
            source = snapshot[source_key]
            if not validate_gfs(source, now)['available'] or not is_direct(source['data']):
                continue
            data = source['data']
            rows.append(dict(model=key, scope='primary weather packet · NOAA direct',
                             **_direct(data, now, snapshot.get('collected_at'))))
        except ERRORS:
            pass
    try:
        status = moisture['gfs']; data = status['data']
        if status['available'] and is_direct(data):
            rows.append(dict(model='gfs_rh', scope='RH/height packet only · NOAA direct',
                             **_direct(data, now, snapshot.get('collected_at'))))
    except ERRORS:
        pass
    for key in ('gefs', 'geps'):
        if not snapshot.get('direct_native_version'):
            continue
        item = dict(model=key, scope='primary weather packet', binding='unavailable')
        try:
            item.update(_ensemble(snapshot, key, now))
            if item['binding'] == 'response-bound':
                item['scope'] += ' · ' + snapshot['models'][key]['data']['metadata']['source_provider'] + ' direct'
        except ERRORS:
            pass
        rows.append(item)
        try:
            status = rh[key]; data = status['data']
            if status['available'] and is_direct(data):
                info = _direct(data, now, snapshot.get('collected_at'))
                if item['binding'] == info['binding'] and item.get('init') == info['init']:
                    item['scope'] = 'primary + separate RH packet (same verified run)'
                else:
                    rows.append(dict(model=key, scope='separate RH packet · direct', **info))
        except ERRORS:
            pass
    packet=snapshot.get('native_wind')
    if isinstance(packet,dict) and packet.get('version')==2:
        native=native_wind_evidence(snapshot,now)
        for key in ('gfs','ifs','aifs_single'):
            provider='NOAA' if key=='gfs' else 'ECMWF'
            item=dict(model=key+'_native_wind',scope=f'native wind samples only · {provider} direct',binding='unavailable')
            source=native.get('models',{}).get(key) if native else None
            if source:
                item.update(binding='response-bound',init=source['init'])
            rows.append(item)
    return rows


def _utc_label(value):
    t = _time(value)
    return t.strftime('%Y-%m-%d %HZ') if not (t.minute or t.second) else t.strftime('%Y-%m-%d %H:%M:%SZ')


def render_initializations(snapshot, now):
    """Semantic table, with horizontal scrolling contained on small screens."""
    parts = ['<section id="model-initializations"><h2>Model initializations</h2>',
             '<p class="small">Latest advertised is not response-bound. Inferred sources remain unverified; '
             'these times do not identify separately fetched low-cloud profiles. Fetch time is not initialization.</p>']
    target = _target(snapshot) if isinstance(snapshot, dict) else None
    if target:
        parts.append('<p class="small">Forecast context: '+escape(target[0].strftime('%Y-%m-%d %H:%M'))+
                     '–'+escape(target[1].strftime('%H:%M %Z'))+'.</p>')
    parts.append('<div class="initialization-scroll" tabindex="0" role="region" aria-label="Model initialization times">'
                 '<table class="initialization-table"><thead><tr>'
                 '<th scope="col">Model</th><th scope="col">Initialization · UTC</th>'
                 '<th scope="col">Source for event</th><th scope="col">Available · Eastern</th>'
                 '<th scope="col">Advertised coverage ends · UTC</th></tr></thead><tbody>')
    for row in initialization_evidence(snapshot, now):
        init, source, available, end = 'Unknown', 'Unavailable / stale / invalid', 'Unknown', 'Unknown'
        if row['binding'] != 'unavailable':
            qualifier = 'Verified response-bound' if row['binding'] == 'response-bound' else 'Latest advertised'
            init = escape(_utc_label(row['init'])) + '<small>' + qualifier + '</small>'
            source = 'Same run · verified' if row['binding'] == 'response-bound' else 'Actual run not verified'
            if row.get('likely_init'):
                source = (escape(_utc_label(row['likely_init'])) + '<small>Inferred/unverified preceding long cycle</small>')
            if row.get('available_at'):
                available = escape(_time(row['available_at']).astimezone(TZ).strftime('%b %d %H:%M %Z'))
            if row.get('data_end'):
                end = escape(_utc_label(row['data_end']))
        parts.append('<tr data-init-model="'+escape(row['model'],quote=True)+'"><th scope="row">'+
                     escape((LABELS | WIND_LABELS | EXTRA_LABELS)[row['model']])+'<small>'+escape(row['scope'])+'</small></th>'+
                     ''.join('<td>'+value+'</td>' for value in (init, source, available, end))+'</tr>')
    return ''.join(parts)+'</tbody></table></div></section>'
