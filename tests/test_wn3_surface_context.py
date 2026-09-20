import copy
import unittest
from kcdw.wn3_surface_context import surface_rh, collect_wind, validate_wind, surface_evidence, render_surface
from kcdw.wn3_cloud_analysis import cloud_evidence
import test_wn3_cloud_analysis as cloud_tests
from test_weathernext3 import NOW


class SurfaceContextTests(unittest.TestCase):
    def snapshot(self):
        return cloud_tests.CloudAnalysisTests().snapshot()

    def packet(self,s):
        clouds=cloud_evidence(s,NOW)
        class Store:
            def fetch(self,run,arrays):
                return dict(rows=[dict(init_time=run,valid_time=r['at'],latitude=40.9,longitude=-74.3,
                                  wind_speed_100m_mean=8.,wind_speed_100m_p10=4.,wind_speed_100m_p90=12.) for r in clouds['hours']],
                            provenance=s['weathernext3']['data']['forecast']['query'])
        return collect_wind(s,NOW,store=Store())

    def test_relative_humidity_formula_and_domain(self):
        self.assertEqual(surface_rh(20,10),52.5)
        self.assertEqual(surface_rh(15,15),100.)
        self.assertEqual(surface_rh(15,16),100.)
        for a,b in [(float('nan'),10),(-60,-70),(20,None),(True,10)]:
            self.assertIsNone(surface_rh(a,b))

    def test_wind_remains_native_statistic_and_surface_rh_has_no_band(self):
        s=self.snapshot();s['wn3_100m_wind']=self.packet(s)
        evidence=surface_evidence(s,NOW)
        self.assertEqual(evidence['rows'][0][1:],[52.5,9.7,15.6,7.8,23.3])
        self.assertNotIn('rh_p90',str(evidence['columns']))
        self.assertIn('not ensemble-mean RH',evidence['rh_method'])
        html=render_surface(evidence)
        self.assertIn('100 m p10–p90',html)
        self.assertIn('neither a predicted gust nor a gust upper bound',html)

    def test_invalid_elevated_wind_does_not_remove_surface_rh(self):
        s=self.snapshot();p=self.packet(s)
        for mutate in (lambda p:p.update(init_time='2026-09-12T06:00:00Z'),lambda p:p['hours'].pop(),
                       lambda p:p['hours'][0].update(p90=200),lambda p:p['hours'][0].update(p10=14),
                       lambda p:p['grid_point'].update(latitude=41.)):
            bad=copy.deepcopy(p);mutate(bad);s['wn3_100m_wind']=bad
            self.assertIsNone(validate_wind(bad,s,NOW))
            self.assertEqual(surface_evidence(s,NOW)['rows'][0][1],52.5)
            self.assertEqual(surface_evidence(s,NOW)['rows'][0][3:],[None,None,None])
