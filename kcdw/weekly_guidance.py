"""Independent rolling-week model fans. No event diagnostics or quorum changes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .common import UTC, iso_z
from .events import TZ
from .event_ensemble import (MODELS, VARIABLES, UNITS, BOUNDS, PERCENTILES, LAT, LON,
                             ENDPOINT, collect_model, _required)
from .event_renderer import Chart, MODEL_COLORS, _extent, _ticks, esc
from .gfs_guidance import collect_gfs, validate_gfs
from .weathernext3 import validate_weather_next3

MAX_FETCH_AGE = timedelta(hours=12)


def weekly_range(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('timezone-aware now required')
    day = now.astimezone(TZ).date()
    return (datetime.combine(day, datetime.min.time(), TZ).astimezone(UTC),
            datetime.combine(day + timedelta(days=7), datetime.min.time(), TZ).astimezone(UTC))


def hourly_axis(start, end):
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    return [start + timedelta(hours=i) for i in range(int((end-start).total_seconds()/3600))]


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp missing')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or iso_z(stamp) != value:
        raise ValueError('canonical UTC timestamp required')
    return stamp


def _fresh(value, now, maximum=MAX_FETCH_AGE):
    stamp = _stamp(value)
    if not timedelta(0) <= now-stamp <= maximum:
        raise ValueError('stale or future source timestamp')
    return stamp


def _model(source, spec, guidance, now):
    if not isinstance(source, dict) or source.get('ok') is not True:
        raise ValueError('source unavailable')
    fetched = _fresh(guidance['collected_at'], now)
    start, end = weekly_range(fetched)
    if guidance['range'] != {'start': iso_z(start), 'end': iso_z(end)}:
        raise ValueError('weekly range mismatch')
    d = source['data']
    _fresh(d['fetched_at'], now)
    if d['fetched_at'] != guidance['collected_at']:
        raise ValueError('fetch provenance mismatch')
    if (d['key'], d['model'], d['provider'], d['model_id'], d['members'], d['endpoint']) != (spec.key, spec.name, spec.provider, spec.model_id, spec.members, ENDPOINT):
        raise ValueError('model identity mismatch')
    if d['hourly_units'] != dict(UNITS, time='iso8601 UTC'):
        raise ValueError('units mismatch')
    for coord, target in [('latitude', LAT), ('longitude', LON)]:
        if abs(_required(d['grid_point'][coord], coord)-target) > .5:
            raise ValueError('grid mismatch')
    metadata = d['metadata']
    if metadata.get('ok') is not True:
        raise ValueError('model metadata unavailable')
    initialized = _fresh(metadata['initialization_time'], now, timedelta(hours=24))
    available = _stamp(metadata['availability_time'])
    if not initialized <= available <= fetched + timedelta(minutes=5) or _stamp(metadata['data_end_time']) <= available:
        raise ValueError('metadata chronology mismatch')
    axis = [iso_z(t) for t in hourly_axis(start, end)]
    h = d['hourly']
    if h['time'] != axis:
        raise ValueError('exact UTC hourly axis mismatch')
    n = len(axis)
    for variable in VARIABLES:
        fan = h[variable]
        count = fan['members_with_data']
        if type(count) is not int or not 0 <= count <= spec.members:
            raise ValueError('member count mismatch')
        counts = fan['sample_counts']
        if not isinstance(counts, list) or len(counts) != n or any(type(c) is not int or not 0 <= c <= count for c in counts):
            raise ValueError('sample count mismatch')
        if count < max(counts):
            raise ValueError('member/sample count mismatch')
        for pct in PERCENTILES:
            if not isinstance(fan[f'p{pct}'], list) or len(fan[f'p{pct}']) != n:
                raise ValueError('percentile length mismatch')
        for i, samples in enumerate(counts):
            values = [fan[f'p{p}'][i] for p in PERCENTILES]
            if not samples:
                if any(v is not None for v in values):
                    raise ValueError('values without samples')
            else:
                low, high = BOUNDS[variable]
                if any(not low <= _required(v, variable) <= high for v in values) or values != sorted(values):
                    raise ValueError('percentile bounds/order mismatch')
    return d


def collect_weekly(client, now):
    """Collect four independent ensembles and pinned GFS for [midnight,+7d)."""
    start, end = weekly_range(now)
    result = {'version': 1, 'collected_at': iso_z(now),
              'range': {'start': iso_z(start), 'end': iso_z(end)}, 'models': {}}
    for spec in MODELS:
        try:
            data = collect_model(client, spec, None, now, display_range=(start, end))
            source = {'ok': True, 'data': data}
            _model(source, spec, result, now)
            result['models'][spec.key] = source
        except Exception as exc:
            result['models'][spec.key] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:240]}
    try:
        # GFS validation forbids hours before the current UTC date. During
        # Eastern evening that excludes earlier today: retain them as gaps.
        gfs_start = max(start, now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
        result['gfs'] = collect_gfs(client, gfs_start, end, now)
    except Exception as exc:
        result['gfs'] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:240]}
    return result


def _mapping(value):
    return value if isinstance(value, dict) else {}


def render_weekly(snapshot, now):
    """Whole rolling snapshot in; validated, non-event HTML fragment out.

    The plotted week stays anchored to the report's collection date, including
    DST; rendering after midnight must not silently disagree with daily cards.
    Source-specific freshness is nevertheless checked against render-time now.
    """
    assessed = _stamp(snapshot['collected_at']) if 'collected_at' in snapshot else now
    start, end = weekly_range(assessed)
    times = hourly_axis(start, end)
    # A null endpoint makes the plotted axis end at next week's exact midnight.
    times.append(end)
    guidance = _mapping(snapshot.get('weekly_guidance'))
    models, provenance = [], []
    for spec in MODELS:
        source = _mapping(guidance.get('models')).get(spec.key)
        try:
            d = _model(source, spec, guidance, now)
            models.append(d)
            provenance.append(f'{spec.name}: fetched {d["fetched_at"]}; latest advertised init {d["metadata"]["initialization_time"]}; {spec.members} members; grid {d["grid_point"]}.')
        except Exception as exc:
            provenance.append(f'{spec.name} unavailable: {exc}.')
    wn3 = {}
    try:
        source = _mapping(_mapping(snapshot.get('sources')).get('weather_next3'))
        if source.get('ok') is not True:
            raise ValueError('source unavailable')
        _fresh(source['fetched_at'], now)
        validate_weather_next3(source['data'], now)
        wn3 = source['data']['forecast']
        provenance.append(f'WeatherNext 3: actual response run {wn3["response_init_utc"]}; fetched {source["fetched_at"]}.')
    except Exception as exc:
        provenance.append(f'WeatherNext 3 unavailable: {exc}.')
    gfs_source = _mapping(guidance.get('gfs'))
    gfs_status = validate_gfs(gfs_source, now)
    gfs = gfs_source['data'] if gfs_status['available'] else {}
    if gfs:
        provenance.append('GFS operational: fetched ' + gfs['fetched_at'] + '; latest advertised cycles ' + ', '.join(item['latest_advertised_init'] for item in gfs['metadata']['datasets'].values()) + '.')
    else:
        provenance.append('GFS operational unavailable: ' + gfs_status['error'])
    names = {m['key']: m['model'] for m in models}
    if wn3:
        names['wn3'] = 'WeatherNext 3'
    if gfs:
        names['gfs'] = 'GFS operational (deterministic)'
    controls = ''.join(f'<label><input id="compare-{key}" type="checkbox" checked><span class="swatch" style="--c:{MODEL_COLORS[key]}"></span>{esc(name)}</label>' for key, name in names.items())
    body = ('<section id="weekly-ensembles" class="multimodel-comparison"><h2>Weekly multimodel guidance</h2>'
            '<p>Seven Eastern calendar dates. Pan any chart to synchronize all six; y-axes stay fixed. Missing hours remain gaps, not benign weather.</p>'
            '<div class="forecast-controls" role="group" aria-label="Forecast time view"><button type="button" data-forecast-view="today">Today · 2–3 days</button><button type="button" data-forecast-view="now">Now · 2–3 days</button><button type="button" data-forecast-view="full">Full week</button></div>'
            '<fieldset class="comparison-controls"><legend>Show models / uncertainty</legend>' + controls + '<label><input id="compare-bands" type="checkbox" checked>Show p10–p90 ranges</label></fieldset>'
            '<p class="chart-note">Conventional lines: Median (p50), p10–p90 bands. WN3: Mean, p10–p90 bands (not minimum–maximum). GFS: deterministic, no uncertainty band. Statistics are not pooled or converted to flight probabilities. Hourly interpolation adds no timing skill.</p>')

    def align(axis, values):
        # WN3 uses .000Z, while other sources use Z; compare UTC instants.
        positions = {datetime.fromisoformat(stamp.replace('Z', '+00:00')): i for i, stamp in enumerate(axis)}
        return [values[positions[t]] if t < end and t in positions else None for t in times]

    for variable, short, title, unit, wn_field, factor in (
        ('precipitation', 'rain', 'Hourly precipitation', 'mm / preceding hour', 'precipitation_1h', 1),
        ('wind_speed_10m', 'wind', 'Sustained wind at 10 m', 'kt', 'wind_speed_10m', 3600/1852),
        ('cloud_cover_low', 'cloud', 'Low-cloud fraction — not ceiling height', '%', 'low_cloud_cover', 1),
        ('pressure_msl', 'pressure', 'Sea-level pressure', 'hPa', 'sea_level_pressure', .01),
        ('temperature_2m', 'temperature', 'Temperature', '°C', 'temperature_2m', 1),
        ('wind_gusts_10m', 'gust', 'Wind gusts', 'kt', None, 1),
    ):
        series = []
        for model in models:
            h = model['hourly']
            fan = h[variable]
            if fan['members_with_data']:
                series.append((model['key'], *(align(h['time'], fan[p]) for p in ('p50', 'p10', 'p90')), 'Median (p50) / p10–p90'))
        if wn3 and wn_field:
            series.append(('wn3', *(align(wn3['valid_time_utc'], [v*factor for v in wn3['fields'][wn_field][p]]) for p in ('mean', 'p10', 'p90')), 'Mean / p10–p90'))
        if gfs:
            series.append(('gfs', align(gfs['hourly']['time'], gfs['hourly'][variable]), [], [], 'Deterministic / no uncertainty band'))
        series = [s for s in series if any(v is not None for v in s[1])]
        note = 'Missing hours are gaps. '
        if short == 'cloud':
            note += 'Low-cloud fraction is not ceiling height. '
        if short == 'gust':
            note += 'WN3: no gust field available. '
        if short == 'rain':
            note += 'Preceding-hour amounts, not cumulative rain or rain probability. '
        if not series:
            body += f'<div data-comparison-field="{short}"><h3 class="weekly-chart-title">{esc(title)}</h3><p>{esc(note)}No usable series.</p></div>'
            continue
        low, high = _extent(*(values for _, mean, lo, hi, _ in series for values in (mean, lo, hi)))
        chart = Chart(times, low, max(high, low+.1), None)
        legend = []
        for key, mean, lo, hi, statistic in series:
            color = MODEL_COLORS[key]
            values = esc(json.dumps(mean, separators=(',', ':'), allow_nan=False))
            chart.parts.append(f'<g data-model="{key}" data-label="{esc(names[key] + " / " + statistic)}" data-unit="{esc(unit)}" data-values="{values}">')
            if key != 'gfs':
                chart.parts.append('<g class="comparison-band">')
                chart.band(lo, hi, color, .14)
                chart.parts.append('</g>')
            chart.line(mean, color, 3.2 if key in ('wn3', 'gfs') else 1.8)
            chart.parts.append('</g>')
            legend.append((names[key] + ' / ' + statistic, color, False))
        rendered = chart.render(title, unit, note, legend, _ticks(low, max(high, low+.1)))
        rendered = rendered.replace('<h3>', '<h3 class="weekly-chart-title">')
        rendered = rendered.replace(f'data-event-center="{iso_z(start)}"', f'data-event-center="{iso_z(now)}" data-focus-utc="{iso_z(now)}"')
        body += f'<div data-comparison-field="{short}">' + rendered + '</div>'
    body += '<details class="chart-reading"><summary>Sources, runs &amp; limitations</summary><ul>' + ''.join('<li>' + esc(p) + '</li>' for p in provenance) + '</ul>'
    body += ('<p>Open-Meteo rolling guidance, CC BY 4.0. Latest advertised dataset metadata is not an immutable response-bound cycle ID; extended IFS output can use an earlier long cycle. GFS is explicitly gfs_global, not a seamless blend or GEFS control; after 120 hours native 3-hourly output is interpolated hourly. No ceiling, visibility or aviation suitability inference follows from these plots.</p>'
             '<p>WeatherNext attribution: Google Weather Lab. © 2024-5 Google LLC. Experimental modelling data, not intended, validated or approved for real world use; provided as is, without warranties. <a href="https://storage.googleapis.com/weathernext-public/terms-of-use.pdf">WeatherNext license and terms</a>. This independent display is not endorsed by Google. Use official aviation guidance for operational decisions.</p></details></section>')
    return body


def chart_css():
    """Scoped chart-only styles; shared navigation JS is included once by root."""
    return '''
#weekly-ensembles{margin:28px 0;min-width:0}#weekly-ensembles .chart{margin:20px 0;padding:0}
#weekly-ensembles .chart figcaption{display:flex;align-items:baseline;justify-content:space-between;gap:12px}#weekly-ensembles h3{font-size:20px;margin:0}
#weekly-ensembles .unit,#weekly-ensembles .chart-note,#weekly-ensembles .legend-row{font-size:11px;color:var(--muted)}
#weekly-ensembles .forecast-frame{position:relative;display:grid;grid-template-columns:48px minmax(0,1fr)}
#weekly-ensembles .fixed-y-axis{position:relative;height:240px;font:11px var(--mono);background:var(--white);z-index:2}
#weekly-ensembles .fixed-y-axis span{position:absolute;right:7px;transform:translateY(-50%)}
#weekly-ensembles .chart-scroll{overflow-x:auto;max-width:100%;min-width:0;overscroll-behavior-x:contain;touch-action:pan-x pan-y}
#weekly-ensembles .forecast-plane{position:relative;max-width:none}#weekly-ensembles .chart svg{width:100%;height:240px;display:block;background:var(--white)}
#weekly-ensembles .forecast-plane [stroke]{vector-effect:non-scaling-stroke}
#weekly-ensembles .forecast-x-axis{position:absolute;bottom:0;left:0;right:0;height:28px;pointer-events:none;overflow:hidden}
#weekly-ensembles .forecast-x-axis span{position:absolute;white-space:nowrap;font:11px var(--mono)}
#weekly-ensembles .legend-row,#weekly-ensembles .comparison-controls,#weekly-ensembles .forecast-controls{display:flex;flex-wrap:wrap;gap:8px 16px}
#weekly-ensembles .comparison-controls{margin:14px 0;padding:10px;border:1px solid var(--line)}
#weekly-ensembles label{display:inline-flex;align-items:center;gap:6px;min-height:40px;font-size:12px}#weekly-ensembles input{width:18px;height:18px;accent-color:#c02679}
#weekly-ensembles button{min-height:44px;padding:8px 12px;cursor:pointer}#weekly-ensembles :focus-visible{outline:2px solid var(--ink);outline-offset:2px}
#weekly-ensembles .swatch{display:inline-block;width:12px;height:12px;background:var(--c);margin-right:5px}
#weekly-ensembles .chart-tooltip{position:absolute;top:0;left:48px;right:0;background:var(--white);border:1px solid var(--line);padding:8px;font-size:11px;z-index:3;pointer-events:none}
#weekly-ensembles .chart-tooltip[hidden]{display:none}#weekly-ensembles details{font-size:12px;overflow-wrap:anywhere}#weekly-ensembles summary{min-height:44px;cursor:pointer}
#weekly-ensembles:has(#compare-bands:not(:checked)) .comparison-band{display:none}
''' + '\n'.join(f'#weekly-ensembles:has(#compare-{key}:not(:checked)) [data-model="{key}"]{{display:none}}' for key in MODEL_COLORS) + '''
@media(max-width:760px),(hover:none),(pointer:coarse){#weekly-ensembles .chart-tooltip{display:none!important}}
'''
