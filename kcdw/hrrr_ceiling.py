"""Native HRRR cloud ceiling within 100 nm of KCDW, from two hours before the flight to its end.

`collect` picks the newest HRRR run on AWS that reaches the flight's end, decodes the ceiling,
low cloud and terrain in the native-weather environment (kcdw/hrrr_ceiling_worker.py), renders
map panels in the chart environment (scripts/hrrr_ceiling_chart.py), and uploads the PNG to the
Worker's content-addressed event-map route. Each run is decoded once and reused from its cache.
The snapshot keeps per-hour statistics, byte-range proofs and the image URL, never the grid.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from html import escape
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import Request, urlopen

from .common import UTC, atomic_write, iso_z, parse_time
from .events import local_clock

VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
WORKER_PYTHON = ROOT / 'var/native-weather-venv/bin/python'
CHART_PYTHON = ROOT / 'var/charts-venv/bin/python'
WORKER = ROOT / 'kcdw/hrrr_ceiling_worker.py'
CHART = ROOT / 'scripts/hrrr_ceiling_chart.py'
CONTEXT_HOURS = 2
RADIUS_NM = 100
RADII = ('25km', '50nm', '100nm')
RADIUS_LABELS = {'25km': '25 km', '50nm': '50 nm', '100nm': '100 nm'}
THRESHOLDS = (1000, 3000, 7000)
COMPASS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')
MAX_PROBES = 8
KEEP_RUNS = 6
NOTES = ('Native HRRR 3 km, one run, hourly instantaneous samples. HGT cloud ceiling is height above sea level; '
         'feet above model ground subtract HRRR terrain. A missing value means HRRR diagnoses no ceiling. '
         'Cell counts are 3 km grid cells inside each radius, cumulative by threshold (below 3,000 ft includes below 1,000 ft); '
         'they are spatial samples, not probabilities. Beyond about 18 hours HRRR places small low-cloud areas poorly. '
         'Not an observation, TAF or official ceiling forecast.')
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _require(ok, message='invalid HRRR ceiling'):
    if not ok:
        raise ValueError(message)


def url_for(init, lead):
    return (f'https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{init:%Y%m%d}/conus/'
            f'hrrr.t{init:%H}z.wrfsfcf{lead:02d}.grib2')


def max_lead(init):
    return 48 if init.hour % 6 == 0 else 18


def frame_times(snapshot):
    """Flight start/end and the hourly samples from two hours before start through end."""
    from .event_model_matrix import flight_window
    start, end, _ = flight_window(snapshot)
    _require(start.minute == 0 and end.minute == 0 and timedelta(0) < end - start <= timedelta(hours=7))
    first = start - timedelta(hours=CONTEXT_HOURS)
    return start, end, [first + timedelta(hours=h) for h in range(int((end - first).total_seconds() // 3600) + 1)]


def candidates(times, start, now):
    """Newest-first runs that cover the whole flight; earlier context hours are dropped if already past."""
    latest = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out = []
    for age in range(1, 49):
        init = latest - timedelta(hours=age)
        if age > 12 and init.hour % 6:
            continue
        if init > start:
            continue
        frames = [t for t in times if t >= init]
        leads = [int((t - init).total_seconds() // 3600) for t in frames]
        if leads and leads[-1] <= max_lead(init):
            out.append((init, leads))
    return out


def _available(init, lead):
    try:
        with urlopen(Request(url_for(init, lead) + '.idx', method='HEAD'), timeout=6) as response:
            return response.status == 200
    except Exception:
        return False


def discover(times, start, now, probe=_available):
    for init, leads in candidates(times, start, now)[:MAX_PROBES]:
        if probe(init, leads[-1]):
            return init, leads
    return None


def _labels(init, leads, start, end):
    frames = []
    for lead in leads:
        at = init + timedelta(hours=lead)
        flight = start <= at <= end
        frames.append({'lead': lead, 'label': f"{local_clock(at)} {local_clock(at, True).split()[-1]}" + (' · flight' if flight else '')})
    return frames


def _render_map(folder, init, leads, start, end):
    request = {'data': str(folder / 'grid.npz'), 'out': str(folder / 'map.png'),
               'title': 'HRRR cloud ceiling within 100 nm of KCDW',
               'subtitle': (f'{init:%HZ %b %-d} run, forecast hours {leads[0]}–{leads[-1]}.\n'
                            'Ceiling is height above sea level minus\nmodel terrain; not an observation or TAF.\n'
                            'Source: NOAA HRRR on AWS.'),
               'frames': _labels(init, leads, start, end)}
    result = subprocess.run([str(CHART_PYTHON), str(CHART)], input=json.dumps(request), capture_output=True,
                            text=True, timeout=300, cwd=ROOT)
    lines = [line for line in result.stdout.splitlines() if line.startswith('{"file"')]
    _require(result.returncode == 0 and lines, 'HRRR map render failed')
    image = json.loads(lines[-1])
    _require(image['file'] == 'map.png')
    return image


def _decode(folder, init, leads):
    request = {'model_init': iso_z(init), 'leads': leads, 'out': str(folder / 'grid.npz')}
    result = subprocess.run([str(WORKER_PYTHON), str(WORKER)], input=json.dumps(request), capture_output=True,
                            text=True, timeout=170, cwd=ROOT)
    _require(result.returncode == 0 and len(result.stdout) <= 200_000, 'HRRR decode failed')
    return json.loads(result.stdout)


def _upload(client, slug, root, folder, image):
    from .coastal_publish import upload_frame
    registry = root / 'published.json'
    done = set(json.loads(registry.read_text())) if registry.exists() else set()
    if image['sha256'] not in done:
        upload_frame(client, slug, {'file': str(folder / image['file']), **{k: image[k] for k in ('sha256', 'width', 'height')}})
        done.add(image['sha256'])
        atomic_write(registry, json.dumps(sorted(done)) + '\n')


def collect(snapshot, var, now, client=None, probe=_available):
    """Decode (or reuse) the newest covering HRRR run and return the snapshot envelope, or None."""
    start, end, times = frame_times(snapshot)
    found = discover(times, start, now, probe)
    if found is None:
        return None
    init, leads = found
    slug = snapshot['event']['slug']
    root = Path(var) / 'events' / slug / 'hrrr-ceiling'
    folder = root / f'{init:%Y%m%d%H}-f{leads[0]:02d}-f{leads[-1]:02d}'
    folder.mkdir(parents=True, exist_ok=True)
    cached = folder / 'result.json'
    if cached.exists():
        result = json.loads(cached.read_text())
    else:
        decoded = _decode(folder, init, leads)
        result = {'decoded': decoded, 'fetched_at': iso_z(now), 'image': _render_map(folder, init, leads, start, end)}
        atomic_write(cached, json.dumps(result, separators=(',', ':')) + '\n')
    for old in sorted((p for p in root.iterdir() if p.is_dir() and p != folder), reverse=True)[KEEP_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)
    if client is not None:
        _upload(client, slug, root, folder, result['image'])
    image = result['image']
    envelope = {
        'version': VERSION, 'model': 'hrrr', 'init': iso_z(init), 'fetched_at': result['fetched_at'],
        'snapshot_collected_at': snapshot['collected_at'], 'event': {k: snapshot['event'][k] for k in ('slug', 'date')},
        'flight': {'start': iso_z(start), 'end': iso_z(end)}, 'radius_nm': RADIUS_NM, 'notes': NOTES,
        'published': client is not None,
        'image': {'url': f"/events/{slug}/maps/{image['sha256']}.png", **{k: image[k] for k in ('sha256', 'width', 'height')}},
        'terrain': result['decoded']['terrain'], 'frames': result['decoded']['frames']}
    checked = validate(envelope, snapshot, now)
    _require(checked is not None, 'HRRR ceiling failed validation')
    return checked


def _number(value, low, high, nullable=False):
    if value is None and nullable:
        return
    _require(type(value) in (int, float) and low <= value <= high)


def _keys(value, keys):
    _require(isinstance(value, dict) and set(value) == set(keys.split()))


def _proof(proof):
    _keys(proof, 'start end total bytes sha256')
    for k in ('start', 'end', 'total', 'bytes'):
        _require(type(proof[k]) is int and 0 <= proof[k] <= 1_000_000_000)
    _require(0 < proof['bytes'] == proof['end'] - proof['start'] + 1 <= 8_000_000 and proof['end'] < proof['total'])
    _require(isinstance(proof['sha256'], str) and re.fullmatch('[0-9a-f]{64}', proof['sha256']) is not None)


def validate(envelope, snapshot, now):
    """Return a detached copy of a consistent envelope, or None."""
    try:
        e = envelope
        _keys(e, 'version model init fetched_at snapshot_collected_at event flight radius_nm notes published image terrain frames')
        _require(e['version'] == VERSION and e['model'] == 'hrrr' and e['radius_nm'] == RADIUS_NM and e['notes'] == NOTES)
        _require(e['snapshot_collected_at'] == snapshot['collected_at'] and type(e['published']) is bool)
        _require(e['event'] == {k: snapshot['event'][k] for k in ('slug', 'date')})
        start, end, times = frame_times(snapshot)
        _require(e['flight'] == {'start': iso_z(start), 'end': iso_z(end)})
        init, fetched = parse_time(e['init']), parse_time(e['fetched_at'])
        _require(init.minute == init.second == 0 and init <= start and init <= fetched <= now)
        _require(now - fetched <= timedelta(hours=24) and now - init <= timedelta(hours=48))
        leads = [int((t - init).total_seconds() // 3600) for t in times if t >= init]
        _require(leads and leads[-1] <= max_lead(init))
        _keys(e['image'], 'url sha256 width height')
        _require(re.fullmatch('[0-9a-f]{64}', e['image']['sha256']) is not None)
        _require(e['image']['url'] == f"/events/{e['event']['slug']}/maps/{e['image']['sha256']}.png")
        _require(all(type(e['image'][k]) is int and 100 <= e['image'][k] <= 6000 for k in ('width', 'height')))
        _keys(e['terrain'], 'lead proof')
        _require(e['terrain']['lead'] == leads[0])
        _proof(e['terrain']['proof'])
        frames = e['frames']
        _require(isinstance(frames, list) and [f.get('lead') for f in frames] == leads)
        for f in frames:
            _keys(f, 'lead valid_at source_url fields kcdw within_10km counts nearest_below_3000')
            _require(f['valid_at'] == iso_z(init + timedelta(hours=f['lead'])) and f['source_url'] == url_for(init, f['lead']))
            _keys(f['fields'], 'ceiling low_cloud')
            for proof in f['fields'].values():
                _proof(proof)
            _keys(f['kcdw'], 'ceiling_agl_ft low_cloud_pct')
            _number(f['kcdw']['ceiling_agl_ft'], 0, 150_000, True)
            _number(f['kcdw']['low_cloud_pct'], 0, 100, True)
            near = f['within_10km']
            _keys(near, 'cells ceiling_cells min_ft max_ft')
            _require(type(near['cells']) is int and 10 <= near['cells'] <= 80)
            _require(type(near['ceiling_cells']) is int and 0 <= near['ceiling_cells'] <= near['cells'])
            if near['ceiling_cells']:
                _number(near['min_ft'], 0, 150_000)
                _number(near['max_ft'], near['min_ft'], 150_000)
            else:
                _require(near['min_ft'] is None and near['max_ft'] is None)
            _keys(f['counts'], ' '.join(RADII))
            previous = 0
            for radius in RADII:
                c = f['counts'][radius]
                _keys(c, 'cells ' + ' '.join(f'below_{t}' for t in THRESHOLDS))
                _require(type(c['cells']) is int and previous < c['cells'] <= 20_000)
                values = [c[f'below_{t}'] for t in THRESHOLDS]
                _require(all(type(v) is int for v in values) and 0 <= values[0] <= values[1] <= values[2] <= c['cells'])
                previous = c['cells']
            nearest = f['nearest_below_3000']
            if nearest is None:
                _require(f['counts']['100nm']['below_3000'] == 0)
            else:
                _keys(nearest, 'distance_nm direction ceiling_ft')
                _number(nearest['distance_nm'], 0, RADIUS_NM)
                _require(nearest['direction'] in COMPASS)
                _number(nearest['ceiling_ft'], 0, 3000)
                _require(nearest['ceiling_ft'] < 3000 and f['counts']['100nm']['below_3000'] > 0)
        _require(len(json.dumps(e, allow_nan=False)) <= 40_000)
        return deepcopy(e)
    except ERRORS:
        return None


def ceiling_evidence(snapshot, now):
    """Compact per-hour statistics for the narrative."""
    e = validate(snapshot.get('hrrr_ceiling'), snapshot, now)
    if e is None:
        return None
    return {'model': 'HRRR 3 km native', 'run': e['init'], 'notes': e['notes'],
            'columns': ['valid_utc', 'lead_h', 'kcdw_ceiling_ft', 'kcdw_low_cloud_pct', 'ceiling_10km_min_ft',
                        'ceiling_10km_max_ft', 'no_ceiling_cells_10km',
                        *[f'cells_below_{t}_{r}' for r in RADII for t in (1000, 3000)], 'nearest_below_3000'],
            'cells_in_radius': {r: e['frames'][0]['counts'][r]['cells'] for r in RADII},
            'rows': [[f['valid_at'], f['lead'], f['kcdw']['ceiling_agl_ft'], f['kcdw']['low_cloud_pct'],
                      f['within_10km']['min_ft'], f['within_10km']['max_ft'],
                      f['within_10km']['cells'] - f['within_10km']['ceiling_cells'],
                      *[f['counts'][r][f'below_{t}'] for r in RADII for t in (1000, 3000)],
                      f['nearest_below_3000']] for f in e['frames']]}


def _ft(value):
    return 'No ceiling' if value is None else f'{value:,.0f} ft'


def render(snapshot, now):
    if 'hrrr_ceiling' not in snapshot:
        return ''
    e = validate(snapshot.get('hrrr_ceiling'), snapshot, now)
    if e is None:
        return ('<section id="hrrr-ceiling" aria-labelledby="hrrr-ceiling-title"><h3 id="hrrr-ceiling-title">HRRR ceiling within 100 nm</h3>'
                '<p>Current HRRR ceiling unavailable.</p></section>')
    init = parse_time(e['init'])
    start, end = parse_time(e['flight']['start']), parse_time(e['flight']['end'])
    rows = []
    def place(n):
        return 'None' if n is None else f"{n['distance_nm']:g} nm {n['direction']} · {n['ceiling_ft']:,.0f} ft"
    for f in e['frames']:
        at = parse_time(f['valid_at'])
        near, counts, nearest = f['within_10km'], f['counts'], f['nearest_below_3000']
        span = ('No ceiling' if not near['ceiling_cells'] else
                _ft(near['min_ft']) if near['min_ft'] == near['max_ft'] else f"{near['min_ft']:,.0f}–{near['max_ft']:,.0f} ft")
        flight = ' · flight' if start <= at <= end else ''
        rows.append('<tr>' f'<th scope="row">{escape(local_clock(at) + flight)}</th>'
                    f'<td>{escape(_ft(f["kcdw"]["ceiling_agl_ft"]))}</td><td>{escape(span)}</td>'
                    + ''.join('<td>' + f'{counts[r]["below_3000"]:,}' + '</td>' for r in RADII)
                    + f'<td>{escape(place(nearest))}</td></tr>')
    first = e['frames'][0]
    cells = ' · '.join(RADIUS_LABELS[r] + ' ' + format(first['counts'][r]['cells'], ',') for r in RADII)
    image = e['image']
    figure = (f'<a class="hrrr-map" href="{escape(image["url"], quote=True)}" target="_blank" rel="noopener">'
              f'<img src="{escape(image["url"], quote=True)}" width="{image["width"]}" height="{image["height"]}" '
              'alt="Map panels of HRRR cloud ceiling within 100 nautical miles of KCDW for each hour shown in the table." '
              'loading="lazy" decoding="async" style="max-width:100%;height:auto"></a>')
    note = '' if e['published'] else '<p class="small">The map was rendered locally and not published.</p>'
    return (f'<section id="hrrr-ceiling" aria-labelledby="hrrr-ceiling-title"><h3 id="hrrr-ceiling-title">HRRR ceiling within 100 nm</h3>'
            f'<p class="small">{escape(init.strftime("%HZ %b %-d"))} HRRR run, forecast hours {first["lead"]}–{e["frames"][-1]["lead"]}, '
            'in feet above model ground. Counts are 3 km grid cells with a ceiling below 3,000 ft inside each radius '
            f'(cells in each radius: {escape(cells)}).</p>'
            '<div class="table-wrap"><table><thead><tr><th scope="col">Eastern</th><th scope="col">Over KCDW</th>'
            '<th scope="col">Within 10 km</th>' + ''.join(f'<th scope="col">Below 3,000 ft · {RADIUS_LABELS[r]}</th>' for r in RADII)
            + '<th scope="col">Nearest below 3,000 ft</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
            + figure + note
            + f'<details><summary>HRRR ceiling method</summary><p>{escape(e["notes"])}</p>'
            f'<p>Retrieved {escape(local_clock(e["fetched_at"], True))} from the '
            '<a href="https://registry.opendata.aws/noaa-hrrr-pds/">NOAA HRRR archive on AWS</a>; map boundaries from Natural Earth.</p></details></section>')
