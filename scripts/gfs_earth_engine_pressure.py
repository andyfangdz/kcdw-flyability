#!/usr/bin/env python3
"""Import exact NOAA GFS PRMSL records as private GeoTIFF inputs for Earth Engine.

This is a data-format bridge: no interpolation, contouring or weather geometry.
The catalog supplies GFS wind; it does not include sea-level pressure.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from kcdw.native_wind_worker import fetch, url_for

VERSION = 1
TRANSFORM = [.25, 0, -180.125, 0, -.25, 90.125]
CRS = '+proj=longlat +R=6371229 +no_defs'


def require(condition):
    if not condition:
        raise ValueError('GFS pressure input failed identity or integrity validation')


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None)
    return result.astimezone(timezone.utc)


def input_key(run, lead):
    return f'{timestamp(run):%Y%m%d%H}F{lead:03d}'


def pressure_range(text, run, lead):
    rows = [row.split(':') for row in text.splitlines()]
    require(1 < len(rows) <= 1500 and all(len(row) >= 6 for row in rows))
    offsets = [int(row[1]) for row in rows]
    require(offsets == sorted(set(offsets)) and offsets[0] == 0)
    matches = [(i, row) for i, row in enumerate(rows[:-1]) if row[3:5] == ['PRMSL', 'mean sea level']]
    require(len(matches) == 1)
    i, row = matches[0]
    require(row[2] == f'd={run:%Y%m%d%H}' and row[5] == f'{lead} hour fcst')
    start, end = offsets[i], offsets[i+1]-1
    require(0 <= start <= end < 1_000_000_000 and end-start+1 <= 8_000_000)
    return start, end


def decode_grib(raw, run, lead):
    import eccodes as ec
    import numpy as np
    require(raw[:4] == b'GRIB' and raw[-4:] == b'7777' and int.from_bytes(raw[8:16], 'big') == len(raw))
    valid = run + timedelta(hours=lead)
    expected = dict(edition=2, centre='kwbc', discipline=0, parameterCategory=3, parameterNumber=1,
        typeOfLevel='meanSea', level=0, units='Pa', dataDate=int(run.strftime('%Y%m%d')),
        dataTime=int(run.strftime('%H%M')), validityDate=int(valid.strftime('%Y%m%d')),
        validityTime=int(valid.strftime('%H%M')), startStep=lead, endStep=lead, stepType='instant',
        gridType='regular_ll', Ni=1440, Nj=721, latitudeOfFirstGridPointInDegrees=90.,
        longitudeOfFirstGridPointInDegrees=0., latitudeOfLastGridPointInDegrees=-90.,
        longitudeOfLastGridPointInDegrees=359.75, iDirectionIncrementInDegrees=.25,
        jDirectionIncrementInDegrees=.25, iScansNegatively=0, jScansPositively=0,
        jPointsAreConsecutive=0, alternativeRowScanning=0, shapeOfTheEarth=6, radius=6371229,
        bitmapPresent=0)
    grib = ec.codes_new_from_message(raw)
    try:
        require(all(ec.codes_get(grib, key) == value for key, value in expected.items()))
        values = ec.codes_get_values(grib)
        require(values.size == 1440*721 and np.all(np.isfinite(values)) and np.all((values >= 80000) & (values <= 115000)))
        # Shift 0..359.75 to -180..179.75 without resampling or changing values.
        values = np.roll(values.reshape(721, 1440), 720, axis=1)
        return values, expected
    finally:
        ec.codes_release(grib)


def write_geotiff(path, values):
    import numpy as np
    import rasterio
    from affine import Affine
    with rasterio.open(path, 'w', driver='COG', width=1440, height=721, count=1,
            dtype='float64', crs=CRS, transform=Affine(*TRANSFORM),
            compress='DEFLATE', predictor='FLOATING_POINT', blocksize=256, overviews='NONE') as dest:
        dest.write(values, 1)
        dest.set_band_description(1, 'mean_sea_level_pressure')
    with rasterio.open(path) as source:
        require(source.transform == Affine(*TRANSFORM) and source.crs.to_dict() == rasterio.crs.CRS.from_string(CRS).to_dict())
        require(source.tags(ns='IMAGE_STRUCTURE').get('LAYOUT') == 'COG')
        require(np.array_equal(values, source.read(1)))


def prepare(run, valid, directory, bucket, project='aviation-486817'):
    from google.cloud import storage
    from google.api_core.exceptions import PreconditionFailed
    start, end = timestamp(run), timestamp(valid)
    lead = (end-start).total_seconds()/3600
    require(start.minute == start.second == start.microsecond == 0 and start.hour in (0,12))
    require(lead.is_integer() and 0 < lead <= 360 and lead % 6 == 0)
    lead = int(lead)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = input_key(run, lead)
    url = url_for('gfs', start, lead)
    text, _ = fetch(url+'.idx', 300_000)
    raw, proof = fetch(url, 8_000_000, pressure_range(text.decode('ascii'), start, lead))
    values, identity = decode_grib(raw, start, lead)
    (directory/(key+'.grib2')).write_bytes(raw)
    tiff = directory/(key+'.tif')
    write_geotiff(tiff, values)
    digest = hashlib.sha256(tiff.read_bytes()).hexdigest()
    blob = storage.Client(project=project).bucket(bucket).blob(f'gfs-pressure/v{VERSION}/{key}-{digest}.tif')
    try:
        blob.upload_from_filename(tiff, content_type='image/tiff', if_generation_match=0, timeout=60)
    except PreconditionFailed:
        pass  # Reuse only after content verification below.
    require(hashlib.sha256(blob.download_as_bytes(timeout=60)).hexdigest() == digest)
    record = dict(version=VERSION, run=run, valid=valid, lead=lead, source_url=url,
        grib_sha256=hashlib.sha256(raw).hexdigest(), byte_range=proof, identity=identity,
        gs_uri=f'gs://{bucket}/{blob.name}', geotiff_sha256=digest, geotiff_bytes=tiff.stat().st_size,
        transform=TRANSFORM, dimensions=[1440,721], crs=CRS,
        imported_at=datetime.now(timezone.utc).isoformat())
    staged = directory/(key+'.json.tmp')
    staged.write_text(json.dumps(record, indent=2)+'\n')
    staged.replace(directory/(key+'.json'))
    print(json.dumps({'imported': key, 'bytes': tiff.stat().st_size, 'sha256': digest}), flush=True)
    return record


def load_record(directory, run, valid):
    lead = int((timestamp(valid)-timestamp(run)).total_seconds()/3600)
    record = json.loads((Path(directory)/(input_key(run, lead)+'.json')).read_text())
    require((record['version'],record['run'],record['valid'],record['lead']) == (VERSION,run,valid,lead))
    require(record['source_url'] == url_for('gfs',timestamp(run),lead))
    require(record['dimensions'] == [1440,721] and record['transform'] == TRANSFORM and record['crs'] == CRS)
    require(record['gs_uri'].startswith('gs://') and record['gs_uri'].endswith(f"/{input_key(run,lead)}-{record['geotiff_sha256']}.tif"))
    for field in ('grib_sha256','geotiff_sha256'):
        require(len(record[field]) == 64 and all(c in '0123456789abcdef' for c in record[field]))
    return record


def validate_crs(value):
    import math
    from pyproj import CRS as Projection
    projection = Projection.from_user_input(value)
    require(projection.is_geographic and not projection.is_bound and len(projection.axis_info) == 2)
    require(projection.ellipsoid.semi_major_metre == projection.ellipsoid.semi_minor_metre == 6371229)
    require(projection.prime_meridian.longitude == 0)
    require([axis.direction for axis in projection.axis_info] == ['east', 'north'])
    require(all(abs(axis.unit_conversion_factor-math.pi/180) < 1e-15 for axis in projection.axis_info))


def pressure_image(record, native):
    import ee
    image = ee.Image.loadGeoTIFF(record['gs_uri'])
    bands = image.getInfo()['bands']
    require(len(bands) == 1)
    band = bands[0]
    require(band['dimensions'] == [1440,721] and band['crs_transform'] == TRANSFORM)
    # GeoTIFF and the catalog assign different names to the same spherical datum.
    # Validate its actual coordinates instead of equating those arbitrary names.
    validate_crs(band['crs'])
    validate_crs(native['crs'])
    return image.select(0).setDefaultProjection(native['crs'], TRANSFORM).rename('pressure')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', nargs='+', required=True)
    parser.add_argument('--times', nargs='+', required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('var/charts/gfs-pressure'))
    parser.add_argument('--bucket', required=True, help='Existing private bucket in US-CENTRAL1 or the US multi-region')
    parser.add_argument('--project', default='aviation-486817')
    args = parser.parse_args()
    for run in args.runs:
        for valid in args.times:
            prepare(timestamp(run).strftime('%Y-%m-%dT%H:%M:%SZ'), timestamp(valid).strftime('%Y-%m-%dT%H:%M:%SZ'),
                args.output_dir, args.bucket, args.project)


if __name__ == '__main__':
    main()
