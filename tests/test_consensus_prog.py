import importlib.util
import unittest

HAS_ANALYSIS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "scipy", "contourpy"))
if HAS_ANALYSIS:
    import numpy as np
    from scripts import consensus_prog as prog


@unittest.skipUnless(HAS_ANALYSIS, "Install requirements-charts.txt to test the consensus prog")
class ConsensusProgTests(unittest.TestCase):
    def setUp(self):
        self.lat, self.lon = prog.grid((-100, -70, 30, 50))
        self.yy, self.xx = np.meshgrid(self.lat, self.lon, indexing="ij")

    def zone(self, wind_ms):
        """Warm air east of a north-south zone at 85W; uniform wind blowing east (+) or west (-)."""
        theta = 310 + 6 * np.tanh((self.xx + 85) / 1.5)
        u = np.full_like(theta, wind_ms)
        return theta, u, np.zeros_like(theta)

    def test_regrid_wraps_longitude_and_flips_latitude(self):
        lat = np.arange(60, 9.5, -.5)
        lon = np.arange(0, 360, .5)
        values = np.add.outer(lat, (lon + 180) % 360 - 180)  # lat + signed lon
        out = prog.regrid(values, lat, lon, self.lat, self.lon)
        np.testing.assert_allclose(out, self.yy + self.xx, atol=1e-9)
        with self.assertRaises(ValueError):
            prog.regrid(values[:20], lat[:20], lon, self.lat, self.lon)

    def test_theta_e_matches_bolton_reference(self):
        # 850 hPa, 15 C, saturated: about 339.5 K (Bolton eq. 43); dry air stays close to potential temperature.
        self.assertAlmostEqual(float(prog.theta_e(288.15, 850.0, rh_pct=100)), 339.5, delta=0.5)
        self.assertAlmostEqual(float(prog.theta_e(288.15, 850.0, rh_pct=1)), 288.15 * (1000 / 850) ** .2854, delta=0.6)  # 1% RH still adds ~0.5 K
        q = 0.622 * 17.04 / (850 - 0.378 * 17.04)  # saturation specific humidity at 15 C, 850 hPa
        self.assertAlmostEqual(float(prog.theta_e(288.15, 850.0, q=q)), float(prog.theta_e(288.15, 850.0, rh_pct=100)), delta=0.2)

    def test_centers_find_isolated_low_and_ignore_flat_field(self):
        distance = np.hypot(self.yy - 41, (self.xx + 80) * np.cos(np.radians(41)))
        pressure = 1015 - 14 * np.exp(-(distance / 3) ** 2)
        lows = prog.centers(pressure, self.lat, self.lon, "L")
        self.assertEqual(len(lows), 1)
        self.assertEqual((lows[0]["lat"], lows[0]["lon"]), (41.0, -80.0))
        self.assertAlmostEqual(lows[0]["hPa"], 1001, delta=.1)
        self.assertEqual(prog.centers(np.full_like(pressure, 1013), self.lat, self.lon, "L"), [])

    def test_front_sits_on_warm_edge_and_is_typed_by_cross_front_wind(self):
        for wind, kind in ((8, "cold"), (-8, "warm"), (0, "stationary")):
            with self.subTest(kind=kind):
                theta, u, v = self.zone(wind)
                fronts = prog.fronts(theta, u, v, self.lat, self.lon, temperature=theta - 30)
                self.assertTrue(fronts)
                self.assertEqual({f["type"] for f in fronts}, {kind})
                points = np.array([p for f in fronts for p in f["points"]])
                # Warm edge of the zone: east of its 85W center, well within the zone.
                self.assertTrue(np.all((points[:, 1] > -85) & (points[:, 1] < -82)))
                self.assertGreater(prog._path_length(points), 1000)
                warm = np.array([w for f in fronts for w in f["warm_side"]])
                self.assertTrue(np.all(warm[:, 1] > .9))  # unit vectors point east, toward warm air

    def test_moisture_only_boundary_is_not_a_front(self):
        theta, u, v = self.zone(8)
        self.assertEqual(prog.fronts(theta, u, v, self.lat, self.lon, temperature=np.full_like(theta, 285)), [])

    def test_high_terrain_masks_fronts(self):
        theta, u, v = self.zone(8)
        self.assertEqual(prog.fronts(theta, u, v, self.lat, self.lon, terrain_hpa=np.full_like(theta, 800.)), [])

    def test_precipitation_needs_model_agreement(self):
        shape = self.yy.shape
        wet, dry = np.full(shape, 5.), np.zeros(shape)
        cold, warm = np.full(shape, 271.), np.full(shape, 290.)  # cold: -2.15 C
        unstable = np.full(shape, 2000.)
        one = prog.precipitation([wet, dry, dry, dry], [], warm, warm)
        self.assertFalse(one["likely"].any())
        two = prog.precipitation([wet, wet, dry, dry], [unstable, unstable], warm, warm)
        self.assertTrue(two["likely"].all())
        self.assertFalse(two["moderate"].any() or two["thunder"].any())
        three = prog.precipitation([wet, wet, wet, dry], [unstable, unstable, dry], warm, warm)
        self.assertTrue(three["moderate"].all() and three["thunder"].all())
        snow = prog.precipitation([wet, wet, wet, wet], [unstable], cold, cold)
        self.assertTrue(snow["snow"].all())
        self.assertFalse(snow["thunder"].any() or snow["mixed"].any())
        # Missing members do not count toward agreement.
        missing = prog.precipitation([wet, np.full(shape, np.nan)], [], warm, warm)
        self.assertFalse(missing["likely"].any())


if __name__ == "__main__":
    unittest.main()
