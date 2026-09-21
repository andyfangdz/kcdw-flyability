import importlib.util
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HAS_CHARTS = all(importlib.util.find_spec(name) is not None for name in (
    'ee', 'PIL', 'pyproj', 'shapely', 'shapefile',
))
if HAS_CHARTS:
    import ee
    from ee import apitestcase
    from PIL import Image
    from scripts import wn3_earth_engine_chart as chart
    from scripts import wn3_earth_engine_layers as layers


@unittest.skipUnless(HAS_CHARTS, 'Install requirements-earth-engine-charts.txt')
class EarthEngineChartTests(apitestcase.ApiTestCase if HAS_CHARTS else unittest.TestCase):
    def test_complete_chart_builds_without_reading_weather_values(self):
        grid,extent=chart.projected_grid(chart.VIEWS['northeast'],chart.ANNOTATION_WIDTH)
        source=ee.Image.constant([103000,10,6,8]).rename(chart.BANDS)
        with patch.object(ee.data,'computeValue',side_effect=AssertionError('Unexpected value download')), \
             patch.object(ee.data,'computePixels',side_effect=AssertionError('Unexpected pixel download')):
            raster,output,checks=layers.compose(source,chart.BANDS,chart.NATIVE,grid,extent,extent,
                4000,'northeast',ee.FeatureCollection([]),chart.PALETTE,
                datetime(2026,9,20,12,tzinfo=timezone.utc),datetime(2026,9,24,15,tzinfo=timezone.utc),99)
            encoded=json.dumps(ee.serializer.encode(raster))
            ee.serializer.encode(checks)
        self.assertEqual(output['dimensions']['width'],5760)
        for operation in ('Image.sample','Image.convolve','Image.focalMax','String.decodeJSON','Number.format','Image.paint'):
            self.assertIn(operation,encoded)
        self.assertLess(len(encoded),1_000_000)

    def test_retina_layout_preserves_extent_and_doubles_pixel_density(self):
        for view in chart.VIEWS:
            _,extent=chart.projected_grid(chart.VIEWS[view],2000)
            standard=layers.chart_layout(extent,2000)
            retina=layers.chart_layout(extent,4000)
            self.assertEqual(retina['grid']['dimensions']['width'],2*standard['grid']['dimensions']['width'])
            self.assertLessEqual(abs(retina['grid']['dimensions']['height']-2*standard['grid']['dimensions']['height']),1)
            self.assertAlmostEqual(retina['point_size'],standard['point_size'],delta=standard['point_size']*.001)
            self.assertAlmostEqual(retina['grid']['affineTransform']['scaleX']*2,
                                   standard['grid']['affineTransform']['scaleX'],delta=1)

    def test_render_strips_preserve_pixels_and_projected_alignment(self):
        expected=bytes(range(8*7*3))
        source=Image.frombytes('RGB',(7,8),expected)
        grid={'dimensions':{'width':7,'height':8},'crsCode':'EPSG:5070',
              'affineTransform':{'scaleX':10,'scaleY':-10,'translateX':100,'translateY':200,
                                 'shearX':0,'shearY':0}}
        def compute(request):
            self.assertEqual(request['fileFormat'],'PNG')
            tile=request['grid']
            self.assertEqual(tile['affineTransform']['translateX'],100)
            row=(200-tile['affineTransform']['translateY'])//10
            output=BytesIO()
            source.crop((0,row,7,row+tile['dimensions']['height'])).save(output,format='PNG')
            return output.getvalue()
        with patch.object(ee.data,'computePixels',side_effect=compute) as compute_pixels:
            png,requests=chart.render_png(None,grid,max_request_pixels=21)
        self.assertEqual(Image.open(BytesIO(png)).tobytes(),expected)
        self.assertEqual(compute_pixels.call_count,3)
        self.assertEqual([(r['row'],r['height']) for r in requests],[(0,3),(3,3),(6,2)])

    def test_missing_pixels_fail_instead_of_becoming_white(self):
        output=BytesIO()
        Image.new('RGBA',(3,2),(0,0,0,0)).save(output,format='PNG')
        grid={'dimensions':{'width':3,'height':2},'crsCode':'EPSG:5070',
              'affineTransform':{'scaleX':10,'scaleY':-10,'translateX':0,'translateY':0}}
        with patch.object(ee.data,'computePixels',return_value=output.getvalue()):
            with self.assertRaisesRegex(ValueError,'Missing rendered image pixels'):
                chart.render_png(None,grid)

    def test_interrupted_export_resumes_and_verifies_cached_strips(self):
        grid={'dimensions':{'width':3,'height':4},'crsCode':'EPSG:5070',
              'affineTransform':{'scaleX':10,'scaleY':-10,'translateX':0,'translateY':0}}
        first=BytesIO();Image.new('RGB',(3,2),'red').save(first,format='PNG')
        second=BytesIO();Image.new('RGB',(3,2),'blue').save(second,format='PNG')
        raster=ee.Image.constant([255]*3)
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)
            with patch.object(ee.data,'computePixels',side_effect=[first.getvalue(),TimeoutError('interrupted')]):
                with self.assertRaises(TimeoutError):
                    chart.render_png(raster,grid,max_request_pixels=6,cache_dir=cache)
            with patch.object(ee.data,'computePixels',return_value=second.getvalue()) as compute:
                png,requests=chart.render_png(raster,grid,max_request_pixels=6,cache_dir=cache)
            self.assertEqual(compute.call_count,1)
            self.assertEqual([r['cached'] for r in requests],[True,False])
            image=Image.open(BytesIO(png))
            self.assertEqual(image.getpixel((1,1)),(255,0,0))
            self.assertEqual(image.getpixel((1,3)),(0,0,255))
            next(cache.glob('*/0-2.png')).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'strip checksum mismatch'):
                chart.render_png(raster,grid,max_request_pixels=6,cache_dir=cache)

    def test_legacy_cache_rejected_without_server_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'provenance.json').write_text(json.dumps({'renderer_version':3}))
            with patch.object(ee.data,'computeValue',side_effect=AssertionError('Unexpected server access')):
                with self.assertRaisesRegex(ValueError,'older renderer'):
                    chart.render_server(None,{'id':'old'},'continental',root,root,4000)

    def test_request_path_downloads_only_png_and_checks_cache(self):
        grid,extent=chart.projected_grid(chart.VIEWS['northeast'],500)
        stride=chart.BARB_STRIDES['northeast']
        count=len(range(8,500,stride))*len(range(8,grid['dimensions']['height'],stride))
        output_grid={'dimensions':{'width':3,'height':2},'crsCode':'EPSG:5070',
                     'affineTransform':{'scaleX':10,'scaleY':-10,'translateX':0,'translateY':0}}
        raw=BytesIO();Image.new('RGB',(3,2),'white').save(raw,format='PNG')
        info={'id':'test-image','properties':{'start_time':'2026-09-20T12:00:00Z',
              'end_time':'2026-09-24T15:00:00Z','forecast_hour':99}}
        class Checks:
            def getInfo(self):
                return {'valid_weather':1,'barb_count':count,'pressure_label_count':10,'upright_pressure_labels':True}
        def compute(request):
            self.assertEqual(request['fileFormat'],'PNG')
            return raw.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(layers,'compose',return_value=(ee.Image.constant([255]*3),output_grid,Checks())), \
                 patch.object(chart,'boundaries',return_value=ee.FeatureCollection([])), \
                 patch.object(ee.data,'computePixels',side_effect=compute), \
                 patch.object(ee.data,'computeValue',side_effect=AssertionError('Unexpected value download')):
                proof=chart.render_server(None,info,'northeast',root,root,4000)
                self.assertFalse(list(root.glob('*.npz')))
                self.assertFalse((root/'barb-geometries.json').exists())
                self.assertEqual(proof['downloads'],['rendered RGB pixels','source metadata','validation booleans and feature counts'])
                self.assertEqual(chart.render_server(None,info,'northeast',root,root,4000),proof)
                (root/'earth-engine-map.png').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                    chart.render_server(None,info,'northeast',root,root,4000)


if __name__=='__main__':
    unittest.main()
