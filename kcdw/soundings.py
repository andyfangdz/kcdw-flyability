"""Model soundings at the flight hour, analysed with SHARPpy.

One vertical profile per model at the same instant as the wind-by-height table
(the 6-hourly synoptic time nearest mid-flight), each from an explicitly
requested run: native NCEP GFS from the NOMADS grib filter (19 levels, 25 hPa
apart near the ground), native NOAA AIGFS from AWS, and ECMWF IFS/AIFS, ICON and
UKMO from Open-Meteo's single-runs archive, which keeps only the standard
1000/925/850/700 hPa levels below 10,000 ft. SHARPpy (isolated worker, pinned
environment) supplies the parcels, the boundary-layer top and the wind inside the
mixed layer. Run-pinned profiles and their analyses never change and are cached
per (model, run).
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from datetime import timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from .common import UTC, atomic_write, iso_z, parse_time
from .events import TZ
from .wind_profile import candidate_runs, sample_time

VERSION = 1
# Bump when the SHARPpy worker's method changes: cached analyses are keyed by it.
ANALYSIS_VERSION = 3
API = 'https://single-runs-api.open-meteo.com/v1/forecast'
NOMADS = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?'
AIGFS_ROOT = 'https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/aigfs.'
ROOT = Path(__file__).resolve().parents[1]
NATIVE_PYTHON = ROOT / 'var/native-weather-venv/bin/python'
SHARPPY_PYTHON = ROOT / 'var/sharppy-venv/bin/python'
# key, label, source, AI model.
MODELS = (
    ('gfs', 'NCEP GFS', 'native:gfs', False),
    ('aigfs', 'NOAA AIGFS', 'native:aigfs', True),
    ('ifs', 'ECMWF IFS', 'ecmwf_ifs025', False),
    ('aifs', 'ECMWF AIFS', 'ecmwf_aifs025_single', True),
    ('icon', 'DWD ICON', 'icon_global', False),
    ('ukmo', 'UKMO Global', 'ukmo_global_deterministic_10km', False),
)
LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200)
KINDS = ('temperature', 'dew_point', 'geopotential_height', 'wind_speed', 'wind_direction')
SURFACE = ('temperature_2m', 'dew_point_2m', 'surface_pressure', 'wind_speed_10m', 'wind_direction_10m')
KEEP_RUNS = 2
MAX_RUN_AGE = timedelta(hours=48)
SETTLED_AFTER = timedelta(hours=12)
DEADLINE_SECONDS = 150
FT = 3.28084
MOIST_DEPRESSION_C = 3.0
NOTES = [
    'One instant per model, the 6-hourly synoptic time nearest mid-flight; temperatures °C, winds knots from true north, heights above the model\'s own ground.',
    'Mixing height is SHARPpy\'s boundary-layer top (where virtual potential temperature first rises 0.5 K above the surface value). Wind inside that layer can mix down as gusts; the strongest mixed-layer wind is gust potential, not a gust forecast.',
    'Cloud base is the surface parcel\'s lifting condensation level: where cumulus would form if surface air mixed up to it. Moist layers are levels with a temperature–dew point spread of 3 °C or less.',
    'GFS comes natively from NOMADS at 25 hPa spacing near the ground. Open-Meteo\'s run archive keeps only 1000/925/850/700 hPa below about 10,000 ft for IFS, AIFS, ICON and UKMO, and AIGFS has the same levels. Every profile is interpolated every 10 hPa (linear in log pressure) before SHARPpy, which places the mixing top between levels but cannot recover structure the coarse models do not report.',
    'The dashed line is SHARPpy\'s surface parcel, drawn as virtual temperature as in SHARPpy\'s own skew-T.',
]
MIXING_BLURB = ('How the mixing top is found: SHARPpy takes the model\'s 2 m afternoon air, adds 0.5 K, and lifts it dry-adiabatically '
                'until the surrounding air is warmer (in virtual potential temperature). Below that height thermals keep stirring the air, so '
                'wind there can reach the ground as gusts; above it they stop. A warmer surface or a weaker inversion raises it, and coarse '
                'model levels make it approximate to a few hundred feet.')
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _require(ok):
    if not ok:
        raise ValueError('invalid model soundings')


def _number(value, low, high):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def check_levels(levels):
    """[[hPa, m MSL, T, Td, dir, kt], ...] surface first; raise ValueError unless physical and ordered."""
    _require(isinstance(levels, list) and 6 <= len(levels) <= 60)
    for row in levels:
        _require(isinstance(row, list) and len(row) == 6)
        p, h, t, td, d, s = row
        _require(_number(p, 150, 1100) is not None and _number(h, -500, 20000) is not None)
        _require(_number(t, -90, 60) is not None and _number(td, -120, 60) is not None and td <= t + 0.01)
        _require(_number(d, 0, 360) is not None and _number(s, 0, 250) is not None)
    _require(all(a[0] > b[0] and a[1] < b[1] for a, b in zip(levels, levels[1:])))
    return levels


def _open_meteo(client, model_id, run, at, airport):
    days = min(16, (at.date() - run.date()).days + 2)
    url = API + '?' + urlencode(dict(latitude=f'{airport["latitude"]:.4f}', longitude=f'{airport["longitude"]:.4f}',
                                     hourly=','.join(SURFACE + tuple(f'{k}_{p}hPa' for k in KINDS for p in LEVELS)),
                                     models=model_id, wind_speed_unit='kn', timezone='UTC', timeformat='iso8601',
                                     forecast_days=str(days), run=run.strftime('%Y-%m-%dT%H:%M')))
    raw = client.get(url)
    hourly = raw.get('hourly') if isinstance(raw, dict) else None
    _require(isinstance(hourly, dict) and raw.get('utc_offset_seconds') == 0 and isinstance(hourly.get('time'), list))
    stamps = [parse_time(t) for t in hourly['time']]
    stamps = [t if t.tzinfo else t.replace(tzinfo=UTC) for t in stamps]
    if at not in stamps:
        return None
    i = stamps.index(at)
    pick = lambda name: hourly[name][i] if isinstance(hourly.get(name), list) and len(hourly[name]) == len(stamps) else None
    ground = _number(raw.get('elevation'), -500, 9000)
    surface = [pick('surface_pressure'), ground, pick('temperature_2m'), pick('dew_point_2m'),
               pick('wind_direction_10m'), pick('wind_speed_10m')]
    if any(v is None for v in surface):
        return None
    levels = [surface]
    for p in LEVELS:
        row = [float(p), pick(f'geopotential_height_{p}hPa'), pick(f'temperature_{p}hPa'), pick(f'dew_point_{p}hPa'),
               pick(f'wind_direction_{p}hPa'), pick(f'wind_speed_{p}hPa')]
        if p >= surface[0] - 1 or (row[1] is not None and row[1] <= ground + 10):
            continue  # below or at the model ground
        if any(v is None for v in row):
            return None
        row[3] = min(row[3], row[2])
        levels.append(row)
    levels = [[round(float(v), 2) for v in row] for row in levels]
    check_levels(levels)
    grid = {'latitude': _number(raw.get('latitude'), -90, 90), 'longitude': _number(raw.get('longitude'), -180, 180)}
    return {'run': iso_z(run), 'source_url': url, 'grid': grid, 'levels': levels}


def _native(source, run, at):
    lead = int((at - run).total_seconds() // 3600)
    if not 0 <= lead <= 384 or lead % 6:
        return None
    result = subprocess.run([str(NATIVE_PYTHON), str(Path(__file__).with_name('sounding_worker.py'))],
                            input=json.dumps({'source': source, 'init': iso_z(run), 'lead': lead}), text=True,
                            capture_output=True, check=True, timeout=210)
    packet = json.loads(result.stdout)
    _require(packet['valid'] == iso_z(at))
    return {'run': iso_z(run), 'source_url': packet['source_url'], 'grid': packet['grid'], 'levels': check_levels(packet['levels'])}


def analyse(entries):
    """SHARPpy parameters for each {'id', 'levels'}; one isolated worker call."""
    request = {'profiles': [{'id': e['id'], **{k: [row[i] for row in e['levels']] for i, k in
                                               enumerate(('pres', 'hght', 'tmpc', 'dwpc', 'wdir', 'wspd'))}} for e in entries]}
    result = subprocess.run([str(SHARPPY_PYTHON), str(Path(__file__).with_name('sharppy_worker.py'))],
                            input=json.dumps(request), text=True, capture_output=True, check=True, timeout=90)
    out = {item['id']: item for item in json.loads(result.stdout)}
    _require(set(out) <= {e['id'] for e in entries})  # an unanalysable profile is omitted, not guessed
    return out


def check_analysis(a, levels):
    _require(isinstance(a, dict))
    depth = levels[-1][1] - levels[0][1]
    height = lambda v, optional=True: _require((optional and v is None) or (_number(v, -50, depth + 50) is not None))
    for name in ('surface', 'mixed_layer'):
        parcel = a[name]
        height(parcel['lcl_m'])
        height(parcel['lfc_m'])
        _require(_number(parcel['cape'], 0, 10000) is not None and _number(parcel['cin'], -2000, 0) is not None)
    top = a['mixing_top']
    _require(_number(top['pres'], levels[-1][0], levels[0][0]) is not None)
    height(top['hght_m'], optional=False)
    peak = a['mixed_layer_max_wind']
    _require(_number(peak['wspd'], 0, 250) is not None and _number(peak['wdir'], 0, 360) is not None)
    height(peak['hght_m'], optional=False)
    mean = a['mixed_layer_mean_wind']
    _require(mean is None or (_number(mean['wspd'], 0, 250) is not None and _number(mean['wdir'], 0, 360) is not None))
    _require(a['lapse_rate_0_3km'] is None or _number(a['lapse_rate_0_3km'], -30, 30) is not None)
    _require(a['precip_water_in'] is None or _number(a['precip_water_in'], 0, 4) is not None)
    trace = a['parcel_trace']
    _require(isinstance(trace, list) and len(trace) <= 400)
    _require(all(isinstance(r, list) and len(r) == 2 and _number(r[0], 50, 1100) is not None
                 and _number(r[1], -150, 70) is not None for r in trace))
    return a


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
    key, _, model_id, _ = spec
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
            entry = (_native(model_id.split(':')[1], run, at) if model_id.startswith('native:')
                     else _open_meteo(client, model_id, run, at, airport))
        except Exception:
            continue  # not yet published or a transient failure; retried next refresh
        if entry:
            cached[stamp] = entry
            runs.append(entry)
        elif now - run >= SETTLED_AFTER:
            cached[stamp] = None  # a settled run that never reaches the flight
    for stamp in [s for s in cached if now - parse_time(s) > MAX_RUN_AGE]:
        del cached[stamp]
    return runs


def collect_soundings(client, snapshot, now, cache_path=None):
    from .event_model_matrix import flight_window
    now = now.astimezone(UTC)
    start, end, kind = flight_window(snapshot)
    at = sample_time(snapshot)
    if now >= end or at - now > timedelta(days=15):
        return None
    cache = load_cache(cache_path, f'{snapshot["event"]["slug"]}|{iso_z(at)}|analysis-{ANALYSIS_VERSION}')
    deadline = time.monotonic() + DEADLINE_SECONDS
    found = {spec[0]: collect_model(client, spec, at, snapshot['airport'], now, deadline, cache['models'].setdefault(spec[0], {}))
             for spec in MODELS}
    pending = [dict(e, id=f'{key}|{e["run"]}') for key, runs in found.items() for e in runs if 'analysis' not in e]
    if pending:
        try:
            results = analyse(pending)
        except Exception:
            results = {}
        for key, runs in found.items():
            for e in runs:
                a = results.get(f'{key}|{e["run"]}')
                if a is not None:
                    a.pop('id', None)
                    try:
                        e['analysis'] = check_analysis(a, e['levels'])
                    except ERRORS:
                        pass
    if cache_path is not None:
        atomic_write(cache_path, json.dumps(cache, allow_nan=False, separators=(',', ':')) + '\n')
    rows = []
    for key, label, model_id, ai in MODELS:
        runs = [e for e in found[key] if 'analysis' in e]
        rows.append({'key': key, 'label': label, 'model_id': model_id, 'ai': ai, 'ok': bool(runs), 'runs': runs[:KEEP_RUNS]})
    if not any(r['ok'] for r in rows):
        return None
    return {'version': VERSION, 'collected_at': iso_z(now), 'snapshot_collected_at': snapshot['collected_at'],
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind}, 'sample_at': iso_z(at), 'models': rows,
            'analysis': 'SHARPpy sharptab (github.com/sharppy/SHARPpy @357a14e)',
            'source': 'NOAA NOMADS GFS and AWS AIGFS native GRIB (public domain); Open-Meteo single-runs API (CC BY 4.0)'}


def validate_soundings(packet, snapshot):
    """Recheck binding, runs, profiles and every SHARPpy value; raise ValueError on failure."""
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
            prefix = (NOMADS if model_id == 'native:gfs' else f'{AIGFS_ROOT}{run:%Y%m%d}/{run:%H}/' if model_id == 'native:aigfs' else API + '?')
            _require(isinstance(entry.get('source_url'), str) and entry['source_url'].startswith(prefix))
            if model_id == 'native:gfs':
                _require(f'gfs.{run:%Y%m%d}%2F{run:%H}' in entry['source_url'])
            check_levels(entry.get('levels'))
            check_analysis(entry.get('analysis'), entry['levels'])
    _require(any(row['ok'] for row in rows))
    return packet


def _ft(m):
    return None if m is None else int(round(m * FT, -2))


def _moist_layers(levels):
    """Bottom/top (ft above ground) of runs of levels with a small temperature–dew point spread, below 700 hPa."""
    ground, layers, current = levels[0][1], [], None
    for p, h, t, td, _, _ in levels:
        if p < 700:
            break
        if t - td <= MOIST_DEPRESSION_C:
            current = [h, h] if current is None else [current[0], h]
        elif current is not None:
            layers.append(current)
            current = None
    if current is not None:
        layers.append(current)
    return [[_ft(a - ground), _ft(b - ground)] for a, b in layers]


def summary(entry):
    a, levels = entry['analysis'], entry['levels']
    peak, mean = a['mixed_layer_max_wind'], a['mixed_layer_mean_wind']
    return {'mixing_height_ft': _ft(a['mixing_top']['hght_m']), 'mixed_layer_max_wind': f'{peak["wdir"]:03.0f}° {peak["wspd"]:.0f} kt',
            'mixed_layer_max_wind_kt': peak['wspd'], 'mixed_layer_max_wind_ft': _ft(peak['hght_m']),
            'mixed_layer_mean_wind': None if mean is None else f'{mean["wdir"]:03.0f}° {mean["wspd"]:.0f} kt',
            'cumulus_base_ft': _ft(a['surface']['lcl_m']), 'surface_cape': a['surface']['cape'],
            'lapse_rate_0_3km_c_per_km': a['lapse_rate_0_3km'], 'precip_water_in': a['precip_water_in'],
            'moist_layers_ft': _moist_layers(levels), 'surface': f'{levels[0][2]:.0f} / {levels[0][3]:.0f} °C'}


def _story(rows):
    ok = [(r, summary(r['runs'][0])) for r in rows if r['ok']]
    if not ok:
        return []
    by_height = sorted(ok, key=lambda x: x[1]['mixing_height_ft'])
    by_wind = sorted(ok, key=lambda x: x[1]['mixed_layer_max_wind_kt'])
    parts = [f'SHARPpy puts the afternoon mixing height between about {by_height[0][1]["mixing_height_ft"]:,} ft ({by_height[0][0]["label"]}) and '
             f'{by_height[-1][1]["mixing_height_ft"]:,} ft ({by_height[-1][0]["label"]}) above the ground. The strongest wind inside that mixed layer ranges from '
             f'{by_wind[0][1]["mixed_layer_max_wind_kt"]:.0f} kt ({by_wind[0][0]["label"]}) to {by_wind[-1][1]["mixed_layer_max_wind_kt"]:.0f} kt '
             f'({by_wind[-1][0]["label"]}); that is the wind available to mix down as gusts.']
    bases = sorted(s['cumulus_base_ft'] for _, s in ok if s['cumulus_base_ft'] is not None)
    if bases:
        parts.append(f'Surface-parcel cloud bases would be about {bases[0]:,}–{bases[-1]:,} ft above the ground.')
    moist = [r['label'] for r, s in ok if s['moist_layers_ft']]
    if moist:
        parts.append(f'Near-saturated layers below 700 hPa: {", ".join(moist)}.')
    unstable = [r['label'] for r, s in ok if s['surface_cape'] >= 100]
    if unstable:
        parts.append(f'Surface-based instability of 100 J/kg or more: {", ".join(unstable)}.')
    return parts


MID_CLOUD_NOTE = ('flight_mid_cloud is the model-matrix mean mid-level cloud fraction over the expected flight from the named run, '
                  'which can differ from the sounding run; null when unavailable. It is coverage, not thickness or a ceiling.')


def _mid_cloud(snapshot):
    """Flight-window mid-level cloud by model key from the validated model matrix."""
    from .event_model_matrix import validate_matrix
    try:
        rows = validate_matrix(snapshot.get('model_matrix'), snapshot)['models']
    except ERRORS:
        return {}
    return {r['key']: {'pct': r['current']['values']['mid_cloud_pct'], 'run': r['current']['run']}
            for r in rows if r['ok'] and r['current']['values'].get('mid_cloud_pct') is not None}


def sounding_evidence(snapshot, now):
    try:
        packet = validate_soundings(snapshot.get('model_soundings'), snapshot)
    except ERRORS:
        return None
    mid = _mid_cloud(snapshot)
    models = []
    for row in packet['models']:
        if row['ok']:
            s = summary(row['runs'][0])
            previous = summary(row['runs'][1]) if len(row['runs']) > 1 else None
            models.append({'model': row['label'] + (' (AI)' if row['ai'] else ''), 'run': row['runs'][0]['run'], **s,
                           'previous_run': None if previous is None else {
                               'run': row['runs'][1]['run'], 'mixing_height_ft': previous['mixing_height_ft'],
                               'mixed_layer_max_wind': previous['mixed_layer_max_wind']},
                           'flight_mid_cloud': mid.get(row['key'])})
    return {'sample_at': packet['sample_at'], 'summary': _story(packet['models']), 'models': models,
            'notes': [*NOTES[:3], MID_CLOUD_NOTE], 'analysis': packet['analysis']}


# Skew-T geometry: log-pressure height, 45-degree skew.
W, H, PL, PR, PT, PB = 320, 390, 34, 58, 16, 34
P_BOTTOM, P_TOP, T_LEFT, T_RIGHT = 1050.0, 400.0, -20.0, 40.0
SKEW_C = 50.0  # isotherms lean this many degrees Celsius across the plot, like a standard skew-T


def _y(p):
    return H - PB - math.log(P_BOTTOM / p) / math.log(P_BOTTOM / P_TOP) * (H - PT - PB)


def _x(t, p):
    lean = (H - PB - _y(p)) / (H - PT - PB) * SKEW_C
    return PL + (t - T_LEFT + lean) / (T_RIGHT - T_LEFT) * (W - PL - PR)


def _barb(x, y, direction, speed):
    """A wind barb at (x, y): staff toward where the wind comes from; 50/10/5 kt flags."""
    if speed < 2.5:
        return f'<circle class="barb" cx="{x:.1f}" cy="{y:.1f}" r="3"/>'
    rad = math.radians(direction)
    ux, uy = math.sin(rad), -math.cos(rad)
    px, py = -uy, ux  # perpendicular, clockwise of the staff
    length = 22
    parts = [f'M{x:.1f},{y:.1f}L{x + ux * length:.1f},{y + uy * length:.1f}']
    left, pos = int(round(speed / 5) * 5), length
    while left >= 50:
        a = (x + ux * pos, y + uy * pos)
        b = (x + ux * (pos - 6), y + uy * (pos - 6))
        parts.append(f'M{a[0]:.1f},{a[1]:.1f}L{a[0] + px * 9:.1f},{a[1] + py * 9:.1f}L{b[0]:.1f},{b[1]:.1f}Z')
        left, pos = left - 50, pos - 8
    while left >= 10:
        a = (x + ux * pos, y + uy * pos)
        parts.append(f'M{a[0]:.1f},{a[1]:.1f}L{a[0] + px * 9 + ux * 3:.1f},{a[1] + py * 9 + uy * 3:.1f}')
        left, pos = left - 10, pos - 4
    if left >= 5:
        pos = pos if pos < length else pos - 4
        a = (x + ux * pos, y + uy * pos)
        parts.append(f'M{a[0]:.1f},{a[1]:.1f}L{a[0] + px * 5 + ux * 1.5:.1f},{a[1] + py * 5 + uy * 1.5:.1f}')
    return f'<path class="barb" d="{"".join(parts)}"/>'


def skew_t(row, entry, index):
    levels, a = entry['levels'], entry['analysis']
    clip = f'skewt-clip-{index}'
    ground = levels[0][1]
    s = summary(entry)
    out = [f'<figure class="clim-panel skewt"><figcaption><h4>{escape(row["label"])}{" · AI" if row["ai"] else ""}</h4>'
           f'<p>{escape(parse_time(entry["run"]).strftime("%b %-d %HZ"))} run · mixing to {s["mixing_height_ft"]:,} ft · '
           f'max mixed-layer wind {escape(s["mixed_layer_max_wind"])}</p></figcaption>'
           f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(row["label"])} skew-T sounding">'
           f'<defs><clipPath id="{clip}"><rect x="{PL}" y="{PT}" width="{W - PL - PR}" height="{H - PT - PB}"/></clipPath></defs>'
           f'<g clip-path="url(#{clip})">']
    for t in range(-80, 41, 10):
        out.append(f'<line class="{"iso0" if t == 0 else "g"}" x1="{_x(t, P_BOTTOM):.1f}" y1="{_y(P_BOTTOM):.1f}" x2="{_x(t, P_TOP):.1f}" y2="{_y(P_TOP):.1f}"/>')
    pressures = [P_BOTTOM - i * 10 for i in range(int((P_BOTTOM - P_TOP) / 10) + 1)]
    for theta in range(270, 371, 10):
        pts = ' '.join(f'{_x(theta * (p / 1000) ** 0.2857 - 273.15, p):.1f},{_y(p):.1f}' for p in pressures)
        out.append(f'<polyline class="dry" points="{pts}"/>')
    mix_y = _y(a['mixing_top']['pres'])
    out.append(f'<rect class="mixed" x="{PL}" y="{mix_y:.1f}" width="{W - PL - PR}" height="{_y(levels[0][0]) - mix_y:.1f}"/>'
               f'<line class="mixtop" x1="{PL}" x2="{W - PR}" y1="{mix_y:.1f}" y2="{mix_y:.1f}"/>')
    trace = [(p, t) for p, t in a['parcel_trace'] if p >= P_TOP]
    if len(trace) > 1:
        out.append(f'<polyline class="parcel" points="{" ".join(f"{_x(t, p):.1f},{_y(p):.1f}" for p, t in trace)}"/>')
    shown = [r for r in levels if r[0] >= P_TOP]
    for col, cls in ((3, 'td'), (2, 't')):
        out.append(f'<polyline class="{cls}" points="{" ".join(f"{_x(r[col], r[0]):.1f},{_y(r[0]):.1f}" for r in shown)}"/>')
    for r in shown:
        tip = (f'{r[0]:.0f} hPa · {_ft(r[1] - ground):,} ft above ground: {r[2]:.1f} / {r[3]:.1f} °C, '
               f'{r[4]:03.0f}° {r[5]:.0f} kt')
        out.append(f'<circle class="pt" cx="{_x(r[2], r[0]):.1f}" cy="{_y(r[0]):.1f}" r="4"><title>{escape(tip)}</title></circle>')
    out.append('</g>')
    for p in (1000, 925, 850, 700, 500, 400):
        if p <= P_BOTTOM:
            out.append(f'<text class="t" x="{PL - 4}" y="{_y(p) + 3:.1f}" text-anchor="end">{p}</text>')
    # Labels sit on the cold (left) side, clear of the low-level temperature trace.
    out.append(f'<text class="rl" x="{PL + 3}" y="{mix_y - 4:.1f}">mixing top {s["mixing_height_ft"]:,} ft</text>')
    lcl = a['surface']['lcl_m']
    if lcl is not None:
        lcl_p = next((r[0] for r in levels if r[1] - ground >= lcl), None)
        if lcl_p and lcl_p >= P_TOP:
            y = _y(lcl_p) - 3
            y = min(y, mix_y - 16) if abs(y - (mix_y - 4)) < 12 else y
            out.append(f'<line class="lcl" x1="{PL}" x2="{PL + 10}" y1="{_y(lcl_p):.1f}" y2="{_y(lcl_p):.1f}"/>'
                       f'<text class="rl" x="{PL + 13}" y="{y:.1f}">cloud base {_ft(lcl):,} ft</text>')
    for r in shown:
        out.append(_barb(W - PR / 2, _y(r[0]), r[4], r[5]))
    for t in (-10, 0, 10, 20, 30):
        out.append(f'<text class="t" x="{_x(t, P_BOTTOM):.1f}" y="{H - PB + 12}" text-anchor="middle">{t}</text>')
    out.append(f'<text class="ax" x="{(PL + W - PR) / 2:.0f}" y="{H - 4}" text-anchor="middle">°C at the bottom · hPa at left · wind at right</text></svg></figure>')
    return ''.join(out)


def render_soundings(snapshot, now):
    try:
        packet = validate_soundings(snapshot.get('model_soundings'), snapshot)
    except ERRORS:
        return ''
    at = parse_time(packet['sample_at'])
    when = at.astimezone(TZ).strftime('%a %b %-d, %-I %p %Z')
    rows = packet['models']
    lines = []
    for row in rows:
        name = f'{escape(row["label"])}{" · AI" if row["ai"] else ""}'
        if not row['ok']:
            lines.append(f'<tr><th scope="row">{name}<small>no recent run</small></th><td colspan="6">—</td></tr>')
            continue
        s = summary(row['runs'][0])
        moist = ', '.join(f'{a:,}–{b:,} ft' if a != b else f'{a:,} ft' for a, b in s['moist_layers_ft']) or 'none'
        change = ''
        if len(row['runs']) > 1:
            p = summary(row['runs'][1])
            change = f' · was {p["mixing_height_ft"]:,} ft, {p["mixed_layer_max_wind"]} ({parse_time(row["runs"][1]["run"]).strftime("%HZ")})'
        lines.append(f'<tr><th scope="row">{name}<small>{escape(parse_time(row["runs"][0]["run"]).strftime("%b %-d %HZ"))} run</small></th>'
                     f'<td>{s["mixing_height_ft"]:,} ft<small>{escape(change.strip(" ·"))}</small></td>'
                     f'<td>{escape(s["mixed_layer_max_wind"])}<small>at {s["mixed_layer_max_wind_ft"]:,} ft</small></td>'
                     f'<td>{escape(s["mixed_layer_mean_wind"] or "—")}</td>'
                     f'<td>{"—" if s["cumulus_base_ft"] is None else format(s["cumulus_base_ft"], ",") + " ft"}<small>{escape(moist)} moist</small></td>'
                     f'<td>{s["surface_cape"]} J/kg</td><td>{"—" if s["lapse_rate_0_3km_c_per_km"] is None else format(s["lapse_rate_0_3km_c_per_km"], ".1f")} °C/km'
                     f'<small>surface {escape(s["surface"])}</small></td></tr>')
    panels = ''.join(skew_t(row, row['runs'][0], i) for i, row in enumerate(rows) if row['ok'])
    story = ''.join(f'<p>{escape(p)}</p>' for p in _story(rows))
    return (f'<section id="soundings" class="weather-pattern" aria-labelledby="soundings-title">'
            f'<p class="eyebrow">Model soundings · {escape(when)} · analysed with SHARPpy</p>'
            f'<h2 id="soundings-title">How deep the afternoon mixes</h2>{story}<p class="small">{escape(MIXING_BLURB)}</p>'
            f'<div class="table-wrap"><table><caption>Heights above each model\'s ground. Mixed-layer wind is gust potential, not a gust forecast.</caption>'
            f'<thead><tr><th scope="col">Model · run</th><th scope="col">Mixing height</th><th scope="col">Strongest mixed-layer wind</th>'
            f'<th scope="col">Mean mixed-layer wind</th><th scope="col">Cloud base (surface parcel)</th><th scope="col">Surface CAPE</th>'
            f'<th scope="col">0–3 km lapse rate</th></tr></thead><tbody>{"".join(lines)}</tbody></table></div>'
            f'<div class="clim-panels">{panels}</div>'
            f'<p class="small">Red: temperature. Green: dew point. Dashed: SHARPpy surface parcel. Shaded: the mixed layer. Hover a point for its values.</p>'
            f'<details><summary>Sounding sources &amp; limits</summary><ul>{"".join("<li>" + escape(n) + "</li>" for n in NOTES)}</ul>'
            f'<p><a href="https://github.com/sharppy/SHARPpy">SHARPpy</a> · <a href="https://nomads.ncep.noaa.gov/">NOAA NOMADS</a> · '
            f'<a href="https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/index.html">NOAA AIGFS on AWS</a> · '
            f'<a href="https://open-meteo.com/en/docs/single-runs-api">Open-Meteo single runs</a></p></details></section>')
