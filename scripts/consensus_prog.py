"""Multi-model consensus surface analysis: pressure centers, objective fronts, precipitation agreement.

Pure numpy/scipy on a shared regular latitude/longitude grid (latitude ascending, longitude
ascending, degrees east in -180..180). Fronts follow Hewson (1998): the thermal front parameter
(TFP) of smoothed 850 hPa equivalent potential temperature, located where TFP peaks along the
temperature gradient, i.e. on the warm edge of the frontal zone, as analysts draw them. Front
type comes from the 850 hPa wind component across the front. See deploy/CONSENSUS-PROG.md.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter, label, maximum_filter, minimum_filter, uniform_filter

EARTH_KM = 6371.0
# 850 hPa theta-e, Gaussian sigma ~75 km. K1 in K per (100 km)^2, K2 in K per 100 km.
# thermal_min (K per 100 km, dry 850 hPa temperature) rejects moisture-only boundaries such as
# drylines, tropical moisture edges and the marine layer, which analysts do not draw as fronts.
FRONT = dict(sigma_km=75, tfp_min=0.6, gradient_min=3.2, thermal_min=0.7, zone_km=200, min_length_km=600,
             stationary_ms=1.5, min_run_km=300, terrain_hpa=880, step_km=25)
WET_MM = 0.25            # 0.01 in per 6 h counts as measurable
MODERATE_MM = 2.5        # 0.10 in per 6 h
CAPE_THUNDER = 1000      # J/kg, with measurable precipitation in at least 3/4 of models


def require(ok, message):
    if not ok:
        raise ValueError(message)


def grid(bounds, step=.25):
    """bounds = (west, east, south, north) in degrees; returns ascending (lat, lon) axes."""
    west, east, south, north = bounds
    require(-180 <= west < east <= 180 and -90 < south < north < 90, 'Invalid grid bounds')
    return (np.round(np.arange(south, north + step / 2, step), 6),
            np.round(np.arange(west, east + step / 2, step), 6))


def regrid(values, lat, lon, target_lat, target_lon):
    """Bilinear interpolation from any ascending regular grid to the target grid.

    Longitude may be 0..360 or -180..180 and may wrap; the source must cover the target.
    """
    lat, lon, values = np.asarray(lat, float), np.asarray(lon, float), np.asarray(values, float)
    if lat[0] > lat[-1]:
        lat, values = lat[::-1], values[::-1]
    lon = (lon + 180) % 360 - 180
    order = np.argsort(lon, kind='stable')
    lon, values = lon[order], values[:, order]
    require(np.all(np.diff(lat) > 0) and np.all(np.diff(lon) > 0), 'Source grid is not regular')
    require(lat[0] <= target_lat[0] and lat[-1] >= target_lat[-1], 'Source grid misses target latitudes')
    require(lon[0] <= target_lon[0] and lon[-1] >= target_lon[-1], 'Source grid misses target longitudes')
    y0, y1 = np.searchsorted(lat, target_lat[0], 'right') - 1, np.searchsorted(lat, target_lat[-1], 'left') + 1
    x0, x1 = np.searchsorted(lon, target_lon[0], 'right') - 1, np.searchsorted(lon, target_lon[-1], 'left') + 1
    interpolate = RegularGridInterpolator((lat[y0:y1], lon[x0:x1]), values[y0:y1, x0:x1])
    yy, xx = np.meshgrid(target_lat, target_lon, indexing='ij')
    return interpolate(np.stack([yy, xx], axis=-1))


def spacing_km(lat, lon):
    """Grid spacing (dy, dx[lat]) in km; dx varies with latitude."""
    dy = EARTH_KM * math.radians(float(lat[1] - lat[0]))
    dx = EARTH_KM * np.radians(float(lon[1] - lon[0])) * np.cos(np.radians(lat))[:, None]
    return dy, dx


def gradient(field, lat, lon):
    """(d/dx, d/dy) in field units per 100 km."""
    dy, dx = spacing_km(lat, lon)
    gy, gx = np.gradient(field)
    return gx / dx * 100, gy / dy * 100


def smooth(field, lat, sigma_km):
    """Gaussian smoothing; sigma is set in km along latitude (0.25° ≈ 28 km)."""
    dy = EARTH_KM * math.radians(float(lat[1] - lat[0]))
    return gaussian_filter(field, sigma=sigma_km / dy, mode='nearest')


def theta_e(t_k, pressure_hpa, rh_pct=None, q=None):
    """Equivalent potential temperature (Bolton 1980) from temperature plus RH (%) or specific humidity."""
    t_k = np.asarray(t_k, float)
    if q is not None:
        q = np.clip(np.asarray(q, float), 1e-7, None)
        e = q * pressure_hpa / (0.622 + 0.378 * q)
    else:
        tc = t_k - 273.15
        e = np.clip(np.asarray(rh_pct, float), 1, 100) / 100 * 6.112 * np.exp(17.67 * tc / (tc + 243.5))
    r = 0.622 * e / (pressure_hpa - e)
    t_lcl = 2840 / (3.5 * np.log(t_k) - np.log(e) - 4.805) + 55
    return (t_k * (1000 / pressure_hpa) ** (0.2854 * (1 - 0.28 * r))
            * np.exp((3.376 / t_lcl - 0.00254) * r * 1000 * (1 + 0.81 * r)))


def consensus(members, minimum):
    """Mean of the members present; NaN where fewer than `minimum` members have values."""
    stack = np.stack(members)
    count = np.isfinite(stack).sum(axis=0)
    with np.errstate(invalid='ignore'):
        mean = np.nanmean(np.where(np.isfinite(stack), stack, np.nan), axis=0)
    return np.where(count >= minimum, mean, np.nan)


def centers(pressure, lat, lon, kind, radius_km=700, prominence=1.5, margin=4):
    """Local MSLP extrema that stand out from their ~1,400 km surroundings, strongest first."""
    require(kind in ('H', 'L'), 'kind must be H or L')
    dy = EARTH_KM * math.radians(float(lat[1] - lat[0]))
    size = max(3, int(round(2 * radius_km / dy)) | 1)
    field = np.where(np.isfinite(pressure), pressure, np.nanmean(pressure))
    extreme = (maximum_filter if kind == 'H' else minimum_filter)(field, size=size, mode='nearest')
    background = uniform_filter(field, size=2 * size, mode='nearest')
    stands_out = (field - background >= prominence) if kind == 'H' else (background - field >= prominence)
    mask = (field == extreme) & stands_out & np.isfinite(pressure)
    mask[:margin], mask[-margin:], mask[:, :margin], mask[:, -margin:] = False, False, False, False
    found = [(float(field[i, j]), float(lat[i]), float(lon[j])) for i, j in np.argwhere(mask)]
    found.sort(key=lambda c: -c[0] if kind == 'H' else c[0])
    chosen = []
    for value, y, x in found:
        if all(distance_km((y, x), (c['lat'], c['lon'])) > radius_km for c in chosen):
            chosen.append({'kind': kind, 'hPa': round(value, 1), 'lat': y, 'lon': x})
    return chosen


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1, math.sqrt(h)))


def _path_length(points):
    return sum(distance_km(tuple(a), tuple(b)) for a, b in zip(points[:-1], points[1:]))


def _resample(points, step_km):
    """Evenly spaced points (lat, lon) along a polyline."""
    seg = np.array([distance_km(tuple(a), tuple(b)) for a, b in zip(points[:-1], points[1:])])
    along = np.concatenate([[0], np.cumsum(seg)])
    if along[-1] < step_km:
        return points
    target = np.linspace(0, along[-1], int(along[-1] // step_km) + 1)
    return np.column_stack([np.interp(target, along, points[:, 0]), np.interp(target, along, points[:, 1])])


def _runs(flags):
    """[(start, stop)] index ranges where flags are True."""
    edges = np.diff(np.concatenate([[0], flags.astype(int), [0]]))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def front_diagnostics(theta, u, v, lat, lon, params=FRONT):
    """Smoothed theta-e, its gradient, TFP and the TFP locator on the grid."""
    theta = smooth(np.where(np.isfinite(theta), theta, np.nanmean(theta)), lat, params['sigma_km'])
    gx, gy = gradient(theta, lat, lon)
    magnitude = np.hypot(gx, gy)
    nx, ny = gx / np.maximum(magnitude, 1e-6), gy / np.maximum(magnitude, 1e-6)
    mx, my = gradient(magnitude, lat, lon)
    tfp = -(mx * nx + my * ny)
    tx, ty = gradient(tfp, lat, lon)
    locator = tx * nx + ty * ny
    dy = EARTH_KM * math.radians(float(lat[1] - lat[0]))
    zone = maximum_filter(magnitude, size=max(3, int(round(params['zone_km'] / dy)) | 1), mode='nearest')
    return dict(theta=theta, magnitude=magnitude, nx=nx, ny=ny, tfp=tfp, locator=locator, zone=zone,
                u=np.where(np.isfinite(u), u, 0), v=np.where(np.isfinite(v), v, 0))


def fronts(theta, u, v, lat, lon, terrain_hpa=None, temperature=None, params=FRONT):
    """Objective fronts as [{'type': cold|warm|stationary, 'points': [[lat, lon], ...], 'warm_side': [[nlat, nlon], ...]}].

    `warm_side` holds unit vectors (north, east components) toward warmer air at each point;
    renderers use it to place triangles (cold) toward warm air and semicircles (warm) toward cold air.
    """
    import contourpy
    d = front_diagnostics(theta, u, v, lat, lon, params)
    usable = (d['tfp'] > params['tfp_min']) & (d['zone'] > params['gradient_min'])
    if terrain_hpa is not None:
        usable &= np.where(np.isfinite(terrain_hpa), terrain_hpa, 0) >= params['terrain_hpa']
    if temperature is not None:
        t = smooth(np.where(np.isfinite(temperature), temperature, np.nanmean(temperature)), lat, params['sigma_km'])
        dy = EARTH_KM * math.radians(float(lat[1] - lat[0]))
        usable &= maximum_filter(np.hypot(*gradient(t, lat, lon)), size=max(3, int(round(params['zone_km'] / dy)) | 1),
                                 mode='nearest') > params['thermal_min']
    usable[:3], usable[-3:], usable[:, :3], usable[:, -3:] = False, False, False, False
    sample = {k: RegularGridInterpolator((lat, lon), d[k], bounds_error=False, fill_value=0)
              for k in ('nx', 'ny', 'u', 'v')}
    allowed = RegularGridInterpolator((lat, lon), usable.astype(float), bounds_error=False, fill_value=0)
    lines = contourpy.contour_generator(x=lon, y=lat, z=d['locator']).lines(0.0)
    result = []
    for line in lines:
        points = np.column_stack([line[:, 1], line[:, 0]])  # (lat, lon)
        if len(points) < 2:
            continue
        points = _resample(points, params['step_km'])
        keep = allowed(points) > .5
        for a, b in _runs(keep):
            piece = points[a:b]
            if len(piece) < 3 or _path_length(piece) < params['min_length_km']:
                continue
            # Light along-front smoothing removes grid-scale kinks without moving the front.
            kernel = np.ones(5) / 5
            if len(piece) > 7:
                piece = np.column_stack([np.convolve(np.pad(piece[:, k], 2, mode='edge'), kernel, 'valid') for k in (0, 1)])
            nx, ny = sample['nx'](piece), sample['ny'](piece)
            speed = sample['u'](piece) * nx + sample['v'](piece) * ny  # >0: air moves from cold toward warm
            window = max(1, int(round(150 / params['step_km'])))
            speed = np.convolve(np.pad(speed, window, mode='edge'), np.ones(2 * window + 1) / (2 * window + 1), 'valid')
            kinds = np.where(speed > params['stationary_ms'], 1, np.where(speed < -params['stationary_ms'], -1, 0))
            kinds = _merge_short(kinds, int(round(params['min_run_km'] / params['step_km'])))
            for start, stop, kind in _typed_runs(kinds):
                stop = min(len(piece), stop + 1)  # share a vertex so the drawn front stays continuous
                result.append({'type': {1: 'cold', -1: 'warm', 0: 'stationary'}[kind],
                               'points': np.round(piece[start:stop], 3).tolist(),
                               'warm_side': np.round(np.column_stack([ny, nx])[start:stop], 3).tolist(),
                               'cross_front_ms': round(float(np.mean(speed[start:stop])), 1)})
    return result


def _typed_runs(kinds):
    runs, start = [], 0
    for i in range(1, len(kinds) + 1):
        if i == len(kinds) or kinds[i] != kinds[start]:
            runs.append((start, i, int(kinds[start])))
            start = i
    return runs


def _merge_short(kinds, minimum):
    """Absorb type runs shorter than `minimum` points into their longer neighbour."""
    kinds = kinds.copy()
    while True:
        runs = _typed_runs(kinds)
        short = [r for r in runs if r[1] - r[0] < minimum]
        if len(runs) == 1 or not short:
            return kinds
        start, stop, _ = min(short, key=lambda r: r[1] - r[0])
        index = runs.index(next(r for r in runs if r[0] == start))
        neighbours = [runs[i] for i in (index - 1, index + 1) if 0 <= i < len(runs)]
        kinds[start:stop] = max(neighbours, key=lambda r: r[1] - r[0])[2]


def precipitation(members_mm, cape_members, t2m_k, t850_k):
    """Agreement-based precipitation areas from 6-hour totals.

    Returns fractions of models with measurable precipitation, consensus amount, and boolean
    masks for the shaded categories: `likely` (at least half of models), `moderate`, `thunder`,
    `snow` and `mixed` (freezing/frozen mix by consensus temperatures).
    """
    stack = np.stack(members_mm)
    present = np.isfinite(stack)
    wet = (np.where(present, stack, 0) >= WET_MM).sum(axis=0)
    count = present.sum(axis=0)
    fraction = np.where(count > 0, wet / np.maximum(count, 1), 0)
    amount = consensus(members_mm, 1)
    likely = (fraction >= .5) & (count >= 2)
    moderate = likely & (fraction >= .75) & (np.nan_to_num(amount) >= MODERATE_MM)
    thunder = np.zeros_like(likely)
    if cape_members:
        cape = np.stack(cape_members)
        valid = np.isfinite(cape)
        unstable = (np.where(valid, cape, 0) >= CAPE_THUNDER).sum(axis=0)
        thunder = likely & (fraction >= .75) & (unstable * 2 >= np.maximum(valid.sum(axis=0), 1)) & (valid.sum(axis=0) > 0)
    t2, t8 = np.nan_to_num(t2m_k, nan=300) - 273.15, np.nan_to_num(t850_k, nan=300) - 273.15
    snow = likely & (t2 <= 1) & (t8 <= -2)
    mixed = likely & ~snow & (t2 <= 0.5) & (t8 > -2)
    return dict(fraction=fraction, amount=amount, likely=likely, moderate=moderate,
                thunder=thunder & ~snow, snow=snow, mixed=mixed)


def areas(mask, lat, lon, minimum_cells=40):
    """Centroid (lat, lon) of each connected area large enough to label, largest first."""
    labels, count = label(mask)
    if not count:
        return []
    sizes = np.bincount(labels.ravel())[1:]
    out = []
    for index in np.argsort(-sizes):
        if sizes[index] < minimum_cells:
            break
        cells = np.argwhere(labels == index + 1)
        # Use the area's most interior cell so labels stay inside curved areas.
        i, j = cells[np.argmin(((cells - cells.mean(axis=0)) ** 2).sum(axis=1))]
        out.append((float(lat[i]), float(lon[j]), int(sizes[index])))
    return out
