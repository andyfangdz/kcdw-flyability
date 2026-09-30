"""Directional site exposure for every station, from Earth Engine (research).

For each station and each of 8 compass sectors (45-degree wedges centred on N, NE, ...,
i.e. the direction the wind comes FROM), in a near ring (0.2-1 km) and a far ring
(1-3 km): fractions of tree cover, built-up area, open land (grass, crop, bare,
herbaceous wetland) and water from ESA WorldCover 10 m (v200); and terrain from
SRTM 30 m: mean and maximum elevation above the station within 1-5 km. Per
station: elevation minus the 25 km-box mean (how unrepresentative the site is of
a model grid cell) and distance to the nearest large low-lying water (sea proxy).
Writes research/mos/station_exposure.json.
"""
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIONS = json.loads((HERE / 'stations.json').read_text())
SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
CLASSES = {'tree': [10], 'built': [50], 'open': [20, 30, 40, 60, 90, 95, 100], 'water': [80]}
RINGS = {'near': (200, 1000), 'far': (1000, 3000)}


def wedge(ee, lon, lat, bearing, r0, r1, steps=12):
    """Annular sector polygon; bearing in degrees clockwise from north, +/- 22.5 degrees."""
    pts = []
    for radius, sweep in ((r1, range(steps + 1)), (r0, reversed(range(steps + 1)))):
        for i in sweep:
            b = math.radians(bearing - 22.5 + 45 * i / steps)
            dlat = radius * math.cos(b) / 111320
            dlon = radius * math.sin(b) / (111320 * math.cos(math.radians(lat)))
            pts.append([lon + dlon, lat + dlat])
    return ee.Geometry.Polygon([pts])


def main():
    import extract_hres_ee
    ee = extract_hres_ee.init_ee()
    cover = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
    dem = ee.Image('USGS/SRTMGL1_003').select('elevation')
    layers = ee.Image.cat([cover.remap(codes, [1] * len(codes), 0).rename(name) for name, codes in CLASSES.items()])
    features = []
    for station, s in STATIONS.items():
        for k, sector in enumerate(SECTORS):
            for ring, (r0, r1) in RINGS.items():
                features.append(ee.Feature(wedge(ee, s['lon'], s['lat'], 45 * k, r0, r1), {'station': station, 'sector': sector, 'ring': ring}))
            features.append(ee.Feature(wedge(ee, s['lon'], s['lat'], 45 * k, 1000, 5000), {'station': station, 'sector': sector, 'ring': 'terrain'}))
    fc = ee.FeatureCollection(features)
    land = layers.reduceRegions(collection=fc.filter(ee.Filter.neq('ring', 'terrain')), reducer=ee.Reducer.mean(), scale=10).getInfo()['features']
    terrain = dem.reduceRegions(collection=fc.filter(ee.Filter.eq('ring', 'terrain')),
                                reducer=ee.Reducer.mean().combine(ee.Reducer.max(), sharedInputs=True), scale=30).getInfo()['features']
    # Large, low-lying water (sea, sounds, bays): water in WorldCover at <= 5 m elevation, distance in km.
    sea = cover.eq(80).And(dem.lte(5)).selfMask()
    points = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([s['lon'], s['lat']]), {'station': k}) for k, s in STATIONS.items()])
    region = points.geometry().buffer(120000).bounds()
    # Distance transforms run in a fixed pixel grid: 200 m pixels, up to 512 pixels (~100 km).
    grid = sea.unmask(0).reproject(crs='EPSG:3857', scale=200)
    distance = grid.fastDistanceTransform(512, 'pixels', 'squared_euclidean').sqrt().multiply(200).rename('d')
    sea_km = distance.reduceRegions(collection=points, reducer=ee.Reducer.first(), scale=200).getInfo()['features']
    box = dem.reduceRegions(collection=points.map(lambda f: f.buffer(12500).bounds()), reducer=ee.Reducer.mean(), scale=90).getInfo()['features']
    out = {k: {'sectors': {sec: {} for sec in SECTORS}} for k in STATIONS}
    for f in land:
        p = f['properties']
        for name in CLASSES:
            out[p['station']]['sectors'][p['sector']][f'{p["ring"]}_{name}'] = round(p.get(name) or 0.0, 4)
    for f in terrain:
        p = f['properties']
        elev = STATIONS[p['station']]['elev_m']
        out[p['station']]['sectors'][p['sector']]['relief_mean_m'] = round((p.get('mean') or elev) - elev, 1)
        out[p['station']]['sectors'][p['sector']]['relief_max_m'] = round((p.get('max') or elev) - elev, 1)
    for f in sea_km:
        out[f['properties']['station']]['sea_km'] = round((f['properties'].get('first') or 0) / 1000, 2)
    for f in box:
        station = f['properties']['station']
        out[station]['elev_minus_area_m'] = round(STATIONS[station]['elev_m'] - (f['properties'].get('mean') or 0), 1)
    (HERE / 'station_exposure.json').write_text(json.dumps(out, indent=1))
    for k, v in out.items():
        trees = {sec: v['sectors'][sec]['near_tree'] for sec in SECTORS}
        print(k, 'sea km', v.get('sea_km'), 'elev-area', v.get('elev_minus_area_m'), 'near tree by sector', trees)


if __name__ == '__main__':
    main()
