"""Consensus surface analysis and progs (GFS, IFS, AIFS, GDPS) for a dated event page.

`collect` runs scripts/consensus_prog_chart.py in the separate chart environment for one shared
00/12Z cycle, from its analysis (F000) through the end of the flight, then uploads new PNGs to the
Worker's content-addressed event-map route. Unchanged cycles are reused within seconds. The
snapshot keeps image URLs plus a small KCDW summary per frame; the page loads images by URL.
See deploy/CONSENSUS-PROG.md.
"""
from __future__ import annotations

from datetime import timedelta
from html import escape
import json
import math
from pathlib import Path
import re
import subprocess

from .common import atomic_write, iso_z, parse_time
from .events import TZ

VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / 'var/charts-venv/bin/python'
SCRIPT = ROOT / 'scripts/consensus_prog_chart.py'
TIMEOUT_SECONDS = 1500
MAX_CARRY = timedelta(hours=48)
VIEWS = {'northeast': 'Northeast', 'conus': 'Continental US'}
MODELS = {'gfs': 'GFS', 'ifs': 'ECMWF IFS', 'aifs': 'ECMWF AIFS', 'gdps': 'Canadian GDPS'}
CENTER_RADIUS_KM, FRONT_RADIUS_KM = 1500, 800
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)
COMPASS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')


def _require(ok, message='invalid consensus prog'):
    if not ok:
        raise ValueError(message)


def _distance_bearing(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    d = 2 * 6371 * math.asin(min(1, math.sqrt(math.sin((lat2 - lat1) / 2) ** 2
                                           + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def _place(origin, lat, lon):
    distance, bearing = _distance_bearing(origin, (lat, lon))
    return {'distance_km': round(distance), 'direction': COMPASS[round(bearing / 22.5) % 16]}


def nearby(frame, airport):
    """Nearest consensus high, low and front to the airport, and how far apart the models put that low."""
    origin = (airport['latitude'], airport['longitude'])
    out = {}
    for key, centers in (('high', frame['highs']), ('low', frame['lows'])):
        close = [c | _place(origin, c['lat'], c['lon']) for c in centers]
        close = [c for c in close if c['distance_km'] <= CENTER_RADIUS_KM]
        if close:
            c = min(close, key=lambda c: c['distance_km'])
            out[key] = {k: c[k] for k in ('hPa', 'lat', 'lon', 'distance_km', 'direction')}
    fronts = []
    for front in frame['fronts']:
        points = [_place(origin, lat, lon) for lat, lon in front['points']]
        closest = min(points, key=lambda p: p['distance_km'])
        if closest['distance_km'] <= FRONT_RADIUS_KM:
            fronts.append({'type': front['type'], **closest})
    if fronts:
        out['front'] = min(fronts, key=lambda f: f['distance_km'])
    low = out.get('low')
    if low:
        placed = {}
        for model, lows in frame['model_lows'].items():
            near = [(_distance_bearing((low['lat'], low['lon']), (c['lat'], c['lon']))[0], c) for c in lows]
            near = [n for n in near if n[0] < 900]
            if near:
                placed[model] = min(near, key=lambda n: n[0])[1]
        spread = max((_distance_bearing((a['lat'], a['lon']), (b['lat'], b['lon']))[0]
                      for a in placed.values() for b in placed.values()), default=0)
        out['low_models'] = {'count': len(placed), 'spread_km': round(spread),
                             'hPa': sorted(round(c['hPa']) for c in placed.values())}
    return out


def envelope(manifest, snapshot, now, published):
    event = snapshot['event']
    frames = []
    for frame in manifest['frames']:
        frames.append({
            'valid': frame['valid'], 'analysis': frame['analysis'], 'precipitation': frame['precipitation'],
            'leads': {m: meta['lead'] for m, meta in frame['models'].items()},
            'point': frame['point'], 'nearby': nearby(frame, snapshot['airport']),
            'images': {view: {'url': f"/events/{event['slug']}/maps/{image['sha256']}.png",
                              **{k: image[k] for k in ('sha256', 'width', 'height')}}
                       for view, image in frame['images'].items() if view in VIEWS}})
    runs = set(manifest['runs'].values())
    _require(len(runs) == 1, 'Consensus prog must share one cycle')
    return {'version': VERSION, 'event': {k: event[k] for k in ('slug', 'date')}, 'cycle': runs.pop(),
            'prepared_at': manifest['prepared_at'], 'collected_at': iso_z(now), 'published': published,
            'models': sorted(manifest['runs'], key=list(MODELS).index), 'failures': len(manifest['failures']),
            'frames': frames}


def _upload(client, slug, root, manifest):
    """Upload images the Worker has not yet confirmed; the registry avoids resending unchanged frames."""
    from .coastal_publish import upload_frame
    registry = root / 'published.json'
    done = set(json.loads(registry.read_text())) if registry.exists() else set()
    folder = Path(manifest['path']).parent
    for frame in manifest['frames']:
        for image in frame['images'].values():
            if image['sha256'] in done:
                continue
            upload_frame(client, slug, {'file': str(folder / image['file']), 'sha256': image['sha256'],
                                        'width': image['width'], 'height': image['height']})
            done.add(image['sha256'])
            atomic_write(registry, json.dumps(sorted(done)) + '\n')


def collect(snapshot, var, now, client=None):
    """Render (or reuse) the current cycle, publish its images, and return the snapshot envelope.

    If this run cannot produce a cycle, the last good envelope is carried forward for up to 48 hours
    of cycle age and labeled as such.
    """
    from .event_model_matrix import flight_window
    event = snapshot['event']
    root = Path(var) / 'events' / event['slug'] / 'consensus-prog'
    root.mkdir(parents=True, exist_ok=True)
    current = root / 'current.json'
    try:
        _, end, _ = flight_window(snapshot)
        airport = snapshot['airport']
        command = [str(PYTHON), str(SCRIPT), '--cycle', 'common', '--through', iso_z(end),
                   '--point', f"{airport['latitude']},{airport['longitude']}", '--no-loop',
                   '--output-dir', str(root), '--cache-dir', str(Path(var) / 'charts/consensus-prog-cache')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=ROOT)
        lines = [line for line in result.stdout.splitlines() if line.startswith('{"manifest"')]
        _require(lines, f'Consensus chart run failed: {result.stderr.strip()[-300:]}')
        path = Path(json.loads(lines[-1])['manifest'])
        manifest = json.loads(path.read_text()) | {'path': str(path)}
        if client is not None:
            _upload(client, event['slug'], root, manifest)
        data = envelope(manifest, snapshot, now, client is not None)
        validate(data, event)
        atomic_write(current, json.dumps(data, separators=(',', ':')) + '\n')
        return data
    except Exception:
        if current.exists():
            data = json.loads(current.read_text())
            validate(data, event)
            if now - parse_time(data['cycle']) <= MAX_CARRY:
                return data | {'carried_over': True}
        raise


def validate(data, event):
    _require(isinstance(data, dict) and data.get('version') == VERSION)
    _require(data['event'] == {'slug': event['slug'], 'date': event['date']})
    cycle = parse_time(data['cycle'])
    _require(cycle.hour in (0, 12) and cycle.minute == 0)
    parse_time(data['prepared_at'])
    _require(set(data['models']) <= set(MODELS) and 2 <= len(data['models']) <= len(MODELS))
    frames = data['frames']
    _require(isinstance(frames, list) and 2 <= len(frames) <= 80)
    times = [parse_time(f['valid']) for f in frames]
    _require(times == sorted(set(times)) and times[0] >= cycle)
    for frame, at in zip(frames, times):
        _require(set(frame['leads']) <= set(MODELS) and len(frame['leads']) >= 2)
        _require(all(isinstance(h, int) and h == (at - cycle).total_seconds() / 3600 for h in frame['leads'].values()))
        _require(frame['analysis'] is (at == cycle) and isinstance(frame['precipitation'], bool))
        _require(set(frame['images']) == set(VIEWS))
        for image in frame['images'].values():
            _require(re.fullmatch(r'[a-f0-9]{64}', image['sha256']) is not None)
            _require(image['url'] == f"/events/{event['slug']}/maps/{image['sha256']}.png")
            _require(800 <= image['width'] <= 3000 and 800 <= image['height'] <= 3000)
        point = frame['point']
        _require(point is None or all(math.isfinite(point[k]) for k in ('msl_hpa', 'gradient_hpa_per_100km')))
    return data


# ---------------------------------------------------------------- rendering

def _local(value, fmt='%a %b %-d · %-I %p %Z'):
    return parse_time(value).astimezone(TZ).strftime(fmt)


def _miles(km):
    return f'{km * 0.621371:,.0f} mi'


def summary(frame):
    """Plain-language bullets for one frame, from the consensus centers, fronts and KCDW point."""
    items = []
    point, near = frame['point'], frame['nearby']
    if point:
        low, high = (f"{v:.0f}" for v in (min(m['msl_hpa'] for m in point['models'].values()),
                                          max(m['msl_hpa'] for m in point['models'].values())))
        agreement = f'all models {low}' if low == high else f'models {low}–{high}'
        items.append(f"KCDW sea-level pressure {point['msl_hpa']:.0f} hPa ({agreement}); "
                     f"gradient {point['gradient_hpa_per_100km']:.1f} hPa per 100 km, which balances about "
                     f"{point['geostrophic_kt']} kt above the surface layer.")
        wet = [m for m, v in point['models'].items() if v.get('precip6_mm', 0) >= 0.25]
        if frame['precipitation']:
            items.append(f"Rain at KCDW in the 6 h ending then: {len(wet)} of {len(point['models'])} models"
                         + (f" ({', '.join(MODELS[m] for m in wet)})." if wet else '.'))
    for key, word in (('high', 'High'), ('low', 'Low')):
        c = near.get(key)
        if c:
            items.append(f"{word} {c['hPa']:.0f} hPa, {_miles(c['distance_km'])} {c['direction']} of KCDW.")
    spread = near.get('low_models')
    if spread and spread['count'] >= 2:
        items.append(f"{spread['count']} models place that low within {_miles(spread['spread_km'])} of each other "
                     f"({min(spread['hPa'])}–{max(spread['hPa'])} hPa).")
    front = near.get('front')
    items.append(f"Nearest objective front: {front['type']}, {_miles(front['distance_km'])} {front['direction']}."
                 if front else f'No objective front within {_miles(FRONT_RADIUS_KM)}.')
    return items


def _card(title, frame, view='northeast'):
    image = frame['images'][view]
    lead = 'Analysis · F000' if frame['analysis'] else f"F{max(frame['leads'].values()):03d}"
    alt = f"Consensus surface {'analysis' if frame['analysis'] else 'prog'}, {VIEWS[view]}, valid {_local(frame['valid'])}"
    bullets = ''.join(f'<li>{escape(item)}</li>' for item in summary(frame))
    return (f'<article class="consensus-card"><div class="consensus-card-heading"><h3>{escape(title)}</h3>'
            f'<span>{escape(_local(frame["valid"]))} · {escape(lead)}</span></div>'
            f'<a class="consensus-expand" href="{escape(image["url"], quote=True)}" target="_blank" rel="noopener">'
            f'<img src="{escape(image["url"], quote=True)}" width="{image["width"]}" height="{image["height"]}" '
            f'alt="{escape(alt, quote=True)}" loading="lazy" decoding="async"></a><ul>{bullets}</ul></article>')


def render(data, snapshot, now):
    try:
        validate(data, snapshot['event'])
        from .event_model_matrix import flight_window
        start, _, label = flight_window(snapshot)
    except ERRORS:
        return ''
    frames = data['frames']
    flight = min(range(len(frames)), key=lambda i: abs(parse_time(frames[i]['valid']) - start))
    analysis = frames[0]
    cards = [_card('Consensus surface analysis' if analysis['analysis'] else 'Earliest consensus prog', analysis)]
    if flight:
        cards.append(_card(f"Checkride · {label}", frames[flight]))
    public = {'views': VIEWS, 'frames': [{'valid': f['valid'], 'label': _local(f['valid']), 'analysis': f['analysis'],
                                          'lead': max(f['leads'].values()),
                                          'urls': {v: f['images'][v]['url'] for v in VIEWS}} for f in frames],
              'initial': flight}
    payload = escape(json.dumps(public, separators=(',', ':')), quote=True)
    options = ''.join(f'<option value="{i}"{" selected" if i == flight else ""}>{escape(item["label"])}'
                      f'{" · analysis" if item["analysis"] else ""}</option>' for i, item in enumerate(public['frames']))
    views = ''.join(f'<option value="{k}">{escape(v)}</option>' for k, v in VIEWS.items())
    cycle = parse_time(data['cycle'])
    models = ', '.join(MODELS[m] for m in data['models'])
    age = now - cycle
    notes = []
    if data.get('carried_over'):
        notes.append('This run could not refresh the charts; showing the last complete cycle.')
    if data['failures']:
        notes.append(f"{data['failures']} model field(s) were unavailable; affected frames use the remaining models.")
    if not data['published']:
        notes.append('Images were rendered locally and not published.')
    note = ''.join(f'<p class="small">{escape(n)}</p>' for n in notes)
    first_frame = frames[flight]['images']['northeast']
    return f'''<section id="consensus-analysis" class="consensus-prog evidence-group" aria-labelledby="consensus-title" data-consensus="{payload}">
<div class="consensus-heading"><div><p class="eyebrow">Surface analysis / model consensus</p><h2 id="consensus-title">Consensus surface analysis &amp; prog</h2></div><span class="consensus-badge">{cycle:%HZ %b %-d} cycle · {len(data['models'])} models · {age.total_seconds() // 3600:.0f} h old</span></div>
<p>WPC-style charts built from the mean of {escape(models)}, all from the {cycle:%HZ %b %-d} cycle: the first frame averages their analyses, later frames their forecasts at the same lead. Averaging keeps what the models agree on, so lows get shallower and fronts disappear where they disagree; the letters on each chart show where each model puts its own low.</p>{note}
<div class="consensus-cards">{''.join(cards)}</div>
<div class="consensus-viewer" hidden><div class="consensus-controls"><label>View <select data-consensus-view>{views}</select></label><label>Valid <select data-consensus-time>{options}</select></label><div class="consensus-stepper"><button type="button" data-consensus-prev aria-label="Earlier chart">←</button><button type="button" data-consensus-next aria-label="Later chart">→</button></div></div>
<p class="consensus-status" aria-live="polite"></p><a class="consensus-expand" data-consensus-link href="{escape(first_frame['url'], quote=True)}" target="_blank" rel="noopener"><img data-consensus-image src="{escape(first_frame['url'], quote=True)}" width="{first_frame['width']}" height="{first_frame['height']}" alt="" loading="lazy" decoding="async"></a></div>
<details class="consensus-methods"><summary>How these charts are made</summary><p>Every 6 hours from the {cycle:%HZ %b %-d} analysis through the flight. Isobars and H/L: mean sea-level pressure. Fronts: objective, from the thermal front parameter of mean 850 hPa θe (Hewson 1998), typed by the 850 hPa wind across the front; they can miss weak fronts and add short spurious ones along coasts. Shading: 6 h precipitation where at least half the models agree; darker where 3 of 4 agree on ≥ 0.10 in; red hatching for thunder (≥ 1000 J/kg CAPE). The KCDW gradient is from the smoothed mean pressure; its balanced wind is an above-surface scale, not a surface wind or gust forecast. Charts prepared {escape(_local(data['prepared_at'], '%b %-d %H:%M %Z'))}. Sources: <a href="https://registry.opendata.aws/noaa-gfs-bdp-pds/">NOAA GFS</a> · <a href="https://www.ecmwf.int/en/forecasts/datasets/open-data">ECMWF open data (CC BY 4.0)</a> · <a href="https://eccc-msc.github.io/open-data/msc-data/nwp_gdps/readme_gdps_en/">ECCC GDPS</a>; boundaries Natural Earth. Not an official WPC/AWC product.</p></details>
</section>'''
