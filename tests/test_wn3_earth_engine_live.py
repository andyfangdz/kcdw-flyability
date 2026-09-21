"""Opt-in EE server checks: run with WN3_EE_LIVE_TESTS=1 and project ADC."""
import importlib.util
from datetime import datetime, timezone
from io import BytesIO
import math
import os
import unittest

LIVE=os.environ.get('WN3_EE_LIVE_TESTS')=='1' and importlib.util.find_spec('ee') is not None


@unittest.skipUnless(LIVE,'Set WN3_EE_LIVE_TESTS=1 with authorized Earth Engine credentials')
class EarthEngineLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ee
        import google.auth
        credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform','https://www.googleapis.com/auth/earthengine'])
        ee.Initialize(credentials=credentials,project='aviation-486817')
        ee.data.setDeadline(180000)

    def test_server_barb_shapes_rounding_and_direction(self):
        import ee
        from scripts import wn3_earth_engine_layers as layers
        checks={}
        for speed,flags,full,half in [(0,0,0,0),(5,0,0,1),(10,0,1,0),(15,0,1,1),
                                      (50,1,0,0),(65,1,1,1),(100,2,0,0)]:
            glyph=layers.barb(ee.Feature(layers.point(0,2000000),{
                'map_x':0,'map_y':2000000,'u_kt':0,'v_kt':-speed}),1000)
            ok=ee.Number(glyph.get('rounded_speed_kt')).eq(speed)
            for key,value in [('flag_count',flags),('full_barbs',full),('half_barbs',half)]:
                ok=ok.And(ee.Number(glyph.get(key)).eq(value))
            ok=ok.And(ee.List(glyph.get('flag_polygons')).size().eq(flags))
            if speed:
                staff=ee.List(glyph.geometry().coordinates().get(0))
                tip=ee.List(staff.get(1))
                ok=ok.And(ee.Number(tip.get(1)).gt(2000000)).And(ee.Number(tip.get(0)).abs().lt(1))
            else:
                ring=ee.List(glyph.geometry().coordinates().get(0))
                ok=ok.And(ring.size().eq(25))
            checks[str(speed)]=ok
        for speed,expected in [(2.49,0),(2.5,5),(7.5,10),(47.5,50)]:
            glyph=layers.barb(ee.Feature(layers.point(0,2000000),{
                'map_x':0,'map_y':2000000,'u_kt':speed,'v_kt':0}),1000)
            checks['round_'+str(speed)]=ee.Number(glyph.get('rounded_speed_kt')).eq(expected)
        self.assertTrue(all(ee.Dictionary(checks).getInfo().values()))

    def test_server_weather_matches_previous_bigquery_reference(self):
        import ee
        from scripts import wn3_earth_engine_chart as chart
        source,_=chart.source_image('2026-09-20T12:00:00Z','2026-09-24T15:00:00Z')
        # Existing independent BigQuery reference; EE returns only comparison booleans.
        expected=[103166.3359375,4.684749603271484,-3.43091082572937,-3.100642204284668]
        reference=ee.Geometry.Point([-74.3,40.9])
        values=source.reduceRegion(ee.Reducer.first(),reference,crs='EPSG:4326',crsTransform=chart.NATIVE,maxPixels=10)
        checks={band:ee.Number(values.get(band)).subtract(value).abs().lte(1e-5)
                for band,value in zip(chart.BANDS,expected)}
        self.assertTrue(all(ee.Dictionary(checks).getInfo().values()))

    def test_server_sampling_uses_pixel_centers_and_preserves_density(self):
        import ee
        from scripts import wn3_earth_engine_chart as chart
        from scripts import wn3_earth_engine_layers as layers
        checks={}
        for view in chart.VIEWS:
            grid,extent=chart.projected_grid(chart.VIEWS[view],500)
            fields=ee.Image.constant([3,4]).rename(['u_kt','v_kt']).reproject(layers.projection(grid))
            stride=chart.BARB_STRIDES[view]
            glyphs=layers.barbs(fields,grid,extent,stride,(extent[1]-extent[0])*.01)
            expected=len(range(8,500,stride))*len(range(8,grid['dimensions']['height'],stride))
            a=grid['affineTransform']
            checks[view]=glyphs.size().eq(expected).And(ee.Number(glyphs.aggregate_min('map_x')).subtract(
                a['translateX']+8.5*a['scaleX']).abs().lt(.001)).And(
                ee.Number(glyphs.first().get('rounded_speed_kt')).eq(5))
        self.assertTrue(all(ee.Dictionary(checks).getInfo().values()))

    def test_pressure_labels_follow_contour_tangents(self):
        import ee
        from scripts import wn3_earth_engine_layers as layers
        from scripts import wn3_earth_engine_chart as chart
        checks={}
        for view in chart.VIEWS:
            grid,extent=chart.projected_grid(chart.VIEWS[view],500)
            coords=ee.Image.pixelCoordinates(layers.projection(grid))
            for direction in (1,-1):
                pressure=coords.select('x').multiply(.004).add(coords.select('y').multiply(.002*direction)).add(1024)
                labels=layers.pressure_labels(pressure,grid,extent,ee.List([1025]),view)
                # 2*column +/- row = constant has map tangent +/- atan(2).
                expected=direction*math.atan(2)
                errors=labels.map(lambda f:f.set('error',ee.Number(f.get('angle')).subtract(expected).abs()))
                key=f'{view}_{direction}'
                checks[key+'_count']=labels.size().gte(1)
                checks[key+'_tangent']=ee.Number(errors.aggregate_max('error')).lt(.001)
        self.assertTrue(all(ee.Dictionary(checks).getInfo().values()))

    def test_complete_frame_preserves_map_legend_and_text(self):
        import ee
        from PIL import Image
        from scripts import wn3_earth_engine_chart as chart
        from scripts import wn3_earth_engine_layers as layers
        _,extent=chart.projected_grid(chart.VIEWS['northeast'],800)
        layout=layers.chart_layout(extent,800)
        weather=ee.Image.constant([30,120,200]).byte().rename(['vis-red','vis-green','vis-blue'])
        raster=layers.chart_frame(weather,layout,extent,800,chart.PALETTE,
            datetime(2026,9,20,12,tzinfo=timezone.utc),datetime(2026,9,24,15,tzinfo=timezone.utc),99)
        png,_=chart.render_png(raster,layout['grid'])
        image=Image.open(BytesIO(png));width,height=image.size
        for x,y in [(.2,.2),(.8,.2),(.2,.8),(.8,.8)]:
            self.assertEqual(image.getpixel((round(x*width),round(y*height))),(30,120,200))
        self.assertEqual(image.getpixel((10,10)),(255,255,255))
        red,green,blue=image.getpixel((round(.9*width),round(.888*height)))
        self.assertGreater(red,150);self.assertLess(blue,120)
        for bounds in [(0,0,width,int(.1*height)),(0,int(.94*height),width,height)]:
            self.assertLess(image.crop(bounds).convert('L').getextrema()[0],150)


if __name__=='__main__':
    unittest.main()
