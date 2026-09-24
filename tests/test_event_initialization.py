"""Synthetic provenance fixtures; never live weather evidence."""
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.test_event_moisture import FakeClient, NOW, START, END
from kcdw.event_moisture import collect_moisture
from kcdw.event_initialization import initialization_evidence, render_initializations


def snapshot():
    return {'event': {'date': '2026-09-23', 'window': '08-17'},
            'event_moisture': collect_moisture(FakeClient(), START, END, NOW)}


def row(s, key, now=NOW):
    return next(r for r in initialization_evidence(s, now) if r['model'] == key)


class InitializationsTests(unittest.TestCase):
    def test_direct_native_wind_rows_are_separate_from_rolling_metadata(self):
        s=snapshot();s['native_wind']={'version':2}
        native={'models':{'gfs':{'init':'2026-09-14T12:00:00Z'},'ifs':{'init':'2026-09-14T12:00:00Z'},'aifs_single':None}}
        with patch('kcdw.event_initialization.native_wind_evidence',return_value=native):
            rows=initialization_evidence(s,NOW)
            html=render_initializations(s,NOW)
        direct={r['model']:r for r in rows if r['model'].endswith('_native_wind')}
        self.assertEqual(len(direct),3)
        self.assertEqual(direct['ifs_native_wind']['binding'],'response-bound')
        self.assertEqual(direct['aifs_single_native_wind']['binding'],'unavailable')
        self.assertEqual(next(r for r in rows if r['model']=='ifs')['binding'],'latest-advertised')
        self.assertNotIn('available_at',direct['ifs_native_wind'])
        self.assertIn('ECMWF direct',html)
        self.assertIn('NOAA direct',html)
        self.assertIn('native wind samples only',html)
        s['native_wind']['version']=1
        with patch('kcdw.event_initialization.native_wind_evidence') as validate:
            self.assertEqual(len(initialization_evidence(s,NOW)),6)
            validate.assert_not_called()

    def test_short_cycle_is_inferred_not_bound(self):
        s = snapshot()
        r = row(s, 'ifs')
        self.assertEqual(r['binding'], 'latest-advertised')
        self.assertEqual(r['init'], '2026-09-14T18:00:00Z')
        self.assertEqual(r['likely_init'], '2026-09-14T12:00:00Z')
        self.assertEqual(r['data_end'], '2026-09-20T18:00:00Z')
        self.assertIn('unverified', r['note'])
        self.assertNotIn('likely_init', row(s, 'aifs_single'))

    def test_inference_requires_beyond_boundary_and_conservative_long_horizon(self):
        for date in ('2026-09-19', '2026-10-01'):
            s = snapshot(); s['event']['date'] = date
            self.assertNotIn('likely_init', row(s, 'ifs'))
        s = snapshot(); del s['event']
        self.assertNotIn('likely_init', row(s, 'ifs'))

    def test_stale_and_invalid_are_independent_and_do_not_leak(self):
        s = snapshot(); s['event_moisture']['models']['ifs']['data']['metadata']['datasets']['ecmwf_ifs025']['endpoint'] = '<script>secret</script>'
        self.assertEqual(row(s, 'ifs')['binding'], 'unavailable')
        self.assertEqual(row(s, 'aifs_single')['binding'], 'latest-advertised')
        self.assertTrue(all(r['binding'] == 'unavailable' for r in initialization_evidence(s, NOW+timedelta(days=2))))
        self.assertNotIn('secret', render_initializations(s, NOW))

    def test_bounded_mobile_section_and_no_mutation(self):
        s = snapshot(); before = copy.deepcopy(s)
        evidence = initialization_evidence(s, NOW)
        self.assertEqual(len(evidence), 6)
        self.assertLess(len(json.dumps(evidence).encode()), 2500)
        html = render_initializations(s, NOW)
        self.assertLess(len(html.encode()), 4000)
        self.assertIn('id="model-initializations"', html)
        self.assertIn('2026-09-14 18Z', html)
        self.assertIn('EDT', html)
        self.assertIn('unverified', html)
        self.assertIn('not response-bound', html)
        self.assertEqual(s, before)

    def test_table_has_explicit_column_and_row_headers(self):
        import xml.etree.ElementTree as ET
        root=ET.fromstring(render_initializations(snapshot(),NOW))
        table=root.find('.//table')
        self.assertIsNotNone(table)
        headers=table.findall('thead/tr/th')
        self.assertEqual(len(headers),5)
        self.assertTrue(all(h.get('scope')=='col' for h in headers))
        rows=table.findall('tbody/tr')
        self.assertEqual(len(rows),len(initialization_evidence(snapshot(),NOW)))
        self.assertTrue(all(r.find('th').get('scope')=='row' for r in rows))
        ifs=next(r for r in rows if r.get('data-init-model')=='ifs')
        self.assertIn('Inferred/unverified',''.join(ifs.itertext()))
        self.assertIn('2026-09-14 12Z',''.join(ifs.itertext()))
        self.assertIn('2026-09-14 18Z',''.join(ifs.itertext()))

    def test_bound_wn3_uses_response_not_requested(self):
        from tests.test_weathernext3 import fixture, NOW as wn_now
        s = {'weathernext3': {'ok': True, 'data': fixture(fallback=True)}}
        r = row(s, 'wn3', wn_now+timedelta(hours=5))
        self.assertEqual(r['binding'], 'response-bound')
        self.assertEqual(r['init'], s['weathernext3']['data']['forecast']['response_init_utc'].replace('.000Z', 'Z'))
        self.assertNotIn('available_at', r)

    def test_empty_and_malformed_sources_still_return_six_rows(self):
        for s in ({}, None, {'models': None, 'event_moisture': []}):
            self.assertEqual(len(initialization_evidence(s, NOW)), 6)

    def test_primary_ensemble_validation_and_rh_packet_separation(self):
        from tests.test_events import FakeClient as EnsembleClient, EVENT, NOW as ens_now
        from kcdw.event_ensemble import MODELS, collect_model, event_range
        from kcdw.common import iso_z
        from tests.test_event_moisture_ensemble import Client as RHClient, NOW as rh_now, START as rh_start, END as rh_end
        from kcdw.event_moisture_ensemble import collect_moisture_ensemble
        start, end = event_range(EVENT, ens_now)
        s: dict = dict(event=EVENT.as_dict(), collected_at=iso_z(ens_now), range=dict(start=iso_z(start), end=iso_z(end)), models={})
        for spec in MODELS:
            s['models'][spec.key] = dict(ok=True, data=collect_model(EnsembleClient(), spec, EVENT, ens_now, display_range=(start, end)))
        self.assertEqual(row(s, 'ecmwf_ens', ens_now)['binding'], 'latest-advertised')
        s['models']['ecmwf_ens']['data']['hourly']['pressure_msl']['p50'][0] = -100
        self.assertEqual(row(s, 'ecmwf_ens', ens_now)['binding'], 'unavailable')
        self.assertEqual(row(s, 'aifs_ens', ens_now)['binding'], 'latest-advertised')
        # Real validator for RH, independently successful despite primary failure.
        s['event_moisture_ensemble'] = collect_moisture_ensemble(RHClient(), rh_start, rh_end, rh_now)
        rows = initialization_evidence(s, rh_now)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r['binding'] == 'latest-advertised' for r in rows[6:]))
        self.assertTrue(all(r['scope'] == 'separate RH packet only' for r in rows[6:]))
        self.assertLess(len(json.dumps(rows).encode()), 2500)

    def test_native_gfs_not_rolling_gfs(self):
        from tests.test_cloud_ceiling import snapshot as native_snapshot, worker, NOW as native_now
        from kcdw.cloud_ceiling import _context, NOTES
        from kcdw.common import iso_z
        s = native_snapshot()
        init, event, leads = _context(s, native_now)
        s['cloud_ceiling'] = dict(version=1, model='gfs_native_025', model_init=iso_z(init), fetched_at=iso_z(native_now), snapshot_collected_at=s['collected_at'], event=event, notes=NOTES, samples=worker({'leads': leads}))
        r = row(s, 'gfs_native', native_now)
        self.assertEqual(r['binding'], 'response-bound')
        self.assertIn('ceiling', r['scope'])
        del s['cloud_ceiling']
        self.assertEqual(row(s, 'gfs_native', native_now)['binding'], 'unavailable')
