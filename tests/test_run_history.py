"""Recovered model cycles must never masquerade as old page fetches."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kcdw.common import UTC, iso_z
from kcdw.run_history import load_run_history, validate_run_history, render_run_history

NOW = datetime(2026, 9, 13, 21, tzinfo=UTC)
EVENT = {'slug': 'commercial-checkride', 'date': '2026-09-24', 'window': '08-17'}


def fixture() -> dict:
    points = []
    for model in ('wn3', 'gfs'):
        for i in range(8):
            run = NOW-timedelta(hours=51-i*6)
            points.append(dict(model_key=model, model_id=12 if model == 'wn3' else 'gfs_global',
                run_time=iso_z(run), retrieved_at=iso_z(NOW),
                source_url='https://deepmind.google.com/science/weatherlab/' if model == 'wn3' else 'https://single-runs-api.open-meteo.com/v1/forecast?models=gfs_global&latitude=40.8752&longitude=-74.2814&wind_speed_unit=kn&precipitation_unit=mm&timezone=UTC&run='+run.strftime('%Y-%m-%dT%H:%M'),
                grid_point={'latitude':40.8752,'longitude':-74.2814},
                sample_time='2026-09-24T16:00:00Z',
                rain_times=[f'2026-09-24T{h}:00:00Z' for h in range(13,22)],
                metrics={key:dict(center=value,low=value-2 if model=='wn3' and key!='rain' else None,high=value+2 if model=='wn3' and key!='rain' else None) for key,value in [('pressure',1025-i if model=='wn3' else 1015-i),('wind',6+i/10),('rain',1+i/10)]},
                run_binding='response-bound' if model=='wn3' else 'archive-request-bound'))
    return dict(version=1,event=EVENT,airport={'icao':'KCDW','latitude':40.8752,'longitude':-74.2814,'timezone':'America/New_York'},units={'pressure':'hPa','wind':'kn','rain':'mm'},points=points,notes=[])


class RunHistoryTests(unittest.TestCase):
    def test_recovered_cycles_preserve_real_retrieval_times(self):
        original=fixture(); clean=validate_run_history(original,EVENT,NOW)
        self.assertEqual(len(clean['points']),16)
        self.assertTrue(all(p['retrieved_at']==iso_z(NOW) for p in clean['points']))
        self.assertNotIn('collected_at',json.dumps(clean))
        self.assertEqual(original,fixture())
        text=render_run_history(clean,EVENT,NOW)
        self.assertEqual(text.count('<svg'),3)
        self.assertIn('initialization',text)
        self.assertIn('not retrieval',text)
        self.assertIn('8 runs',text)
        self.assertIn('mean',text)
        self.assertIn('deterministic',text)
        self.assertIn('Same initialization',text)
        self.assertLess(len(text),22000)

    def test_source_isolation_and_identity_units_coverage(self):
        for field,value in [('sample_time','2026-09-24T15:00:00Z'),('model_id','gfs_seamless'),('run_binding','rolling'),('rain_times',[]),('grid_point',{'latitude':0,'longitude':0})]:
            a=fixture();a['points'][-1][field]=value
            self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)
        for field,value in [('event',dict(EVENT,date='2026-09-25')),('units',{'pressure':'Pa','wind':'m/s','rain':'mm'})]:
            a=fixture();a[field]=value
            self.assertIsNone(validate_run_history(a,EVENT,NOW))

    def test_archive_request_must_match_model_location_units_and_utc_run(self):
        substitutions = [('models=gfs_global','models=ecmwf_ifs'),('latitude=40.8752','latitude=0'),
                         ('longitude=-74.2814','longitude=0'),('wind_speed_unit=kn','wind_speed_unit=ms'),
                         ('precipitation_unit=mm','precipitation_unit=inch'),('/v1/forecast?','/v1/ensemble?'),
                         ('timezone=UTC','timezone=Asia%2FTokyo'),('models=gfs_global','models=gfs_global&models=ecmwf_ifs')]
        for old,new in substitutions:
            a=fixture();a['points'][-1]['source_url']=a['points'][-1]['source_url'].replace(old,new)
            self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15,(old,new))
        a=fixture();a['points'][-1]['source_url']+='%2B09:00'
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)

    def test_bad_band_duplicate_cycle_and_missing_metrics(self):
        a=fixture();a['points'][0]['metrics']['rain']['low']=0;a['points'][0]['metrics']['rain']['high']=5
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)
        a=fixture();a['points'].append(copy.deepcopy(a['points'][0]))
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),16)
        a['points'][-1]['metrics']['pressure']['center']=990
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)
        a=fixture();a['points'][0]['metrics']['wind']=None
        self.assertIsNone(validate_run_history(a,EVENT,NOW)['points'][0]['metrics']['wind'])
        a=fixture();a['points'][0]['metrics']['pressure']['center']=float('nan')
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)

    def test_retains_full_event_horizon_over_sixteen_cycles(self):
        a=fixture(); a['points']=[]
        end=datetime(2026,9,24,6,tzinfo=UTC)
        for model in ('wn3','gfs'):
            template=next(p for p in fixture()['points'] if p['model_key']==model)
            for i in range(58):
                run=end-timedelta(hours=6*i)
                p=copy.deepcopy(template)
                p.update(run_time=iso_z(run),retrieved_at=iso_z(end))
                if model=='gfs':
                    p['source_url']=p['source_url'].split('&run=')[0]+'&run='+run.strftime('%Y-%m-%dT%H:%M')
                a['points'].append(p)
        clean=validate_run_history(a,EVENT,end)
        self.assertEqual(len(clean['points']),116)
        # Aggregate input accommodates all supported models at the 64-cycle cap.
        duplicated=dict(a,points=a['points']*2)
        self.assertEqual(len(validate_run_history(duplicated,EVENT,end)['points']),116)

    def test_old_runs_remain_historical_not_live(self):
        text=render_run_history(fixture(),EVENT,NOW+timedelta(days=3))
        self.assertIn('Archived guidance',text)
        self.assertIn('not current-source availability',text)
        self.assertNotIn('current GFS',text)
        a=fixture();a['points'][0]['retrieved_at']=iso_z(NOW+timedelta(hours=1))
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)

    def test_update_persists_backfill_and_page_keeps_fetch_history_separate(self):
        from unittest.mock import patch
        from kcdw import event_update, event_renderer
        from test_events import EVENT as configured
        import test_event_comparison as comparison
        snapshot=comparison.ComparisonTests().snapshot()
        data=fixture()
        from contextlib import ExitStack
        from test_event_update import SOURCES
        with tempfile.TemporaryDirectory() as tmp:
            with ExitStack() as stack:
                # Every external collector is stubbed via the shared list in test_event_update.
                for name in SOURCES:
                    if name != 'load_run_history':
                        stack.enter_context(patch.object(event_update, name, return_value=None))
                stack.enter_context(patch.object(event_update,'upcoming_events',return_value=[configured]))
                stack.enter_context(patch.object(event_update,'collect_event',return_value=snapshot))
                stack.enter_context(patch.object(event_update,'generate_event_narrative',return_value=None))
                stack.enter_context(patch('kcdw.event_personal.load_event_personal', return_value=None))
                load=stack.enter_context(patch.object(event_update,'load_run_history',return_value=data,create=True))
                stack.enter_context(patch.object(event_update,'render',return_value=('<html>backfill</html>',{})))
                self.assertEqual(event_update.update(Path(tmp),None,NOW),0)
            load.assert_called_once_with(Path(tmp)/'events'/configured.slug/'backfill.json',snapshot['event'],NOW)
            saved=json.loads((Path(tmp)/'events'/configured.slug/'current'/'snapshot.json').read_text())
            self.assertEqual(saved['ensemble_run_history'],data)
        snapshot['ensemble_run_history']=data
        with patch.object(event_renderer,'render_run_history',return_value='<section id="ensemble-run-history">cycles</section>',create=True):
            markup,_=event_renderer.render(snapshot,NOW)
        self.assertIn('id="ensemble-run-history"',markup)
        self.assertIn('Earlier page-fetch history',markup)
        self.assertEqual(markup.count('id="ensemble-trends"'),1)
        self.assertIn('id="ensemble-fetch-history"',markup)

    def test_loader_missing_symlink_oversized_and_grid_consistency(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'backfill.json'
            self.assertIsNone(load_run_history(p,EVENT,NOW))
            p.write_text(json.dumps(fixture()))
            self.assertEqual(len(load_run_history(p,EVENT,NOW)['points']),16)
            link=Path(tmp)/'link';link.symlink_to(p)
            self.assertIsNone(load_run_history(link,EVENT,NOW))
            p.write_text(' '*2_000_001)
            self.assertIsNone(load_run_history(p,EVENT,NOW))
        a=fixture();a['points'][0]['grid_point']['latitude']=41
        self.assertEqual(len(validate_run_history(a,EVENT,NOW)['points']),15)


if __name__=='__main__':unittest.main()
