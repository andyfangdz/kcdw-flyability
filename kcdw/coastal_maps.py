"""Validated, pre-rendered synoptic comparisons embedded in event snapshots."""
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/New_York')


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Map times require a timezone')
    return result


def validate(data, event):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', event['slug']):
        raise ValueError('Invalid event slug')
    if data.get('version') != 1 or data.get('event_slug') != event['slug'] or data.get('event_date') != event['date']:
        raise ValueError('Map collection belongs to another event')
    stamp(data['prepared_at'])
    runs, times, models = data['runs'], data['times'], data['models']
    if not 1 <= len(runs) <= 4 or not 2 <= len(times) <= 24 or not 1 <= len(models) <= 4:
        raise ValueError('Invalid comparison size')
    for axis in (runs, times):
        parsed = [stamp(t) for t in axis]
        if sorted(set(parsed)) != parsed:
            raise ValueError('Map times must be distinct and ordered')
    ids = [m['id'] for m in models]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-z0-9]{1,12}', m) for m in ids):
        raise ValueError('Invalid model identifiers')
    for model in models:
        if not model['source_url'].startswith('https://developers.google.com/'):
            raise ValueError('Invalid model source URL')
    if len(data['palette']) != 13 or any(not re.fullmatch(r'[a-f0-9]{6}', color) for color in data['palette']):
        raise ValueError('Invalid wind palette')
    expected = {(m,r,t) for m in ids for r in runs for t in times}
    seen = set()
    dimensions = set()
    for f in data['frames']:
        identity = f['model'], f['run'], f['valid']
        if identity not in expected or identity in seen:
            raise ValueError('Unexpected or duplicate frame')
        seen.add(identity)
        lead = (stamp(f['valid'])-stamp(f['run'])).total_seconds()/3600
        if f['lead'] != lead or not 0 < lead <= 360:
            raise ValueError('Map forecast lead mismatch')
        if not re.fullmatch(r'[a-f0-9]{64}', f['sha256']):
            raise ValueError('Invalid map checksum')
        if f['url'] != f"/events/{event['slug']}/maps/{f['sha256']}.png":
            raise ValueError('Invalid map URL')
        if not 800 <= f['width'] <= 2000 or not 800 <= f['height'] <= 3000:
            raise ValueError('Invalid map dimensions')
        dimensions.add((f['width'], f['height']))
    if seen != expected or len(dimensions) != 1:
        raise ValueError('Map comparison is incomplete or misaligned')
    return data


def load(path, event):
    path = Path(path)
    if not path.exists():
        return None
    if path.stat().st_size > 100_000:
        raise ValueError('Map manifest exceeds size limit')
    return validate(json.loads(path.read_text()), event)


def local_time(value):
    return stamp(value).astimezone(TZ).strftime('%a %b %-d · %-I %p %Z')


def render(data, event):
    if not data:
        return ''
    try:
        validate(data, event)
    except (KeyError, TypeError, ValueError, AttributeError):
        return ''
    esc = lambda v: escape(str(v), quote=True)
    # Open at the last cycle, nearest the morning of the event; both selectors remain synchronized.
    run = data['runs'][-1]
    target = stamp(event['date']+'T14:00:00Z')
    initial = min(range(len(data['times'])), key=lambda i: abs((stamp(data['times'][i])-target).total_seconds()))
    valid = data['times'][initial]
    frame_index = {(f['model'],f['run'],f['valid']): f for f in data['frames']}
    cards = []
    for model in data['models']:
        f = frame_index[model['id'],run,valid]
        alt = f"{model['name']} mean sea-level pressure and 10 m wind, valid {local_time(valid)}, initialized {run}, F{f['lead']:03d}"
        cards.append(f'''<article class="coastal-card" data-map-model="{esc(model['id'])}">
<div class="coastal-card-heading"><h3>{esc(model['name'])}</h3><span>{esc(model['statistic'])} · {esc(model['resolution'])}</span></div>
<a class="coastal-expand" href="{esc(f['url'])}" target="_blank" rel="noopener" aria-label="Expand {esc(model['name'])} map">
<img src="{esc(f['url'])}" width="{f['width']}" height="{f['height']}" alt="{esc(alt)}" loading="lazy" decoding="async"></a>
<p class="coastal-frame-status" role="status">F{f['lead']:03d} · <a href="{esc(f['url'])}" target="_blank" rel="noopener">Expand map ↗</a></p></article>''')
    run_options = ''.join(f'<option value="{esc(r)}"{" selected" if r == run else ""}>{esc(stamp(r).strftime("%b %-d · %HZ"))}</option>' for r in reversed(data['runs']))
    time_options = ''.join(f'<option value="{i}"{" selected" if i == initial else ""}>{esc(local_time(t))}</option>' for i,t in enumerate(data['times']))
    public = {k: data[k] for k in ('runs','times','models','frames')}
    public['labels'] = [local_time(t) for t in data['times']]
    payload = esc(json.dumps(public, separators=(',', ':')))
    colors = ','.join('#'+c for c in data['palette'])
    sources = ' · '.join(f'<a href="{esc(m["source_url"])}">{esc(m["name"])}</a>' for m in data['models'])
    if any(m['id'] == 'gfs' for m in data['models']):
        sources += ' · <a href="https://www.nco.ncep.noaa.gov/pmb/products/gfs/">NOAA pressure inputs</a>'
    four_models = ' coastal-grid-four' if len(data['models']) == 4 else ''
    gfs_method = (' GFS uses Earth Engine’s 10 m wind plus matching NOAA sea-level pressure imported without resampling; '
        'Earth Engine renders both fields. IFS and GFS are separate deterministic forecasts.'
        if any(m['id'] == 'gfs' for m in data['models']) else ' IFS is a single deterministic forecast.')
    return f'''<section id="coastal-low" class="coastal-maps evidence-group" aria-labelledby="coastal-title" data-coastal="{payload}">
<div class="coastal-heading"><div><p class="eyebrow">Regional evolution / Earth Engine</p><h2 id="coastal-title">Coastal low: how the forecasts evolve</h2></div><span class="coastal-badge">{len(data['models'])} models · {len(data['runs'])} runs · {len(data['times'])} times</span></div>
<p>Follow the pressure pattern and surface winds through the checkride. All panels share a valid time, geographic extent and scale. Switch initialization to compare how the forecast changed.</p>
<div class="coastal-controls" hidden><label>Model initialization <select data-map-run>{run_options}</select></label><label>Forecast valid <select data-map-time>{time_options}</select></label><div class="coastal-stepper"><button type="button" data-map-prev aria-label="Previous forecast time">←</button><button type="button" data-map-play>Play evolution</button><button type="button" data-map-next aria-label="Next forecast time">→</button></div></div>
<p class="coastal-valid" aria-live="polite">Valid {esc(local_time(valid))} · initialized {esc(stamp(run).strftime('%b %-d %HZ'))}</p>
<div class="coastal-grid{four_models}">{''.join(cards)}</div>
<div class="coastal-legend"><strong>10 m sustained wind · kt</strong><div class="coastal-colorbar" style="background:linear-gradient(to right,{colors})"></div><div class="coastal-ticks"><span>0</span><span>10</span><span>20</span><span>30</span><span>40</span><span>50</span><span>60+</span></div><p>Isobars: mean sea-level pressure, every 2 hPa. Wind barbs: half feather 5 kt, full feather 10 kt, flag 50 kt. H/L: local pressure extrema.</p></div>
<details class="coastal-methods"><summary>Map runs, methods &amp; sources</summary><p>Selected model runs: {esc(' / '.join(stamp(r).strftime('%b %-d %HZ') for r in data['runs']))}. Maps prepared {esc(local_time(data['prepared_at']))}; these are a saved comparison, separate from the briefing’s latest point forecasts.</p><p>WeatherNext shading is mean scalar wind speed and barbs are mean U/V.{gfs_method} Ensemble means can smooth or blur lows; these maps do not show storm-track probabilities, gusts or uncertainty. Pressure smoothing uses a 0.2° Gaussian sigma for all models. Higher display resolution adds no model detail.</p><p>{sources}. Rendered by Google Earth Engine. © Google / DeepMind WeatherNext; ECMWF open data; NOAA public-domain data; boundaries: Natural Earth. <a href="https://storage.googleapis.com/weathernext-public/terms-of-use.pdf">WeatherNext terms</a>.</p></details>
<dialog class="coastal-dialog" aria-labelledby="coastal-detail-title"><div class="coastal-dialog-heading"><div><h3 id="coastal-detail-title"></h3><p data-map-detail-time></p></div><button type="button" data-map-close aria-label="Close expanded map">Close ×</button></div><img data-map-detail-image alt=""><div data-map-detail-legend></div><p class="coastal-download"><a data-map-download target="_blank" rel="noopener">Open original PNG ↗</a></p></dialog>
<noscript><p>Showing the event-morning forecast from the later initialization. Enable JavaScript to change the run and forecast time.</p></noscript></section>'''
