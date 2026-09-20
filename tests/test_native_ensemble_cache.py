"""Offline behavioral coverage; no GRIB/network downloads in these tests."""
import tempfile
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kcdw import native_ensemble_cache as cache

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 23, tzinfo=UTC)
INIT = NOW.replace(hour=18)
END = NOW + timedelta(hours=7)


class FakeWorker:
    def __init__(self, missing=None):
        self.calls = []
        self.cached = {}
        self.downloads = 0
        self.missing = missing

    def groups(self, model):
        return ['all']

    def url_for(self, model, init, lead, group):
        return f'https://example.test/{model}/{cache.stamp(init)}/{lead}'

    def index(self, model, init, lead, group):
        self.calls.append(('index', lead))
        keys = cache.required_keys(model, [lead])
        return self.url_for(model, init, lead, group), {(m, f): (0, 9) for m, f, _ in keys}

    def point(self, model, init, lead, member, field, url, span, collected):
        self.calls.append(('point', lead))
        key = (cache.stamp(init), member, field, lead)
        if field == self.missing:
            raise ValueError('temporary missing field')
        if key not in self.cached:
            self.downloads += 1
            self.cached[key] = dict(model=model, init=cache.stamp(init), lead=lead,
                member=member, field=field, value=1, collected_at=collected,
                fetched_at=collected)
        return self.cached[key]


def packet(model='gefs', init=INIT, now=NOW, end=END) -> dict:
    leads = cache.plan_leads(model, init, now, end)
    return dict(model=model, init=cache.stamp(init), offered_members=cache.members(model),
        requested_leads=leads, required_fields=list(cache.REQUIRED_FIELDS[model]),
        unsupported_fields=list(cache.UNSUPPORTED_FIELDS[model]),
        points=[dict(model=model, init=cache.stamp(init), member=m, field=f, lead=h,
                     value=1, collected_at=cache.stamp(now), fetched_at=cache.stamp(now))
                for m, f, h in cache.required_keys(model, leads)])


class NativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(cache, '_validate_packet', side_effect=lambda p, n: p)
        self.patch.start()
        self.clock = patch.object(cache, '_now', return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.patch.stop()
        self.clock.stop()
        self.tmp.cleanup()

    def test_full_matrix_not_just_member_count(self):
        p = packet()
        self.assertTrue(cache.completeness(p, NOW, END, NOW))
        p['points'].pop()
        self.assertFalse(cache.completeness(p, NOW, END, NOW))

    def test_contract_unsupported_and_baseline(self):
        for model in ('ecmwf_ens', 'aifs_ens'):
            leads = cache.plan_leads(model, INIT - timedelta(hours=12), NOW, END)
            self.assertEqual(leads[0], 12)  # same-member cumulative baseline for closing lead 18
        self.assertNotIn('lcc', cache.REQUIRED_FIELDS['ecmwf_ens'])
        self.assertNotIn('gust', cache.REQUIRED_FIELDS['aifs_ens'])
        self.assertIn('gust', cache.UNSUPPORTED_FIELDS['aifs_ens'])
        self.assertNotIn(('00', 'lcc', 0), cache.required_keys('gefs', [0]))
        self.assertIn(('00', 'gust', 0), cache.required_keys('gefs', [0]))

    def test_rejects_cross_run_duplicates_stale_and_missing_range(self):
        for mutation in ('cross', 'duplicate', 'stale', 'range'):
            p = packet()
            if mutation == 'cross': p['points'][0]['init'] = cache.stamp(INIT-timedelta(hours=6))
            if mutation == 'duplicate': p['points'].append(p['points'][0])
            if mutation == 'stale': p['points'][0]['collected_at'] = cache.stamp(NOW-timedelta(hours=13))
            if mutation == 'range': p['requested_leads'] = p['requested_leads'][:-1]
            self.assertFalse(cache.completeness(p, NOW, END, NOW), mutation)

    def test_resume_partial_never_promoted_global_index_phase(self):
        worker = FakeWorker(missing='lcc')
        self.assertIsNone(cache.produce_run('gefs', INIT, NOW, END, worker=worker, root=self.root, workers=3))
        self.assertFalse(list(self.root.rglob('complete.json')))
        self.assertTrue(list(self.root.rglob('progress.json')))
        point_at = next(i for i, call in enumerate(worker.calls) if call[0] == 'point')
        self.assertTrue(all(c[0] == 'index' for c in worker.calls[:point_at]))
        self.assertTrue(all(c[0] == 'point' for c in worker.calls[point_at:]))
        downloads = worker.downloads
        worker.missing = None
        complete = cache.produce_run('gefs', INIT, NOW, END, worker=worker, root=self.root, workers=3)
        self.assertIsNotNone(complete)
        self.assertEqual(worker.downloads-downloads, sum(f == 'lcc' for m, f, h in cache.required_keys('gefs', complete['requested_leads'])))
        loaded = cache.load_completed('gefs', NOW, END, NOW, root=self.root)
        self.assertEqual(loaded, complete)
        calls = len(worker.calls)
        cache.produce_run('gefs', INIT, NOW, END, worker=worker, root=self.root)
        self.assertEqual(len(worker.calls), calls)

    def test_offline_reader_uses_latest_complete_not_partial_and_keeps_clocks(self):
        old = packet(init=INIT-timedelta(hours=6))
        cache.promote(old, NOW, END, NOW, root=self.root)
        run = self.root/'gefs'/INIT.strftime('%Y%m%d%H')
        run.mkdir(parents=True)
        (run/'progress.json').write_text('{}')
        with patch.object(cache, '_worker', side_effect=AssertionError('reader must stay offline')):
            loaded = cache.load_completed('gefs', NOW, END, NOW+timedelta(hours=1), root=self.root)
        self.assertEqual(loaded, old)
        self.assertIsNone(cache.load_completed('gefs', NOW, END, NOW+timedelta(hours=13), root=self.root))

    def test_incomplete_endpoint_is_retried_not_frozen_in_index_cache(self):
        class Publishing(FakeWorker):
            incomplete = True
            def index(self, model, init, lead, group):
                url, ranges = super().index(model, init, lead, group)
                if init.hour == 18 and self.incomplete:
                    ranges.pop(next(iter(ranges)))
                return url, ranges
        worker = Publishing()
        self.assertEqual(cache.discover('gefs', NOW, END, worker=worker, root=self.root)[0].hour, 12)
        worker.incomplete = False
        self.assertEqual(cache.discover('gefs', NOW, END, worker=worker, root=self.root)[0].hour, 18)

    def test_producer_marks_native_v3_and_geps_matches_worker_fields(self):
        p = cache.produce_run('gefs', INIT, NOW, END, worker=FakeWorker(), root=self.root)
        self.assertEqual(p['native_version'], 3)
        from kcdw import direct_geps_worker
        self.assertEqual(set(cache.REQUIRED_FIELDS['geps']), set(direct_geps_worker.FIELDS))

    def test_geps_analysis_does_not_request_nonexistent_accumulation(self):
        class Grouped:
            def fetch_frame(self, init, lead, collected, *, fields=None):
                if fields is None or (lead==0 and 'tp' in fields):
                    raise ValueError('analysis accumulation is not supplied')
                return [dict(model='geps',init=cache.stamp(init),lead=lead,member=m,field=f,value=1,
                             collected_at=collected,fetched_at=collected)
                        for m in cache.members('geps') for f in fields]
        result=cache.produce_run('geps',INIT.replace(hour=12),NOW,END,worker=Grouped(),root=self.root)
        self.assertIsNotNone(result)
        self.assertTrue(cache.completeness(result,NOW,END,NOW))

    def test_slow_first_model_does_not_starve_later_models(self):
        import time
        marker=self.root/'visited.txt'
        def run_model(model,end,workers):
            with marker.open('a') as stream:stream.write(model+'\n')
            if model=='gefs':time.sleep(5)
        with patch.object(cache,'_run_model',side_effect=run_model):
            result=cache.run_models(['gefs','geps'],END,seconds=.4,workers=1)
        self.assertEqual(marker.read_text().splitlines(),['gefs','geps'])
        self.assertEqual(result,124)

    def test_model_retries_partial_progress_once_before_returning(self):
        with patch.object(cache,'CACHE',self.root), patch.object(cache,'load_completed',return_value=None), \
             patch.object(cache,'_worker',return_value=object()), patch.object(cache,'discover',return_value=(INIT,[])), \
             patch.object(cache,'produce_run',side_effect=[None,{'init':cache.stamp(INIT)}]) as produce:
            cache._run_model('gefs',END,1)
        self.assertEqual(produce.call_count,2)

    def test_tampered_packet_not_returned(self):
        p = packet()
        cache.promote(p, NOW, END, NOW, root=self.root)
        path = next(self.root.rglob('packet-*.json'))
        path.write_text(path.read_text().replace('"value":1', '"value":2', 1))
        self.assertIsNone(cache.load_completed('gefs', NOW, END, NOW, root=self.root))

    def test_parent_sigterm_reaps_child_and_releases_lock(self):
        import fcntl, os, signal, time
        marker=self.root/'child.pid'
        code=('from pathlib import Path; import os,time; from kcdw import native_ensemble_cache as c;\n'
              f'c.CACHE=Path({str(self.root)!r})\n'
              'c.default_end=lambda now: now+c.timedelta(days=1)\n'
              'def sleeper(*args):\n'
              f' Path({str(marker)!r}).write_text(str(os.getpid()))\n'
              ' time.sleep(30)\n'
              'c._run_model=sleeper\n'
              'raise SystemExit(c.main(["--models","gefs","--seconds","10"]))\n')
        parent=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        child=None
        try:
            deadline=time.monotonic()+4
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(marker.exists(),'worker did not start')
            child=int(marker.read_text())
            parent.terminate();parent.wait(timeout=5)
            with (self.root/'producer.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ProcessLookupError):os.kill(child,0)
        finally:
            if parent.poll() is None:parent.kill();parent.wait()
            if child:
                try:os.kill(child,signal.SIGKILL)
                except ProcessLookupError:pass

    def test_real_subprocess_alarm_and_flock_bound(self):
        code = ('from pathlib import Path; import time; from kcdw import native_ensemble_cache as c; '
                f'c.CACHE=Path({str(self.root)!r}); '
                'c.default_end=lambda now: now+c.timedelta(days=1); '
                'c.discover=lambda *a, **k: time.sleep(30); '
                'raise SystemExit(c.main(["--models","gefs","--seconds","1"]))')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 124)
        import fcntl
        with (self.root/'producer.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'already active', result.stdout)

    def test_discovery_checks_entire_endpoint_and_ifs_cycle_limit(self):
        class Incomplete(FakeWorker):
            def index(self, model, init, lead, group):
                url, ranges = super().index(model, init, lead, group)
                if init.hour == 18: ranges.pop(next(iter(ranges)))
                return url, ranges
        self.assertEqual(cache.discover('gefs', NOW, END, worker=Incomplete(), root=self.root)[0].hour, 12)
        # Long IFS request cannot use a 06/18 cycle (144h limit).
        init, _ = cache.discover('ecmwf_ens', NOW, NOW+timedelta(days=9), worker=FakeWorker(), root=self.root)
        self.assertEqual(init.hour, 12)


if __name__ == '__main__':
    unittest.main()
