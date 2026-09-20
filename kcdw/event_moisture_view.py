"""Supplemental moisture plots and bounded narrative evidence."""
from datetime import datetime, timedelta
import math

from .common import UTC, iso_z, parse_time
from .events import Event, TZ
from .source_presentation import is_direct, source_description, SOURCE_LICENSES

MODELS = {'gfs': ('GFS operational', '#172b3a'),
          'ifs': ('ECMWF IFS', '#2166ac'),
          'aifs_single': ('AIFS Single', '#984ea3'),
          'gefs': ('NCEP GEFS', '#31865a'),
          'ecmwf_ens': ('ECMWF ENS', '#2166ac'),
          'aifs_ens': ('AIFS-ENS', '#d28518')}
ENSEMBLES = {'gefs', 'ecmwf_ens', 'aifs_ens'}
FIELDS = [('relative_humidity_2m', 'Surface relative humidity · 2 m'),
          ('relative_humidity_1000hPa', 'Low-level relative humidity · 1000 hPa'),
          ('relative_humidity_925hPa', 'Low-level relative humidity · 925 hPa'),
          ('relative_humidity_850hPa', 'Low-level relative humidity · 850 hPa')]


def validated(snapshot, now):
    from .event_moisture import validate_moisture
    from .event_moisture_ensemble import validate_moisture_ensemble
    deterministic = validate_moisture(snapshot.get('event_moisture'), now)
    ensembles = validate_moisture_ensemble(snapshot.get('event_moisture_ensemble'), now)
    return {'models': {**deterministic.get('models', {}), **ensembles.get('models', {})}}


def render_moisture(snapshot, times, window, now, history=None):
    from .event_renderer import (Chart, esc, MODEL_COLORS, _history_series,
                                 _saved_group, _aligned, encode_chart_values)
    palette = {k: MODEL_COLORS.get(k, v[1]) for k, v in MODELS.items()}
    status = validated(snapshot, now).get('models', {})
    good = {k: status[k]['data'] for k in MODELS if status.get(k, {}).get('available')}
    body = ['<section id="low-level-rh"><h2>Surface &amp; low-level moisture</h2>',
            '<p class="comparison-intro">Relative humidity at the surface and three pressure levels · 3-hour chart samples. Ensemble medians with p10–p90 bands; deterministic forecasts have no band. AIFS Single is separate from AIFS-ENS. High RH supports a cloud scenario, not a ceiling height or probability.</p>']
    for key, (label, _) in MODELS.items():
        if key not in good:
            body.append(f'<p class="chart-note">{esc(label)} unavailable.</p>')
    historical_keys = {k for k, source in (history or {}).get('sources', {}).items() if k in MODELS and any(field in source['hourly'] for field, _ in FIELDS)}
    if history:
        body.append('<p class="chart-note">Earlier saved forecasts—not observations. Faded dotted curves retain saved RH statistics and gaps.</p>')
    if not good and not historical_keys:
        return ''.join(body) + '</section>'
    body.append('<div class="forecast-controls" role="group" aria-label="Moisture forecast time view"><button type="button" data-forecast-view="today">Today</button><button type="button" data-forecast-view="checkride">Center checkride</button><button type="button" data-forecast-view="full">Full date range</button></div>')
    body.append('<fieldset class="comparison-controls"><legend>Show moisture models</legend>')
    for key in MODELS:
        if key not in good and key not in historical_keys:
            continue
        label, color = MODELS[key][0], palette[key]
        body.append(f'<label><input id="rh-{key}" type="checkbox" checked><span class="swatch" style="--c:{color}"></span>{esc(label)}</label>')
    body.append('<label><input id="rh-bands" type="checkbox" checked>Show p10–p90 bands</label></fieldset>')
    member_spans = {}
    member_details = []
    for field, title in FIELDS:
        # Use actual 3-hour samples, retain every gap boundary and the complete
        # hourly tooltip array. Time-based coordinates keep all panels aligned.
        keep = {0, len(times)-1, *(window or ())}
        keep.update(i for i,t in enumerate(times) if t.hour % 3 == 0 or t.astimezone(TZ).hour in (0,12))
        for data in good.values():
            h = data['hourly']; positions = {parse_time(t):i for i,t in enumerate(h['time'])}
            raw = h.get(field, [])
            values = raw['p50'] if isinstance(raw, dict) else raw
            full = [values[positions[t]] if t in positions and values else None for t in times]
            for i in range(1,len(full)):
                if (full[i] is None) != (full[i-1] is None):
                    keep.update((i-1,i))
        current = {}
        for key, data in good.items():
            raw = data['hourly'].get(field, [])
            current[key] = _aligned(data['hourly'], raw['p50'] if isinstance(raw, dict) else raw, times)
        saved = [row for row in _history_series(snapshot, history, field, times, current) if row[0] in MODELS]
        for row in saved:
            for values in row[1:4]:
                for i in range(1, len(values)):
                    if (values[i] is None) != (values[i-1] is None):
                        keep.update((i-1, i))
        indices = sorted(keep)
        plot_window = tuple(indices.index(i) for i in window) if window else None
        values_for_scale = [v for data in good.values()
                            for row in ([data['hourly'][field]['p90']] if isinstance(data['hourly'].get(field),dict)
                                        else [data['hourly'].get(field,[])]) for v in row if v is not None]
        values_for_scale.extend(v for row in saved for series in row[1:4] for v in series if v is not None)
        y_max = max(100, math.ceil(max(values_for_scale, default=100)/25)*25)
        chart = Chart([times[i] for i in indices], 0, y_max, plot_window)
        legend, missing = [], []
        field_counts = []
        for key, data in good.items():
            hourly = data['hourly']
            axis = {parse_time(t): i for i, t in enumerate(hourly['time'])}
            raw = hourly.get(field, [])
            fan = raw if key in ENSEMBLES and isinstance(raw, dict) else None
            def aligned(values):
                return [values[axis[t]] if t in axis and values else None for t in times]
            values = aligned(fan['p50'] if fan else raw)
            if fan:
                present = [n for n in fan['sample_counts'] if n > 0]
                if present:
                    a,b=min(present),max(present)
                    span=str(a) if a==b else f"{a}–{b}"
                    field_counts.append(f"{MODELS[key][0]} {span}")
                    low, high = member_spans.get(key, (a,b))
                    member_spans[key] = (min(low, *present), max(high, *present))
            label, color = MODELS[key][0], palette[key]
            if not any(v is not None for v in values):
                missing.append(label)
                continue
            statistic = 'median RH / p10–p90' if fan else 'deterministic RH'
            encoded = esc(encode_chart_values(values))
            chart.parts.append(f'<g data-rh-model="{key}" data-model="rh-{key}" data-label="{esc(label)} / {statistic}" data-unit="% RH" data-values="{encoded}"><title>{esc(label)} / {statistic}</title>')
            if fan:
                chart.parts.append('<g class="rh-band">')
                low, high = aligned(fan['p10']), aligned(fan['p90'])
                chart.band([low[i] for i in indices], [high[i] for i in indices], color, .13)
                chart.parts.append('</g>')
            chart.line([values[i] for i in indices], color, 3.2 if key == 'gfs' else 1.8, dashed=key in ('ifs','aifs_single'))
            chart.parts.append('</g>')
            legend.append((label + (' median / p10–p90' if fan else ' deterministic'), color, key in ('ifs','aifs_single')))
        for row in saved:
            _saved_group(chart, row, MODELS[row[0]][0], palette[row[0]], '% RH', indices, moisture=True)
        if field_counts:
            member_details.append(title+': '+', '.join(field_counts))
        note = 'Field unavailable: ' + ', '.join(missing) + '.' if missing else ''
        body.append(f'<div data-moisture-field="{field}">' + chart.render(title, '% RH', note, legend, [0,25,50,75,100]+([y_max] if y_max>100 else [])) + '</div>')
    if member_spans:
        spans = ' · '.join(f'{MODELS[k][0]} {a}' if a == b else f'{MODELS[k][0]} {a}–{b}' for k, (a, b) in member_spans.items())
        body.append(f'<p class="chart-note">Members per available hour: {esc(spans)}.</p>')
    body.append('<details class="chart-reading"><summary>Moisture source times and level heights</summary><p>Pressure levels do not have fixed altitudes. Below-ground levels and missing samples are gaps. Hourly display can interpolate coarser native output. Geopotential heights in the narrative evidence are meters MSL, not cloud bases.</p>')
    if member_details:
        body.append('<p>Eligible members per available hour, by level:</p><ul>'+''.join('<li>'+esc(line)+'</li>' for line in member_details)+'</ul>')
    for key, data in good.items():
        body.append(f'<p>{esc(MODELS[key][0])} · fetched {esc(data["fetched_at"])}. {esc(source_description(data))}</p>')
    body.append('<p><a href="https://www.nco.ncep.noaa.gov/pmb/products/gfs/">NOAA GFS</a> · <a href="https://www.ecmwf.int/en/forecasts/datasets/open-data">ECMWF open data</a> · <a href="https://open-meteo.com/en/docs">Open-Meteo fallback</a>. '+esc(SOURCE_LICENSES)+'</p></details></section>')
    return ''.join(body)


def moisture_evidence(snapshot, now):
    """Only three mission samples/model enter Codex, never entire RH arrays."""
    event = Event(**snapshot['event'])
    midnight = datetime.combine(event.day, datetime.min.time(), TZ)
    hours = sorted({event.start_hour, max(event.start_hour, min(12, event.end_hour-1)), event.end_hour-1})
    moments = [(midnight+timedelta(hours=h)).astimezone(UTC) for h in hours]
    status = validated(snapshot, now).get('models', {})
    result = {'models': [], 'interpretation': 'Ensemble RH p10/p50/p90 and available-member counts; deterministic RH is a single value. RH is not cloud fraction or ceiling probability. AIFS Single is not AIFS-ENS. Pressure-level heights are MSL, not a predicted cloud base. Rolling response initialization is unverified.'}
    for key, (label, _) in MODELS.items():
        source = status.get(key, {})
        row = {'key': key, 'model': label, 'available': bool(source.get('available')), 'statistic': 'p10/p50/p90' if key in ENSEMBLES else 'deterministic'}
        if row['available']:
            data = source['data']; hourly = data['hourly']
            axis = {parse_time(t): i for i, t in enumerate(hourly['time'])}
            def value(field, at):
                values = hourly.get(field, [])
                if at not in axis or not values:
                    return None
                i = axis[at]
                if isinstance(values, dict):
                    return {p: values[p][i] for p in ('p10','p50','p90')} | {'members': values['sample_counts'][i]}
                return values[i]
            row['fetched_at'] = data['fetched_at']
            if is_direct(data):
                row.update(run_binding='response-bound', initialization_time=data['metadata']['initialization_time'],
                           source_provider=data['metadata']['source_provider'], sampling=data['metadata']['sampling'])
            row['samples'] = [{'at': iso_z(at), 'surface_RH_percent': value('relative_humidity_2m', at),
                              'levels': [{'pressure_hPa': p, 'RH_percent': value(f'relative_humidity_{p}hPa', at),
                                          'height_m_MSL': value(f'geopotential_height_{p}hPa', at)} for p in (1000,925,850)]} for at in moments]
        result['models'].append(row)
    return result
