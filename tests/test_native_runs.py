"""Synthetic discovery tests; network data are independently smoke-tested."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from kcdw import native_runs as runs

NOW=datetime(2026,9,17,19,tzinfo=timezone.utc)
START=datetime(2026,9,24,14,tzinfo=timezone.utc)
END=START+timedelta(hours=2)

def gfs_index(init,lead):
    specs=[('UGRD','10 m above ground'),('VGRD','10 m above ground'),('UGRD','925 mb'),('VGRD','925 mb'),('UGRD','850 mb'),('VGRD','850 mb'),('HGT','surface'),('LCDC','low cloud layer'),('HGT','cloud ceiling'),('TMP','surface')]
    return '\n'.join(f'{i+1}:{i*100}:d={init:%Y%m%d%H}:{p}:{level}:{lead} hour fcst:' for i,(p,level) in enumerate(specs))

class NativeRunTests(unittest.TestCase):
    def test_outer_discovery_deadline_terminates_stalled_process(self):
        import subprocess,time,sys
        run=subprocess.run
        def stalled(*args,**kwargs):
            return run([sys.executable,'-c','import time;time.sleep(10)'],timeout=kwargs['timeout'],capture_output=True,text=True)
        before=time.monotonic()
        with patch.object(runs,'PROCESS_TIMEOUT',0.1,create=True),patch('subprocess.run',side_effect=stalled) as call,patch.object(runs,'_read_index',side_effect=ValueError('offline')):
            self.assertIsNone(runs.discover_run('gfs',START,END,NOW))
        call.assert_called_once()
        self.assertLess(time.monotonic()-before,1)

    def test_process_result_must_be_one_of_the_eligible_candidates(self):
        import subprocess,json
        good={'init':'2026-09-17T12:00:00Z','leads':[168,171,174]}
        for result,expected in ((good,good),(dict(good,init='2026-09-18T00:00:00Z'),None),(dict(good,leads=[168]),None),(dict(good,extra='unexpected'),None)):
            with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(result),'')):
                self.assertEqual(runs.discover_run('gfs',START,END,NOW),expected)

    def test_candidates_are_newest_first_horizon_aware_and_bounded(self):
        g=runs.candidate_runs('gfs',START,END,NOW)
        self.assertEqual([p['init'] for p in g],['2026-09-17T18:00:00Z','2026-09-17T12:00:00Z','2026-09-17T06:00:00Z','2026-09-17T00:00:00Z'])
        self.assertEqual(g[1]['leads'],[168,171,174])
        e=runs.candidate_runs('ifs',START,END,NOW)
        self.assertEqual([p['init'] for p in e],['2026-09-17T12:00:00Z','2026-09-17T00:00:00Z'])
        self.assertEqual(e[0]['leads'],[168,174])
        a=runs.candidate_runs('aifs_single',START,END,NOW)
        self.assertEqual(a[0]['init'],'2026-09-17T18:00:00Z')
        self.assertEqual(runs.candidate_runs('gfs',START,END,NOW,kind='ceiling')[1]['leads'],[171])

    def test_invalid_context_does_not_make_requests(self):
        for model,a,b,n,kind in [('bogus',START,END,NOW,'wind'),('gfs',END,START,NOW,'wind'),('gfs',START,END,NOW.replace(tzinfo=None),'wind'),('ifs',START,END,NOW,'ceiling')]:
            with patch.object(runs,'_read_index') as read:
                self.assertIsNone(runs.discover_run(model,a,b,n,kind=kind))
                read.assert_not_called()

    def test_missing_newest_or_incomplete_required_lead_uses_complete_prior(self):
        calls=[]
        def read(url,deadline):
            calls.append(url)
            if '/18/' in url: raise ValueError('not published')
            lead=int(url.split('.f')[-1].split('.')[0])
            init=NOW.replace(hour=12)
            return gfs_index(init,lead)
        with patch.object(runs,'_read_index',side_effect=read):
            chosen=runs._discover_run('gfs',START,END,NOW)
        self.assertEqual(chosen,{'init':'2026-09-17T12:00:00Z','leads':[168,171,174]})
        self.assertTrue(all('noaa-gfs-bdp-pds.s3.amazonaws.com' in u for u in calls))
        def missing_field(url,deadline):
            lead=int(url.split('.f')[-1].split('.')[0])
            hour=int(url.split('/atmos')[0][-2:]);init=NOW.replace(hour=hour)
            text=gfs_index(init,lead)
            return text.replace(':UGRD:925 mb:',':TMP:925 mb:') if hour==18 else text
        with patch.object(runs,'_read_index',side_effect=missing_field):
            self.assertEqual(runs._discover_run('gfs',START,END,NOW)['init'],'2026-09-17T12:00:00Z')

    def test_mixed_run_indexes_cannot_select_run(self):
        def read(url,deadline):
            lead=int(url.split('.f')[-1].split('.')[0])
            return gfs_index(NOW.replace(hour=6),lead)
        with patch.object(runs,'_read_index',side_effect=read):
            result=runs._discover_run('gfs',START,END,NOW)
        self.assertEqual(result['init'],'2026-09-17T06:00:00Z')

    def test_missing_middle_hour_rejects_partial_cycle(self):
        def read(url,deadline):
            lead=int(url.split('.f')[-1].split('.')[0]);hour=int(url.split('/atmos')[0][-2:])
            if hour==18 and lead==165: raise ValueError('partial run')
            return gfs_index(NOW.replace(hour=hour),lead)
        with patch.object(runs,'_read_index',side_effect=read):
            self.assertEqual(runs._discover_run('gfs',START,END,NOW)['init'],'2026-09-17T12:00:00Z')

    def test_all_source_failures_remain_unavailable(self):
        with patch.object(runs,'_read_index',side_effect=ValueError('offline')):
            self.assertIsNone(runs._discover_run('gfs',START,END,NOW))

    def test_ceiling_requires_its_own_fields(self):
        def read(url,deadline):
            lead=int(url.split('.f')[-1].split('.')[0]);hour=int(url.split('/atmos')[0][-2:])
            return gfs_index(NOW.replace(hour=hour),lead).replace(':HGT:cloud ceiling:',':HGT:unknown:')
        with patch.object(runs,'_read_index',side_effect=read):
            self.assertIsNone(runs._discover_run('gfs',START,END,NOW,kind='ceiling'))
            self.assertIsNotNone(runs._discover_run('gfs',START,END,NOW))

    def test_bounded_read_rejects_redirect_large_body_and_expired_budget(self):
        import time
        with patch('requests.get') as get:
            with self.assertRaises(ValueError): runs._read_index('https://example.invalid',time.monotonic()-1)
            get.assert_not_called()
            response=MagicMock();response.__enter__.return_value=response;get.return_value=response
            response.status_code=302
            with self.assertRaises(ValueError): runs._read_index('https://example.invalid',time.monotonic()+5)
            response.status_code=200;response.iter_content.return_value=[b'x'*250001]
            with self.assertRaises(ValueError): runs._read_index('https://example.invalid',time.monotonic()+5)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
