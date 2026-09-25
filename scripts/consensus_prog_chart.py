#!/usr/bin/env python3
"""Render AWC/WPC-style surface progs synthesized from GFS, ECMWF IFS, ECMWF AIFS and Canadian GDPS.

Install requirements-consensus-prog.txt; see deploy/CONSENSUS-PROG.md for usage.
Each model's newest run covering every requested valid time is fetched (only the needed GRIB
messages), regridded to a shared 0.25° grid and cached. Isobars, H/L centers and fronts come
from the equal-weight model mean; precipitation shading shows how many models agree.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault('MPLCONFIGDIR', str(REPO / 'var/charts/.matplotlib'))

import numpy as np
# pyproj must load its bundled PROJ/sqlite before ecCodes' wheel loads different copies;
# the reverse order corrupts the heap and aborts the interpreter at exit.
import pyproj  # noqa: F401

try:
    from . import consensus_prog as prog
except ImportError:
    import consensus_prog as prog

UTC = timezone.utc
VERSION = 1
BOUNDS = (-140, -50, 15, 62)          # fetch grid: west, east, south, north
MODELS = {
    'gfs': {'name': 'GFS', 'center': 'NOAA', 'letter': 'G', 'color': '#d9730d', 'cycles': (0, 6, 12, 18),
            'max_lead': lambda h: 384, 'source': 'https://registry.opendata.aws/noaa-gfs-bdp-pds/'},
    'ifs': {'name': 'ECMWF IFS', 'center': 'ECMWF', 'letter': 'E', 'color': '#7b3fa0', 'cycles': (0, 6, 12, 18),
            'max_lead': lambda h: 360 if h in (0, 12) else 144, 'source': 'https://www.ecmwf.int/en/forecasts/datasets/open-data'},
    'aifs': {'name': 'ECMWF AIFS', 'center': 'ECMWF', 'letter': 'A', 'color': '#12877a', 'cycles': (0, 6, 12, 18),
             'max_lead': lambda h: 360, 'source': 'https://www.ecmwf.int/en/forecasts/datasets/open-data'},
    'gdps': {'name': 'Canadian GDPS', 'center': 'ECCC', 'letter': 'C', 'color': '#3a4a5c', 'cycles': (0, 12),
             'max_lead': lambda h: 240, 'source': 'https://eccc-msc.github.io/open-data/msc-data/nwp_gdps/readme_gdps_en/'},
}
GDPS_FILES = {'msl': 'Pressure_MSL', 't850': 'AirTemp_IsbL-0850', 'moist850': 'RelativeHumidity_IsbL-0850',
              'u850': 'WindU_IsbL-0850', 'v850': 'WindV_IsbL-0850', 't2m': 'AirTemp_AGL-2m',
              'precip6': 'Precip-Accum6h_Sfc', 'cape': 'CAPE_Sfc'}
# (converted low, high) plausibility bounds
RANGES = {'msl': (870, 1090), 't850': (200, 330), 'moist850': (-5, 1e3), 'u850': (-120, 120), 'v850': (-120, 120),
          't2m': (200, 335), 'precip6': (-0.01, 800), 'cape': (0, 15000), 'sp': (400, 1090)}
TZ_LABEL = 'America/New_York'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def stamp(value):
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'Times need a timezone')
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------- download

def http(url, byte_range=None, limit=40_000_000, attempts=3):
    import requests
    headers = {'User-Agent': 'KCDW-Flyability-Consensus/1.0'}
    if byte_range:
        headers['Range'] = f'bytes={byte_range[0]}-{byte_range[1]}'
    for attempt in range(attempts):
        try:
            with requests.get(url, headers=headers, timeout=(6, 60), stream=True) as r:
                if r.status_code == 404:
                    raise FileNotFoundError(url)
                require(r.status_code == (206 if byte_range else 200), f'HTTP {r.status_code} for {url}')
                content = bytearray()
                for block in r.iter_content(1 << 16):
                    content.extend(block)
                    require(len(content) <= limit, 'Download exceeds limit')
                if byte_range:
                    require(len(content) == byte_range[1] - byte_range[0] + 1, 'Short byte range')
                return bytes(content)
        except FileNotFoundError:
            raise
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def exists(url):
    import requests
    try:
        return requests.head(url, timeout=(6, 20), allow_redirects=True).status_code == 200
    except Exception:
        return False


def gfs_url(init, lead):
    return (f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{init:%Y%m%d}/{init:%H}/atmos/'
            f'gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}')


def ecmwf_url(model, init, lead):
    stream = 'ifs' if model == 'ifs' else 'aifs-single'
    return (f'https://data.ecmwf.int/forecasts/{init:%Y%m%d}/{init:%H}z/{stream}/0p25/oper/'
            f'{init:%Y%m%d%H}0000-{lead}h-oper-fc.grib2')


def gdps_url(init, lead, field):
    return (f'https://dd.weather.gc.ca/{init:%Y%m%d}/WXO-DD/model_gdps/15km/{init:%H}/{lead:03d}/'
            f'{init:%Y%m%dT%H}Z_MSC_GDPS_{GDPS_FILES[field]}_LatLon0.15_PT{lead:03d}H.grib2')


def probe_url(model, init, lead):
    if model == 'gfs':
        return gfs_url(init, lead) + '.idx'
    if model == 'gdps':
        return gdps_url(init, lead, 'msl')
    return ecmwf_url(model, init, lead)[:-6] + '.index'


def choose_run(model, valid_times, now, override=None):
    """Newest cycle (within 48 h) whose files exist for the latest requested valid time."""
    spec = MODELS[model]
    first, last = min(valid_times), max(valid_times)
    candidates = [override] if override else [
        t for i in range(9) if (t := now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6) - timedelta(hours=6 * i)).hour in spec['cycles']]
    for init in candidates:
        if first < init or (last - init).total_seconds() / 3600 > spec['max_lead'](init.hour):
            continue
        if exists(probe_url(model, init, int((last - init).total_seconds() // 3600))):
            return init
    raise RuntimeError(f'No {model} run covers {stamp(first)}–{stamp(last)}')


def choose_cycle(models, through, now, every=6):
    """Newest 00/12Z cycle (within 48 h) that every model has published through `through`.

    One shared cycle makes the first frame a true consensus analysis (every model at F000)
    and keeps each later frame at the same lead for all models, as a WPC prog set would.
    """
    latest = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 12 * 12)
    for init in (latest - timedelta(hours=12 * i) for i in range(5)):
        lead = -(-int((through - init).total_seconds() // 3600) // every) * every  # last frame, rounded up to the step
        if all(lead <= MODELS[m]['max_lead'](init.hour) and exists(probe_url(m, init, lead)) for m in models):
            return init
    raise RuntimeError(f'No common 00/12Z cycle covers {stamp(through)}')


def gfs_ranges(init, lead):
    text = http(gfs_url(init, lead) + '.idx', limit=400_000).decode('ascii')
    rows = [r.split(':') for r in text.splitlines()]
    offsets = [int(r[1]) for r in rows]
    wanted = {'msl': ('PRMSL', 'mean sea level'), 't850': ('TMP', '850 mb'), 'moist850': ('RH', '850 mb'),
              'u850': ('UGRD', '850 mb'), 'v850': ('VGRD', '850 mb'), 't2m': ('TMP', '2 m above ground'),
              'cape': ('CAPE', 'surface'), 'sp': ('PRES', 'surface'), 'precip6': ('APCP', 'surface')}
    found = {}
    for i, row in enumerate(rows[:-1]):
        for name, (param, level) in wanted.items():
            if row[3:5] != [param, level] or name in found:
                continue
            require(row[2] == f'd={init:%Y%m%d%H}', 'GFS index belongs to another run')
            if name == 'precip6' and row[5] != f'{lead - 6}-{lead} hour acc fcst':
                continue
            if name != 'precip6' and row[5] != (f'{lead} hour fcst' if lead else 'anl'):
                continue
            found[name] = (offsets[i], offsets[i + 1] - 1)
    return found


def ecmwf_ranges(model, init, lead):
    text = http(ecmwf_url(model, init, lead)[:-6] + '.index', limit=2_000_000).decode('ascii')
    wanted = {('msl', None): 'msl', ('t', '850'): 't850', ('r' if model == 'ifs' else 'q', '850'): 'moist850',
              ('u', '850'): 'u850', ('v', '850'): 'v850', ('2t', None): 't2m'}
    if lead:
        wanted[('tp', None)] = 'tp'
    if model == 'ifs':
        wanted[('mucape', None)] = 'cape'
    found = {}
    for line in text.splitlines():
        row = json.loads(line)
        key = (row['param'], row.get('levelist'))
        if key in wanted and wanted[key] not in found:
            require(row['date'] == f'{init:%Y%m%d}' and row['time'] == f'{init:%H%M}' and row['step'] == str(lead),
                    'ECMWF index belongs to another run or step')
            found[wanted[key]] = (row['_offset'], row['_offset'] + row['_length'] - 1)
    return found


DECODE = threading.Lock()  # ecCodes handles are not safe to use from several threads at once


def decode(raw, init, lead):
    """GRIB2 message -> (values[lat, lon], lat axis, lon axis, identity)."""
    import eccodes as ec
    require(raw[:4] == b'GRIB' and raw[-4:] == b'7777', 'Not a complete GRIB message')
    with DECODE:
        return _decode(ec, raw, init, lead)


def _decode(ec, raw, init, lead):
    handle = ec.codes_new_from_message(raw)
    try:
        get = lambda key: ec.codes_get(handle, key)
        require(get('gridType') == 'regular_ll' and get('jPointsAreConsecutive') == 0, 'Unexpected grid layout')
        ni, nj = get('Ni'), get('Nj')
        valid = datetime.strptime(f"{get('validityDate'):08d}{get('validityTime'):04d}", '%Y%m%d%H%M').replace(tzinfo=UTC)
        base = datetime.strptime(f"{get('dataDate'):08d}{get('dataTime'):04d}", '%Y%m%d%H%M').replace(tzinfo=UTC)
        require(base == init and valid == init + timedelta(hours=lead), 'GRIB message has the wrong run or valid time')
        values = np.array(ec.codes_get_values(handle), float).reshape(nj, ni)
        if get('bitmapPresent'):
            values[values == get('missingValue')] = np.nan
        lat = np.linspace(get('latitudeOfFirstGridPointInDegrees'), get('latitudeOfLastGridPointInDegrees'), nj)
        west = get('longitudeOfFirstGridPointInDegrees')
        step = get('iDirectionIncrementInDegrees')
        require(not get('iScansNegatively'), 'Unexpected longitude scan')
        lon = west + np.arange(ni) * step
        identity = {'shortName': get('shortName'), 'units': get('units'), 'level': get('level'),
                    'parameter': [get('discipline'), get('parameterCategory'), get('parameterNumber')],
                    'stepRange': get('stepRange'), 'grid': [nj, ni, float(step)]}
        return values, lat, lon, identity
    finally:
        ec.codes_release(handle)


def convert(name, values, identity, model):
    units = identity['units']
    if name in ('msl', 'sp'):
        require(units == 'Pa', f'{model} {name} units {units}')
        return values / 100
    if name == 'precip6':
        # ECCC encodes total precipitation (0/1/8, kg m-2) without an ecCodes short name.
        require(units in ('kg m**-2', 'm') or identity['parameter'] == [0, 1, 8], f'{model} precipitation units {units}')
        return values * (1000 if units == 'm' else 1)
    if name == 'cape':
        return np.nan_to_num(values)  # GDPS leaves CAPE undefined (missing) where there is none
    if name == 'moist850' and model == 'aifs':
        require(units == 'kg kg**-1', f'{model} humidity units {units}')
    return values


def model_fields(model, init, lead, lat, lon, cache):
    """Regridded fields for one model/run/lead, cached as float32 with a provenance file."""
    folder = cache / model / f'{init:%Y%m%dT%HZ}'
    data, proof = folder / f'f{lead:03d}.npz', folder / f'f{lead:03d}.json'
    if data.exists() and proof.exists():
        saved = json.loads(proof.read_text())
        if saved['version'] == VERSION and saved['sha256'] == hashlib.sha256(data.read_bytes()).hexdigest():
            with np.load(data) as stored:
                return {k: stored[k].astype(float) for k in stored.files}, saved
    messages = {}
    if model == 'gfs':
        url = gfs_url(init, lead)
        messages = {name: (url, r) for name, r in gfs_ranges(init, lead).items()}
    elif model in ('ifs', 'aifs'):
        url = ecmwf_url(model, init, lead)
        messages = {name: (url, r) for name, r in ecmwf_ranges(model, init, lead).items()}
        if lead > 6:
            earlier = ecmwf_ranges(model, init, lead - 6)
            messages['tp_before'] = (ecmwf_url(model, init, lead - 6), earlier['tp'])
    else:
        messages = {name: (gdps_url(init, lead, name), None) for name in GDPS_FILES if lead or name != 'precip6'}
    out, sources = {}, {}
    for name, (url, byte_range) in messages.items():
        try:
            raw = http(url, byte_range)
        except FileNotFoundError:
            if name == 'cape':
                continue  # optional: thunder shading uses the models that publish CAPE
            raise
        values, glat, glon, identity = decode(raw, init, lead if name != 'tp_before' else lead - 6)
        key = 'precip6' if name == 'tp' else name
        out[key] = (prog.regrid(values, glat, glon, lat, lon), identity)
        sources[name] = {'url': url, 'bytes': list(byte_range) if byte_range else None,
                         'sha256': hashlib.sha256(raw).hexdigest(), **identity}
    fields = {}
    for name, (values, identity) in out.items():
        if name == 'tp_before':
            continue
        fields[name] = convert(name, values, identity, model)
    if model in ('ifs', 'aifs') and lead:
        # ECMWF precipitation accumulates from initialization; 6-hour totals are differences.
        before = convert('precip6', *out['tp_before'], model) if 'tp_before' in out else 0
        fields['precip6'] = np.clip(fields['precip6'] - before, 0, None)
    # Analyses (F000) carry no precipitation; every forecast lead must.
    missing = [n for n in ('msl', 't850', 'moist850', 'u850', 'v850', 't2m') + (('precip6',) if lead else ()) if n not in fields]
    require(not missing, f'{model} {init:%dT%HZ} F{lead}: missing {missing}')
    for name, values in fields.items():
        low, high = RANGES[name]
        finite = values[np.isfinite(values)]
        require(finite.size and finite.min() >= low and finite.max() <= high, f'{model} {name} out of range')
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f'f{lead:03d}.tmp.npz'
    np.savez_compressed(temporary, **{k: v.astype(np.float32) for k, v in fields.items()})
    os.replace(temporary, data)
    saved = {'version': VERSION, 'model': model, 'init': stamp(init), 'lead': lead,
             'valid': stamp(init + timedelta(hours=lead)), 'grid_bounds': BOUNDS, 'sources': sources,
             'retrieved_at': stamp(datetime.now(UTC)), 'sha256': hashlib.sha256(data.read_bytes()).hexdigest()}
    proof.write_text(json.dumps(saved, indent=2) + '\n')
    return {k: np.asarray(v, float) for k, v in fields.items()}, saved


# ---------------------------------------------------------------- synthesis

def synthesize(members, lat, lon, point=None):
    """Consensus fields, centers, fronts and precipitation categories for one valid time."""
    need = max(2, len(members) - 1)
    mean = lambda name: prog.consensus([m[name] for m in members.values()], need)
    theta = {}
    for model, f in members.items():
        humidity = dict(q=f['moist850']) if model == 'aifs' else dict(rh_pct=f['moist850'])
        theta[model] = prog.theta_e(f['t850'], 850.0, **humidity)
    fields = {'msl': mean('msl'), 'u850': mean('u850'), 'v850': mean('v850'), 't2m': mean('t2m'),
              't850': mean('t850'), 'theta_e850': prog.consensus(list(theta.values()), need)}
    terrain = next((f['sp'] for f in members.values() if 'sp' in f), None)
    pressure = prog.smooth(np.where(np.isfinite(fields['msl']), fields['msl'], np.nanmean(fields['msl'])), lat, 40)
    per_model = {}
    for model, f in members.items():
        smoothed = prog.smooth(f['msl'], lat, 40)
        per_model[model] = prog.centers(smoothed, lat, lon, 'L', radius_km=500, prominence=1.0)
    has_precip = all('precip6' in m for m in members.values())
    if has_precip:
        rain = prog.precipitation([m['precip6'] for m in members.values()],
                                  [m['cape'] for m in members.values() if 'cape' in m], fields['t2m'], fields['t850'])
    else:
        empty = np.zeros(lat.shape + lon.shape, bool)
        rain = {k: empty for k in ('likely', 'moderate', 'thunder', 'snow', 'mixed')}
    return {'fields': fields, 'pressure': pressure, 'has_precip': has_precip,
            'point': point_diagnostics(members, pressure, lat, lon, point) if point else None,
            'highs': prog.centers(pressure, lat, lon, 'H'), 'lows': prog.centers(pressure, lat, lon, 'L', prominence=1.0),
            'model_lows': per_model, 'fronts': prog.fronts(fields['theta_e850'], fields['u850'], fields['v850'], lat, lon, terrain,
                                                    fields['t850']),
            'precip': rain}


def point_diagnostics(members, pressure, lat, lon, point):
    """Consensus and per-model sea-level pressure (and 6 h precipitation) at one location.

    The gradient comes from the smoothed consensus pressure; the geostrophic speed it implies
    (at 1.2 kg/m3) is an upper-air scale, not a surface wind or gust forecast.
    """
    from scipy.interpolate import RegularGridInterpolator
    y, x = point
    at = lambda field: float(RegularGridInterpolator((lat, lon), field)((y, x)))
    gx, gy = prog.gradient(pressure, lat, lon)
    gradient = at(np.hypot(gx, gy))
    coriolis = 2 * 7.292e-5 * np.sin(np.radians(y))
    geostrophic_kt = gradient * 100 / 1e5 / (1.2 * coriolis) * 3600 / 1852
    models = {m: {'msl_hpa': round(at(f['msl']), 1),
                  **({'precip6_mm': round(max(0.0, at(f['precip6'])), 1)} if 'precip6' in f else {})}
              for m, f in members.items()}
    return {'lat': y, 'lon': x, 'msl_hpa': round(at(pressure), 1), 'gradient_hpa_per_100km': round(gradient, 2),
            'geostrophic_kt': round(float(geostrophic_kt)), 'models': models}


# ---------------------------------------------------------------- rendering

VIEWS = {
    'conus': {'extent': [-121.5, -65, 21.5, 51.5], 'center': (-96, 39), 'parallels': (33, 45), 'size': (16, 10.6), 'isobar': 4},
    'northeast': {'extent': [-86, -63.5, 33.5, 48.5], 'center': (-75, 41), 'parallels': (37, 45), 'size': (14, 10.6), 'isobar': 2},
}
LETTER_OFFSETS = [(-17, 13), (17, 13), (-17, -9), (17, -9)]  # points; one direction per model, in MODELS order
COLORS = {'cold': '#1f5fd6', 'warm': '#d7263d', 'high': '#1f4fbf', 'low': '#c8102e', 'isobar': '#51463d',
          'rain': '#9bd88f', 'moderate': '#3fa34d', 'thunder': '#d7263d', 'snow': '#79aef0', 'mixed': '#e89ad6',
          'land': '#f6f3ec', 'ocean': '#eaf2f8', 'ink': '#1d2830'}


def draw_front(ax, front, projection, spacing, size):
    import cartopy.crs as ccrs
    from matplotlib.patches import Polygon, Wedge
    points = np.array(front['points'])
    warm = np.array(front['warm_side'])
    xy = projection.transform_points(ccrs.PlateCarree(), points[:, 1], points[:, 0])[:, :2]
    ahead = projection.transform_points(ccrs.PlateCarree(), points[:, 1] + .2 * warm[:, 1] / np.cos(np.radians(points[:, 0])),
                                        points[:, 0] + .2 * warm[:, 0])[:, :2]
    along = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    kind = front['type']
    if kind == 'stationary':
        edges = np.arange(0, along[-1] + spacing, spacing / 2)
        for k, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
            s = np.linspace(a, min(b, along[-1]), 8)
            ax.plot(np.interp(s, along, xy[:, 0]), np.interp(s, along, xy[:, 1]), color=COLORS['cold' if k % 2 else 'warm'],
                    lw=2.3, solid_capstyle='butt', zorder=7)
    else:
        ax.plot(xy[:, 0], xy[:, 1], color=COLORS[kind], lw=2.3, solid_capstyle='round', zorder=7)
    for k, s in enumerate(np.arange(spacing / 2, along[-1] - size / 2, spacing)):
        a, b = max(0, s - size / 2), min(along[-1], s + size / 2)
        p0 = np.array([np.interp(a, along, xy[:, 0]), np.interp(a, along, xy[:, 1])])
        p1 = np.array([np.interp(b, along, xy[:, 0]), np.interp(b, along, xy[:, 1])])
        tangent = (p1 - p0) / max(np.hypot(*(p1 - p0)), 1)
        i = min(int(np.searchsorted(along, s)), len(xy) - 1)
        toward_warm = ahead[i] - xy[i]
        left = np.array([-tangent[1], tangent[0]])
        warm_side = 1 if left @ toward_warm >= 0 else -1
        symbol = kind if kind != 'stationary' else ('cold' if k % 2 else 'warm')
        mid = (p0 + p1) / 2
        if symbol == 'cold':   # triangles point into the warm air the cold front advances on
            apex = mid + left * warm_side * size * .9
            ax.add_patch(Polygon([p0, p1, apex], closed=True, color=COLORS['cold'], zorder=7, lw=0))
        else:                  # semicircles face the cold air the warm front advances on
            angle = np.degrees(np.arctan2(tangent[1], tangent[0]))
            start = angle if -warm_side > 0 else angle + 180
            ax.add_patch(Wedge(mid, size / 2, start, start + 180, color=COLORS['warm'], zorder=7, lw=0))


def smooth_mask(mask, lat):
    return prog.smooth(mask.astype(float), lat, 30)


def weather_symbol(ax, x, y, kind, transform):
    style = dict(transform=transform, ha='center', va='center', zorder=6, clip_on=True)
    if kind == 'thunder':
        ax.text(x, y, '☈', fontsize=22, color=COLORS['thunder'], weight='bold', **style)
    elif kind == 'snow':
        ax.text(x, y, '✳✳', fontsize=12, color='#1f4fa0', **style)
    elif kind == 'mixed':
        ax.text(x, y, '●✳', fontsize=12, color='#a0307f', **style)
    else:
        ax.text(x, y, '● ●', fontsize=9, color='#1f6b2a', **style)


def render(result, members_meta, valid, lat, lon, view, output, dpi):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as effects
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import cartopy
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from zoneinfo import ZoneInfo

    cartopy.config['data_dir'] = str(REPO / 'var/charts/cartopy-data')
    spec = VIEWS[view]
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    fig = plt.figure(figsize=spec['size'], facecolor='white')
    projection = ccrs.LambertConformal(central_longitude=spec['center'][0], central_latitude=spec['center'][1],
                                       standard_parallels=spec['parallels'])
    ax = fig.add_axes([.02, .135, .96, .77], projection=projection)
    ax.set_extent(spec['extent'], crs=ccrs.PlateCarree())
    geo = ccrs.PlateCarree()
    ax.set_facecolor(COLORS['ocean'])
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor=COLORS['land'], zorder=0)
    ax.add_feature(cfeature.LAKES.with_scale('50m'), facecolor=COLORS['ocean'], edgecolor='#8a9aa5', lw=.4, zorder=1)
    xx, yy = np.meshgrid(lon, lat)
    rain = result['precip']
    shade = lambda mask, color, alpha=1, hatch=None: ax.contourf(
        xx, yy, smooth_mask(mask, lat), levels=[.5, 2], colors=[color if not hatch else 'none'], alpha=alpha,
        hatches=[hatch], transform=geo, transform_first=True, zorder=2)
    shade(rain['likely'], COLORS['rain'], .85)
    shade(rain['moderate'], COLORS['moderate'], .8)
    shade(rain['snow'], COLORS['snow'], .9)
    shade(rain['mixed'], COLORS['mixed'], .9)
    if rain['thunder'].any():
        plt.rcParams['hatch.color'] = COLORS['thunder']
        plt.rcParams['hatch.linewidth'] = 1.1
        shade(rain['thunder'], None, None, '////')
    ax.coastlines('50m', lw=.6, color='#5d6a73', zorder=3)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), lw=.6, edgecolor='#5d6a73', zorder=3)
    ax.add_feature(cfeature.STATES.with_scale('50m'), lw=.35, edgecolor='#9aa4ab', zorder=3)
    contours = ax.contour(xx, yy, result['pressure'], levels=np.arange(940, 1064, spec['isobar']), colors=COLORS['isobar'],
                          linewidths=.85, transform=geo, transform_first=True, zorder=4)
    for text in ax.clabel(contours, fmt='%d', fontsize=8, inline=True, inline_spacing=3):
        text.set_path_effects([effects.withStroke(linewidth=2, foreground='white')])
    width = np.ptp(ax.get_xlim())
    for front in result['fronts']:
        draw_front(ax, front, projection, spacing=width / 42, size=width / 105)
    stroke = [effects.withStroke(linewidth=3, foreground='white')]
    def inside(x, y, margin=0):
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        return x0 + margin * (x1 - x0) < x < x1 - margin * (x1 - x0) and y0 + margin * (y1 - y0) < y < y1 - margin * (y1 - y0)
    for kind in ('thunder', 'snow', 'mixed', 'likely'):
        area = rain[kind] & ~(rain['thunder'] | rain['snow'] | rain['mixed']) if kind == 'likely' else rain[kind]
        for y, x, _ in prog.areas(area, lat, lon, minimum_cells=60 if view == 'conus' else 30)[:12]:
            px, py = projection.transform_point(x, y, geo)
            if inside(px, py, .04):
                weather_symbol(ax, px, py, 'rain' if kind == 'likely' else kind, projection)
    for center in result['highs'] + result['lows']:
        px, py = projection.transform_point(center['lon'], center['lat'], geo)
        if not inside(px, py, .04):
            continue
        color = COLORS['high' if center['kind'] == 'H' else 'low']
        ax.text(px, py, center['kind'], fontsize=26, weight='bold', color=color, ha='center', va='center',
                path_effects=stroke, zorder=9)
        ax.annotate(f"{center['hPa']:.0f}", (px, py), xytext=(0, -22), textcoords='offset points', ha='center',
                    va='top', fontsize=10, weight='bold', color=color, path_effects=stroke, zorder=9)
    # Where each model puts its own lows near the consensus lows: the spread behind the mean.
    for center in result['lows']:
        for model, lows in result['model_lows'].items():
            near = [c for c in lows if prog.distance_km((c['lat'], c['lon']), (center['lat'], center['lon'])) < 900]
            if not near:
                continue
            c = min(near, key=lambda c: prog.distance_km((c['lat'], c['lon']), (center['lat'], center['lon'])))
            px, py = projection.transform_point(c['lon'], c['lat'], geo)
            if inside(px, py):
                # A dot marks the model's low; its letter sits in a fixed direction so agreeing models stay legible.
                dx, dy = LETTER_OFFSETS[list(MODELS).index(model)]
                ax.plot(px, py, 'o', ms=4.5, color=MODELS[model]['color'], mec='white', mew=.8, zorder=10)
                ax.annotate(MODELS[model]['letter'], (px, py), xytext=(dx, dy), textcoords='offset points', fontsize=9,
                            weight='bold', color=MODELS[model]['color'], ha='center', va='center', zorder=10,
                            path_effects=[effects.withStroke(linewidth=2.2, foreground='white')])
    local = valid.astimezone(ZoneInfo(TZ_LABEL))
    ink = COLORS['ink']
    analysis = all(meta['lead'] == 0 for meta in members_meta.values())
    fig.text(.02, .962, 'Consensus surface analysis' if analysis else 'Consensus surface prog', fontsize=21, weight='bold', color=ink)
    fig.text(.98, .962, f'VALID {valid:%HZ %a %d %b %Y}'.upper(), fontsize=17, weight='bold', color=ink, ha='right')
    fig.text(.98, .928, f'{local:%-I %p %Z %a %b %-d}', fontsize=12, color=ink, ha='right')
    if analysis:
        runs = ' · '.join(MODELS[m]['name'] for m in members_meta)
        fig.text(.02, .928, f'Mean of {len(members_meta)} model analyses (F000)   ·   {runs}', fontsize=10.5, color=ink)
    else:
        runs = '   '.join(f"{MODELS[m]['name']} {parse_time(meta['init']):%d/%HZ} F{meta['lead']:03d}" for m, meta in members_meta.items())
        fig.text(.02, .928, f'Equal-weight mean of {len(members_meta)} models   ·   {runs}', fontsize=10.5, color=ink)
    handles = [
        Line2D([], [], color=COLORS['cold'], lw=2.3, marker=(3, 0, 0), markersize=8, label='Cold front'),
        Line2D([], [], color=COLORS['warm'], lw=2.3, marker='o', markersize=6, label='Warm front'),
        Line2D([], [], color=COLORS['warm'], lw=2.3, ls=(0, (3, 3)), gapcolor=COLORS['cold'], label='Stationary front'),
        Patch(facecolor=COLORS['rain'], label='Precip: ≥ half of models'),
        Patch(facecolor=COLORS['moderate'], label='≥ 3/4 models, mean ≥ 0.10 in'),
        Patch(facecolor='white', edgecolor=COLORS['thunder'], hatch='////', label='Thunder: ≥ 3/4 wet, CAPE ≥ 1000'),
        Patch(facecolor=COLORS['snow'], label='Snow'), Patch(facecolor=COLORS['mixed'], label='Mixed / freezing'),
    ]
    handles += [Line2D([], [], ls='none', marker=f"${MODELS[m]['letter']}$", markersize=9, color=MODELS[m]['color'],
                       label=f"{MODELS[m]['name']} low") for m in members_meta]
    fig.legend(handles=handles, loc='lower left', bbox_to_anchor=(.02, .05), ncol=6, frameon=False, fontsize=9,
               handlelength=2.6, columnspacing=1.4)
    fig.text(.02, .026, f'{spec["isobar"]} hPa isobars and H/L from the mean sea-level pressure. Fronts: objective, from the 850 hPa θe thermal front '
             'parameter (Hewson 1998), typed by the cross-front 850 hPa wind.', fontsize=8.5, color='#46555e')
    precipitation = ('Analysis: model initial states, so no precipitation is shown.' if not result['has_precip']
                     else 'Precipitation: 6 h ending at the valid time, ≥ 0.01 in.')
    fig.text(.02, .008, f'{precipitation} Letters: where each model puts its own low. '
             'Experimental model synthesis, not an official WPC/AWC product.', fontsize=8.5, color='#46555e')
    fig.savefig(output, dpi=dpi, facecolor='white')
    plt.close(fig)


def loop(folder, names, path, width=1200, seconds=0.8):
    """Half-resolution animated GIF of the frames, pausing on the last one."""
    from PIL import Image
    if len(names) < 2:
        return
    frames = []
    for name in names:
        with Image.open(folder / name) as image:
            image = image.convert('RGB')
            frames.append(image.resize((width, round(image.height * width / image.width)), Image.LANCZOS).quantize(colors=128))
    durations = [int(seconds * 1000)] * (len(frames) - 1) + [int(seconds * 3000)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)


# ---------------------------------------------------------------- driver

def valid_times(args, now):
    if args.times:
        times = [parse_time(t) for t in args.times]
    else:
        start = parse_time(args.start) if args.start else now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6)
        end = parse_time(args.end) if args.end else start + timedelta(hours=144)
        times = [start + timedelta(hours=h) for h in range(0, int((end - start).total_seconds() // 3600) + 1, args.every)]
    times = sorted(set(times))
    require(times and all(t.minute == 0 and t.hour % 6 == 0 for t in times), 'Valid times must be 00/06/12/18 UTC')
    return times


def png_size(path):
    with open(path, 'rb') as handle:
        header = handle.read(24)
    require(header[:8] == b'\x89PNG\r\n\x1a\n', 'Not a PNG')
    return int.from_bytes(header[16:20], 'big'), int.from_bytes(header[20:24], 'big')


def prune(cache, output_root, keep_cycles=3, keep_days=4):
    """Drop cached model runs older than `keep_days` and all but the newest cycle folders."""
    import shutil
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    for folder in cache.glob('*/*Z'):
        try:
            if datetime.strptime(folder.name, '%Y%m%dT%HZ').replace(tzinfo=UTC) < cutoff:
                shutil.rmtree(folder)
        except ValueError:
            continue
    if output_root:
        cycles = sorted(p for p in output_root.iterdir() if p.is_dir() and re.fullmatch(r'\d{8}T\d{2}Z', p.name))
        for folder in cycles[:-keep_cycles]:
            shutil.rmtree(folder)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--times', nargs='+', help='Explicit valid times (00/06/12/18 UTC, with timezone)')
    parser.add_argument('--start', help='First valid time; default: the current synoptic hour')
    parser.add_argument('--end', help='Last valid time; default: start + 144 h')
    parser.add_argument('--every', type=int, default=6, choices=(6, 12, 24))
    parser.add_argument('--cycle', choices=('newest', 'common'), default='newest',
                        help='newest: each model\'s newest run; common: one shared 00/12Z cycle from its analysis (F000) '
                             'every --every hours through --through')
    parser.add_argument('--through', help='With --cycle common: last time to cover (rounded up to the step)')
    parser.add_argument('--point', help='LAT,LON for consensus/per-model pressure diagnostics in the manifest')
    parser.add_argument('--models', nargs='+', choices=MODELS, default=list(MODELS))
    parser.add_argument('--run', action='append', default=[], metavar='MODEL=TIME', help='Pin a model run, e.g. ifs=2026-09-25T00:00Z')
    parser.add_argument('--views', nargs='+', choices=VIEWS, default=['conus', 'northeast'])
    parser.add_argument('--output-dir', type=Path, help='Default: var/charts/consensus-prog-<first valid>; '
                        'with --cycle common, a <cycle> subfolder is used and older cycles are pruned')
    parser.add_argument('--cache-dir', type=Path, default=REPO / 'var/charts/consensus-prog-cache')
    parser.add_argument('--no-loop', action='store_true', help='Skip the animated GIFs')
    parser.add_argument('--dpi', type=int, default=150)
    parser.add_argument('--jobs', type=int, default=6)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    lat, lon = prog.grid(BOUNDS)
    point = [float(v) for v in args.point.split(',')] if args.point else None
    root = None
    if args.cycle == 'common':
        require(args.through and not (args.times or args.start or args.end or args.run), 'Use --through (only) with --cycle common')
        through = parse_time(args.through)
        cycle = choose_cycle(args.models, through, now, args.every)
        steps = -(-int((through - cycle).total_seconds() // 3600) // args.every)
        times = [cycle + timedelta(hours=args.every * k) for k in range(steps + 1)]
        runs = {m: cycle for m in args.models}
        root = args.output_dir or REPO / 'var/charts/consensus-prog'
        output = root / f'{cycle:%Y%m%dT%HZ}'
    else:
        times = valid_times(args, now)
        pinned = {}
        for item in args.run:
            model, _, value = item.partition('=')
            require(model in MODELS, f'Unknown model {model}')
            pinned[model] = parse_time(value)
        runs = {m: choose_run(m, times, now, pinned.get(m)) for m in args.models}
        output = args.output_dir or REPO / 'var/charts' / f'consensus-prog-{times[0]:%Y%m%dT%HZ}'
    for m, init in runs.items():
        print(f'{MODELS[m]["name"]}: {stamp(init)}', flush=True)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'manifest.json'
    identity = {'version': VERSION, 'runs': {m: stamp(t) for m, t in runs.items()}, 'times': [stamp(t) for t in times],
                'views': args.views, 'point': point, 'dpi': args.dpi}
    if manifest_path.exists():
        # Hourly callers reuse a complete, verified set for the same cycle instead of rebuilding it.
        saved = json.loads(manifest_path.read_text())
        images = [(output / image['file'], image['sha256']) for frame in saved['frames'] for image in frame['images'].values()]
        if saved.get('identity') == identity and not saved['failures'] and all(
                path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in images):
            print(json.dumps({'manifest': str(manifest_path), 'reused': True}), flush=True)
            return

    def load(task):
        model, valid = task
        lead = int((valid - runs[model]).total_seconds() // 3600)
        return task, model_fields(model, runs[model], lead, lat, lon, args.cache_dir)

    tasks = [(m, t) for t in times for m in args.models]
    loaded, failures = {}, []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for future in [pool.submit(load, t) for t in tasks]:
            try:
                (model, valid), value = future.result()
                loaded[model, valid] = value
                print(f'  {model} {stamp(valid)} ok', flush=True)
            except Exception as error:
                failures.append(str(error))
                print(f'  failed: {error}', flush=True)
    frames = []
    for valid in times:
        members = {m: loaded[m, valid][0] for m in args.models if (m, valid) in loaded}
        if len(members) < 2:
            print(f'{stamp(valid)}: skipped, only {len(members)} model(s)', flush=True)
            continue
        meta = {m: {k: loaded[m, valid][1][k] for k in ('init', 'lead')} for m in members}
        result = synthesize(members, lat, lon, point)
        frame = {'valid': stamp(valid), 'models': meta, 'analysis': all(v['lead'] == 0 for v in meta.values()),
                 'precipitation': result['has_precip'], 'images': {}, 'point': result['point'],
                 'highs': result['highs'], 'lows': result['lows'], 'model_lows': result['model_lows'],
                 'fronts': [{k: f[k] for k in ('type', 'points', 'cross_front_ms')} for f in result['fronts']]}
        for view in args.views:
            path = output / f'{view}-{valid:%Y%m%dT%HZ}.png'
            render(result, meta, valid, lat, lon, view, path, args.dpi)
            width, height = png_size(path)
            frame['images'][view] = {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                     'width': width, 'height': height}
        frames.append(frame)
        print(f'{stamp(valid)}: {len(result["fronts"])} front segments, {len(result["lows"])} lows, '
              f'{len(result["highs"])} highs', flush=True)
    manifest = {'version': VERSION, 'identity': identity, 'prepared_at': stamp(datetime.now(UTC)),
                'cycle': args.cycle, 'runs': identity['runs'], 'views': args.views,
                'sources': {m: MODELS[m]['source'] for m in args.models}, 'failures': failures, 'frames': frames}
    if not args.no_loop:
        for view in args.views:
            loop(output, [f['images'][view]['file'] for f in frames], output / f'{view}-loop.gif')
    staged = output / 'manifest.json.tmp'
    staged.write_text(json.dumps(manifest, indent=1) + '\n')
    os.replace(staged, manifest_path)
    prune(args.cache_dir, root)
    print(json.dumps({'manifest': str(manifest_path), 'reused': False, 'failures': len(failures)}), flush=True)
    if failures:
        raise SystemExit(f'{len(failures)} model field(s) failed; charts use the remaining models')


if __name__ == '__main__':
    main()
