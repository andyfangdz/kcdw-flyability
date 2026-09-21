"""Verify the GRIB bridge preserves pressure, grid placement and source identity."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from scripts import gfs_earth_engine_pressure as bridge

HAS_IMPORT = all(importlib.util.find_spec(name) for name in ('eccodes', 'rasterio', 'numpy'))


class PressureIndexTests(unittest.TestCase):
    def test_index_rejects_wrong_cycle_lead_level_and_duplicate_pressure(self):
        run = datetime(2026,9,20,12,tzinfo=timezone.utc)
        text = '1:0:d=2026092012:PRMSL:mean sea level:96 hour fcst:\n2:100:d=2026092012:UGRD:10 m above ground:96 hour fcst:\n3:200:d=2026092012:VGRD:10 m above ground:96 hour fcst:'
        self.assertEqual(bridge.pressure_range(text,run,96),(0,99))
        for bad in (text.replace('d=2026092012','d=2026091912'), text.replace('96 hour','90 hour'),
                    text.replace('mean sea level','surface'), text.replace('UGRD:10 m above ground','PRMSL:mean sea level')):
            with self.assertRaises(ValueError):
                bridge.pressure_range(bad,run,96)

    def test_import_record_rejects_misaligned_pressure_and_changed_object(self):
        run, valid = '2026-09-20T12:00:00Z', '2026-09-24T12:00:00Z'
        record = dict(version=1,run=run,valid=valid,lead=96,
            source_url=bridge.url_for('gfs',bridge.timestamp(run),96),dimensions=[1440,721],
            transform=bridge.TRANSFORM,crs=bridge.CRS,grib_sha256='a'*64,geotiff_sha256='b'*64,
            gs_uri='gs://private-inputs/gfs-pressure/v1/2026092012F096-'+('b'*64)+'.tif')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'2026092012F096.json'
            path.write_text(json.dumps(record))
            self.assertEqual(bridge.load_record(tmp,run,valid),record)
            for update in ({'valid':'2026-09-24T18:00:00Z'},{'lead':102},{'source_url':'https://elsewhere.example'},
                           {'transform':[.25,0,-180,0,-.25,90]}, {'gs_uri':record['gs_uri'].replace('b','c')}):
                path.write_text(json.dumps(record|update))
                with self.assertRaises(ValueError):
                    bridge.load_record(tmp,run,valid)


@unittest.skipUnless(HAS_IMPORT, 'Install requirements-gfs-earth-engine.txt')
class PressureConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import eccodes as ec
        import numpy as np
        cls.initialization = datetime(2026,9,20,12,tzinfo=timezone.utc)
        grib = ec.codes_grib_new_from_samples('regular_ll_sfc_grib2')
        try:
            fields = dict(centre='kwbc', discipline=0, parameterCategory=3, parameterNumber=1,
                typeOfLevel='meanSea', level=0, dataDate=20260920, dataTime=1200,
                stepUnits=1, forecastTime=96, Ni=1440, Nj=721, shapeOfTheEarth=6,
                latitudeOfFirstGridPointInDegrees=90., longitudeOfFirstGridPointInDegrees=0.,
                latitudeOfLastGridPointInDegrees=-90., longitudeOfLastGridPointInDegrees=359.75,
                iDirectionIncrementInDegrees=.25, jDirectionIncrementInDegrees=.25,
                iScansNegatively=0, jScansPositively=0, jPointsAreConsecutive=0, alternativeRowScanning=0,
                packingType='grid_simple', bitsPerValue=24)
            for key,value in fields.items():
                ec.codes_set(grib,key,value)
            cls.values = 98000 + np.arange(721)[:,None]*2 + np.arange(1440)[None,:]/64
            ec.codes_set_values(grib,cls.values.ravel())
            cls.raw = ec.codes_get_message(grib)
        finally:
            ec.codes_release(grib)

    def test_global_rewrap_and_cog_roundtrip_are_lossless(self):
        import numpy as np
        import rasterio
        values, identity = bridge.decode_grib(self.raw,self.initialization,96)
        np.testing.assert_array_equal(values,np.roll(self.values,720,axis=1))
        # Native CDW grid center: row 196, -74.25 longitude column 423 after rewrap.
        self.assertEqual(values[196,423],self.values[196,1143])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'pressure.tif'
            bridge.write_geotiff(path,values)
            with rasterio.open(path) as source:
                self.assertEqual(source.xy(196,423),(-74.25,41.0))
                self.assertEqual(source.tags(ns='IMAGE_STRUCTURE')['LAYOUT'],'COG')
                np.testing.assert_array_equal(source.read(1),values)

    def test_wrong_grib_field_cycle_lead_grid_and_missing_values_are_rejected(self):
        import eccodes as ec
        for key,value in (('parameterNumber',0),('dataDate',20260919),('forecastTime',102),
                          ('longitudeOfFirstGridPointInDegrees',.25),('bitmapPresent',1)):
            grib = ec.codes_new_from_message(self.raw)
            try:
                ec.codes_set(grib,key,value)
                raw = ec.codes_get_message(grib)
            finally:
                ec.codes_release(grib)
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.decode_grib(raw,self.initialization,96)
        with self.assertRaises(ValueError):
            bridge.decode_grib(self.raw[:-8],self.initialization,96)
