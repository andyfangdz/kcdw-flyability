#!/usr/bin/env python3
"""Render synchronized WeatherNext / IFS / GFS maps entirely in Earth Engine.

Only source metadata, validation counts, and finished PNGs leave Earth Engine.
The manifest is finalized only when every requested frame passes validation.
GFS pressure is imported separately from NOAA; catalog winds stay in EE.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import ee
import google.auth

try:
    from . import wn3_earth_engine_chart as chart
    from . import gfs_earth_engine_pressure as gfs_pressure
except ImportError:
    import wn3_earth_engine_chart as chart
    import gfs_earth_engine_pressure as gfs_pressure

VERSION = 1
MODELS = {
    'wn3': {'name': 'WeatherNext 3', 'statistic': 'Ensemble mean', 'resolution': '0.1°',
        'collection': chart.COLLECTION, 'bands': chart.BANDS,
        'source_url': 'https://developers.google.com/weathernext/guides/earth-engine'},
    'wn2': {'name': 'WeatherNext 2', 'statistic': 'Ensemble mean', 'resolution': '0.25°',
        'collection': 'projects/gcp-public-data-weathernext/assets/weathernext_2_0_0_mean',
        'bands': ['mean_sea_level_pressure', '10m_wind_speed', '10m_u_component_of_wind', '10m_v_component_of_wind'],
        'source_url': 'https://developers.google.com/earth-engine/datasets/catalog/projects_gcp-public-data-weathernext_assets_weathernext_2_0_0_mean'},
    'ifs': {'name': 'ECMWF IFS', 'statistic': 'Deterministic', 'resolution': '0.25°',
        'collection': 'ECMWF/NRT_FORECAST/IFS/OPER',
        'bands': ['mean_sea_level_pressure_sfc', 'u_component_of_wind_10m_sfc', 'v_component_of_wind_10m_sfc'],
        'source_url': 'https://developers.google.com/earth-engine/datasets/catalog/ECMWF_NRT_FORECAST_IFS_OPER'},
    'gfs': {'name': 'NOAA GFS', 'statistic': 'Deterministic', 'resolution': '0.25°',
        'collection': 'NOAA/GFS0P25',
        'bands': ['u_component_of_wind_10m_above_ground', 'v_component_of_wind_10m_above_ground'],
        'source_url': 'https://developers.google.com/earth-engine/datasets/catalog/NOAA_GFS0P25'},
}
CANONICAL = ['pressure', 'speed', 'u', 'v']


def initialize(project):
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform', 'https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials=credentials, project=project)
    ee.data.setDeadline(300000)
    ee.data.setMaxRetries(1)


def source_image(model, run, valid, pressure_dir=None):
    spec = MODELS[model]
    start, end = chart.timestamp(run), chart.timestamp(valid)
    lead = (end-start).total_seconds()/3600
    if not lead.is_integer() or not 1 <= lead <= 360 or lead % 6:
        raise ValueError('Comparison requires positive six-hour forecast leads through 360h')
    collection = ee.ImageCollection(spec['collection'])
    if model in ('ifs', 'gfs'):
        collection = collection.filter(ee.Filter.eq('creation_time', int(start.timestamp()*1000))).filter(ee.Filter.eq('forecast_hours', lead))
    else:
        collection = collection.filterDate(ee.Date(run), ee.Date(run).advance(1, 'hour')).filter(ee.Filter.eq('start_time', run)).filter(ee.Filter.eq('end_time', valid))
    infos = collection.select(spec['bands']).limit(2).getInfo()['features']
    if len(infos) != 1:
        raise ValueError(f'{model} {run} {valid}: expected one source, found {len(infos)}')
    info = infos[0]
    p = info['properties']
    if model in ('ifs', 'gfs'):
        actual = (p['creation_time'], p['forecast_time'], p['forecast_hours'])
        expected = (int(start.timestamp()*1000), int(end.timestamp()*1000), lead)
    else:
        actual = (p['start_time'], p['end_time'], p['forecast_hour'])
        expected = (run, valid, lead)
    if actual != expected:
        raise ValueError(f'{model}: source time mismatch')
    native = info['bands'][0]
    spacing = .1 if model == 'wn3' else .25
    expected_transform = [spacing, 0, -180-spacing/2, 0, -spacing, 90+spacing/2]
    for b in info['bands']:
        if b['crs_transform'] != expected_transform or b['crs'] != native['crs'] or b['dimensions'] != [round(360/spacing), round(180/spacing)+1]:
            raise ValueError(f'{model}: unexpected native grid')
    source = ee.Image(info['id']).select(spec['bands'])
    if model in ('ifs', 'gfs'):
        # The GRIB-imported ECMWF/NRT_FORECAST/IFS/OPER and NOAA/GFS0P25 catalogs are georeferenced one
        # 0.25-degree row north of their GRIB grids (checked 2026-09-24 against ecCodes, native GFS and the
        # land-sea mask along 73W; see kcdw/ecmwf_ee_worker.py). Move rows back before any geometry.
        # Our own GFS pressure GeoTIFF is placed correctly and is not shifted.
        shifted = list(native['crs_transform'])
        shifted[5] -= spacing
        source = source.changeProj(ee.Projection(native['crs'], native['crs_transform']), ee.Projection(native['crs'], shifted))
    if model == 'ifs':
        pressure, u, v = [source.select(b) for b in spec['bands']]
        source = ee.Image.cat([pressure, u.hypot(v), u, v])
    elif model == 'gfs':
        if pressure_dir is None:
            raise ValueError('GFS requires imported NOAA pressure; supply --gfs-pressure-dir')
        record = gfs_pressure.load_record(pressure_dir, run, valid)
        pressure = gfs_pressure.pressure_image(record, native)
        u, v = [source.select(b) for b in spec['bands']]
        source = ee.Image.cat([pressure, u.hypot(v), u, v])
        info['pressure_input'] = record
    return source.rename(CANONICAL), info, int(lead)


def render_frame(task):
    model, run, valid, root, width, project, pressure_dir = task
    initialize(project)
    identity = {'version': VERSION, 'model': model, 'run': run, 'valid': valid, 'width': width}
    if model == 'gfs':
        identity['pressure_sha256'] = gfs_pressure.load_record(pressure_dir, run, valid)['geotiff_sha256']
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    root = Path(root)/key
    root.mkdir(parents=True, exist_ok=True)
    proof = root/'provenance.json'
    if proof.exists():
        saved = json.loads(proof.read_text())
        if saved['identity'] != identity or chart.digest(root/'map.png') != saved['frame']['sha256']:
            raise ValueError('Cached frame mismatch')
        return saved['frame']
    source, info, lead = source_image(model, run, valid, pressure_dir)
    grid, extent = chart.projected_grid(chart.VIEWS['northeast'], width)
    ann, ann_extent = chart.projected_grid(chart.VIEWS['northeast'], chart.ANNOTATION_WIDTH)
    native = info['bands'][0]
    raster, grid, diagnostics = chart.layers.compose(source, CANONICAL, native['crs_transform'], ann, ann_extent,
        extent, width, 'northeast', chart.boundaries(chart.VIEWS['northeast'], chart.REPO/'var/charts/cartopy-data/shapefiles/natural_earth'),
        chart.PALETTE, chart.timestamp(run), chart.timestamp(valid), lead,
        native_crs=native['crs'], map_grid=grid)
    checks = diagnostics.getInfo()
    expected_barbs = len(range(8,ann['dimensions']['width'],15))*len(range(8,ann['dimensions']['height'],15))
    if checks['valid_weather'] != 1 or checks['barb_count'] != expected_barbs or checks['pressure_label_count'] < 1 or not checks['upright_pressure_labels']:
        raise ValueError('Invalid weather: '+json.dumps(checks))
    print(json.dumps({'validated': identity, 'checks': checks}), flush=True)
    (root/'expression.json').write_text(json.dumps(ee.serializer.encode(raster), separators=(',', ':')))
    png, requests = chart.render_png(raster, grid, cache_dir=root/'tiles')
    (root/'map.png').write_bytes(png)
    frame = {'model': model, 'run': run, 'valid': valid, 'lead': lead, 'source_id': info['id'],
             'sha256': hashlib.sha256(png).hexdigest(), **grid['dimensions'], 'file': str(root/'map.png')}
    if model == 'gfs':
        frame['pressure_source'] = {key: info['pressure_input'][key] for key in ('source_url', 'grib_sha256', 'geotiff_sha256')}
    proof.write_text(json.dumps({'identity': identity, 'frame': frame, 'source': info, 'grid': grid,
        'server_checks': checks, 'requests': requests, 'rendered_at': datetime.now(timezone.utc).isoformat()}, indent=2))
    print(json.dumps({'complete': identity, 'bytes': len(png)}), flush=True)
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', nargs='+', required=True)
    parser.add_argument('--times', nargs='+', required=True)
    parser.add_argument('--models', nargs='+', choices=MODELS, default=list(MODELS))
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--event', default='commercial-checkride')
    parser.add_argument('--event-date', default='2026-09-24')
    parser.add_argument('--project', default='aviation-486817')
    parser.add_argument('--width', type=int, default=1800)
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--gfs-pressure-dir', type=Path, default=Path('var/charts/gfs-pressure'),
        help='Verified inputs prepared by gfs_earth_engine_pressure.py')
    args = parser.parse_args()
    if not 800 <= args.width <= 2000 or not 1 <= args.jobs <= 3:
        parser.error('width must be 800–2000 and jobs 1–3')
    runs = sorted({chart.timestamp(t).strftime('%Y-%m-%dT%H:%M:%SZ') for t in args.runs})
    times = sorted({chart.timestamp(t).strftime('%Y-%m-%dT%H:%M:%SZ') for t in args.times})
    if any(chart.timestamp(t).hour not in (0,12) for t in runs):
        parser.error('Use common 00Z or 12Z long-range cycles')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.probe:
        initialize(args.project)
        for model in args.models:
            for run in runs:
                for valid in times:
                    _, info, lead = source_image(model, run, valid, args.gfs_pressure_dir)
                    print(json.dumps({'model': model, 'run': run, 'valid': valid, 'lead': lead, 'id': info['id']}), flush=True)
        return
    tasks = [(m,r,t,str(args.output_dir.resolve()),args.width,args.project,str(args.gfs_pressure_dir.resolve())) for m in args.models for r in runs for t in times]
    frames, failures = [], []
    with ProcessPoolExecutor(max_workers=args.jobs) as executor:
        for future in as_completed([executor.submit(render_frame,t) for t in tasks]):
            try:
                frames.append(future.result())
            except Exception as error:
                failures.append(str(error))
                print(json.dumps({'failed': str(error)}), flush=True)
    if failures:
        raise RuntimeError(f'{len(failures)} frame(s) failed; rerun to resume completed frames: '+ '; '.join(failures))
    manifest = {'version': 1, 'event_slug': args.event, 'event_date': args.event_date,
        'prepared_at': datetime.now(timezone.utc).isoformat(), 'runs': runs, 'times': times,
        'models': [{'id': m, **{k:v for k,v in MODELS[m].items() if k != 'bands'}} for m in args.models],
        'palette': chart.PALETTE, 'frames': sorted(frames, key=lambda f:(f['model'],f['run'],f['valid'])),
        'bounds': chart.VIEWS['northeast'], 'renderer_version': VERSION}
    staged = args.output_dir/'manifest.json.tmp'
    staged.write_text(json.dumps(manifest, indent=2)+'\n')
    staged.replace(args.output_dir/'manifest.json')


if __name__ == '__main__':
    main()
