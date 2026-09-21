import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

HAS_CHARTS = all(importlib.util.find_spec(name) is not None for name in (
    'ee', 'numpy', 'matplotlib', 'cartopy', 'scipy', 'pyproj', 'shapely',
))
if HAS_CHARTS:
    import numpy as np
    from scripts import wn3_earth_engine_chart as chart


@unittest.skipUnless(HAS_CHARTS, 'Install requirements-earth-engine-charts.txt')
class EarthEngineWindBarbTests(unittest.TestCase):
    def test_standard_speed_symbols_and_filled_flags(self):
        # NWS symbols: a half feather is 5 kt, full feather 10, pennant 50.
        for speed, expected in [(0,(0,0,0)),(5,(0,0,1)),(10,(0,1,0)),
                                (15,(0,1,1)),(50,(1,0,0)),(65,(1,1,1)),(100,(2,0,0))]:
            with self.subTest(speed=speed):
                glyph=chart.barb_geometry(100,200,0,-speed,10)
                self.assertEqual((len(glyph['flags']),glyph['full_barbs'],glyph['half_barbs']),expected)
                self.assertEqual(glyph['rounded_speed_kt'],speed)
                if speed:
                    self.assertEqual(len(glyph['lines']),1+expected[1]+expected[2])
                for triangle in glyph['flags']:
                    self.assertEqual(triangle[0],triangle[-1])
                    self.assertEqual(len(triangle),4)
                    self.assertEqual(len(set(map(tuple,triangle))),3)

    def test_staff_points_to_wind_origin_and_feathers_face_clockwise(self):
        for u,v,tip in [(10,0,(-20,0)),(-10,0,(20,0)),(0,10,(0,-20)),(0,-10,(0,20))]:
            with self.subTest(u=u,v=v):
                glyph=chart.barb_geometry(0,0,u,v,20)
                np.testing.assert_allclose(glyph['lines'][0],[[0,0],tip],atol=1e-10)
        north=chart.barb_geometry(0,0,0,-10,20)
        self.assertGreater(north['lines'][1][1][0],0)
        diagonal=chart.barb_geometry(0,0,3,4,20)
        np.testing.assert_allclose(diagonal['lines'][0][-1],[-12,-16],atol=1e-10)

    def test_rounding_and_zero_speed_do_not_invent_direction(self):
        for speed in (0,2.49):
            glyph=chart.barb_geometry(100,200,speed,0,10)
            self.assertEqual(glyph['rounded_speed_kt'],0)
            ring=np.asarray(glyph['lines'][0])
            np.testing.assert_allclose(np.hypot(ring[:,0]-100,ring[:,1]-200),1.3)
            np.testing.assert_array_equal(ring[0],ring[-1])
        for speed,expected in [(2.5,5),(7.49,5),(7.5,10),(47.49,45),(47.5,50)]:
            self.assertEqual(chart.barb_geometry(0,0,speed,0,10)['rounded_speed_kt'],expected)

    def test_projected_barbs_use_vector_magnitude_not_scalar_mean(self):
        grid,_=chart.projected_grid(chart.VIEWS['northeast'],800)
        annotation,_=chart.projected_grid(chart.VIEWS['northeast'],200)
        shape=(annotation['dimensions']['height'],annotation['dimensions']['width'])
        fields={'u_kt':np.full(shape,3.),'v_kt':np.full(shape,4.),'wind_kt':np.full(shape,30.)}
        result=chart.barb_features(fields,annotation,grid,'northeast')
        self.assertGreater(len(result['symbols']),1)
        for symbol in result['symbols']:
            self.assertEqual(symbol['rounded_speed_kt'],5)
            self.assertAlmostEqual(math.hypot(symbol['u_map_kt'],symbol['v_map_kt']),5)
        for geometry,symbol in zip(result['lines'],result['symbols']):
            shaft=np.asarray(geometry['geometry']['coordinates'][0])
            direction=shaft[1]-shaft[0]
            self.assertLess(np.dot(direction,[symbol['u_map_kt'],symbol['v_map_kt']]),0)
            self.assertAlmostEqual(float(np.linalg.norm(direction)),result['length_projected_m'])

    def test_invalid_values_and_legacy_cache_fail_before_rendering(self):
        for u,v,length in [(float('nan'),0,10),(0,float('inf'),10),(201,0,10),(1,1,0)]:
            with self.assertRaises(ValueError):
                chart.barb_geometry(0,0,u,v,length)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'provenance.json').write_text(json.dumps({'source':{'id':'old'}}))
            with self.assertRaisesRegex(ValueError,'older renderer'):
                chart.render_server(None,{'id':'old'},'continental',root,root,2000)
            with self.assertRaisesRegex(ValueError,'older renderer'):
                chart.annotate(root,{})


if __name__=='__main__':
    unittest.main()
