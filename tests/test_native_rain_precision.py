import copy
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from kcdw import native_rain_precision as p
from kcdw import direct_ensemble_worker as w

class RainPrecisionTests(unittest.TestCase):
    def test_only_decreasing_pairs_get_proofs_without_changing_totals_or_clocks(self):
        points=[dict(member='00',field='tp',lead=h,value=v,collected_at='original',fetched_at='original') for h,v in [(6,.75390625),(12,.75),(18,.8)]]
        packet=dict(model='aifs_ens',points=points)
        before=copy.deepcopy(points)
        with patch.object(p,'_error',return_value=.00390625) as fetch:
            self.assertIs(p.enrich(packet),packet)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual([{k:v for k,v in point.items() if k!='packing_error'} for point in points],before)
        self.assertNotIn('packing_error',points[-1])

    def test_non_decreasing_cumulative_data_needs_no_network(self):
        packet=dict(model='geps',points=[dict(member='00',field='tp',lead=h,value=h) for h in (6,12)])
        with patch.object(p,'_error',side_effect=AssertionError('unnecessary network')):
            p.enrich(packet)
        self.assertEqual(packet['rain_precision_version'],1)

    def test_jpeg_lossless_quantization_and_hash_binding(self):
        raw=b'test GRIB message';digest=hashlib.sha256(raw).hexdigest()
        point=dict(sha256=digest,url='https://example.test/grib',range=[0,len(raw)-1])
        values=dict(packingType='grid_jpeg',typeOfCompressionUsed=0,referenceValue=0.,binaryScaleFactor=-1,decimalScaleFactor=2)
        def get(handle,key):
            if key=='packingError':raise KeyError(key)
            return values[key]
        ec=types.SimpleNamespace(codes_new_from_message=lambda _:1,codes_release=lambda _:None,codes_get=get,KeyValueNotFoundError=KeyError)
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'CACHE',Path(directory)),patch.dict(sys.modules,{'eccodes':ec}),patch.object(w,'fetch',return_value=raw) as fetch:
            self.assertEqual(p._error(point),.0025)
            self.assertEqual(p._error(point),.0025)
            self.assertEqual(fetch.call_count,1)
            bad=dict(point,sha256='a'*64)
            with self.assertRaisesRegex(ValueError,'source changed'):p._error(bad)

    def test_lossy_or_nonzero_reference_is_not_assumed_lossless(self):
        raw=b'other test GRIB';point=dict(sha256=hashlib.sha256(raw).hexdigest(),url='https://example.test/grib',range=[0,len(raw)-1])
        def get(handle,key):
            if key=='packingError':raise KeyError(key)
            return {'packingType':'grid_jpeg','typeOfCompressionUsed':1}[key]
        ec=types.SimpleNamespace(codes_new_from_message=lambda _:1,codes_release=lambda _:None,codes_get=get,KeyValueNotFoundError=KeyError)
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'CACHE',Path(directory)),patch.dict(sys.modules,{'eccodes':ec}),patch.object(w,'fetch',return_value=raw):
            with self.assertRaisesRegex(ValueError,'unsupported'):p._error(point)
