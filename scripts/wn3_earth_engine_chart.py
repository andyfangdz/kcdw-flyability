#!/usr/bin/env python3
"""Render WN3 wind shading and pressure contours on Earth Engine.

Earth Engine produces the map raster, including boundaries and wind barbs.
Python prepares barb geometry; local Matplotlib adds labels and the legend.
Optional dependencies: earthengine-api, numpy, matplotlib, cartopy, scipy.
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
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patheffects as effects
from matplotlib.colors import LinearSegmentedColormap, Normalize
import cartopy.crs as ccrs
from cartopy.io.shapereader import Reader
from PIL import Image
from scipy.ndimage import maximum_filter, minimum_filter
from shapely.geometry import box, mapping
from pyproj import Transformer

REPO = Path(__file__).resolve().parents[1]
COLLECTION = 'projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg'
BANDS = ['mean_sea_level_pressure_mean', 'wind_speed_10m_mean',
         'u_component_of_wind_10m_mean', 'v_component_of_wind_10m_mean']
NATIVE = [.1, 0, -180.05, 0, -.1, 90.05]
KCDW = [-74.2814, 40.8752]
KT = 3600/1852
PALETTE = ['ffffff', 'edf6fc', 'c8e9f7', '8dc5eb', '6789ce', '916cbe',
           'c57bb9', 'df579d', 'd7386d', 'e33b3f', 'ed7946', 'f5be62', 'd3a74b']
VIEWS = {'continental': [-125.5, 23, -60, 53.5], 'northeast': [-85, 30, -60, 48]}
RENDERER_VERSION = 2

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
    lon = np.concatenate([np.linspace(w,e,200), np.linspace(w,e,200), np.full(200,w), np.full(200,e)])
    lat = np.concatenate([np.full(200,s), np.full(200,n), np.linspace(s,n,200), np.linspace(s,n,200)])
    x,y = transformer.transform(lon,lat)
    xmin,xmax,ymin,ymax = min(x),max(x),min(y),max(y)
    resolution=(xmax-xmin)/width
    height=math.ceil((ymax-ymin)/resolution)
    ymin=ymax-height*resolution
    return {'dimensions':{'width':width,'height':height}, 'crsCode':'EPSG:5070',
            'affineTransform':{'scaleX':resolution,'shearX':0,'translateX':xmin,
                               'shearY':0,'scaleY':-resolution,'translateY':ymax}}, [xmin,xmax,ymin,ymax]

def ee_projection(grid):
    a=grid['affineTransform']
    return ee.Projection(grid['crsCode'],[a['scaleX'],0,a['translateX'],0,a['scaleY'],a['translateY']])

def boundaries(bounds, path):
    # Include padding because the projected rectangle extends beyond lon/lat bounds.
    w,s,e,n=bounds
    region=box(w-15,s-10,e+15,n+10)
    features=[]
    files=['physical/ne_50m_coastline.shp','physical/ne_50m_lakes.shp',
           'cultural/ne_50m_admin_0_boundary_lines_land.shp',
           'cultural/ne_50m_admin_1_states_provinces_lakes.shp']
    for filename in files:
        for geom in Reader(path/filename).geometries():
            if not geom.intersects(region):
                continue
            if geom.geom_type in ('Polygon','MultiPolygon'):
                geom=geom.boundary
            geom=geom.intersection(region).simplify(.012,preserve_topology=True)
            if geom.is_empty:
                continue
            features.append(ee.Feature(ee.Geometry(mapping(geom),proj='EPSG:4326',geodesic=False)))
    return ee.FeatureCollection(features)

def barb_geometry(x, y, u, v, length):
    """Planar glyph for map-axis U/V in knots; staff points upwind.

    Northern Hemisphere feathers sit clockwise from the upwind staff.
    Speed is rounded to the nearest 5 kt (half increments round upward).
    Separate polygon geometry lets Earth Engine fill 50 kt flags.
    """
    if not all(math.isfinite(value) for value in (x,y,u,v,length)) or length<=0:
        raise ValueError('Invalid wind barb coordinates, components or length')
    speed=math.hypot(u,v)
    if speed>200:
        raise ValueError('Wind barb exceeds the chart wind range')
    rounded=5*math.floor(speed/5+.5)
    flags,remainder=divmod(rounded,50)
    full,half=divmod(remainder,10)
    half=int(half>=5)
    counts={'rounded_speed_kt':rounded,'flag_count':flags,'full_barbs':full,'half_barbs':half}
    if rounded==0:
        angles=np.linspace(0,2*math.pi,25)
        ring=[[x+.13*length*math.cos(a),y+.13*length*math.sin(a)] for a in angles]
        ring[-1]=ring[0]
        return {'lines':[ring],'flags':[],**counts}
    sx,sy=-u/speed,-v/speed
    def point(along,across=0):
        return [x+length*(along*sx+across*sy),y+length*(along*sy-across*sx)]
    lines=[[point(0),point(1)]];polygons=[];offset=1.
    for _ in range(flags):
        ring=[point(offset),point(offset-.125,.4),point(offset-.25)]
        polygons.append([*ring,ring[0]])
        offset-=.3
    for _ in range(full):
        lines.append([point(offset),point(offset+.125,.4)])
        offset-=.125
    if half:
        if flags==0 and full==0:
            offset-=.1875
        lines.append([point(offset),point(offset+.0625,.2)])
    return {'lines':lines,'flags':polygons,**counts}

def barb_features(fields, annotation_grid, grid, view):
    """Prepare planar vectors from the same checked samples as the old chart."""
    rows,cols=fields['u_kt'].shape
    a=annotation_grid['affineTransform']
    x=a['translateX']+(np.arange(cols)+.5)*a['scaleX']
    y=a['translateY']+(np.arange(rows)+.5)*a['scaleY']
    xx,yy=np.meshgrid(x,y)
    stride=25 if view=='continental' else 30
    xx,yy=xx[8::stride,8::stride],yy[8::stride,8::stride]
    geographic=Transformer.from_crs('EPSG:5070','EPSG:4326',always_xy=True)
    lon,lat=geographic.transform(xx,yy)
    u,v=ccrs.epsg(5070).transform_vectors(ccrs.PlateCarree(),lon,lat,
        fields['u_kt'][8::stride,8::stride],fields['v_kt'][8::stride,8::stride])
    # The fixed fraction of map width keeps glyph size stable in the finished figure.
    length=grid['affineTransform']['scaleX']*grid['dimensions']['width']*.0065
    lines=[];flags=[];symbols=[]
    for index in np.ndindex(xx.shape):
        glyph=barb_geometry(float(xx[index]),float(yy[index]),float(u[index]),float(v[index]),length)
        lines.append({'type':'Feature','properties':{},'geometry':{'type':'MultiLineString','coordinates':glyph['lines']}})
        if glyph['flags']:
            flags.append({'type':'Feature','properties':{},'geometry':{'type':'MultiPolygon','coordinates':[[ring] for ring in glyph['flags']]}})
        symbols.append({'x':float(xx[index]),'y':float(yy[index]),'u_map_kt':float(u[index]),'v_map_kt':float(v[index]),
                        **{k:glyph[k] for k in ('rounded_speed_kt','flag_count','full_barbs','half_barbs')}})
    return {'crs':'EPSG:5070','lines':lines,'flags':flags,'symbols':symbols,
            'length_projected_m':length,'sample_stride':stride}

def paint_barbs(features, projection):
    def collection(items):
        return ee.FeatureCollection([ee.Feature(ee.Geometry(item['geometry'],proj=features['crs'],geodesic=False)) for item in items])
    layer=ee.Image(0).byte().reproject(projection).paint(collection(features['lines']),1,1)
    if features['flags']:
        layer=layer.paint(collection(features['flags']),1)
    return layer.selfMask().visualize(palette=['3c4b54'],opacity=.85)

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
        for name in ['earth-engine-map.png','annotation-fields.npz','barb-geometries.json']:
            if digest(root/name)!=saved['files'][name]:
                raise ValueError('Cached output checksum mismatch')
        return saved
    grid,extent=projected_grid(VIEWS[view],width)
    projection=ee_projection(grid)
    native=ee.Projection('EPSG:4326',NATIVE)
    pressure=source.select(BANDS[0]).multiply(.01).rename('pressure_hpa')
    smooth=pressure.convolve(ee.Kernel.gaussian(radius=6,sigma=2,units='pixels',normalize=True)).reproject(native).rename('smoothed_pressure_hpa')
    wind=source.select(BANDS[1]).multiply(KT).rename('wind_kt')
    u=source.select(BANDS[2]).multiply(KT).rename('u_kt')
    v=source.select(BANDS[3]).multiply(KT).rename('v_kt')
    # A modest, projected grid supplies only annotation positions and barbs.
    annotation_grid,annotation_extent=projected_grid(VIEWS[view],width//4)
    sample=ee.Image.cat([smooth,wind,u,v]).resample('bilinear')
    sample=sample.addBands(sample.mask().reduce(ee.Reducer.min()).rename('valid')).unmask(-9999)
    t=time.monotonic()
    numeric=ee.data.computePixels({'expression':sample,'fileFormat':'NUMPY_NDARRAY','grid':annotation_grid,'workloadTag':'kcdw-wind-pressure-annotations'})
    annotation_seconds=time.monotonic()-t
    if not np.all(numeric['valid']==1):
        raise ValueError('Missing weather pixels')
    arrays={name:numeric[name] for name in numeric.dtype.names if name!='valid'}
    if any(not np.isfinite(a).all() for a in arrays.values()):
        raise ValueError('Nonfinite weather field')
    if not (850<arrays['smoothed_pressure_hpa'].min()<=arrays['smoothed_pressure_hpa'].max()<1100):
        raise ValueError('Invalid pressure range')
    if arrays['wind_kt'].min()<0 or arrays['wind_kt'].max()>200:
        raise ValueError('Invalid wind speed')
    if np.any(np.hypot(arrays['u_kt'],arrays['v_kt'])>arrays['wind_kt']+.03):
        raise ValueError('Vector magnitude exceeds mean scalar wind')
    np.savez_compressed(root/'annotation-fields.npz',**arrays)
    barbs=barb_features(arrays,annotation_grid,grid,view)
    (root/'barb-geometries.json').write_text(json.dumps(barbs,separators=(',',':'),allow_nan=False))
    # All actual map pixels, including the isobars, are rendered remotely.
    p=smooth.resample('bilinear').reproject(projection)
    lo=math.floor(float(arrays['smoothed_pressure_hpa'].min())/2)*2-2
    hi=math.ceil(float(arrays['smoothed_pressure_hpa'].max())/2)*2+2
    levels=list(range(lo,hi+1,2))
    contours=ee.ImageCollection([p.subtract(level).zeroCrossing() for level in levels]).max().selfMask()
    base=wind.resample('bilinear').visualize(min=0,max=60,palette=PALETTE)
    boundary=ee.Image(0).byte().reproject(projection).paint(boundaries(VIEWS[view],cartopy_dir),1,1).selfMask()
    raster=(base.blend(boundary.visualize(palette=['77868f']))
            .blend(contours.visualize(palette=['253746']))
            .blend(paint_barbs(barbs,projection)))
    t=time.monotonic()
    png=ee.data.computePixels({'expression':raster,'fileFormat':'PNG','grid':grid,'workloadTag':'kcdw-wind-pressure-map'})
    render_seconds=time.monotonic()-t
    image=Image.open(BytesIO(png));image.load()
    if image.size!=(grid['dimensions']['width'],grid['dimensions']['height']):
        raise ValueError('Unexpected rendered image dimensions')
    if len(image.getbands())==4 and image.getchannel('A').getextrema()!=(255,255):
        raise ValueError('Missing rendered image pixels')
    (root/'earth-engine-map.png').write_bytes(png)
    point_grid={'dimensions':{'width':1,'height':1},'crsCode':'EPSG:4326',
        'affineTransform':{'scaleX':.1,'shearX':0,'translateX':-74.35,'shearY':0,'scaleY':-.1,'translateY':40.95}}
    raw=ee.data.computePixels({'expression':source,'fileFormat':'NUMPY_NDARRAY','grid':point_grid,'workloadTag':'kcdw-wind-pressure-validation'})
    point={b:float(raw[b][0,0]) for b in BANDS}
    comparisons=[]
    for path in (REPO/'var/wn3-bigquery').glob('*.json'):
        cache=json.loads(path.read_text())
        if timestamp(cache['identity']['run'])!=timestamp(info['properties']['start_time']) or not set(BANDS)<=set(cache['identity']['arrays']):
            continue
        for row in cache['rows']:
            if timestamp(row['valid_time'])!=timestamp(info['properties']['end_time']):
                continue
            if (row['longitude'],row['latitude'])!=(-74.3,40.9):
                raise ValueError('BigQuery point mismatch')
            for b in BANDS:
                error=abs(point[b]-row[b])
                if error>1e-5:
                    raise ValueError('BigQuery parity failure: '+b)
                comparisons.append({'band':b,'absolute_error':error})
        if comparisons:
            break
    proof={'renderer_version':RENDERER_VERSION,'source':info,'view':view,'width':width,'grid':grid,'extent':extent,
        'annotation_grid':annotation_grid,'annotation_extent':annotation_extent,
        'fetched_at':datetime.now(timezone.utc).isoformat(),'annotation_seconds':annotation_seconds,
        'server_render_seconds':render_seconds,'png_bytes':len(png),
        'pressure_levels_hpa':levels,'point_raw':point,'bigquery_parity':comparisons,
        'rendering':{'earth_engine':['wind shading','pressure smoothing and contours','map boundaries','wind barb rasterization','projection and rasterization'],
                     'local_geometry':['wind barb projection rotation and glyph geometry'],
                     'local':['pressure labels','H/L labels','KCDW marker','titles and legend']},
        'wind_barbs':{'count':len(barbs['symbols']),'rounding':'Nearest 5 kt; half increments upward',
                      'marks_kt':{'half':5,'full':10,'flag':50},'staff_direction':'Wind from (upwind)',
                      'speed':'Magnitude of ensemble-mean U/V; circle when rounded magnitude is zero'},
        'files':{name:digest(root/name) for name in ['earth-engine-map.png','annotation-fields.npz','barb-geometries.json']},
        'notes':['Pressure smoothing: Gaussian sigma 0.2 degrees; contours every 2 hPa.',
                 'Wind shading: mean scalar speed. Barbs: mean U/V, which can have a smaller magnitude.',
                 'Bilinear interpolation improves rendering, not source resolution (0.1 degrees).',
                 'No ensemble spread or gust forecast shown. Experimental model guidance.',
                 'Elapsed times are single requests, not latency guarantees. Billed compute was not measured.'],
        'source_url':'https://developers.google.com/weathernext/guides/earth-engine',
        'terms_url':'https://storage.googleapis.com/weathernext-public/terms-of-use.pdf'}
    proof_file.write_text(json.dumps(proof,indent=2))
    print(json.dumps({'view':view,'server_render_seconds':render_seconds,'png_bytes':len(png),'point':point,'parity':comparisons}),flush=True)
    return proof

def annotate(root,proof):
    if proof.get('renderer_version')!=RENDERER_VERSION:
        raise ValueError('Cached map uses an older renderer; regenerate before annotating')
    with np.load(root/'annotation-fields.npz',allow_pickle=False) as saved:
        fields={k:saved[k] for k in saved.files}
    p=fields['smoothed_pressure_hpa']; rows,cols=p.shape
    a=proof['annotation_grid']['affineTransform']
    x=a['translateX']+(np.arange(cols)+.5)*a['scaleX']
    y=a['translateY']+(np.arange(rows)+.5)*a['scaleY']
    xx,yy=np.meshgrid(x,y)
    extent=proof['extent']; aspect=(extent[1]-extent[0])/(extent[3]-extent[2])
    fig=plt.figure(figsize=(16,16/aspect+2.4),facecolor='white')
    ax=fig.add_axes([.025,.15,.95,.72])
    ax.imshow(Image.open(root/'earth-engine-map.png'),extent=extent,origin='upper',interpolation='none')
    ax.set_xlim(extent[:2]);ax.set_ylim(extent[2:]);ax.axis('off')
    # Invisible local contours provide label placements only; the visible lines
    # are already in the Earth Engine PNG.
    contours=ax.contour(xx,yy,p,levels=proof['pressure_levels_hpa'],colors='none',linewidths=0)
    labels=ax.clabel(contours,fmt='%d',fontsize=9,colors='#253746',inline=False)
    for label in labels:
        label.set_bbox({'facecolor':'white','edgecolor':'none','pad':.8,'alpha':.9})
    geographic=Transformer.from_crs('EPSG:5070','EPSG:4326',always_xy=True)
    centers=[]
    size=61 if proof['view']=='continental' else 91
    for symbol,comparison,condition,color in [('H',maximum_filter(p,size=size),p>=1022,'#176aa3'),('L',minimum_filter(p,size=size),p<=1016,'#ba304d')]:
        candidates=np.argwhere((p==comparison)&condition)
        candidates=sorted(candidates,key=lambda ij:float(p[tuple(ij)]),reverse=symbol=='H')
        placed=[]
        for i,j in candidates:
            if i<20 or i>=rows-20 or j<20 or j>=cols-20:
                continue
            if any(math.hypot(x[j]-px,y[i]-py)<(800000 if proof['view']=='continental' else 450000) for px,py in placed):
                continue
            placed.append((x[j],y[i]));pressure=float(p[i,j])
            ax.text(x[j],y[i],symbol,fontsize=25,weight='bold',color=color,ha='center',va='center',zorder=7,path_effects=[effects.withStroke(linewidth=3,foreground='white')])
            ax.annotate(f'{pressure:.0f}',(x[j],y[i]),xytext=(0,-21),textcoords='offset points',ha='center',color=color,fontsize=11,zorder=7,path_effects=[effects.withStroke(linewidth=2.5,foreground='white')])
            clon,clat=geographic.transform(x[j],y[i]);centers.append({'symbol':symbol,'pressure_hpa':pressure,'longitude':clon,'latitude':clat})
            if len(placed)>=4:break
    transformer=Transformer.from_crs('EPSG:4326','EPSG:5070',always_xy=True)
    px,py=transformer.transform(*KCDW)
    ax.plot(px,py,'o',markersize=5,markerfacecolor='#172b36',markeredgecolor='white',markeredgewidth=1.5,zorder=10)
    ax.annotate('KCDW',(px,py),xytext=(7,6),textcoords='offset points',fontsize=10,weight='bold',color='#172b36',zorder=10,path_effects=[effects.withStroke(linewidth=3,foreground='white')])
    run=timestamp(proof['source']['properties']['start_time']);valid=timestamp(proof['source']['properties']['end_time'])
    fig.text(.032,.953,'10 m wind & sea-level pressure',fontsize=23,weight='bold',color='#182d35')
    fig.text(.968,.953,'WEATHERNEXT 3',fontsize=19,ha='right',weight='bold',color='#182d35')
    from zoneinfo import ZoneInfo
    local=valid.astimezone(ZoneInfo('America/New_York'))
    fig.text(.032,.918,f'Valid {local:%a %b %d, %-I %p %Z}  |  {valid:%H UTC}  |  F{proof["source"]["properties"]["forecast_hour"]:03}',fontsize=12)
    fig.text(.968,.918,f'Init {run:%b %d, %H UTC}  |  Ensemble mean',fontsize=12,ha='right')
    cmap=LinearSegmentedColormap.from_list('ee-wind',['#'+c for c in PALETTE],N=256)
    bar=fig.add_axes([.07,.102,.86,.019])
    colorbar=fig.colorbar(plt.cm.ScalarMappable(norm=Normalize(0,60),cmap=cmap),cax=bar,orientation='horizontal',ticks=np.arange(0,61,5),extend='max')
    colorbar.set_label('10 m wind speed (kt)',fontsize=11)
    fig.text(.032,.043,'Earth Engine rendered wind shading, wind barbs, 2 hPa isobars and boundaries. Local labels and legend.',fontsize=10,color='#43535e')
    fig.text(.032,.022,'Shading: mean speed  |  Barbs: mean U/V  |  0.1° model grid  |  Experimental forecast; no gusts or ensemble spread shown',fontsize=9,color='#43535e')
    fig.text(.032,.006,'© 2026 Google / DeepMind WeatherNext. Boundaries: Natural Earth. Terms: storage.googleapis.com/weathernext-public/terms-of-use.pdf',fontsize=8,color='#43535e')
    out=root/'wn3-earth-engine-wind-pressure.png'
    fig.savefig(out,dpi=180,facecolor='white');plt.close(fig)
    proof['pressure_centers']=centers
    proof['files'][out.name]=digest(out)
    (root/'provenance.json').write_text(json.dumps(proof,indent=2))
    print(out,flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True)
    parser.add_argument('--valid',required=True)
    parser.add_argument('--view',choices=[*VIEWS,'both'],default='both')
    parser.add_argument('--project',default='aviation-486817')
    parser.add_argument('--width',type=int,default=2000)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--cartopy-dir',type=Path,default=REPO/'var/charts/cartopy-data/shapefiles/natural_earth')
    args=parser.parse_args()
    run,valid=timestamp(args.run),timestamp(args.valid)
    lead=(valid-run).total_seconds()/3600
    if run.minute or run.second or run.hour%6 or not lead.is_integer() or not 1<=lead<=360 or not 800<=args.width<=3000:
        parser.error('Use a synoptic initialization, an hourly lead 1–360, and width 800–3000')
    credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform','https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials=credentials,project=args.project)
    ee.data.setDeadline(60000);ee.data.setMaxRetries(1)
    image,info=source_image(run.strftime('%Y-%m-%dT%H:%M:%SZ'),valid.strftime('%Y-%m-%dT%H:%M:%SZ'))
    for view in VIEWS if args.view=='both' else [args.view]:
        root=args.output_dir/view
        proof=render_server(image,info,view,root,args.cartopy_dir,args.width)
        annotate(root,proof)

if __name__=='__main__':
    main()
