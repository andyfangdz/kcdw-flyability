"""Wind by height at the flight hour: surface, ~2,500 ft (925 hPa) and ~5,000 ft (850 hPa).

Afternoon mixing can bring part of the wind aloft to the surface as gusts, and
how the direction turns with height shows whether a model's surface wind is
tied to the flow above it. Each row is one model bound to an explicit run:
Open-Meteo single-runs for the physics and AI globals, and NOAA's AIGFS (AI
model on GFS initial conditions) read natively through the isolated ecCodes
worker. Every model is sampled at the same instant: the 6-hourly synoptic time
nearest the middle of the flight (AIGFS has only 6-hourly output). Each model
keeps its newest covering runs, so a run-to-run change is shown. Run-pinned
results never change and are cached per (model, run).
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from .common import UTC, atomic_write, iso_z, parse_time
from .events import TZ

VERSION = 1
API = 'https://single-runs-api.open-meteo.com/v1/forecast'
AIGFS_ROOT = 'https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/aigfs.'
AIGFS_HORIZON = 384
PYTHON = Path(__file__).resolve().parents[1] / 'var/native-weather-venv/bin/python'
# key, label, Open-Meteo model or native source, AI model.
MODELS = (
    ('ifs', 'ECMWF IFS', 'ecmwf_ifs025', False),
    ('aifs', 'ECMWF AIFS', 'ecmwf_aifs025_single', True),
    ('gfs', 'NCEP GFS', 'gfs_global', False),
    ('aigfs', 'NOAA AIGFS', 'native:aigfs', True),
    ('icon', 'DWD ICON', 'icon_global', False),
    ('ukmo', 'UKMO Global', 'ukmo_global_deterministic_10km', False),
)
LEVELS = (('10', 'Surface', '10m'), ('925', '~2,500 ft', '925hPa'), ('850', '~5,000 ft', '850hPa'))
FIELDS = tuple(f'wind_{kind}_{suffix}' for _, _, suffix in LEVELS for kind in ('speed', 'direction')) + ('pressure_msl',)
KEEP_RUNS = 3
CANDIDATES = 6
MAX_RUN_AGE = timedelta(hours=48)
DEADLINE_SECONDS = 90
SETTLED_AFTER = timedelta(hours=12)
NOTES = [
    'Knots; true degrees. One instant per model: the 6-hourly synoptic time nearest the middle of the flight, because AIGFS is 6-hourly.',
    'Heights are approximate: 925 hPa is about 2,500 ft and 850 hPa about 5,000 ft above sea level on a normal-pressure day.',
    'Afternoon mixing can bring part of the wind aloft down as gusts; how much depends on how deep the mixing reaches. This is gust potential, not a gust forecast.',
    'AIFS and AIGFS are AI models: AIFS is initialized from ECMWF analyses, AIGFS from the same GFS/GDAS analyses as GFS. A GFS-only feature that AIGFS does not share points to GFS physics rather than its starting data.',
    'IFS uses Open-Meteo\'s 0.25-degree IFS for its pressure levels; its surface can differ slightly from the 9 km IFS elsewhere on this page.',
]
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _require(ok):
    if not ok:
        raise ValueError('invalid wind profile')


def _number(value, low, high):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def sample_time(snapshot):
    """The 6-hourly UTC synoptic time nearest the middle of the flight."""
    from .event_model_matrix import flight_window
    start, end, _ = flight_window(snapshot)
    middle = start + (end - start) / 2
    base = middle.replace(minute=0, second=0, microsecond=0) - timedelta(hours=middle.hour % 6)
    return min((base, base + timedelta(hours=6)), key=lambda t: (abs(t - middle), t))


def candidate_runs(now):
    latest = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    latest -= timedelta(hours=latest.hour % 6)
    return [latest - timedelta(hours=6 * i) for i in range(CANDIDATES)]


def run_url(model_id, run, at, latitude, longitude):
    days = min(16, (at.date() - run.date()).days + 2)
    return API + '?' + urlencode(dict(latitude=f'{latitude:.4f}', longitude=f'{longitude:.4f}', hourly=','.join(FIELDS),
                                      models=model_id, wind_speed_unit='kn', timezone='UTC', timeformat='iso8601',
                                      forecast_days=str(days), run=run.strftime('%Y-%m-%dT%H:%M')))


def _values(values):
    """Checked values, or None when any level is missing."""
    out = {}
    for level, _, _ in LEVELS:
        out[f'wind{level}_kt'] = _number(values.get(f'wind{level}_kt'), 0, 250)
        out[f'from{level}_deg'] = _number(values.get(f'from{level}_deg'), 0, 360)
    out['mslp_hpa'] = _number(values.get('mslp_hpa'), 850, 1100)
    if any(v is None for v in out.values()):
        return None
    return {k: round(v, 1) for k, v in out.items()}


def _open_meteo(client, model_id, run, at, airport):
    url = run_url(model_id, run, at, airport['latitude'], airport['longitude'])
    raw = client.get(url)
    hourly = raw.get('hourly') if isinstance(raw, dict) else None
    _require(isinstance(hourly, dict) and raw.get('utc_offset_seconds') == 0 and isinstance(hourly.get('time'), list))
    stamps = [parse_time(t) for t in hourly['time']]
    stamps = [t if t.tzinfo else t.replace(tzinfo=UTC) for t in stamps]
    if at not in stamps:
        return None
    i = stamps.index(at)
    pick = lambda name: hourly[name][i] if isinstance(hourly.get(name), list) and len(hourly[name]) == len(stamps) else None
    values = {f'wind{level}_kt': pick(f'wind_speed_{suffix}') for level, _, suffix in LEVELS}
    values.update({f'from{level}_deg': pick(f'wind_direction_{suffix}') for level, _, suffix in LEVELS})
    values['mslp_hpa'] = pick('pressure_msl')
    checked = _values(values)
    if checked is None:
        return None
    grid = {'latitude': _number(raw.get('latitude'), -90, 90), 'longitude': _number(raw.get('longitude'), -180, 180)}
    return {'run': iso_z(run), 'source_url': url, 'grid': grid, 'values': checked}


def _aigfs(run, at):
    lead = int((at - run).total_seconds() // 3600)
    if not 0 <= lead <= AIGFS_HORIZON or lead % 6:
        return None
    result = subprocess.run([str(PYTHON), str(Path(__file__).with_name('aigfs_worker.py'))],
                            input=json.dumps({'init': iso_z(run), 'leads': [lead]}), text=True,
                            capture_output=True, check=True, timeout=210)
    [point] = json.loads(result.stdout)
    _require(point['at'] == iso_z(at) and point['lead'] == lead)
    checked = _values(point)
    _require(checked is not None)
    return {'run': iso_z(run), 'source_url': point['url'], 'grid': {'latitude': point['latitude'], 'longitude': point['longitude']},
            'values': checked}


def load_cache(path, key):
    fresh = {'version': VERSION, 'key': key, 'models': {}}
    if path is None:
        return fresh
    try:
        cache = json.loads(Path(path).read_text(encoding='utf-8'))
        if cache.get('version') == VERSION and cache.get('key') == key and isinstance(cache.get('models'), dict):
            return cache
    except (OSError, ValueError, AttributeError):
        pass
    return fresh


def collect_model(client, spec, at, airport, now, deadline, cached):
    key, label, model_id, ai = spec
    runs = []
    for run in candidate_runs(now):
        if len(runs) == KEEP_RUNS or run >= at:
            continue
        stamp = iso_z(run)
        if stamp in cached:
            if cached[stamp]:
                runs.append(cached[stamp])
            continue
        if time.monotonic() > deadline:
            continue
        try:
            entry = _aigfs(run, at) if model_id == 'native:aigfs' else _open_meteo(client, model_id, run, at, airport)
        except Exception:
            continue  # not yet published or a transient failure; retried next refresh
        if entry:
            cached[stamp] = entry
            runs.append(entry)
        elif now - run >= SETTLED_AFTER:
            cached[stamp] = None  # a settled run that stops short of the flight never reaches it
    for stamp in [s for s in cached if now - parse_time(s) > MAX_RUN_AGE]:
        del cached[stamp]
    row = {'key': key, 'label': label, 'model_id': model_id, 'ai': ai, 'ok': bool(runs), 'runs': runs}
    if not runs:
        row['error'] = 'no recent run reaches the flight'
    return row


def collect_profile(client, snapshot, now, cache_path=None):
    from .event_model_matrix import flight_window
    now = now.astimezone(UTC)
    start, end, kind = flight_window(snapshot)
    at = sample_time(snapshot)
    if now >= end or at - now > timedelta(days=15):
        return None
    cache = load_cache(cache_path, f'{snapshot["event"]["slug"]}|{iso_z(at)}')
    deadline = time.monotonic() + DEADLINE_SECONDS
    rows = [collect_model(client, spec, at, snapshot['airport'], now, deadline, cache['models'].setdefault(spec[0], {}))
            for spec in MODELS]
    if cache_path is not None:
        atomic_write(cache_path, json.dumps(cache, allow_nan=False, separators=(',', ':')) + '\n')
    if not any(row['ok'] for row in rows):
        return None
    return {'version': VERSION, 'collected_at': iso_z(now), 'snapshot_collected_at': snapshot['collected_at'],
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind}, 'sample_at': iso_z(at), 'models': rows,
            'source': 'Open-Meteo single-runs API (CC BY 4.0); NOAA AIGFS native GRIB (public domain)', 'notes': list(NOTES)}


def validate_profile(packet, snapshot):
    """Recheck binding, runs and every value; raise ValueError on failure."""
    from .event_model_matrix import flight_window
    _require(isinstance(packet, dict) and packet.get('version') == VERSION)
    _require(packet.get('snapshot_collected_at') == snapshot['collected_at'])
    _require(packet.get('event') == {k: snapshot['event'][k] for k in ('slug', 'date', 'window')})
    start, end, kind = flight_window(snapshot)
    _require(packet.get('window') == {'start': iso_z(start), 'end': iso_z(end), 'kind': kind})
    at = sample_time(snapshot)
    _require(packet.get('sample_at') == iso_z(at))
    collected = parse_time(packet['collected_at'])
    _require(abs(collected - parse_time(snapshot['collected_at'])) <= timedelta(minutes=30))
    rows = packet.get('models')
    _require(isinstance(rows, list) and [r.get('key') if isinstance(r, dict) else None for r in rows] == [m[0] for m in MODELS])
    for row, (key, label, model_id, ai) in zip(rows, MODELS):
        _require(row.get('label') == label and row.get('model_id') == model_id and row.get('ai') is ai and type(row.get('ok')) is bool)
        runs = row.get('runs')
        _require(isinstance(runs, list) and len(runs) <= KEEP_RUNS and bool(runs) == row['ok'])
        stamps = [parse_time(r['run']) for r in runs]
        _require(stamps == sorted(set(stamps), reverse=True))
        for run, entry in zip(stamps, runs):
            _require(run.hour % 6 == 0 and not run.minute and run <= collected and collected - run <= MAX_RUN_AGE and run < at)
            prefix = f'{AIGFS_ROOT}{run:%Y%m%d}/{run:%H}/' if model_id == 'native:aigfs' else API + '?'
            _require(isinstance(entry.get('source_url'), str) and entry['source_url'].startswith(prefix))
            _require(isinstance(entry.get('values'), dict) and _values(entry['values']) == entry['values'])
    _require(any(row['ok'] for row in rows))
    return packet


def _wind(values, level):
    return f'{values[f"from{level}_deg"]:03.0f}° {values[f"wind{level}_kt"]:.0f} kt'


def _change(runs):
    if len(runs) < 2:
        return None
    now, then = runs[0]['values'], runs[1]['values']
    turn = (now['from10_deg'] - then['from10_deg'] + 180) % 360 - 180
    return {'previous_run': runs[1]['run'], 'aloft_kt': round(now['wind850_kt'] - then['wind850_kt'], 1),
            'surface_kt': round(now['wind10_kt'] - then['wind10_kt'], 1), 'surface_turn_deg': round(turn)}


def _story(rows, at):
    ok = [r for r in rows if r['ok']]
    if not ok:
        return []
    aloft = sorted(ok, key=lambda r: r['runs'][0]['values']['wind850_kt'])
    lo, hi = aloft[0], aloft[-1]
    surface = sorted(r['runs'][0]['values']['from10_deg'] for r in ok)
    # Smallest arc covering every direction: 360 minus the widest gap between neighbours.
    gaps = [((surface[(i + 1) % len(surface)] - d) % 360, i) for i, d in enumerate(surface)]
    widest, after = max(gaps) if len(surface) > 1 else (360, 0)
    first, last = surface[(after + 1) % len(surface)], surface[after]
    spread = 360 - widest if len(surface) > 1 else 0
    parts = [f'At about 5,000 ft the models range from {lo["runs"][0]["values"]["wind850_kt"]:.0f} kt ({lo["label"]}) to '
             f'{hi["runs"][0]["values"]["wind850_kt"]:.0f} kt ({hi["label"]}). Afternoon mixing can bring part of that wind down as gusts, '
             'so the stronger the wind aloft, the more gust potential.']
    if len(surface) > 1 and spread <= 180:
        parts.append(f'Surface directions span about {spread:.0f}° across the models, from {first:03.0f}° to {last:03.0f}°.')
    pairs = {r['key']: r for r in ok}
    for physics, ai in (('gfs', 'aigfs'), ('ifs', 'aifs')):
        if physics in pairs and ai in pairs:
            a, b = pairs[physics]['runs'][0]['values'], pairs[ai]['runs'][0]['values']
            gap = abs((a['from10_deg'] - b['from10_deg'] + 180) % 360 - 180)
            if gap >= 30:
                parts.append(f'{pairs[physics]["label"]} and its AI counterpart {pairs[ai]["label"]} disagree on surface direction by about '
                             f'{gap:.0f}° ({a["from10_deg"]:03.0f}° vs {b["from10_deg"]:03.0f}°).')
    return [p for p in parts if p]


def profile_evidence(snapshot, now):
    try:
        packet = validate_profile(snapshot.get('wind_profile'), snapshot)
    except ERRORS:
        return None
    models = []
    for row in packet['models']:
        if not row['ok']:
            continue
        latest = row['runs'][0]
        models.append({'model': row['label'] + (' (AI)' if row['ai'] else ''), 'run': latest['run'],
                       **{name: _wind(latest['values'], level) for level, name in (('10', 'surface'), ('925', 'about_2500_ft'), ('850', 'about_5000_ft'))},
                       'mslp_hpa': latest['values']['mslp_hpa'], 'change_since_previous_run': _change(row['runs'])})
    return {'sample_at': packet['sample_at'], 'summary': _story(packet['models'], parse_time(packet['sample_at'])),
            'models': models, 'notes': NOTES[:3]}


def render_profile(snapshot, now):
    try:
        packet = validate_profile(snapshot.get('wind_profile'), snapshot)
    except ERRORS:
        return ''
    at = parse_time(packet['sample_at'])
    rows = packet['models']

    def change_cell(runs):
        change = _change(runs)
        if not change:
            return '<td>—</td>'
        turn = change['surface_turn_deg']
        detail = f'surface {change["surface_kt"]:+.0f} kt, {"veered" if turn > 0 else "backed"} {abs(turn)}°' if abs(turn) >= 10 else f'surface {change["surface_kt"]:+.0f} kt'
        return (f'<td>{change["aloft_kt"]:+.0f} kt aloft<small>{escape(detail)} · vs {escape(parse_time(change["previous_run"]).strftime("%b %-d %HZ"))}</small></td>')

    lines = []
    for row in rows:
        tag = ' · AI' if row['ai'] else ''
        if not row['ok']:
            lines.append(f'<tr><th scope="row">{escape(row["label"])}{tag}<small>no recent run</small></th><td colspan="5">—</td></tr>')
            continue
        latest = row['runs'][0]
        values = latest['values']
        lines.append(f'<tr><th scope="row">{escape(row["label"])}{tag}<small>{escape(parse_time(latest["run"]).strftime("%b %-d %HZ"))} run</small></th>'
                     + ''.join(f'<td>{escape(_wind(values, level))}</td>' for level, _, _ in LEVELS)
                     + f'<td>{values["mslp_hpa"]:.1f} hPa</td>{change_cell(row["runs"])}</tr>')
    body = ''.join(lines)
    story = ''.join(f'<p>{escape(p)}</p>' for p in _story(rows, at))
    when = at.astimezone(TZ).strftime('%a %b %-d, %-I %p %Z').replace(' 0', ' ')
    head = ''.join(f'<th scope="col">{escape(name)}</th>' for _, name, _ in LEVELS)
    return (f'<section id="wind-profile" class="weather-pattern" aria-labelledby="wind-profile-title">'
            f'<p class="eyebrow">Wind by height · {escape(when)}</p><h2 id="wind-profile-title">Wind aloft and the AI models</h2>{story}'
            f'<div class="table-wrap"><table><caption>Wind from (true) and speed at one instant, {escape(when)} ({escape(iso_z(at)[11:16])}Z), '
            f'from each model\'s newest run that reaches it. AI marks machine-learning models.</caption>'
            f'<thead><tr><th scope="col">Model · run</th>{head}<th scope="col">Sea-level pressure</th><th scope="col">Since previous run</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>'
            f'<details><summary>Wind-by-height sources &amp; limits</summary><ul>{"".join("<li>" + escape(n) + "</li>" for n in NOTES)}</ul>'
            f'<p><a href="https://open-meteo.com/en/docs/single-runs-api">Open-Meteo single runs</a> · '
            f'<a href="https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/index.html">NOAA AIGFS on AWS</a></p></details></section>')
