"""Offline, initialization-bound forward collection contracts."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kcdw.common import UTC, iso_z
from kcdw.ensemble_trends import _event
from kcdw.run_history import load_run_history
from kcdw.run_history_update import refresh_run_history, gfs_source_url
from test_run_history import EVENT, NOW, fixture
from test_weathernext3 import fixture as weather_fixture

CHECK = NOW + timedelta(days=3)


def gfs(run):
    return {'retrieved_at': iso_z(CHECK), 'source_url': gfs_source_url(run), 'raw': {
        'latitude': 40.8752, 'longitude': -74.2814, 'timezone': 'GMT', 'utc_offset_seconds': 0,
        'hourly_units': {'time': 'iso8601', 'pressure_msl': 'hPa', 'wind_speed_10m': 'kn', 'precipitation': 'mm'},
        'hourly': {'time': [(run+timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(384)],
                   'pressure_msl': [1010]*384, 'wind_speed_10m': [10]*384, 'precipitation': [1]*384}}}


def wn3(run):
    from kcdw.weathernext3 import FIELD_SPECS, SOURCE
    f = weather_fixture()['forecast']['fields']
    _,sample,rain_times=_event(EVENT)
    fields={}
    for name in ('sea_level_pressure','wind_speed_10m'):
        spec=FIELD_SPECS[name]
        fields[name]=dict(unit=spec.unit,source_array=spec.array,
                          **{stat:f[name][stat][0] for stat in ('mean','p10','p90')})
    spec=FIELD_SPECS['precipitation_1h']
    fields['precipitation_1h']=dict(unit=spec.unit,source_array=spec.array,
                                    mean=[f['precipitation_1h']['mean'][0]]*len(rain_times))
    return dict(source=SOURCE,run_time=iso_z(run),sample_time=iso_z(sample),
                rain_times=[iso_z(value) for value in rain_times],
                grid_point=dict(latitude=40.9,longitude=-74.3),fields=fields,
                query=dict(weather_fixture()['forecast']['query'], retrieved_at=iso_z(CHECK)),
                retrieved_at=iso_z(CHECK))


def batch(runs):
    return {'records': [wn3(r) for r in runs], 'errors': []}


class RunHistoryUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'backfill.json'
        self.seed = fixture()
        self.path.write_text(json.dumps(self.seed))

    def refresh(self, **kwargs):
        return refresh_run_history(self.root, EVENT, CHECK, wn3_fetcher=kwargs.get('wn3_fetcher',batch),
                                   gfs_fetcher=kwargs.get('gfs_fetcher',gfs))

    def test_appends_identical_neighbor_values_preserving_seed_and_sources(self):
        result = self.refresh()
        self.assertGreater(len(result['points']),16)
        saved = json.loads(self.path.read_text())
        for point in self.seed['points']:
            self.assertIn(point, saved['points'])
        for model in ('wn3','gfs'):
            latest = max(p['run_time'] for p in result['points'] if p['model_key']==model)
            self.assertEqual(latest,'2026-09-16T12:00:00Z')  # 18Z is not available at 21Z.
        sources = list((self.root/'backfill-sources').rglob('*.json'))
        self.assertEqual(len(sources),16)
        before = {p:p.read_bytes() for p in sources}
        self.refresh()
        for p, data in before.items():
            self.assertEqual(p.read_bytes(),data)
        self.assertEqual(load_run_history(self.path,EVENT,CHECK)['points'],json.loads(self.path.read_text())['points'])

    def test_new_cycle_after_five_hour_lag(self):
        checked = []
        later = NOW+timedelta(hours=2)
        def fetch(run):
            checked.append(run)
            d=gfs(run); d['retrieved_at']=iso_z(later); return d
        refresh_run_history(self.root,EVENT,later,wn3_fetcher=batch,gfs_fetcher=fetch)
        self.assertEqual(checked[0],datetime(2026,9,13,18,tzinfo=UTC))
        self.assertLessEqual(len(checked),8)

    def test_persistent_failures_do_not_starve_older_holes_and_retry(self):
        attempts = []
        def fail(run):
            attempts.append(iso_z(run))
            raise RuntimeError('SECRET bearer example URL https://bad.test/?token=SECRET')
        for _ in range(8):
            self.refresh(gfs_fetcher=fail,wn3_fetcher=lambda r: {'records':[],'errors':[]})
        self.assertGreater(len(set(attempts)),8)
        self.assertGreater(len(attempts),len(set(attempts)))
        self.assertEqual(json.loads(self.path.read_text()),self.seed)
        status = (self.root/'run-history-status.json').read_text()
        self.assertNotIn('SECRET',status)
        self.assertLess(len(status),50000)

    def test_malformed_run_and_model_failure_are_isolated(self):
        calls = []
        def fetch(run):
            calls.append(run)
            d = gfs(run)
            if len(calls)==1:
                d['raw']['hourly']['time'][0]='2000-01-01T00:00'
            return d
        result = self.refresh(gfs_fetcher=fetch,wn3_fetcher=lambda r: (_ for _ in ()).throw(RuntimeError('secret')))
        self.assertEqual(len([p for p in result['points'] if p['model_key']=='wn3']),8)
        self.assertEqual(len([p for p in result['points'] if p['model_key']=='gfs']),15)

    def test_rejects_bad_gfs_units_grid_axis_model_and_target(self):
        mutations = [lambda d:d['raw'].update(timezone='Europe/Berlin'),
                     lambda d:d['raw'].update(latitude=41.3),
                     lambda d:d['raw']['hourly_units'].update(wind_speed_10m='m/s'),
                     lambda d:d.update(source_url=d['source_url'].replace('gfs_global','gfs_seamless')),
                     lambda d:d['raw']['hourly']['time'].__setitem__(10,'2000-01-01T00:00'),
                     lambda d:d['raw']['hourly']['pressure_msl'].__setitem__(10,True),
                     lambda d:d['raw']['hourly']['precipitation'].__setitem__(10,float('nan')),
                     lambda d:d['raw']['hourly'].update(pressure_msl=[None]*384)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                def fetch(run):
                    d=gfs(run);mutate(d);return d
                result=self.refresh(gfs_fetcher=fetch,wn3_fetcher=lambda r:{'records':[]})
                self.assertEqual(len(result['points']),16)

    def test_wn3_wrong_response_run_or_fixed_target_is_rejected(self):
        for field,value in [('run_time','2000-01-01T00:00:00Z'),('grid_point',{'latitude':0,'longitude':0}),
                            ('source','https://wrong.example/'),('rain_times',[])]:
            def bad(runs):
                records=[wn3(r) for r in runs]
                for r in records:r[field]=value
                return {'records':records}
            result=self.refresh(wn3_fetcher=bad,gfs_fetcher=lambda r:None)
            self.assertEqual(len(result['points']),16)

    def test_malformed_existing_seed_is_not_overwritten(self):
        self.path.write_text('{broken')
        result=self.refresh()
        self.assertIsNone(result)
        self.assertEqual(self.path.read_text(),'{broken')

    def test_atomic_history_write_failure_keeps_original(self):
        from kcdw import run_history_update as update
        original=update.atomic_write
        before=self.path.read_bytes()
        def fail(path, data):
            if Path(path)==self.path:raise OSError('disk full')
            return original(path,data)
        with patch.object(update,'atomic_write',side_effect=fail):
            result=self.refresh()
        self.assertEqual(result['points'],load_run_history(self.path,EVENT,CHECK)['points'])
        self.assertEqual(self.path.read_bytes(),before)
        # Saved originals allow recovery without re-fetching or retiming.
        def no_fetch(run):raise RuntimeError('must reuse source')
        result=self.refresh(gfs_fetcher=no_fetch,wn3_fetcher=lambda r:{'records':[]})
        self.assertGreater(len(result['points']),16)

    def test_seed_lower_bound_and_empty_seed_recent_bootstrap(self):
        seen=[]
        def fail(run):seen.append(run);return
        self.refresh(gfs_fetcher=fail,wn3_fetcher=lambda r:{'records':[]})
        earliest=min(datetime.fromisoformat(p['run_time'].replace('Z','+00:00')) for p in self.seed['points'])
        self.assertTrue(all(r>=earliest for r in seen))
        self.path.unlink();seen.clear()
        self.refresh(gfs_fetcher=fail,wn3_fetcher=lambda r:{'records':[]})
        self.assertTrue(all(CHECK-timedelta(hours=48)<=r<=CHECK-timedelta(hours=5) for r in seen))
        self.assertLessEqual(len(seen),8)

    def test_gfs_grid_change_cannot_discard_acquired_seed(self):
        def shifted(run):
            d=gfs(run);d['raw']['latitude']=41.0;return d
        result=self.refresh(gfs_fetcher=shifted,wn3_fetcher=lambda r:{'records':[]})
        self.assertEqual(result['points'],load_run_history(self.path,EVENT,CHECK)['points'])
        self.assertEqual(json.loads(self.path.read_text()),self.seed)

    def test_malformed_status_is_ignored_and_weather_records_are_allowlisted(self):
        (self.root/'run-history-status.json').write_text(json.dumps({'event':EVENT,'models':[]}))
        def fetch(run):
            d=gfs(run);d['headers']={'Authorization':'SECRET'};return d
        self.refresh(gfs_fetcher=fetch)
        self.assertNotIn('SECRET',''.join(p.read_text() for p in (self.root/'backfill-sources').rglob('*.json')))

    def test_single_runs_url_requests_full_horizon(self):
        q=parse_qs(urlparse(gfs_source_url(NOW.replace(hour=12))).query)
        self.assertEqual(q['forecast_days'],['16'])
        self.assertEqual(q['models'],['gfs_global'])
        self.assertEqual(q['run'],['2026-09-13T12:00'])
        self.assertNotIn('start_date',q)


if __name__=='__main__':unittest.main()
