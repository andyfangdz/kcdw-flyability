#!/usr/bin/env python3
"""Render the complete WN3 wind and pressure chart on Earth Engine.

All weather sampling, barb geometry, pressure labels, titles, and legend are
computed and painted on EE. Python submits static layout/fonts/boundaries,
downloads completed RGB strips, and joins them without resampling.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import time

import ee
import google.auth
import shapefile
from PIL import Image
from shapely.geometry import box, mapping, shape
from pyproj import Transformer

try:
    from . import wn3_earth_engine_layers as layers
except ImportError:
    import wn3_earth_engine_layers as layers

REPO = Path(__file__).resolve().parents[1]
COLLECTION = 'projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg'
BANDS = ['mean_sea_level_pressure_mean', 'wind_speed_10m_mean',
         'u_component_of_wind_10m_mean', 'v_component_of_wind_10m_mean']
NATIVE = [.1, 0, -180.05, 0, -.1, 90.05]
PALETTE = ['ffffff', 'edf6fc', 'c8e9f7', '8dc5eb', '6789ce', '916cbe',
           'c57bb9', 'df579d', 'd7386d', 'e33b3f', 'ed7946', 'f5be62', 'd3a74b']
VIEWS = {'continental': [-125.5, 23, -60, 53.5], 'northeast': [-85, 30, -60, 48]}
RENDERER_VERSION = 5
BASE_MAP_WIDTH = 2000
DEFAULT_MAP_WIDTH = 4000
ANNOTATION_WIDTH = 500
BARB_STRIDES = {'continental': 12, 'northeast': 15}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamps require a timezone')
    return result.astimezone(timezone.utc)


def projected_grid(bounds, width):
    transformer = Transformer.from_crs('EPSG:4326', 'EPSG:5070', always_xy=True)
    w,s,e,n = bounds
    longitude=[w+(e-w)*i/199 for i in range(200)]
    latitude=[s+(n-s)*i/199 for i in range(200)]
    lon=longitude+longitude+[w]*200+[e]*200
    lat=[s]*200+[n]*200+latitude+latitude
    x,y = transformer.transform(lon,lat)
    xmin,xmax,ymin,ymax = min(x),max(x),min(y),max(y)
    resolution=(xmax-xmin)/width
    height=math.ceil((ymax-ymin)/resolution)
    ymin=ymax-height*resolution
    return {'dimensions':{'width':width,'height':height}, 'crsCode':'EPSG:5070',
            'affineTransform':{'scaleX':resolution,'shearX':0,'translateX':xmin,
                               'shearY':0,'scaleY':-resolution,'translateY':ymax}}, [xmin,xmax,ymin,ymax]


def boundaries(bounds, path):
    # Include padding because the projected rectangle extends beyond lon/lat bounds.
    w,s,e,n=bounds
    region=box(w-15,s-10,e+15,n+10)
    features=[]
    files=['physical/ne_50m_coastline.shp','physical/ne_50m_lakes.shp',
           'cultural/ne_50m_admin_0_boundary_lines_land.shp',
           'cultural/ne_50m_admin_1_states_provinces_lakes.shp']
    for filename in files:
        for raw_shape in shapefile.Reader(path/filename).iterShapes():
            geom=shape(raw_shape.__geo_interface__)
            if not geom.intersects(region):
                continue
            if geom.geom_type in ('Polygon','MultiPolygon'):
                geom=geom.boundary
            geom=geom.intersection(region).simplify(.012,preserve_topology=True)
            if geom.is_empty:
                continue
            features.append(ee.Feature(ee.Geometry(mapping(geom),proj='EPSG:4326',geodesic=False)))
    return ee.FeatureCollection(features)


def render_png(raster, grid, max_request_pixels=4_000_000, cache_dir=None):
    """Fetch aligned row strips within EE's request limit; join without resampling."""
    width,height=grid['dimensions']['width'],grid['dimensions']['height']
    rows_per_request=max(1,max_request_pixels//width)
    canvas=Image.new('RGB',(width,height))
    requests=[]
    if cache_dir is not None:
        identity=json.dumps({'expression':ee.serializer.encode(raster),'grid':grid},sort_keys=True)
        cache_dir=cache_dir/hashlib.sha256(identity.encode()).hexdigest()
        cache_dir.mkdir(parents=True,exist_ok=True)
    for row in range(0,height,rows_per_request):
        rows=min(rows_per_request,height-row)
        affine=dict(grid['affineTransform'])
        affine['translateY']+=row*affine['scaleY']
        tile_grid={**grid,'dimensions':{'width':width,'height':rows},'affineTransform':affine}
        started=time.monotonic()
        cached=False
        if cache_dir is not None:
            tile_file=cache_dir/f'{row}-{rows}.png'
            tile_meta=tile_file.with_suffix('.json')
            if tile_file.exists() and tile_meta.exists():
                expected=json.loads(tile_meta.read_text())['sha256']
                if digest(tile_file)!=expected:
                    raise ValueError('Cached strip checksum mismatch')
                png=tile_file.read_bytes();cached=True
        if not cached:
            png=ee.data.computePixels({'expression':raster,'fileFormat':'PNG','grid':tile_grid,
                                      'workloadTag':'kcdw-wind-pressure-map'})
        requests.append({'row':row,'height':rows,'seconds':time.monotonic()-started,'png_bytes':len(png),'cached':cached})
        print(json.dumps({'rendered_rows':[row,row+rows],'total_rows':height,
                          'seconds':requests[-1]['seconds']}),flush=True)
        with Image.open(BytesIO(png)) as tile:
            tile.load()
            if tile.size!=(width,rows):
                raise ValueError('Unexpected rendered image dimensions')
            if 'A' in tile.getbands() and tile.getchannel('A').getextrema()!=(255,255):
                raise ValueError('Missing rendered image pixels')
            canvas.paste(tile.convert('RGB'),(0,row))
        if cache_dir is not None and not cached:
            tile_file.write_bytes(png)
            tile_meta.write_text(json.dumps({'sha256':digest(tile_file)}))
    output=BytesIO()
    canvas.save(output,format='PNG')
    return output.getvalue(),requests


def source_image(run, valid):
    subset=ee.ImageCollection(COLLECTION).filter(ee.Filter.eq('start_time',run)).filter(ee.Filter.eq('end_time',valid)).select(BANDS)
    infos=subset.getInfo()['features']
    if len(infos)!=1:
        raise ValueError('Expected exactly one source image')
    info=infos[0]; p=info['properties']
    lead=(timestamp(valid)-timestamp(run)).total_seconds()/3600
    if p['start_time']!=run or p['end_time']!=valid or p['forecast_hour']!=lead:
        raise ValueError('Source time mismatch')
    for b in info['bands']:
        if b['crs']!='EPSG:4326' or b['crs_transform']!=NATIVE or b['dimensions']!=[3600,1801]:
            raise ValueError('Unexpected native grid')
    return ee.Image(info['id']).select(BANDS),info


def render_server(source, info, view, root, cartopy_dir, width):
    root.mkdir(parents=True,exist_ok=True)
    proof_file=root/'provenance.json'
    if proof_file.exists():
        saved=json.loads(proof_file.read_text())
        if saved.get('renderer_version')!=RENDERER_VERSION:
            raise ValueError('Cached map uses an older renderer; choose a new output directory')
        if saved['source']['id']!=info['id'] or saved['view']!=view or saved['width']!=width:
            raise ValueError('Output directory belongs to a different map')
        for name,expected in saved['files'].items():
            if digest(root/name)!=expected:
                raise ValueError('Cached output checksum mismatch')
        return saved
    _,extent=projected_grid(VIEWS[view],width)
    annotation_grid,annotation_extent=projected_grid(VIEWS[view],ANNOTATION_WIDTH)
    properties=info['properties']
    run,valid=timestamp(properties['start_time']),timestamp(properties['end_time'])
    raster,grid,diagnostics=layers.compose(source,BANDS,NATIVE,annotation_grid,annotation_extent,
        extent,width,view,boundaries(VIEWS[view],cartopy_dir),PALETTE,run,valid,int(properties['forecast_hour']))
    # Only validation booleans/counts come back; no sampled model values.
    checks=diagnostics.getInfo()
    stride=BARB_STRIDES[view]
    expected_barbs=len(range(8,annotation_grid['dimensions']['width'],stride))*len(range(8,annotation_grid['dimensions']['height'],stride))
    if checks['valid_weather']!=1 or checks['barb_count']!=expected_barbs or checks['pressure_label_count']<1 or not checks['upright_pressure_labels']:
        raise ValueError('Server-side weather validation failed: '+json.dumps(checks))
    print(json.dumps({'view':view,'server_checks':checks}),flush=True)
    expression=root/'earth-engine-expression.json'
    expression.write_text(json.dumps(ee.serializer.encode(raster),separators=(',',':')))
    started=time.monotonic()
    png,requests=render_png(raster,grid,cache_dir=root/'render-tiles')
    elapsed=time.monotonic()-started
    for name in ['earth-engine-map.png','wn3-earth-engine-wind-pressure.png']:
        (root/name).write_bytes(png)
    proof={'renderer_version':RENDERER_VERSION,'source':info,'view':view,'width':width,
        'grid':grid,'extent':extent,'pixel_ratio':width/BASE_MAP_WIDTH,
        'fetched_at':datetime.now(timezone.utc).isoformat(),'server_render_seconds':elapsed,
        'render_requests':requests,'png_bytes':len(png),'server_checks':checks,
        'finished_image':{**grid['dimensions'],'text_backgrounds':False,'text_halos':False},
        'rendering':{'earth_engine':['weather sampling and validation','wind shading','pressure smoothing and contours',
            'wind vector rotation and barb geometry','wind barb rasterization','pressure label placement and glyphs',
            'H/L identification and labels','map boundaries','titles and legend','complete chart rasterization'],
            'local':['static layout, font and basemap submission','PNG strip assembly and file checksums']},
        'downloads':['rendered RGB pixels','source metadata','validation booleans and feature counts'],
        'files':{name:digest(root/name) for name in ['earth-engine-map.png','wn3-earth-engine-wind-pressure.png','earth-engine-expression.json']},
        'font_sha256':digest(layers.FONT_PATH),
        'notes':['No model-value grids or weather-derived geometry are downloaded.',
                 'Pressure smoothing: Gaussian sigma 0.2 degrees; contours every 2 hPa.',
                 'Wind shading: mean scalar speed. Barbs: mean U/V; nearest 5 kt.',
                 'Northern Hemisphere feathers. Geographic-grid vector rotation matches the previous chart.',
                 '0.1 degree model grid; display interpolation adds no forecast detail.',
                 'Experimental guidance; no gusts or ensemble spread.',
                 'Elapsed time includes PNG transfer and assembly; billed compute was not measured.'],
        'source_url':'https://developers.google.com/weathernext/guides/earth-engine',
        'terms_url':'https://storage.googleapis.com/weathernext-public/terms-of-use.pdf'}
    proof_file.write_text(json.dumps(proof,indent=2))
    print(json.dumps({'view':view,'server_render_seconds':elapsed,'png_bytes':len(png)}),flush=True)
    return proof


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True)
    parser.add_argument('--valid',required=True)
    parser.add_argument('--view',choices=[*VIEWS,'both'],default='both')
    parser.add_argument('--project',default='aviation-486817')
    parser.add_argument('--width',type=int,default=DEFAULT_MAP_WIDTH,
                        help='Map resolution setting (default 4000); finished chart width is 1.44 times this')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--cartopy-dir',type=Path,default=REPO/'var/charts/cartopy-data/shapefiles/natural_earth')
    args=parser.parse_args()
    run,valid=timestamp(args.run),timestamp(args.valid)
    lead=(valid-run).total_seconds()/3600
    if run.minute or run.second or run.hour%6 or not lead.is_integer() or not 1<=lead<=360 or not 800<=args.width<=4000:
        parser.error('Use a synoptic initialization, an hourly lead 1–360, and width 800–4000')
    credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform','https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials=credentials,project=args.project)
    ee.data.setDeadline(300000);ee.data.setMaxRetries(1)
    image,info=source_image(run.strftime('%Y-%m-%dT%H:%M:%SZ'),valid.strftime('%Y-%m-%dT%H:%M:%SZ'))
    for view in VIEWS if args.view=='both' else [args.view]:
        root=args.output_dir/view
        render_server(image,info,view,root,args.cartopy_dir,args.width)
        print(root/'wn3-earth-engine-wind-pressure.png',flush=True)


if __name__=='__main__':
    main()
