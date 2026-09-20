#!/usr/bin/env python3
"""Render a WN3 ensemble-mean pressure and surface-wind map from GCS Zarr.

Install requirements-charts.txt; see deploy/WN3-CHARTS.md for usage.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault('MPLCONFIGDIR', str(REPO / 'var/charts/.matplotlib'))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
import matplotlib.patheffects as effects
from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter
import cartopy
import cartopy.crs as ccrs
import cartopy.feature as cfeature

from kcdw.weathernext3_zarr import (
    BUCKET, GrpcStore, WeatherNext3Zarr, parse_utc, run_name, validate_array_metadata,
)

FIELDS = {
    'pressure_hpa': ('mean_sea_level_pressure_mean', 'Pa', .01, 750, 1150),
    'speed_kt': ('wind_speed_10m_mean', 'm s**-1', 3600 / 1852, 0, 312),
    'u_kt': ('u_component_of_wind_10m_mean', 'm s**-1', 3600 / 1852, -312, 312),
    'v_kt': ('v_component_of_wind_10m_mean', 'm s**-1', 3600 / 1852, -312, 312),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def forecast_times(run, valid):
    _, init = run_name(run)
    when = parse_utc(valid)
    hours = (when - init).total_seconds() / 3600
    require(hours.is_integer() and 1 <= hours <= 360, 'Valid time must be an hourly lead from 1 through 360')
    return init, when, int(hours)


def regional_grid(latitudes, longitudes):
    lat = np.asarray(latitudes)
    lon = np.asarray(longitudes)
    require(lat.shape == (1801,) and np.allclose(lat, np.linspace(-90, 90, 1801), rtol=0, atol=1e-5),
            'Unexpected latitude grid')
    require(lon.shape == (3600,) and np.allclose(lon, np.arange(3600) / 10, rtol=0, atol=4e-5),
            'Unexpected longitude grid')
    lon = (lon + 180) % 360 - 180
    # Lambert-map corners extend beyond the geographic display bounds.
    iy = np.flatnonzero((lat >= 14) & (lat <= 66))
    ix = np.flatnonzero((lon >= -140) & (lon <= -47))
    ix = ix[np.argsort(lon[ix])]
    return {'latitude': lat[iy], 'longitude': lon[ix]}, iy, ix


def decode_field(encoded, shape, iy, ix, factor, low, high):
    import zstandard
    expected_size = shape[0] * shape[1] * 4
    decoded = zstandard.ZstdDecompressor().decompress(encoded, max_output_size=expected_size)
    require(len(decoded) == expected_size, 'Incorrect decoded field size')
    plane = np.frombuffer(decoded, dtype='<f4').reshape(shape)
    cropped = plane[np.ix_(iy, ix)].copy() * factor
    require(np.isfinite(cropped).all() and cropped.min() >= low and cropped.max() <= high,
            'Missing or out-of-range regional field values')
    return cropped


def validate_fields(fields):
    require(set(fields) == {'latitude', 'longitude', *FIELDS}, 'Unexpected chart fields')
    lat, lon = fields['latitude'], fields['longitude']
    for axis in (lat, lon):
        require(axis.ndim == 1 and len(axis) > 1 and np.isfinite(axis).all()
                and np.allclose(np.diff(axis), .1, rtol=0, atol=4e-5), 'Invalid chart coordinate axis')
    for key, (_, _, _, low, high) in FIELDS.items():
        value = fields[key]
        require(value.shape == (len(lat), len(lon)) and np.isfinite(value).all()
                and value.min() >= low and value.max() <= high, 'Invalid chart field: ' + key)
    # Mean vector magnitude cannot exceed mean scalar speed (roundoff allowed).
    require(np.all(np.hypot(fields['u_kt'], fields['v_kt']) <= fields['speed_kt'] + .02),
            'Mean vector magnitude exceeds mean scalar speed')


def read_fields(root, run, valid, cache_dir):
    init, when, lead = forecast_times(run, valid)
    run, valid = (value.strftime('%Y-%m-%dT%H:%M:%SZ') for value in (init, when))
    data_path, proof_path = root / 'fields.npz', root / 'provenance.json'
    if data_path.exists() and proof_path.exists():
        proof = json.loads(proof_path.read_text())
        require(proof['init'] == run and proof['valid'] == valid, 'Output directory belongs to another forecast')
        require(hashlib.sha256(data_path.read_bytes()).hexdigest() == proof['data_sha256'], 'Chart data checksum mismatch')
        with np.load(data_path, allow_pickle=False) as data:
            fields = {key: data[key] for key in data.files}
        validate_fields(fields)
        return fields, proof

    root.mkdir(parents=True, exist_ok=True)
    store = GrpcStore(cache_dir=cache_dir)
    source = WeatherNext3Zarr(store, run)
    fields, iy, ix = regional_grid(source.latitudes, source.longitudes)

    def fetch(item):
        key, (array, unit, factor, low, high) = item
        metadata = source.metadata[array]
        shape = validate_array_metadata(array, metadata)
        require(metadata['attributes']['units'] == unit, 'Unexpected source unit: ' + array)
        name = f'{source.prefix}/{array}/c/{lead - 1}/0/0'
        info = source.point_info(array, valid)
        encoded, cached = store.read_object(name, info)
        cropped = decode_field(encoded, shape, iy, ix, factor, low, high)
        proof = {'array': array, 'source_unit': unit, 'conversion_factor': factor,
                 'object': f'gs://{BUCKET}/{name}', **asdict(info),
                 'sha256': hashlib.sha256(encoded).hexdigest(), 'cache_hit': cached,
                 'regional_min': float(cropped.min()), 'regional_max': float(cropped.max())}
        print(f'{array}: {len(encoded):,} bytes, regional range {cropped.min():.2f}–{cropped.max():.2f}', flush=True)
        return key, cropped, proof

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fetch, FIELDS.items()))
    sources = []
    for key, field, proof in results:
        fields[key] = field
        sources.append(proof)
    validate_fields(fields)
    temporary = root / 'fields.tmp.npz'
    np.savez_compressed(temporary, **fields)
    os.replace(temporary, data_path)
    proof = {'model': 'WeatherNext 3', 'statistic': '64-member ensemble mean',
             'init': run, 'valid': valid, 'lead_hours': lead,
             'retrieved_at': datetime.now(timezone.utc).isoformat(), 'grid_degrees': .1,
             'sources': sources, 'data_sha256': hashlib.sha256(data_path.read_bytes()).hexdigest(),
             'source_documentation': 'https://developers.google.com/weathernext/guides/models',
             'license': 'https://storage.googleapis.com/weathernext-public/terms-of-use.pdf',
             'notes': ['Contours are ensemble-mean sea-level pressure, smoothed over 0.2 degrees.',
                       'Shading is ensemble-mean scalar speed; barbs use ensemble-mean U/V.',
                       'Mean-vector magnitude can be lower than mean scalar speed.',
                       'Wind shading spans 0–60 kt in 0.5 kt increments. Ensemble spread is not shown.',
                       'This is experimental model guidance, not an observed weather map.']}
    proof_path.write_text(json.dumps(proof, indent=2) + '\n')
    return fields, proof


def pressure_centers(ax, pressure, longitude, latitude):
    centers = []
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    for symbol, extrema, threshold, color in [
        ('H', maximum_filter(pressure, size=81), 1022, '#0879aa'),
        ('L', minimum_filter(pressure, size=81), 1016, '#b32638'),
    ]:
        mask = (pressure == extrema) & ((pressure >= threshold) if symbol == 'H' else (pressure <= threshold))
        points = np.argwhere(mask)
        points = sorted(points, key=lambda p: float(pressure[tuple(p)]), reverse=symbol == 'H')
        selected = []
        for i, j in points:
            lon, lat = float(longitude[j]), float(latitude[i])
            x, y = ax.projection.transform_point(lon, lat, ccrs.PlateCarree())
            if not (xlim[0] + .045 * np.ptp(xlim) < x < xlim[1] - .045 * np.ptp(xlim)
                    and ylim[0] + .05 * np.ptp(ylim) < y < ylim[1] - .05 * np.ptp(ylim)):
                continue
            if any(np.hypot(x - a, y - b) < 950000 for a, b in selected):
                continue
            selected.append((x, y))
            value = float(pressure[i, j])
            ax.text(x, y, symbol, ha='center', va='center', fontsize=25, weight='bold', color=color,
                    path_effects=[effects.withStroke(linewidth=3.2, foreground='white')], zorder=8)
            ax.annotate(f'{value:.0f}', (x, y), xytext=(0, 18), textcoords='offset points', ha='center',
                        color=color, fontsize=11, weight='bold', zorder=8,
                        path_effects=[effects.withStroke(linewidth=2.5, foreground='white')])
            centers.append({'symbol': symbol, 'pressure_hpa': value, 'longitude': lon, 'latitude': lat})
            if len(selected) == (3 if symbol == 'H' else 6):
                break
    return centers


def render(fields, proof, root, cartopy_dir, dpi=300):
    init, valid, lead = forecast_times(proof['init'], proof['valid'])
    cartopy.config['data_dir'] = str(cartopy_dir)
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'savefig.facecolor': 'white'})
    fig = plt.figure(figsize=(16, 11.3), facecolor='white')
    projection = ccrs.LambertConformal(central_longitude=-97, central_latitude=38, standard_parallels=(33, 45))
    ax = fig.add_axes([.025, .15, .95, .745], projection=projection)
    ax.set_extent([-125.5, -63, 23, 53.5], crs=ccrs.PlateCarree())
    lon, lat = fields['longitude'], fields['latitude']
    xx, yy = np.meshgrid(lon, lat)
    speed = fields['speed_kt']
    pressure = gaussian_filter(fields['pressure_hpa'], sigma=2)
    levels = np.arange(0, 60.5, .5)
    colors = ['#eaf5fd', '#a3d9f2', '#668acf', '#a371c4', '#da82c2', '#d947a0',
              '#d22a6a', '#df2532', '#ee7841', '#f8d263', '#c8ad46', '#9a671d']
    stops = [(0, '#ffffff'), (5 / 60, '#f3f8fc')]
    stops.extend((value / 60, color) for value, color in zip(np.linspace(10, 60, len(colors)), colors))
    cmap = LinearSegmentedColormap.from_list('wind', stops, N=512)
    cmap.set_under('white')
    shading = ax.contourf(xx, yy, speed, levels=levels, cmap=cmap,
                          norm=BoundaryNorm(levels, cmap.N), extend='max',
                          transform=ccrs.PlateCarree(), transform_first=True, zorder=1)
    ax.coastlines('50m', linewidth=.65, color='#353e42', zorder=4)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=.6, edgecolor='#3a4145', zorder=4)
    ax.add_feature(cfeature.STATES.with_scale('50m'), linewidth=.45, edgecolor='#687175', zorder=4)
    ax.add_feature(cfeature.LAKES.with_scale('50m'), facecolor='none', edgecolor='#5b676d', linewidth=.5, zorder=4)
    contours = ax.contour(xx, yy, pressure, levels=np.arange(960, 1062, 2), colors='#35383c',
                          linewidths=.72, transform=ccrs.PlateCarree(), transform_first=True, zorder=5)
    labels = ax.clabel(contours, fmt='%d', fontsize=8.5, inline=True, inline_spacing=4)
    for label in labels:
        label.set_path_effects([effects.withStroke(linewidth=1.8, foreground='white')])
    stride = (slice(5, None, 15), slice(5, None, 20))
    ax.barbs(xx[stride], yy[stride], fields['u_kt'][stride], fields['v_kt'][stride],
             transform=ccrs.PlateCarree(), length=4.1, linewidth=.5, color='#42464b',
             sizes={'emptybarb': .035, 'spacing': .18, 'height': .36}, zorder=3)
    centers = pressure_centers(ax, pressure, lon, lat)
    fig.text(.032, .958, 'MSLP (hPa / mb) and 10 m AGL wind (kt)', fontsize=20, weight='bold', color='#182d35')
    fig.text(.97, .958, 'WEATHERNEXT 3', ha='right', fontsize=20, weight='bold', color='#182d35')
    fig.text(.032, .922, f'F{lead:03d}  •  Valid {valid:%a %d %b %Y, %H UTC}', fontsize=12, weight='bold')
    fig.text(.97, .922, f'Init {init:%a %d %b %Y, %H UTC}  •  Ensemble mean', ha='right', fontsize=12)
    barax = fig.add_axes([.055, .102, .89, .022])
    colorbar = fig.colorbar(shading, cax=barax, orientation='horizontal', ticks=np.arange(0, 61, 5))
    colorbar.set_label('10 m wind speed (kt)  •  Shading in 0.5 kt increments', fontsize=10, labelpad=5)
    colorbar.ax.tick_params(labelsize=10, length=3)
    colorbar.outline.set_edgecolor('#aab2b8')
    fig.text(.032, .035, '2 hPa isobars  •  Shading: mean speed  •  Barbs: mean U/V vector (may be weaker)  •  Ensemble spread not shown', fontsize=9, color='#46555e')
    fig.text(.032, .015, 'Source: Google WeatherNext 3 / Weather Lab, 64 members, 0.1° grid. Experimental guidance. Boundaries: Natural Earth.', fontsize=9, color='#46555e')
    output = root / f'wn3-mslp-wind-f{lead:03d}.png'
    fig.savefig(output, dpi=dpi)
    plt.close(fig)
    proof['pressure_centers'] = centers
    proof['figure_file'] = output.name
    proof['wind_shading'] = {'minimum_kt': 0, 'maximum_kt': 60, 'increment_kt': .5}
    proof['kcdw_marker'] = False
    proof.setdefault('verification', {})['image_pixels'] = [round(16 * dpi), round(11.3 * dpi)]
    proof['figure_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    (root / 'provenance.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(output, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, help='00/06/12/18 UTC initialization, with timezone')
    parser.add_argument('--valid', required=True, help='Hourly valid time, with timezone; lead 1–360')
    parser.add_argument('--output-dir', type=Path, help='Default: var/charts/wn3-<init>-f<lead>')
    parser.add_argument('--cache-dir', type=Path, default=REPO / 'var/wn3-zarr')
    parser.add_argument('--cartopy-dir', type=Path, default=REPO / 'var/charts/cartopy-data')
    parser.add_argument('--dpi', type=int, default=300, help='PNG resolution, 100–600 (default: 300)')
    args = parser.parse_args(argv)
    try:
        init, _, lead = forecast_times(args.run, args.valid)
        require(100 <= args.dpi <= 600, 'DPI must be between 100 and 600')
        root = args.output_dir or REPO / 'var/charts' / f'wn3-{init:%Y%m%dT%HZ}-f{lead:03d}'
        fields, proof = read_fields(root, args.run, args.valid, args.cache_dir)
        render(fields, proof, root, args.cartopy_dir, args.dpi)
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
