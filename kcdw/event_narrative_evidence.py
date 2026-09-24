"""Internal, bounded evidence for event narration; never pass whole snapshots.

Current sources are independently revalidated at assessment time. Historical
initializations remain historical even when today's corresponding source fails.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta

from .common import UTC, iso_z
from .events import _event as validate_event
from .ensemble_trends import _event, _point, _source, _time
from .event_ensemble import MODELS
from .run_history import validate_run_history
from .synoptic_context import context_evidence, _Text
from .tropical_guidance import validate_wn3_cyclones

MAX_BYTES = 60_000
# Restored paired changes and flight-window wind need more room. The observe-only
# TypeSafe change review sends the full packet: ~29k input tokens at this size.
PRIORITY_MAX_BYTES = 72_000
MODEL_URL = 'https://open-meteo.com/en/docs/ensemble-api'
WEATHERLAB_URL = 'https://storage.googleapis.com/weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/'
SOURCES = {s.key: (s.name, MODEL_URL) for s in MODELS} | {
    'wn3_point': ('WeatherNext 3 point guidance', 'https://developers.google.com/weathernext/guides/bigquery'),
    'gfs': ('GFS operational (deterministic)', 'https://open-meteo.com/en/docs/gfs-api'),
    'nhc': ('National Hurricane Center', 'https://www.nhc.noaa.gov/'),
    'cpc_wpc': ('CPC / WPC official outlooks', 'https://www.cpc.ncep.noaa.gov/products/predictions/610day/'),
    'wn3_cyclones': ('WeatherNext 3 experimental cyclones', WEATHERLAB_URL),
    'run_history': ('Initialization history', 'https://developers.google.com/weathernext/guides/bigquery'),
    'low_level_rh': ('Surface and low-level humidity', MODEL_URL),
    'low_cloud_analysis': ('WN3 cloud timing and uncertainty; native ceiling and independent member persistence', MODEL_URL),
    'snapshot_changes': ('Changes since the previous forecast', MODEL_URL),
}
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)
# Least to most important when whole sources must be omitted for size.
# Unlisted sources go first; protected cloud/RH/change sources are never listed.
EVICTION_ORDER = ('cpc_wpc', 'nhc', 'wn3_cyclones', 'wn3_hourly', 'geps', 'aifs_ens', 'gfs',
                  'gefs', 'ecmwf_ens', 'run_history', 'wind_native', 'synoptic_pattern', 'week_ahead', 'afd_aly', 'wind_trends',
                  'wn3_point', 'afd_phi', 'wind_deterministic', 'afd_okx', 'wind_surface')


def _text(value, limit=6000):
    if not isinstance(value, str):
        return ''
    parser = _Text()
    parser.feed(value[:30000])
    text = ' '.join(' '.join(parser.parts).split())
    # Public narratives have no reason to carry bearer tokens or local paths.
    text = re.sub(r'https?://\S+|(?:/home/|/tmp/|/etc/|file:|Bearer\s+)\S+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*\S+', '[redacted]', text, flags=re.I)
    return text[:limit]


def _metric(value, unit, statistic):
    return {'unit': unit, statistic: value['center'], 'p10': value['low'], 'p90': value['high']} if value else None


def _quantiles(fan, index, at):
    return {'at': iso_z(at), **{p: fan[p][index] for p in ('p10', 'p50', 'p90')}}


def _current(snapshot, key, now, sample, rain_times):
    point = _point(snapshot, key, now, sample, rain_times)
    start, end = rain_times[0]-timedelta(hours=1), rain_times[-1]
    statistic = 'mean' if key == 'wn3' else 'value' if key == 'gfs' else 'p50'
    d = _source(snapshot, key)['data']
    result = {
        'is_current': True, 'statistic': 'deterministic' if key == 'gfs' else 'mean' if key == 'wn3' else 'median',
        'run_time': point['run_time'], 'run_binding': point['run_binding'],
        'sample_time': iso_z(sample), 'collected_at': point['collected_at'],
        'event_window': {'start': iso_z(start), 'end': iso_z(end),
                         'instant_sampling': 'start inclusive; end exclusive',
                         'rain_sampling': 'preceding-hour totals; opening excluded; closing included'},
        'pressure': _metric(point['metrics']['pressure'], 'hPa', statistic),
        'wind': _metric(point['metrics']['wind'], 'kt', statistic),
        'rain_window': _metric(point['metrics']['rain'], 'mm', statistic),
    }
    instant_times = [t-timedelta(hours=1) for t in rain_times]
    if 'source_provenance' in point:
        result['source_provenance'] = point['source_provenance']
    f, h = d.get('forecast', {}), d.get('hourly', {})
    if key == 'wn3':
        f = d['forecast']; times = [_time(t) for t in f['valid_time_utc']]
        fields = [('pressure', 'sea_level_pressure', .01), ('wind', 'wind_speed_10m', 3600/1852),
                  ('low_cloud', 'low_cloud_cover', 1), ('dewpoint', 'dewpoint_temperature_2m', 1)]
        result['event_samples'] = [dict(at=iso_z(t), **{name: {p: round(f['fields'][field][p][times.index(t)]*scale, 4) for p in ('mean', 'p10', 'p90')} for name, field, scale in fields}) for t in instant_times]
        result['low_cloud'] = {'unit': '%', 'meaning': 'Low-cloud fraction, not ceiling height.',
            'samples': [{'at': iso_z(t), **{p: f['fields']['low_cloud_cover'][p][times.index(t)] for p in ('mean', 'p10', 'p90')}}
                        for t in instant_times]}
        result['missing_fields'] = ['ceiling', 'visibility', 'wind_direction_distribution', 'gust', 'convection']
        result['rain_note'] = 'Sum of hourly means; hourly marginal quantiles cannot form a window-total band.'
    else:
        h = d['hourly']; times = [_time(t) for t in h['time']]
        if key == 'gfs':
            result['event_samples'] = [dict(at=iso_z(t), **{name: h[field][times.index(t)] for name, field in (('pressure', 'pressure_msl'), ('wind', 'wind_speed_10m'))}) for t in instant_times]
            result['low_cloud'] = {'unit': '%', 'statistic': 'deterministic', 'samples': [{'at': iso_z(t), 'value': h['cloud_cover_low'][times.index(t)]} for t in (start, sample, end-timedelta(hours=1))]}
        else:
            result['event_samples'] = [dict(at=iso_z(t), **{name: {p: h[field][p][times.index(t)] for p in ('p10', 'p50', 'p90')} for name, field in (('pressure', 'pressure_msl'), ('wind', 'wind_speed_10m'))}) for t in instant_times]
            cloud = h['cloud_cover_low']
            result['low_cloud'] = {'unit': '%', 'meaning': 'Low-cloud fraction, not ceiling probability.',
                **{label: _quantiles(cloud, times.index(t), t) for label, t in (('morning', start), ('midday', sample), ('afternoon', end-timedelta(hours=1)))}}
            w = d['window']['low_cloud_mean_percent']
            if w is not None and any(not isinstance(w[k], (int, float)) or isinstance(w[k], bool) or not math.isfinite(w[k]) or not 0 <= w[k] <= 100 for k in ('min', 'p10', 'median', 'p90', 'max')):
                raise ValueError('cloud window bounds')
            result['low_cloud']['window_mean'] = None if w is None else {'p10': w['p10'], 'p50': w['median'], 'p90': w['p90'], 'members': w['count']}
            result['low_cloud']['window_mean_sampling'] = 'Member means over opening-excluded, closing-included samples; quantiles across members, not mean hourly quantiles.'
    result['rain_intervals'] = []
    for at in rain_times:
        i = times.index(at)
        if key == 'wn3':
            rain = f['fields']['precipitation_1h']
            values = {p: rain[p][i] for p in ('mean', 'p10', 'p90')}
        elif key == 'gfs':
            values = {'value': h['precipitation'][i]}
        else:
            rain = h.get('precipitation')
            values = {p: rain[p][i] if rain else None for p in ('p10', 'p50', 'p90')}
        result['rain_intervals'].append({'start': iso_z(at-timedelta(hours=1)), 'end': iso_z(at), 'unit': 'mm', **values})
    return result


def _official(context, key, start, end, now):
    selected = context_evidence({key: context.get(key, {})}, start, end, now)
    providers = ('nhc',) if key == 'nhc' else ('cpc', 'wpc')
    products = []
    for unit in selected['products']:
        if unit.get('source') not in providers:
            continue
        clean: dict = {k: _text(unit[k], 300) for k in ('source', 'id', 'title', 'status', 'coverage', 'issue_precision', 'scope', 'category', 'probability_label') if k in unit}
        for field in ('issued_at', 'valid_start', 'valid_end', 'feed_refreshed_at'):
            if unit.get(field):
                try:
                    clean[field] = iso_z(_time(unit[field]))
                except ERRORS:
                    pass
        if unit.get('status') == 'current':
            clean['text'] = _text(unit.get('text', ''), 6000)
            if key == 'extended':
                # context_evidence validates product dates/overlap but caps all
                # excerpts at 700. Keep a longer *same validated product* excerpt.
                provider = unit['source']
                raw_products = context[key]['data'][provider]['products']
                matches = [p for p in raw_products[:16] if isinstance(p, dict) and all(p.get(k) == unit.get(k) for k in ('id', 'issued_at', 'valid_start', 'valid_end'))]
                if len(matches) == 1:
                    clean['text'] = _text(matches[0].get('excerpt', ''), 6000 if provider == 'cpc' else 3000)
                cats = unit.get('regional_categories')
                if isinstance(cats, dict):
                    clean['regional_categories'] = {k: cats[k] for k in ('temperature', 'precipitation') if cats.get(k) in ('A', 'N', 'B')}
            if key == 'nhc':
                if type(unit.get('atlantic_count')) is int and 0 <= unit['atlantic_count'] <= 100:
                    clean['atlantic_count'] = unit['atlantic_count']
                if 'no_formation_expected' in unit:
                    clean['no_formation_expected'] = unit['no_formation_expected'] is True
                clean['areas'] = [{k: _text(a.get(k), 600) for k in ('narrative', 'chance_48h', 'chance_7day')} for a in unit.get('areas', [])[:8] if isinstance(a, dict)]
        products.append(clean)
    return {'window_start': iso_z(start), 'window_end': iso_z(end), 'products': products}


def low_cloud_evidence(snapshot, now):
    from .low_cloud_analysis import narrative_cloud_evidence as build
    return build(snapshot, now)


def validated_afds(snapshot, now):
    from .event_afd_view import validated_afds as collect
    return collect(snapshot, now)


def validated_event_changes(snapshot, now):
    if snapshot.get('event_changes') is None:
        return None
    from .event_change_evidence import validate_event_changes
    return validate_event_changes(snapshot['event_changes'], snapshot, now)


def initialization_evidence(snapshot, now):
    from .event_initialization import initialization_evidence as collect
    return collect(snapshot, now)


def build_event_evidence(snapshot, now) -> dict:
    """Return <=60k JSON bytes, known aliases only, no raw arrays or errors.

    Invalid event identity or naive assessment time raises ValueError. Bad
    individual providers fail closed without suppressing independent evidence.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware assessment time required')
    event = validate_event({k: snapshot['event'][k] for k in ('slug', 'title', 'date', 'window', 'nav_label', 'description')})
    _, sample, rain_times = _event(event.as_dict())
    collected = _time(snapshot['collected_at'])
    start, end = rain_times[0]-timedelta(hours=1), rain_times[-1]
    result = {'version': 1, 'event': event.as_dict(), 'collected_at': iso_z(collected), 'assessed_at': iso_z(now.astimezone(UTC)), 'sources': [],
        'limitations': [
            'Planning guidance, not an aviation clearance. Low-cloud fraction is not ceiling probability; verify ceilings, visibility, runway wind and maneuvering-room requirements closer in.',
            'WeatherNext 3 is the preferred comparator beyond 48 hours; AIFS is an independent comparator, not a vote or calibrated flight probability.',
            'Missing fields and unavailable sources are unknown, never zero or favorable. Hourly p10/p90 are marginal quantiles, not all-member ranges.',
            'CPC describes period-average categories, not daily rain chances. Tropical formation and member proximity are not local airport-impact probabilities. Respect issue times and mission overlap.',
            'Run history is archived initialization evidence, not current-source availability. Compare like target times and paired initializations; latest-advertised rolling runs are not response-bound.'
        ]}
    from .event_timing import timing_evidence
    timing = timing_evidence(snapshot)
    if timing is not None:
        result["operational_timing"] = timing
    from .chart_retention import retention_evidence
    retained = retention_evidence(snapshot)
    if retained:
        result['retained_chart_sources'] = retained
    if snapshot.get('initialization_provenance_version') == 1:
        result['model_initializations'] = initialization_evidence(snapshot, now)
    context = snapshot.get('synoptic_context')
    context = context if isinstance(context, dict) else {}
    for alias, (label, url) in SOURCES.items():
        if alias == 'run_history':
            url = 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#ensemble-run-history'
        elif alias == 'snapshot_changes':
            url = 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#event-narrative'
        elif alias == 'low_cloud_analysis':
            url = 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#low-cloud-analysis'
        elif alias == 'low_level_rh':
            url = 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#low-level-rh'
        source = {'id': alias, 'label': label, 'url': url, 'status': 'unavailable', 'evidence': {'reason': 'Missing, stale, incomplete or invalid; no favorable inference.'}}
        try:
            available = False
            if alias in {s.key for s in MODELS} | {'wn3_point', 'gfs'}:
                evidence = _current(snapshot, 'wn3' if alias == 'wn3_point' else alias, now, sample, rain_times)
                direct = evidence.get('source_provenance')
                if direct:
                    source['url'] = {'NOAA':'https://www.nco.ncep.noaa.gov/pmb/products/gfs/',
                                     'ECMWF':'https://www.ecmwf.int/en/forecasts/datasets/open-data',
                                     'ECCC':'https://eccc-msc.github.io/open-data/msc-data/nwp_geps/readme_geps_en/'}[direct['source_provider']]
                    result['version'] = 2
                available = True
            elif alias in ('nhc', 'cpc_wpc'):
                evidence = _official(context, 'nhc' if alias == 'nhc' else 'extended', start, end, now)
                available = any(p.get('status') == 'current' for p in evidence['products'])
            elif alias == 'wn3_cyclones':
                if not validate_wn3_cyclones(context.get(alias), now)['ok']:
                    raise ValueError('invalid cyclone data')
                def rendered(a, b):
                    products = context_evidence({alias: context[alias]}, a, b, now)['products']
                    return next(_text(p['text'], 5000) for p in products if p['source'] == alias)
                evidence = {'mission': rendered(start, end), 'preceding_48h': rendered(start-timedelta(hours=48), start), 'meaning': 'Experimental member counts, not airport-impact or landfall probability.'}
                available = True
            elif alias == 'snapshot_changes':
                evidence = validated_event_changes(snapshot, now)
                if evidence is None:
                    raise ValueError('missing validated snapshot comparison')
                available = True
            elif alias == 'low_cloud_analysis':
                evidence = low_cloud_evidence(snapshot, now)
                available = any(evidence.values())
            elif alias == 'low_level_rh':
                from .event_moisture_view import moisture_evidence
                evidence = moisture_evidence(snapshot, now)
                available = any(m['available'] for m in evidence['models'])
            else:
                history = validate_run_history(snapshot.get('ensemble_run_history'), event.as_dict(), now)
                if not history or not history['points']:
                    raise ValueError('missing history')
                points = []
                for key in (*[s.key for s in MODELS], 'wn3', 'gfs'):
                    records = [p for p in history['points'] if p['model_key'] == key and now-timedelta(hours=96) <= _time(p['run_time']) <= now]
                    for p in records[-8:]:
                        points.append({k: p[k] for k in ('model_key', 'run_time', 'retrieved_at', 'sample_time', 'metrics', 'run_binding')} | {'is_current': False})
                evidence = {'historical': True, 'units': history['units'], 'rain_window': {'start': iso_z(start), 'end': iso_z(end)}, 'points': points}
                available = bool(points)
            source.update(status='available' if available else 'unavailable', evidence=evidence)
        except ERRORS:
            pass
        result['sources'].append(source)
    if 'wn3_hourly' in snapshot:
        from .wn3_hourly import event_evidence
        source = dict(id='wn3_hourly', label='WeatherNext 3 interim hourly run (48h)',
                      url='https://developers.google.com/weathernext/guides/bigquery',
                      status='unavailable', evidence={'reason':'Hourly guidance unavailable; no favorable inference.'})
        try:
            source['evidence'] = event_evidence(snapshot, now)
            source['status'] = 'available' if source['evidence']['coverage'] != 'none' else 'unavailable'
        except ERRORS:
            pass
        result['sources'].append(source)
    # Optional per-office sources keep older snapshot evidence hashes stable.
    if 'event_afds' in snapshot:
        try:
            afds = validated_afds(snapshot, now)
        except ERRORS:
            afds = {}
        for office, name in (('OKX', 'New York/Upton'), ('PHI', 'Mount Holly'), ('ALY', 'Albany')):
            packet = afds.get(office)
            result['sources'].append({'id': 'afd_' + office.lower(), 'label': f'NWS {office} · {name} AFD',
                'url': packet['product_url'] if packet else None,
                'status': 'available' if packet else 'unavailable',
                'evidence': packet if packet else {'reason': 'Current office discussion unavailable; no carry-forward or favorable inference.'}})
    if 'synoptic_pattern' in snapshot:
        from .synoptic_pattern import pattern_evidence
        packet = pattern_evidence(snapshot, now)
        result['sources'].append({'id': 'synoptic_pattern', 'label': 'WPC surface analysis and forecast centers',
            'url': 'https://www.wpc.ncep.noaa.gov/html/sfc-zoom.php', 'status': 'available' if packet else 'unavailable',
            'evidence': packet if packet else {'reason': 'WPC coded surface products unavailable; no favorable inference.'}})
    if 'event_gusts' in snapshot or 'synoptic_pattern' in snapshot:
        from .week_ahead import week_evidence
        packet = week_evidence(snapshot, now)
        url = 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#week-ahead'
        result['sources'].append({'id': 'week_ahead', 'label': 'Week ahead · WPC days 3-7 and each model through the flight',
            'url': url, 'status': 'available' if packet else 'unavailable',
            'evidence': packet if packet else {'reason': 'Week-ahead context unavailable; no favorable inference.'}})
    # Prospective supplemental sources: legacy evidence digests stay unchanged.
    from .event_wind_view import wind_sources, SOURCES as WIND_SOURCES
    for alias, packet in wind_sources(snapshot, now).items():
        result['sources'].append({'id': alias, 'label': WIND_SOURCES[alias][0],
            'url': 'https://kcdw-flyability.andyfang.workers.dev' + event.path() + '#wind-analysis',
            'status': 'available' if packet else 'unavailable',
            'evidence': packet if packet else {'reason': 'Wind diagnostic unavailable; missing is not calm.'}})
    if any(key in snapshot for key in ('event_wind','native_wind','wind_trends')):
        from .event_evidence_compact import compact_moisture, compact_changes
        for source in result['sources']:
            if source['id']=='low_level_rh' and source['status']=='available':
                source['evidence']=compact_moisture(source['evidence'])
            elif source['id']=='snapshot_changes' and source['status']=='available':
                source['evidence']=compact_changes(source['evidence'])
    if snapshot.get('narrative_sampling_dictionary_version') == 1:
        from .event_evidence_compact import compact_sampling
        result = compact_sampling(result)
    prioritized = snapshot.get('narrative_priority_version') == 1
    if prioritized:
        from .event_evidence_compact import round_values
        result = round_values(result)
    # Bound the complete serialized envelope, including Unicode escaping used by
    # callers' default json.dumps. Keep dates/status if a large bulletin is cut.
    limit = PRIORITY_MAX_BYTES if prioritized else MAX_BYTES
    while len(json.dumps(result, allow_nan=False).encode()) > limit:
        candidates = [(len(p.get('text', '')), p) for s in result['sources'] for p in s['evidence'].get('products', []) if len(p.get('text', '')) > 700]
        if candidates:
            _, product = max(candidates, key=lambda item: item[0])
            product['text'] = product['text'][:max(700, len(product['text'])//2)]
            product['truncated'] = True
        else:
            # Preserve current summaries and the requested before/after pairs
            # before spending the budget on repeated hourly detail.
            hourly = [(len(json.dumps(s['evidence'][key])), s['evidence'], key)
                      for s in result['sources']
                      for key in ('event_samples', 'rain_intervals') if key in s['evidence']]
            if hourly:
                _, packet, key = max(hourly, key=lambda item: item[0])
                del packet[key]
                packet['hourly_detail_omitted'] = 'Window totals and fixed-time summaries retained; hourly detail omitted for size.'
                continue
            # Retain current evidence and the requested snapshot comparison.
            # Older initialization detail is already available on the charts;
            # keep at least the newest two points per model for run changes.
            history = next((s['evidence'] for s in result['sources'] if s['id'] == 'run_history'), {})
            points = history.get('points', [])
            removable = []
            for key in {p['model_key'] for p in points}:
                ordered = sorted((p for p in points if p['model_key'] == key), key=lambda p: _time(p['run_time']))
                removable.extend(ordered[:-2])
            if removable:
                points.remove(min(removable, key=lambda p: _time(p['run_time'])))
                history['older_run_points_omitted'] = history.get('older_run_points_omitted', 0) + 1
                continue
            # Keep office reasoning and current comparisons before long,
            # supplemental regional bulletins or cyclone-description repeats.
            background = [(len(p['text']), p, 'text', 350)
                          for s in result['sources'] for p in s['evidence'].get('products', [])
                          if len(p.get('text', '')) > 350]
            cyclone_floor = 600 if snapshot.get('native_ensemble_evidence_version') == 1 else 1000
            background += [(len(s['evidence'][key]), s['evidence'], key, cyclone_floor)
                           for s in result['sources'] if s['id'] == 'wn3_cyclones'
                           for key in ('mission', 'preceding_48h')
                           if len(s['evidence'].get(key, '')) > cyclone_floor]
            if background:
                size, packet, key, floor = max(background, key=lambda item: item[0])
                packet[key] = packet[key][:max(floor, size // 2)]
                packet['truncated'] = True
                continue
            # Preserve requested paired changes before secondary regional prose.
            regional = [s for s in result['sources'] if s['id'] in ('cpc_wpc','nhc')
                        and s['status']=='available' and not s['evidence'].get('detail_omitted')
                        and any(key in snapshot for key in ('event_wind','native_wind','wind_trends'))]
            if regional:
                from .event_evidence_compact import compact_official_prose
                source=max(regional,key=lambda s:len(json.dumps(s['evidence'])))
                source['evidence']=compact_official_prose(source['evidence'])
                continue
            # Paired member maxima/counts already retain the flight screen.
            # Prefer these and cloud changes over redundant hourly wind points.
            wind_hourly=[model for source in result['sources'] if source['id']=='wind_surface'
                         for model in source['evidence'].get('ensembles',{}).values()
                         if model and 'samples' in model]
            if wind_hourly:
                model=max(wind_hourly,key=lambda m:len(json.dumps(m['samples'])))
                del model['samples']
                model['hourly_detail_omitted']='Flight maxima, paired counts and return direction retained; hourly points omitted for size.'
                continue
            # Scientific cloud/RH and the requested paired changes are protected.
            # Compact their redundant representation before sacrificing sources.
            protected = {'snapshot_changes', 'low_level_rh', 'low_cloud_analysis'}
            from .event_evidence_compact import compact_moisture, compact_changes
            compacted = False
            for source in result['sources']:
                helper = {'snapshot_changes': compact_changes, 'low_level_rh': compact_moisture}.get(source['id'])
                already_compact = 'column_layouts' in source['evidence'] if source['id'] == 'snapshot_changes' else 'sample_columns' in source['evidence']
                if helper and source['status'] == 'available' and not already_compact:
                    packet = helper(source['evidence'])
                    if len(json.dumps(packet)) < len(json.dumps(source['evidence'])):
                        source['evidence'] = packet
                        compacted = True
            if compacted:
                continue
            disposable = [s for s in result['sources'] if s['id'] not in protected and s['status'] == 'available']
            if not disposable:
                raise ValueError('Protected scientific evidence exceeds narrative byte cap')
            if prioritized:
                # Flight-window NWS/ensemble wind and office reasoning control
                # near-term briefs; drop distant outlooks and duplicates first.
                rank = {alias: i for i, alias in enumerate(EVICTION_ORDER)}
                largest = min(disposable, key=lambda s: (rank.get(s['id'], -1), -len(json.dumps(s))))
            else:
                largest = max(disposable, key=lambda s: len(json.dumps(s)))
            largest.update(status='unavailable', evidence={'reason': 'Omitted to maintain evidence size limit.'})
    return result
