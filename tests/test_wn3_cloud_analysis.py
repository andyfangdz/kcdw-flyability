import copy
import unittest
from datetime import timedelta

from kcdw.wn3_cloud_analysis import cloud_evidence, render_clouds, LAYERS
from kcdw.low_cloud_analysis import narrative_cloud_evidence, render_low_cloud
from kcdw.common import parse_time
from test_weathernext3 import fixture, NOW
from test_event_wn2_members import SNAP


class CloudAnalysisTests(unittest.TestCase):
    def snapshot(self):
        return dict(SNAP, weathernext3={'ok': True, 'data': fixture()})

    def test_all_layers_and_exact_opening_included_hours(self):
        data=cloud_evidence(self.snapshot(), NOW)
        self.assertEqual(len(data['hours']), 8)
        self.assertEqual([r['at'] for r in data['hours'] if r['period']=='flight'],
                         ['2026-09-24T14:00:00Z','2026-09-24T15:00:00Z'])
        self.assertEqual(data['hours'][-1]['at'], '2026-09-24T18:00:00Z')
        self.assertEqual(set(data['periods']['flight']), set(LAYERS))
        self.assertEqual(data['periods']['flight']['low_cloud_cover'],
                         dict(mean_cover_pct=30.,highest_hourly_p90_pct=33.,widest_hourly_p10_p90_pp=6.))

    def test_mean_of_hourly_means_keeps_hourly_tail_distinct(self):
        snapshot=self.snapshot();f=snapshot['weathernext3']['data']['forecast'];i=f['valid_time_utc'].index('2026-09-24T15:00:00Z')
        for stat,value in [('mean',50),('p10',10),('p90',90)]: f['fields']['low_cloud_cover'][stat][i]=value
        data=cloud_evidence(snapshot,NOW)
        self.assertEqual(data['periods']['flight']['low_cloud_cover']['mean_cover_pct'],40.)
        self.assertEqual(data['periods']['flight']['low_cloud_cover']['highest_hourly_p90_pct'],90.)
        self.assertEqual(data['periods']['flight']['low_cloud_cover']['widest_hourly_p10_p90_pp'],80.)

    def test_incomplete_stale_or_corrupt_guidance_is_unavailable(self):
        for mutate in (lambda s:s['weathernext3'].update(ok=False),
                       lambda s:s['weathernext3']['data']['forecast']['fields']['low_cloud_cover']['p90'].__setitem__(0,101),
                       lambda s:s['weathernext3']['data']['forecast']['valid_time_utc'].pop()):
            snapshot=self.snapshot();mutate(snapshot);self.assertIsNone(cloud_evidence(snapshot,NOW))
        self.assertIsNone(cloud_evidence(self.snapshot(),NOW+timedelta(days=2)))

    def test_cloud_panel_and_narrative_work_without_other_sources(self):
        snapshot=self.snapshot();data=narrative_cloud_evidence(snapshot,NOW)
        self.assertIsNotNone(data['wn3'])
        html=render_low_cloud(snapshot,NOW)
        for text in ('WN3 cloud layers around the flight','Middle cloud','High cloud','Total cloud','p10–p90','Layers overlap'):
            self.assertIn(text,html)
        self.assertIn('not a probability of clearing',data['wn3']['limits'])
        self.assertIn('unavailable',render_clouds(None))


class RunComparisonTests(unittest.TestCase):
    def test_distinct_prior_run_same_window_and_grid_only(self):
        from kcdw.wn3_cloud_analysis import run_comparison
        from test_weathernext3 import RUN, iso
        snapshot=CloudAnalysisTests().snapshot();current=cloud_evidence(snapshot,NOW)
        old=dict(init_time=iso(RUN-timedelta(hours=6)),collected_at=iso(RUN),
                 source_sha256='a'*64,grid_point=current['grid_point'],window=current['window'],hours=copy.deepcopy(current['hours']))
        for row in old['hours']:
            row['layers']['low_cloud_cover']['mean']=40.
        snapshot['wn3_cloud_previous']=old
        result=run_comparison(snapshot,current,NOW)
        self.assertEqual(result['layers']['low_cloud_cover']['change_pp'],-10.)
        for mutate in (lambda p:p.update(init_time=current['init_time']),
                       lambda p:p['hours'].pop(),lambda p:p['hours'][0]['layers']['low_cloud_cover'].update(p90=101),
                       lambda p:p.update(grid_point={'latitude':41.,'longitude':-74.25})):
            bad=copy.deepcopy(snapshot);mutate(bad['wn3_cloud_previous']);self.assertIsNone(run_comparison(bad,current,NOW))

    def test_archive_selection_validates_old_run_at_its_own_clock(self):
        import json
        import tempfile
        from pathlib import Path
        from kcdw.wn3_cloud_analysis import collect_previous
        from test_weathernext3 import RUN, iso
        current=CloudAnalysisTests().snapshot()
        old=copy.deepcopy(current);old['collected_at']=iso(RUN)
        env=old['weathernext3']['data'];earlier=RUN-timedelta(hours=6)
        env['status'].update(actual_run_utc=iso(earlier),attempted_init_utc=iso(earlier),state='degraded',fallback=True)
        env['forecast']['response_init_utc']=iso(earlier)
        env['forecast']['valid_time_utc']=[iso(parse_time(t)-timedelta(hours=6)) for t in env['forecast']['valid_time_utc']]
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)/'20260912T120000Z';directory.mkdir();(directory/'snapshot.json').write_text(json.dumps(old))
            prior=collect_previous(current,Path(root),NOW)
            self.assertEqual(prior['init_time'],iso(earlier))
            old['weathernext3']['data']['forecast']['grid_point']['latitude']=0
            (directory/'snapshot.json').write_text(json.dumps(old))
            self.assertIsNone(collect_previous(current,Path(root),NOW))


    def test_backfill_collected_during_refresh_has_stable_bound_evidence(self):
        from kcdw.wn3_cloud_analysis import run_comparison
        from test_weathernext3 import RUN, iso
        snapshot=CloudAnalysisTests().snapshot();snapshot['collected_at']=iso(NOW)
        current=cloud_evidence(snapshot,NOW)
        snapshot['wn3_cloud_previous']=dict(init_time=iso(RUN-timedelta(hours=6)),collected_at=iso(NOW+timedelta(seconds=15)),
                 source_sha256='a'*64,grid_point=current['grid_point'],window=current['window'],hours=copy.deepcopy(current['hours']))
        bound=run_comparison(snapshot,current,NOW)
        self.assertIsNotNone(bound)
        self.assertEqual(bound,run_comparison(snapshot,current,NOW+timedelta(minutes=2)))
        snapshot['wn3_cloud_previous']['collected_at']=iso(NOW+timedelta(hours=1))
        self.assertIsNone(run_comparison(snapshot,current,NOW))
