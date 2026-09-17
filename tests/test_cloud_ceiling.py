"""Native ceiling tests; fixtures are synthetic, not weather evidence."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kcdw.cloud_ceiling import collect_ceiling, validate_ceiling, _context, _source_url, NOTES
from kcdw.gfs_guidance import collect_gfs, UNITS, VARIABLES
from kcdw.events import find_event

NOW = datetime(2026, 9, 15, 22, tzinfo=timezone.utc)
INIT = NOW.replace(hour=12)

class Client:
    def get(self, url):
        if '/static/' in url:
            return dict(last_run_initialisation_time=int(INIT.timestamp()),
                        last_run_modification_time=int((INIT+timedelta(hours=4)).timestamp()),
                        last_run_availability_time=int((INIT+timedelta(hours=5)).timestamp()),
                        data_end_time=int((INIT+timedelta(days=16)).timestamp()),
                        temporal_resolution_seconds=3600, update_interval_seconds=21600)
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        a = datetime.fromisoformat(q['start_hour'][0]).replace(tzinfo=timezone.utc)
        b = datetime.fromisoformat(q['end_hour'][0]).replace(tzinfo=timezone.utc)
        times = [int((a+timedelta(hours=i)).timestamp()) for i in range(int((b-a).total_seconds()/3600)+1)]
        return dict(latitude=41, longitude=-74.25, timezone='GMT', utc_offset_seconds=0,
                    hourly_units=UNITS, hourly=dict(time=times, **{k:[v]*len(times) for k,v in zip(VARIABLES,[1010,0,10,15,100,20])}))

def snapshot():
    return {'event':find_event('commercial-checkride').as_dict(), 'collected_at':'2026-09-15T22:00:00Z',
            'gfs':collect_gfs(Client(), datetime(2026,9,24,4,tzinfo=timezone.utc), datetime(2026,9,25,4,tzinfo=timezone.utc), NOW)}

def discovery(model, start, end, now, *, kind):
    """Offline native selection; unrelated callers may still use worker alone."""
    assert model == 'gfs' and kind == 'ceiling'
    return dict(init=INIT.strftime('%Y-%m-%dT%H:%M:%SZ'),
                leads=[h for h in range(0,385,3) if start <= INIT+timedelta(hours=h) <= end])


def worker(request):
    init = datetime.fromisoformat(request['model_init'].replace('Z','+00:00')) if 'model_init' in request else INIT
    samples = []
    for lead in request['leads']:
        samples.append(dict(lead_hour=lead, valid_at=(init+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            source_url=_source_url(init,lead), latitude=41.0, longitude=-74.25,
            ceiling_msl_gpm=400.0, terrain_m=140.0, ceiling_agl_ft=260*3.280839895,
            low_cloud_pct=100.0, neighborhood=dict(cells=25, ceiling_valid=25, ceiling_missing=0,
            under_1000_ft=25, agl_ft_min=800.0, agl_ft_max=900.0, low_cloud_valid=25,
            low_cloud_pct_min=100.0, low_cloud_pct_max=100.0),
            fields={k:dict(start=10,end=19,total=100,bytes=10,sha256='a'*64) for k in ('ceiling','terrain','low_cloud')}))
    return samples

class CeilingTests(unittest.TestCase):
    def setUp(self):
        p = patch('kcdw.cloud_ceiling.discover_run', side_effect=discovery, create=True)
        self.discover = p.start()
        self.addCleanup(p.stop)

    def test_v2_does_not_depend_on_open_meteo(self):
        for state in ('missing', 'failed', 'older', 'malformed'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                snap = snapshot()
                if state == 'missing': del snap['gfs']
                elif state == 'failed': snap['gfs']['ok'] = False
                elif state == 'malformed': snap['gfs'] = object()
                else:
                    snap['gfs']['data']['metadata']['datasets']['ncep_gfs025']['latest_advertised_init'] = '2026-09-15T06:00:00Z'
                with patch('kcdw.cloud_ceiling._run_worker', side_effect=worker) as run:
                    env = collect_ceiling(snap, Path(d), NOW)
                self.assertIsNotNone(env)
                self.assertEqual(env['version'], 2)
                self.assertEqual(env['model_init'], '2026-09-15T12:00:00Z')
                run.assert_called_once_with({'model_init': env['model_init'], 'leads': [216,219,222,225]})
                self.assertEqual(self.discover.call_args.args,
                    ('gfs', datetime(2026,9,24,12,tzinfo=timezone.utc),
                     datetime(2026,9,24,21,tzinfo=timezone.utc), NOW))
                self.assertEqual(self.discover.call_args.kwargs, {'kind':'ceiling'})
                with patch('kcdw.cloud_ceiling.discover_run', side_effect=AssertionError('render discovery')) as live:
                    self.assertIs(validate_ceiling(env, snap, NOW), env)
                    live.assert_not_called()

    def test_new_discovery_precedes_cache_and_newer_cycle_refetches(self):
        with tempfile.TemporaryDirectory() as d:
            original = self.good(d)
            snap = snapshot(); snap['collected_at'] = '2026-09-15T22:30:00Z'
            self.discover.side_effect = None
            self.discover.return_value = {'init':'2026-09-15T18:00:00Z', 'leads':[210,213,216,219]}
            with patch('kcdw.cloud_ceiling._run_worker', side_effect=worker) as run:
                fresh = collect_ceiling(snap, Path(d), NOW+timedelta(minutes=30))
            self.assertIsNotNone(fresh); run.assert_called_once()
            self.assertNotEqual(fresh['model_init'], original['model_init'])
            self.assertEqual(self.discover.call_count, 2)
            self.discover.return_value = None
            with patch('kcdw.cloud_ceiling._run_worker') as run:
                self.assertIsNone(collect_ceiling(snap, Path(d), NOW+timedelta(minutes=30)))
                run.assert_not_called()
            self.assertEqual(self.discover.call_count, 3)

    def test_fetch_future_at_collection_never_becomes_valid_later(self):
        with tempfile.TemporaryDirectory() as d: env = self.good(d)
        env['fetched_at'] = '2026-09-15T22:06:00Z'
        for clock in (NOW, NOW+timedelta(minutes=6), NOW+timedelta(hours=1)):
            self.assertIsNone(validate_ceiling(env, snapshot(), clock))
        env['fetched_at'] = '2026-09-15T22:05:00Z'
        self.assertIsNone(validate_ceiling(env, snapshot(), NOW))
        self.assertIsNone(validate_ceiling(env, snapshot(), NOW+timedelta(minutes=5)))

    def test_native_init_synoptic_and_age_are_checked(self):
        with tempfile.TemporaryDirectory() as d: env = self.good(d)
        for init in ('2026-09-15T13:00:00Z','2026-09-15T12:00:01Z',
                     '2026-09-14T18:00:00Z','2026-09-16T00:00:00Z'):
            bad = copy.deepcopy(env); bad['model_init'] = init
            self.assertIsNone(validate_ceiling(bad,snapshot(),NOW))
        self.assertIsNotNone(validate_ceiling(env,snapshot(),INIT+timedelta(hours=24)))
        self.assertIsNone(validate_ceiling(env,snapshot(),INIT+timedelta(hours=24,seconds=1)))

    def test_discovery_bad_axis_or_run_fails_before_worker(self):
        for selection in (None, {'init':'2026-09-15T12:00:00Z','leads':[216]},
                          {'init':'2026-09-15T12:00:00Z','leads':[216,219,222,True]},
                          {'init':'2026-09-15T13:00:00Z','leads':[216,219,222]},
                          {'init':'2026-09-14T12:00:00Z','leads':[240,243,246,249]}):
            self.discover.side_effect = None; self.discover.return_value = selection
            with tempfile.TemporaryDirectory() as d, patch('kcdw.cloud_ceiling._run_worker') as run:
                self.assertIsNone(collect_ceiling(snapshot(),Path(d),NOW))
                run.assert_not_called()

    def test_legacy_historical_context_and_cache_not_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.good(d); legacy = copy.deepcopy(env); legacy['version'] = 1
            self.assertIsNotNone(validate_ceiling(legacy,snapshot(),NOW))
            snap = snapshot(); del snap['gfs']
            self.assertIsNone(validate_ceiling(legacy,snap,NOW))
            snap = snapshot()
            snap['gfs']['data']['metadata']['datasets']['ncep_gfs025']['latest_advertised_init'] = '2026-09-15T06:00:00Z'
            self.assertIsNone(validate_ceiling(legacy,snap,NOW))
            # Legacy validation deliberately retains its historical fetch rules.
            legacy['fetched_at'] = '2026-09-15T22:06:00Z'
            self.assertIsNotNone(validate_ceiling(legacy,snapshot(),NOW+timedelta(minutes=6)))
            legacy['fetched_at'] = env['fetched_at']
            cache = next(Path(d).glob('ceiling-*.json'))
            cache.write_text(json.dumps(legacy))
            snap = snapshot(); snap['collected_at'] = '2026-09-15T22:30:00Z'
            with patch('kcdw.cloud_ceiling._run_worker',side_effect=worker) as run:
                fresh = collect_ceiling(snap,Path(d),NOW+timedelta(minutes=30))
            self.assertIsNotNone(fresh); run.assert_called_once()
            self.assertEqual(fresh['version'],2)
            self.assertEqual(fresh['fetched_at'],snap['collected_at'])

    def test_axis_cannot_be_clipped_to_native_horizon(self):
        snap = snapshot(); snap['event']['date'] = '2026-10-01'
        self.discover.side_effect = None
        self.discover.return_value = {'init':'2026-09-15T12:00:00Z','leads':[384]}
        with tempfile.TemporaryDirectory() as d, patch('kcdw.cloud_ceiling._run_worker',side_effect=worker) as run:
            self.assertIsNone(collect_ceiling(snap,Path(d),NOW))
            run.assert_not_called()

    def test_fetch_within_old_grace_and_future_init_never_become_valid(self):
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
        env['fetched_at']='2026-09-15T22:01:00Z'
        for later in (NOW,NOW+timedelta(minutes=2)):
            self.assertIsNone(validate_ceiling(env,snapshot(),later))
        env['model_init']=env['fetched_at']='2026-09-15T18:00:00Z'
        env['samples']=worker({'model_init':env['model_init'],'leads':[210,213,216,219]})
        s=snapshot();s['collected_at']=env['snapshot_collected_at']='2026-09-15T17:59:00Z'
        for later in (NOW.replace(hour=17,minute=59),NOW.replace(hour=18)):
            self.assertIsNone(validate_ceiling(env,s,later))

    def test_cache_cannot_launder_future_fetch_or_another_run(self):
        for kind in ('future', 'other_run'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                env = self.good(d)
                if kind == 'future': env['fetched_at'] = '2026-09-15T22:06:00Z'
                else:
                    env['model_init'] = '2026-09-15T18:00:00Z'
                    env['samples'] = worker({'model_init':env['model_init'],'leads':[210,213,216,219]})
                next(Path(d).glob('ceiling-*.json')).write_text(json.dumps(env))
                snap = snapshot(); snap['collected_at'] = '2026-09-15T22:30:00Z'
                with patch('kcdw.cloud_ceiling._run_worker',side_effect=worker) as run:
                    refreshed = collect_ceiling(snap,Path(d),NOW+timedelta(minutes=30))
                self.assertIsNotNone(refreshed); run.assert_called_once()
                self.assertEqual(refreshed['model_init'],'2026-09-15T12:00:00Z')
                self.assertEqual(refreshed['fetched_at'],snap['collected_at'])

    def test_cache_reuse_boundary_and_different_mission(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.good(d)
            later = NOW+timedelta(hours=12)
            snap = snapshot(); snap['collected_at'] = later.strftime('%Y-%m-%dT%H:%M:%SZ')
            with patch('kcdw.cloud_ceiling._run_worker') as run:
                cached = collect_ceiling(snap,Path(d),later)
                run.assert_not_called()
            self.assertEqual(cached['fetched_at'],env['fetched_at'])
            snap['event']['window'] = '09-16'
            with patch('kcdw.cloud_ceiling._run_worker',side_effect=worker) as run:
                changed = collect_ceiling(snap,Path(d),later)
                run.assert_called_once()
            self.assertEqual([s['lead_hour'] for s in changed['samples']],[219,222])

    def test_worker_evidence_must_validate_before_cache_write(self):
        def wrong_url(request):
            samples = worker(request)
            samples[0]['source_url'] = 'https://example.com/forged'
            return samples
        with tempfile.TemporaryDirectory() as d, patch('kcdw.cloud_ceiling._run_worker',side_effect=wrong_url):
            self.assertIsNone(collect_ceiling(snapshot(),Path(d),NOW))
            self.assertEqual(list(Path(d).iterdir()),[])

    def good(self, directory):
        with patch('kcdw.cloud_ceiling._run_worker', side_effect=worker) as run:
            env = collect_ceiling(snapshot(), Path(directory), NOW)
            self.assertIsNotNone(env)
            self.assertEqual(run.call_count,1)
        return env

    def test_native_axis_and_cache_preserves_retrieval(self):
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
            self.assertEqual([s['lead_hour'] for s in env['samples']], [216,219,222,225])
            snap=snapshot(); snap['collected_at']='2026-09-15T22:30:00Z'
            with patch('kcdw.cloud_ceiling._run_worker', side_effect=AssertionError('cache missed')):
                reused=collect_ceiling(snap,Path(d),NOW+timedelta(minutes=30))
            self.assertEqual(reused['fetched_at'],env['fetched_at'])
            self.assertEqual(reused['snapshot_collected_at'],snap['collected_at'])
            self.assertEqual(reused['samples'],env['samples'])
            self.assertEqual(self.discover.call_count,2)

    def test_cache_refresh_after_twelve_hours(self):
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
            later=NOW+timedelta(hours=13)
            snap=snapshot(); stamp=later.strftime('%Y-%m-%dT%H:%M:%SZ')
            snap['collected_at']=stamp; snap['gfs']['data']['fetched_at']=stamp
            for item in snap['gfs']['data']['metadata']['datasets'].values(): item['fetched_at']=stamp
            # Forecast request begins after this new collection date's midnight.
            with patch('kcdw.cloud_ceiling._run_worker',side_effect=worker) as run:
                refreshed=collect_ceiling(snap,Path(d),later)
            self.assertIsNotNone(refreshed); self.assertEqual(run.call_count,1)
            self.assertNotEqual(refreshed['fetched_at'],env['fetched_at'])

    def test_nonexact_closing_endpoint_and_current_run(self):
        snap=snapshot(); snap['event']['window']='09-16'
        _,_,leads=_context(snap,NOW)
        self.assertEqual(leads,[219,222])
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
        snap=snapshot()
        snap['gfs']['data']['metadata']['datasets']['ncep_gfs025']['latest_advertised_init']='2026-09-15T06:00:00Z'
        self.assertIsNotNone(validate_ceiling(env,snap,NOW))
        env['version']=1
        self.assertIsNone(validate_ceiling(env,snap,NOW))

    def test_tampering_and_stale_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
        mutations=[lambda e:e.update(extra='secret'), lambda e:e.update(model_init='2026-09-15T18:00:00Z'),
            lambda e:e['samples'][0].update(source_url='file:///tmp/private'),
            lambda e:e['samples'][0].update(ceiling_agl_ft=float('nan')),
            lambda e:e['samples'][0].update(ceiling_agl_ft=999),
            lambda e:e['samples'][0].update(lead_hour=219),
            lambda e:e['samples'][0].update(valid_at='2026-09-24T13:00:00Z'),
            lambda e:e['samples'][0]['fields']['terrain'].update(bytes=11),
            lambda e:e['samples'][0]['neighborhood'].update(under_1000_ft=26),
            lambda e:e['event'].update(slug='wrong'), lambda e:e.update(fetched_at='2026-09-16T00:00:00Z')]
        for mutate in mutations:
            bad=copy.deepcopy(env); mutate(bad)
            self.assertIsNone(validate_ceiling(bad,snapshot(),NOW))
        self.assertIsNone(validate_ceiling(env,snapshot(),NOW+timedelta(hours=25)))

    def test_missing_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            env=self.good(d)
        env['samples'][0].update(ceiling_msl_gpm=None,ceiling_agl_ft=None)
        self.assertIsNotNone(validate_ceiling(env,snapshot(),NOW))
        env['samples'][0]['ceiling_agl_ft']=0
        self.assertIsNone(validate_ceiling(env,snapshot(),NOW))

    def test_source_failure_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'link').symlink_to(root,target_is_directory=True)
            with patch('kcdw.cloud_ceiling._run_worker',side_effect=RuntimeError('private')):
                self.assertIsNone(collect_ceiling(snapshot(),root,NOW))
            with patch('kcdw.cloud_ceiling._run_worker') as run:
                self.assertIsNone(collect_ceiling(snapshot(),root/'link',NOW)); run.assert_not_called()
        snap=snapshot(); self.discover.side_effect=RuntimeError('NOAA unavailable')
        with patch('kcdw.cloud_ceiling._run_worker') as run:
            self.assertIsNone(collect_ceiling(snap,Path('/tmp/unused'),NOW)); run.assert_not_called()

class WorkerTests(unittest.TestCase):
    def test_index_requires_exact_init_fields_and_instantaneous(self):
        from kcdw.native_ceiling_worker import indexed_ranges
        rows = ('1:0:d=2026091512:HGT:surface:216 hour fcst:\n'
                '2:10:d=2026091512:LCDC:low cloud layer:216 hour fcst:\n'
                '3:20:d=2026091512:HGT:cloud ceiling:216 hour fcst:\n'
                '4:30:d=2026091512:TMP:surface:216 hour fcst:\n')
        self.assertEqual(indexed_ranges(rows,INIT,216)['ceiling'],(20,29))
        for bad in (rows.replace('216 hour fcst','213-216 hour ave fcst'),
                    rows.replace('2026091512','2026091518'),rows.replace('3:20:','3:10:')):
            with self.assertRaises(ValueError): indexed_ranges(bad,INIT,216)

    def test_http_range_validation_and_byte_bounds(self):
        from kcdw.native_ceiling_worker import fetch
        from unittest.mock import MagicMock
        import sys
        response=MagicMock(); response.__enter__.return_value=response
        response.status_code=206; response.headers={'Content-Range':'bytes 10-19/100'}
        response.iter_content.return_value=[b'0123456789']
        requests=MagicMock(); requests.get.return_value=response
        with patch.dict(sys.modules,{'requests':requests}):
            content,proof=fetch('https://noaa-gfs-bdp-pds.s3.amazonaws.com/test',10,(10,19))
            self.assertEqual(proof['bytes'],len(content))
            for status,cr,body in [(200,'bytes 10-19/100',b'0123456789'),
                (206,'bytes 0-9/100',b'0123456789'),(206,'bytes 10-19/*',b'0123456789'),
                (206,'bytes 10-19/100',b'0123'),(206,'bytes 10-19/100',b'01234567890')]:
                response.status_code=status; response.headers={'Content-Range':cr}; response.iter_content.return_value=[body]
                with self.assertRaises(ValueError): fetch('https://noaa-gfs-bdp-pds.s3.amazonaws.com/test',10,(10,19))
            self.assertFalse(requests.get.call_args.kwargs['allow_redirects'])

    def test_decode_rejects_wrong_metadata(self):
        from kcdw.native_ceiling_worker import decode
        from unittest.mock import MagicMock
        import sys
        ec=MagicMock(); ec.codes_get.return_value='wrong'
        content=b'GRIB'+b'\0'*4+(20).to_bytes(8,'big')+b'7777'
        with patch.dict(sys.modules,{'eccodes':ec}):
            with self.assertRaises(ValueError): decode(content,'ceiling',INIT,216)
            ec.codes_release.assert_called_once()

if __name__ == '__main__': unittest.main()
