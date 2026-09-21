"""Server-side Earth Engine weather symbols, labels, and chart composition.

Functions build EE expressions. No forecast values or glyph coordinates are
evaluated in Python; only static fonts, basemap shapes, and layout go upstream.
"""
import math
from pathlib import Path

import ee

CRS='EPSG:5070'
FONT_PATH=Path(__file__).with_name('earth_engine_assets')/'font.json'


def indices(count):
    count=ee.Number(count)
    return ee.List.sequence(0,count.max(1).subtract(1)).slice(0,count)


def projection(grid):
    a=grid['affineTransform']
    return ee.Projection(CRS,[a['scaleX'],0,a['translateX'],0,a['scaleY'],a['translateY']])


def rectangle(extent):
    w,e,s,n=extent
    return ee.Geometry.Rectangle([w,s,e,n],proj=CRS,geodesic=False)


def point(x,y):
    return ee.Geometry.Point([x,y],proj=CRS)


def positioned(feature,grid):
    a=grid['affineTransform']
    x=ee.Number(feature.get('x')).add(.5).multiply(a['scaleX']).add(a['translateX'])
    y=ee.Number(feature.get('y')).add(.5).multiply(a['scaleY']).add(a['translateY'])
    return feature.setGeometry(point(x,y)).set({'map_x':x,'map_y':y})


def barb(feature,length):
    """Rotate the vector and construct all feathers/flags on EE's server."""
    feature=ee.Feature(feature)
    x,y=ee.Number(feature.get('map_x')),ee.Number(feature.get('map_y'))
    u,v=ee.Number(feature.get('u_kt')),ee.Number(feature.get('v_kt'))
    speed=u.hypot(v)
    rounded=speed.divide(5).add(.5).floor().multiply(5)
    flags=rounded.divide(50).floor()
    full=rounded.mod(50).divide(10).floor()
    half=rounded.mod(10).gte(5)
    # Match the previous Cartopy geographic-grid vector rotation: project a
    # small displacement in the U/V direction, then preserve its knot magnitude.
    lonlat=point(x,y).transform('EPSG:4326',.01).coordinates()
    lon,lat=ee.Number(lonlat.get(0)),ee.Number(lonlat.get(1))
    tip=ee.Geometry.Point([lon.add(u.divide(speed.max(1e-9)).multiply(.001)),
                           lat.add(v.divide(speed.max(1e-9)).multiply(.001))],
                          proj='EPSG:4326').transform(CRS,.01).coordinates()
    dx,dy=ee.Number(tip.get(0)).subtract(x),ee.Number(tip.get(1)).subtract(y)
    magnitude=dx.hypot(dy).max(1e-9)
    sx,sy=dx.divide(magnitude).multiply(-1),dy.divide(magnitude).multiply(-1)
    def vertex(along,across=0):
        along,across=ee.Number(along),ee.Number(across)
        return ee.List([x.add(along.multiply(sx).add(across.multiply(sy)).multiply(length)),
                        y.add(along.multiply(sy).subtract(across.multiply(sx)).multiply(length))])
    def flag(i):
        offset=ee.Number(1).subtract(ee.Number(i).multiply(.3))
        return ee.List([[vertex(offset),vertex(offset.subtract(.125),.4),
                         vertex(offset.subtract(.25)),vertex(offset)]])
    pennants=indices(flags).map(flag)
    def feather(i):
        offset=ee.Number(1).subtract(flags.multiply(.3)).subtract(ee.Number(i).multiply(.125))
        return ee.List([vertex(offset),vertex(offset.add(.125),.4)])
    lines=ee.List([[vertex(0),vertex(1)]]).cat(indices(full).map(feather))
    offset=ee.Number(1).subtract(flags.multiply(.3)).subtract(full.multiply(.125))
    offset=ee.Number(ee.Algorithms.If(flags.add(full).eq(0),offset.subtract(.1875),offset))
    lines=ee.List(ee.Algorithms.If(half,lines.add([vertex(offset),vertex(offset.add(.0625),.2)]),lines))
    circle=ee.List.sequence(0,24).map(lambda i: ee.List([
        x.add(ee.Number(i).multiply(2*math.pi/24).cos().multiply(.13*length)),
        y.add(ee.Number(i).multiply(2*math.pi/24).sin().multiply(.13*length))]))
    lines=ee.List(ee.Algorithms.If(rounded.eq(0),ee.List([circle]),lines))
    return ee.Feature(ee.Geometry.MultiLineString(lines,proj=CRS,geodesic=False),{
        'flag_polygons':pennants,'rounded_speed_kt':rounded,'flag_count':flags,
        'full_barbs':full,'half_barbs':half,'map_x':x,'map_y':y,
        'u_map_kt':sx.multiply(speed).multiply(-1),'v_map_kt':sy.multiply(speed).multiply(-1)})


def barbs(fields,grid,extent,stride,length):
    # EE reports pixel centers as i + 0.5; use integer indices for stride masks.
    coords=ee.Image.pixelCoordinates(projection(grid)).floor().int()
    x,y=coords.select('x'),coords.select('y')
    mask=(x.gte(8).And(y.gte(8)).And(x.lt(grid['dimensions']['width']))
          .And(y.lt(grid['dimensions']['height'])).And(x.mod(stride).eq(8%stride))
          .And(y.mod(stride).eq(8%stride)))
    samples=fields.addBands(coords).updateMask(mask).sample(
        region=rectangle(extent),projection=projection(grid),geometries=False,tileScale=2)
    return samples.map(lambda f:barb(positioned(f,grid),length))


def paint(collection,proj,color,width=None,opacity=1):
    image=ee.Image(0).byte().reproject(proj)
    image=image.paint(collection,1) if width is None else image.paint(collection,1,width)
    return image.selfMask().visualize(palette=[color],opacity=opacity)


def paint_barbs(glyphs,proj,width):
    flags=glyphs.filter(ee.Filter.gt('flag_count',0)).map(lambda f:ee.Feature(
        ee.Geometry.MultiPolygon(f.get('flag_polygons'),proj=CRS,geodesic=False)))
    return paint(glyphs,proj,'3c4b54',width,.85).blend(paint(flags,proj,'3c4b54',opacity=.85))


class Text:
    """Place bundled static font outlines using server-side text and coordinates."""
    def __init__(self):
        self.fonts=ee.Dictionary(ee.String(FONT_PATH.read_text()).decodeJSON())
        signature={'name':'chart_text','returns':'Feature','args':[
            {'name':name,'type':'Object'} for name in (
                'label_text','label_x','label_y','label_size','label_angle','label_anchor','label_weight')]}
        self.renderer=ee.CustomFunction(signature,self._feature)

    def feature(self,text,x,y,size,angle=0,align='left',weight='normal'):
        return ee.Feature(self.renderer.call(text,x,y,size,angle,{'left':0,'center':-.5,'right':-1}[align],weight))

    def _feature(self,text,x,y,size,angle,anchor,weight):
        text=ee.String(text)
        x,y,size,angle=map(ee.Number,(x,y,size,angle))
        font=ee.Dictionary(self.fonts.get(weight))
        chars=text.split('')
        widths=chars.map(lambda c:ee.Dictionary(font.get(ee.String(c))).get('advance'))
        cumulative=ee.Array(widths).accum(0,ee.Reducer.sum()).toList()
        total=ee.Number(cumulative.get(-1))
        shift=total.multiply(anchor)
        def character(i):
            i=ee.Number(i)
            before=ee.Number(cumulative.get(i)).subtract(ee.Number(widths.get(i)))
            glyph=ee.Dictionary(font.get(ee.String(chars.get(i))))
            def vertex(p):
                p=ee.List(p)
                a=ee.Number(p.get(0)).add(before).add(shift).multiply(size)
                b=ee.Number(p.get(1)).multiply(size)
                return ee.List([x.add(a.multiply(angle.cos())).subtract(b.multiply(angle.sin())),
                                y.add(a.multiply(angle.sin())).add(b.multiply(angle.cos()))])
            return ee.List(glyph.get('polygons')).map(lambda polygon:ee.List(polygon).map(
                lambda ring:ee.List(ring).map(vertex)))
        groups=indices(chars.size()).map(character)
        polygons=ee.List(groups.iterate(lambda group,all_groups:ee.List(all_groups).cat(ee.List(group)),ee.List([])))
        return ee.Feature(ee.Geometry.MultiPolygon(polygons,proj=CRS,geodesic=False,evenOdd=True),{'text':text})


def pressure_labels(p,grid,extent,levels,view):
    proj=projection(grid)
    p=p.reproject(proj)
    coords=ee.Image.pixelCoordinates(proj).floor().int()
    # Explicit map-grid differences keep the label tangent in chart axes.
    # Image.gradient instead reports geographic derivatives, which rotate
    # relative to EPSG:5070 away from its central meridian.
    gradient=p.convolve(ee.Kernel.fixed(3,1,[[-.5,0,.5]])).rename('gx').addBands(
        p.convolve(ee.Kernel.fixed(1,3,[[.5],[0],[-.5]])).rename('gy'))
    cols,rows=grid['dimensions']['width'],grid['dimensions']['height']
    margin=coords.select('x').gt(20).And(coords.select('x').lt(cols-20)).And(
        coords.select('y').gt(20)).And(coords.select('y').lt(rows-20))
    def level_labels(level):
        level=ee.Number(level)
        crossings=p.subtract(level).zeroCrossing().And(margin)
        candidates=coords.addBands(gradient).updateMask(crossings).sample(
            region=rectangle(extent),projection=proj,geometries=False,tileScale=2)
        def ranked(f):
            target_x=level.multiply(.6180339).mod(1).multiply(cols*.7).add(cols*.15)
            target_y=level.multiply(.4142136).mod(1).multiply(rows*.7).add(rows*.15)
            score=ee.Number(f.get('x')).subtract(target_x).pow(2).add(
                ee.Number(f.get('y')).subtract(target_y).pow(2))
            # The tangent is perpendicular to the map-space pressure gradient.
            # EE uses x.atan2(y), the reverse of Python's atan2(y, x).
            angle=ee.Number(f.get('gy')).multiply(-1).atan2(ee.Number(f.get('gx')))
            # EE remainder keeps the dividend's sign, so shift positive first.
            angle=angle.add(3*math.pi/2).mod(math.pi).subtract(math.pi/2)
            return positioned(f,grid).set({'score':score,'angle':angle,'label':level.format('%.0f')})
        candidates=candidates.map(ranked)
        if view=='continental':
            selected=candidates.filter(ee.Filter.lt('x',cols/2)).sort('score').limit(1).merge(
                candidates.filter(ee.Filter.gte('x',cols/2)).sort('score').limit(1))
        else:
            selected=candidates.sort('score').limit(1)
        return selected
    return ee.FeatureCollection(levels.map(level_labels)).flatten()


def pressure_centers(p,grid,extent,view,symbol):
    proj=projection(grid)
    radius=30 if view=='continental' else 45
    neighborhood=p.focalMax(radius=radius,kernelType='square') if symbol=='H' else p.focalMin(radius=radius,kernelType='square')
    condition=p.gte(1022) if symbol=='H' else p.lte(1016)
    coords=ee.Image.pixelCoordinates(proj).floor().int()
    cols,rows=grid['dimensions']['width'],grid['dimensions']['height']
    margin=coords.select('x').gte(20).And(coords.select('x').lt(cols-20)).And(
        coords.select('y').gte(20)).And(coords.select('y').lt(rows-20))
    candidates=p.rename('pressure').addBands(coords).updateMask(p.eq(neighborhood).And(condition).And(margin)).sample(
        region=rectangle(extent),projection=proj,geometries=False,tileScale=2).map(lambda f:positioned(f,grid))
    separation=800000 if view=='continental' else 450000
    def select(f,chosen):
        f,chosen=ee.Feature(f),ee.List(chosen)
        def distance(other):
            other=ee.Feature(other)
            return ee.Number(f.get('map_x')).subtract(ee.Number(other.get('map_x'))).hypot(
                ee.Number(f.get('map_y')).subtract(ee.Number(other.get('map_y'))))
        minimum=ee.Number(ee.Algorithms.If(chosen.size(),chosen.map(distance).reduce(ee.Reducer.min()),1e20))
        return ee.List(ee.Algorithms.If(chosen.size().lt(4).And(minimum.gte(separation)),chosen.add(f),chosen))
    selected=candidates.sort('pressure',symbol=='L').toList(1000).iterate(select,ee.List([]))
    return ee.FeatureCollection(ee.List(selected))


def chart_layout(extent,width):
    """Static canvas layout in the same projection as the map."""
    aspect=(extent[1]-extent[0])/(extent[3]-extent[2])
    canvas_width=round(width*1.44)
    dpi=180*width/2000
    canvas_height=int((16/aspect+2.4)*dpi)
    map_width=min(canvas_width*.95,canvas_height*.72*aspect)
    map_height=map_width/aspect
    left=(canvas_width-map_width)/2
    top=canvas_height*(1-.15-.72)+(canvas_height*.72-map_height)/2
    scale=(extent[1]-extent[0])/map_width
    grid={'dimensions':{'width':canvas_width,'height':canvas_height},'crsCode':CRS,
          'affineTransform':{'scaleX':scale,'shearX':0,'translateX':extent[0]-left*scale,
                             'shearY':0,'scaleY':-scale,'translateY':extent[3]+top*scale}}
    return {'grid':grid,'dpi':dpi,'point_size':dpi/72*scale,'map_width_pixels':map_width}


def chart_frame(weather,layout,map_extent,width,palette,run,valid,lead,font=None):
    grid=layout['grid'];proj=projection(grid);ps=layout['point_size']
    font=font or Text()
    canvas=(ee.Image.constant([255,255,255]).byte().rename(['vis-red','vis-green','vis-blue'])
            .reproject(proj).blend(weather.clip(rectangle(map_extent))))
    cw,ch=grid['dimensions']['width'],grid['dimensions']['height']
    aff=grid['affineTransform'];scale=aff['scaleX']
    def xy(x,y):
        return aff['translateX']+x*cw*scale,aff['translateY']-(1-y)*ch*scale
    text_groups={}
    def static(text,x,y,size,color='182d35',align='left',weight='normal'):
        px,py=xy(x,y)
        text_groups.setdefault(color,[]).append(font.feature(text,px,py,size*ps,align=align,weight=weight))
    static('10 m wind & sea-level pressure',.032,.953,23,weight='bold')
    static('WEATHERNEXT 3',.968,.953,19,align='right',weight='bold')
    from zoneinfo import ZoneInfo
    local=valid.astimezone(ZoneInfo('America/New_York'))
    static(f'Valid {local:%a %b %d, %-I %p %Z}  |  {valid:%H UTC}  |  F{lead:03}',.032,.918,12)
    static(f'Init {run:%b %d, %H UTC}  |  Ensemble mean',.968,.918,12,align='right')
    lx,ly=xy(.07,.102);rx,ty=xy(.93,.121)
    legend_rect=rectangle([lx,rx,ly,ty])
    pixel_x=ee.Image.pixelCoordinates(proj).select('x')
    legend=pixel_x.subtract(.07*cw).divide(.86*cw).multiply(60).visualize(min=0,max=60,palette=palette).clip(legend_rect)
    # Mosaic preserves the canvas when map and legend have disjoint footprints.
    canvas=ee.ImageCollection.fromImages([canvas,legend,
        paint(ee.FeatureCollection([ee.Feature(legend_rect)]),proj,'253746',max(1,round(width/2000)))]).mosaic()
    for knot in range(0,61,5):
        static(str(knot),.07+.86*knot/60,.088,9,align='center')
    static('10 m wind speed (kt)',.5,.072,11,align='center')
    static('Earth Engine rendered wind shading, wind barbs, isobars, labels, boundaries and legend.',.032,.043,10,'43535e')
    static('Shading: mean speed  |  Barbs: mean U/V  |  0.1° model grid  |  Experimental forecast; no gusts or ensemble spread shown',.032,.022,9,'43535e')
    static('© 2026 Google / DeepMind WeatherNext. Boundaries: Natural Earth. Terms: storage.googleapis.com/weathernext-public/terms-of-use.pdf',.032,.006,8,'43535e')
    for color,features in text_groups.items():
        canvas=ee.ImageCollection.fromImages([canvas,paint(ee.FeatureCollection(features),proj,color)]).mosaic()
    return canvas.byte()


def compose(source,bands,native_transform,annotation_grid,annotation_extent,map_extent,width,view,boundaries,palette,run,valid,lead):
    layout=chart_layout(map_extent,width)
    grid=layout['grid'];proj=projection(grid);annproj=projection(annotation_grid)
    line_width=max(1,round(layout['map_width_pixels']/2000))
    native=ee.Projection('EPSG:4326',native_transform)
    pressure=source.select(bands[0]).multiply(.01)
    smooth=pressure.convolve(ee.Kernel.gaussian(radius=6,sigma=2,units='pixels',normalize=True)).reproject(native)
    wind=source.select(bands[1]).multiply(3600/1852).rename('wind_kt')
    u=source.select(bands[2]).multiply(3600/1852).rename('u_kt')
    v=source.select(bands[3]).multiply(3600/1852).rename('v_kt')
    fields=ee.Image.cat([smooth.rename('pressure'),wind,u,v]).resample('bilinear').reproject(annproj)
    region=rectangle(annotation_extent)
    statistics=fields.reduceRegion(ee.Reducer.minMax(),region,crs=annproj,maxPixels=2000000)
    lo=ee.Number(statistics.get('pressure_min'));hi=ee.Number(statistics.get('pressure_max'))
    levels=ee.List.sequence(lo.divide(2).floor().multiply(2).subtract(2),hi.divide(2).ceil().multiply(2).add(2),2)
    valid_fields=(fields.mask().reduce(ee.Reducer.min()).eq(1).And(fields.select('pressure').gt(850))
                  .And(fields.select('pressure').lt(1100)).And(fields.select('wind_kt').gte(0))
                  .And(fields.select('wind_kt').lte(200)).And(fields.select('u_kt').hypot(fields.select('v_kt')).lte(fields.select('wind_kt').add(.03))))
    complete=valid_fields.unmask(0).rename('valid').reduceRegion(ee.Reducer.min(),region,crs=annproj,maxPixels=2000000).get('valid')
    stride=12 if view=='continental' else 15
    glyphs=barbs(fields,annotation_grid,annotation_extent,stride,(map_extent[1]-map_extent[0])*.01)
    p=smooth.resample('bilinear').reproject(proj)
    contours=ee.ImageCollection.fromImages(levels.map(lambda level:p.subtract(ee.Number(level)).zeroCrossing())).max().reproject(proj)
    if line_width>1:
        contours=contours.focalMax(kernel=ee.Kernel.fixed(line_width,line_width,[[1]*line_width for _ in range(line_width)]))
    weather=(wind.resample('bilinear').visualize(min=0,max=60,palette=palette)
             .blend(paint(boundaries,proj,'77868f',line_width))
             .blend(contours.selfMask().visualize(palette=['253746']))
             .blend(paint_barbs(glyphs,proj,line_width)))
    font=Text();ps=layout['point_size']
    labels=pressure_labels(fields.select('pressure'),annotation_grid,annotation_extent,levels,view)
    def label(f):
        a=ee.Number(f.get('angle'));size=9*ps
        return font.feature(f.get('label'),ee.Number(f.get('map_x')).subtract(a.sin().multiply(size*.25)),
                            ee.Number(f.get('map_y')).add(a.cos().multiply(size*.25)),size,a,'center')
    weather=weather.blend(paint(labels.map(label),proj,'253746'))
    center_counts={}
    for symbol,color in [('H','176aa3'),('L','ba304d')]:
        centers=pressure_centers(fields.select('pressure'),annotation_grid,annotation_extent,view,symbol)
        center_counts[symbol]=centers.size()
        def center_symbol(f):
            return font.feature(symbol,f.get('map_x'),ee.Number(f.get('map_y')).subtract(25*ps*.35),25*ps,align='center',weight='bold')
        def center_value(f):
            return font.feature(ee.Number(f.get('pressure')).format('%.0f'),f.get('map_x'),ee.Number(f.get('map_y')).subtract(25*ps),11*ps,align='center')
        weather=weather.blend(paint(centers.map(center_symbol).merge(centers.map(center_value)),proj,color))
    canvas=chart_frame(weather,layout,map_extent,width,palette,run,valid,lead,font)
    upright=ee.Number(labels.aggregate_min('angle')).gte(-math.pi/2).And(
        ee.Number(labels.aggregate_max('angle')).lte(math.pi/2))
    diagnostics=ee.Dictionary({'valid_weather':complete,'barb_count':glyphs.size(),'upright_pressure_labels':upright,
                               'pressure_label_count':labels.size(),'pressure_center_counts':center_counts})
    return canvas.byte(),grid,diagnostics
