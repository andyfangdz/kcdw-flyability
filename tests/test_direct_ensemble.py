import unittest
from datetime import datetime, timezone
from kcdw.direct_ensemble import interpolate, rh_from_q, rh_from_dewpoint, validate_metadata
from kcdw.direct_ensemble_worker import url_for, indexed_ranges

class DirectEnsembleTests(unittest.TestCase):
    def test_interpolation_has_no_extrapolation_or_gap_bridging(self):
        self.assertEqual(interpolate({0:10.,6:22.,18:50.},[0,3,6,9,18,19]),[10.,16.,22.,None,50.,None])
    def test_derived_supersaturation_is_not_capped(self):
        from kcdw.direct_deterministic import derived_rh
        self.assertGreater(rh_from_q(.02,285,850),100)
        self.assertGreater(rh_from_dewpoint(290,291),100)
        self.assertGreater(derived_rh({'q850':{'value':.02},'t850':{'value':285}},850)['value'],100)

    def test_physics(self):
        self.assertAlmostEqual(rh_from_dewpoint(290,290),100)
        self.assertTrue(0 < rh_from_q(.005,285,850) < 100)
        with self.assertRaises(ValueError):rh_from_q(float('nan'),285,850)
    def test_native_catalog_names(self):
        t=datetime(2026,9,17,12,tzinfo=timezone.utc)
        self.assertIn('enfo-ef.grib2',url_for('ecmwf_ens',t,240,'ef'))
        self.assertIn('enfo-cf.grib2',url_for('aifs_ens',t,240,'cf'))
        self.assertIn('gep30.t12z.pgrb2a.0p50.f240',url_for('gefs',t,240,'30'))
    def test_index_identity_rejected(self):
        t=datetime(2026,9,17,12,tzinfo=timezone.utc)
        with self.assertRaises(ValueError):indexed_ranges('aifs_ens','{}\n{}',t,6,'cf')
    def test_gefs_final_index_field_has_a_bounded_range(self):
        from kcdw.direct_ensemble_worker import indexed_ranges
        t=datetime(2026,9,17,12,tzinfo=timezone.utc)
        text='1:0:d=2026091712:TMP:2 m above ground:168 hour fcst:ENS=low-res ctl\n2:200:d=2026091712:PRMSL:mean sea level:168 hour fcst:ENS=low-res ctl'
        result=indexed_ranges('gefs',text,t,168,'00',object_size=350)
        self.assertEqual(result[('00','msl')],(200,349))
        self.assertEqual(result[('00','2t')],(0,199))
    def test_gefs_final_range_rejects_unbounded_size(self):
        from kcdw.direct_ensemble_worker import indexed_ranges, MAX_FIELD
        t=datetime(2026,9,17,12,tzinfo=timezone.utc)
        text='1:0:d=2026091712:TMP:2 m above ground:168 hour fcst:ENS=low-res ctl\n2:200:d=2026091712:PRMSL:mean sea level:168 hour fcst:ENS=low-res ctl'
        for size in (200,201+MAX_FIELD):
            with self.assertRaises(ValueError):indexed_ranges('gefs',text,t,168,'00',object_size=size)
    def test_metadata_cannot_claim_direct_without_binding(self):
        with self.assertRaises(ValueError):validate_metadata({'direct_native':True},'gefs',datetime.now(timezone.utc))
