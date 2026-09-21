"""Source alignment matters more than display interpolation in a model comparison."""
import copy
import importlib.util
import json
import unittest
from unittest.mock import patch

HAS_CHARTS = all(importlib.util.find_spec(name) is not None for name in ('ee','PIL','pyproj','shapely','shapefile'))
if HAS_CHARTS:
    import ee
    from ee import apitestcase
    from scripts import earth_engine_coastal_maps as maps


@unittest.skipUnless(HAS_CHARTS, 'Install requirements-earth-engine-charts.txt')
class CoastalSourceTests(apitestcase.ApiTestCase if HAS_CHARTS else unittest.TestCase):
    initialization = '2026-09-20T12:00:00Z'
    valid = '2026-09-24T12:00:00Z'

    def metadata(self, model):
        step = .1 if model == 'wn3' else .25
        props = {'start_time':self.initialization, 'end_time':self.valid, 'forecast_hour':96}
        if model in ('ifs', 'gfs'):
            props = {'creation_time':int(maps.chart.timestamp(self.initialization).timestamp()*1000),
                'forecast_time':int(maps.chart.timestamp(self.valid).timestamp()*1000), 'forecast_hours':96}
        return {'id':'test-image', 'properties':props, 'bands':[{'id':b, 'crs':'EPSG:4326',
            'crs_transform':[step,0,-180-step/2,0,-step,90+step/2],
            'dimensions':[round(360/step),round(180/step)+1]} for b in maps.MODELS[model]['bands']]}

    def test_four_sources_keep_exact_cycles_and_derive_only_deterministic_speed(self):
        for model in maps.MODELS:
            with self.subTest(model=model), patch.object(ee.ImageCollection, 'getInfo', return_value={'features':[self.metadata(model)]}), \
                    patch.object(maps.gfs_pressure, 'load_record', return_value={'source_url':'https://noaa.example/prmsl'}), \
                    patch.object(maps.gfs_pressure, 'pressure_image', return_value=ee.Image(100000)) as pressure:
                image, info, lead = maps.source_image(model, self.initialization, self.valid, '/inputs')
                self.assertEqual(lead,96)
                expression = json.dumps(ee.serializer.encode(image))
                self.assertEqual('Image.hypot' in expression, model in ('ifs', 'gfs'))
                self.assertIn('pressure',expression)
                self.assertEqual(pressure.called, model == 'gfs')

    def test_wrong_valid_time_ambiguous_image_and_shifted_grid_are_rejected(self):
        for model in maps.MODELS:
            original = self.metadata(model)
            wrong_time = copy.deepcopy(original)
            wrong_time['properties']['forecast_time' if model in ('ifs', 'gfs') else 'end_time'] = 0
            wrong_grid = copy.deepcopy(original)
            wrong_grid['bands'][0]['crs_transform'][2] += .1
            for features in ([],[original,original],[wrong_time],[wrong_grid]):
                with self.subTest(model=model, features=features), patch.object(ee.ImageCollection, 'getInfo', return_value={'features':features}):
                    with self.assertRaises(ValueError):
                        maps.source_image(model,self.initialization,self.valid)

    def test_gfs_rejects_absent_pressure_instead_of_borrowing_another_models_field(self):
        with patch.object(ee.ImageCollection, 'getInfo', return_value={'features':[self.metadata('gfs')]}):
            with self.assertRaisesRegex(ValueError, 'GFS requires imported NOAA pressure'):
                maps.source_image('gfs', self.initialization, self.valid)

    def test_imported_pressure_preserves_sphere_and_rejects_shifted_or_ellipsoidal_grids(self):
        bridge = maps.gfs_pressure
        native = self.metadata('gfs')['bands'][0]
        native['crs'] = '+proj=longlat +R=6371229 +no_defs'
        record = {'gs_uri':'gs://test-inputs/pressure.tif'}
        with patch.object(ee.Image, 'getInfo', return_value={'bands':[native]}):
            image = bridge.pressure_image(record,native)
            self.assertIn('Image.loadGeoTIFF',json.dumps(ee.serializer.encode(image)))
        bad = copy.deepcopy(native)
        bad['crs_transform'][2] += .125
        for band in (bad,native|{'crs':'EPSG:4326'},native|{'crs':'+proj=longlat +R=6371000 +no_defs'}):
            with patch.object(ee.Image, 'getInfo', return_value={'bands':[band]}), self.assertRaises(ValueError):
                bridge.pressure_image(record,native)
