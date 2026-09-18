"""Offline GEPS contracts; optional actual saved GRIB integration fixtures."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from kcdw import direct_geps_worker as w

INIT = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
NOW = INIT + timedelta(hours=11)


def point(field='r850', member='00', lead=210):
    return dict(model='geps', init=w.stamp(INIT), lead=lead, member=member,
                field=field, value=105.5 if field.startswith('r') else 28.2,
                latitude=41., longitude=-74.5, identity=w.expected_identity(INIT, lead, member, field),
                units=w.model_specs()[field][1], sha256='a'*64, range=[int(member)*100, int(member)*100+99],
                url=w.url_for(INIT, lead, field), collected_at=w.stamp(NOW), fetched_at=w.stamp(NOW))


def packet(fields=('r850',)) -> dict[str, Any]:
    return dict(model='geps', init=w.stamp(INIT), offered_members=w.expected_members(),
                requested_leads=[210], points=[point(f, m) for f in fields for m in w.expected_members()])


class GepsContracts(unittest.TestCase):
    def test_fields_members_and_url(self):
        self.assertEqual(len(w.model_specs()), 10)
        self.assertEqual(w.expected_members(), [f'{i:02}' for i in range(21)])
        self.assertIn('/today/ensemble/geps/grib2/raw/12/210/CMC_geps-raw_RH_TGL_2m_latlon0p5x0p5_2026091712_P210_allmbrs.grib2', w.url_for(INIT,210,'r2'))
        self.assertNotIn('gust', w.model_specs())

    def test_offline_identity_and_supersaturation(self):
        p=packet(); self.assertIs(w.validate_packet(p,NOW),p)
        for key,val in [('centre','kwbc'),('jScansPositively',0),('dataDate',20260916),('parameterNumber',8),('units','unknown'),('number',1)]:
            q=copy.deepcopy(p);q['points'][0]['identity'][key]=val
            with self.subTest(key=key),self.assertRaises(ValueError):w.validate_packet(q,NOW)
        for key,val in [('model','gefs'),('init','2026-09-17T00:00:00Z'),('url',w.url_for(INIT-timedelta(hours=12),210,'r850')),('range',[-1,99]),('sha256','z'*64),('value',float('nan'))]:
            q=copy.deepcopy(p);q['points'][0][key]=val
            with self.subTest(key=key),self.assertRaises(ValueError):w.validate_packet(q,NOW)

    def test_cumulative_unknown_parameter_units_and_clocks(self):
        p=packet(('tp',));self.assertEqual(p['points'][0]['identity']['paramId'],0)
        self.assertEqual(p['points'][0]['identity']['units'],'unknown')
        self.assertEqual(p['points'][0]['units'],'kg m**-2')
        w.validate_packet(p,NOW)
        q=copy.deepcopy(p);q['points'][0]['identity']['startStep']=204
        with self.assertRaises(ValueError):w.validate_packet(q,NOW)
        with self.assertRaises(ValueError):w.validate_packet(p,NOW+timedelta(hours=13))
        q=copy.deepcopy(p);q['points'].append(q['points'][0])
        with self.assertRaises(ValueError):w.validate_packet(q,NOW)
        q=copy.deepcopy(p);q['points'].pop()
        with self.assertRaises(ValueError):w.validate_packet(q,NOW)

    def test_grouped_message_boundaries(self):
        raw=b'GRIB'+b'\0\0\0\2'+(20).to_bytes(8,'big')+b'7777'
        self.assertEqual(list(w.messages(raw*2)),[(0,19,raw),(20,39,raw)])
        for bad in (raw[:-1],raw+b'x',b'x'+raw):
            with self.assertRaises(ValueError):list(w.messages(bad))

    def test_cache_retains_clocks_and_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as d,patch.object(w,'CACHE',Path(d)),patch.object(w,'fetch',return_value=b'raw') as fetch,patch.object(w,'decode_group',return_value=packet()['points']):
            a=w.fetch_frame(INIT,210,w.stamp(NOW),fields=['r850'])
            b=w.fetch_frame(INIT,210,w.stamp(NOW+timedelta(minutes=1)),fields=['r850'])
            self.assertEqual(a,b);self.assertEqual(fetch.call_count,1)
            path=w.path_for(INIT,210,'r850');path.write_text(path.read_text().replace('"cwao"','"kwbc"'))
            w.fetch_frame(INIT,210,w.stamp(NOW+timedelta(minutes=2)),fields=['r850'])
            self.assertEqual(fetch.call_count,2)

    def test_default_complete_frame_and_worker_bounds(self):
        def field(init,lead,name,collected):
            pts=[point(name,m,lead) for m in w.expected_members()]
            for p in pts:
                lo,hi=w.model_specs()[name][-2:]
                p['value']=max(lo,min(hi,100))
            return pts
        with patch.object(w,'_field',side_effect=field):
            pts=w.fetch_frame(INIT,210,NOW)
            self.assertEqual(len(pts),210)
            p=packet();p['points']=pts;w.validate_packet(p,NOW)
            self.assertEqual({x['field'] for x in pts},set(w.model_specs()))
        for workers in (0,7,True):
            with self.assertRaises(ValueError):w.fetch_frame(INIT,210,NOW,workers=workers)

    def test_stream_limit_header_and_chunked_body(self):
        class Response:
            status_code=200
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,size):return iter([b'a'*20,b'b'*21])
        with patch.object(w,'session') as session:
            r=Response();session.return_value.get.return_value=r
            with self.assertRaises(ValueError):w.fetch('https://example.invalid/',40)
            r.headers={'Content-Length':'41'}
            with self.assertRaises(ValueError):w.fetch('https://example.invalid/',40)
            r.headers={'Content-Length':'42'}
            with self.assertRaises(ValueError):w.fetch('https://example.invalid/',50)
            r.headers={'Content-Length':'41'}
            self.assertEqual(len(w.fetch('https://example.invalid/',50)),41)
            self.assertFalse(session.return_value.get.call_args.kwargs['allow_redirects'])

    def test_probe_exact_cycle_all_fields(self):
        catalog=''.join('<a href="'+w.url_for(INIT,210,f).rsplit('/',1)[1]+'">x</a>' for f in w.model_specs()).encode()
        with patch.object(w,'fetch',return_value=catalog):self.assertTrue(w.probe(INIT,210))
        with patch.object(w,'fetch',return_value=catalog.replace(b'2026091712',b'2026091700')):self.assertFalse(w.probe(INIT,210))

    @unittest.skipUnless(importlib.util.find_spec('eccodes') and Path('/tmp/noaa-fullrange-research/geps-measurements.json').exists(),'native saved fixtures unavailable')
    def test_saved_actual_grouped_gribs(self):
        for field,stem in [('r850','RH_ISBL_0850'),('tp','APCP_SFC_0')]:
            raw=Path('/tmp/noaa-fullrange-research',f'CMC_geps-raw_{stem}_latlon0p5x0p5_2026091712_P210_allmbrs.grib2').read_bytes()
            pts=w.decode_group(raw,INIT,210,field,w.stamp(NOW),w.stamp(NOW))
            self.assertEqual(len(pts),21)
            p=packet();p['points']=pts;w.validate_packet(p,NOW)
            self.assertEqual(pts[-1]['range'][1],len(raw)-1)
            self.assertEqual(pts[0]['identity']['jScansPositively'],1)
            if field=='tp':self.assertAlmostEqual(pts[0]['value'],28.2)

if __name__=='__main__':unittest.main()
