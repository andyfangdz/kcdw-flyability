"""Independent resumable native runs; consumers read only committed manifests.

Run with var/native-weather-venv/bin/python -m kcdw.native_ensemble_cache.
A fresh older COMPLETE cycle remains displayable while the producer warms the
newest eligible catalog. No partial-run promotion, cross-cycle splice, or clock
renewal. Point/index caches and progress are private and never served publicly.
GEPS supplies fetch_frame(init, lead, collected) and validate_packet(packet, now).
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'var/native-ensemble-runs'
COUNTS = {'gefs': 31, 'ecmwf_ens': 50, 'aifs_ens': 51, 'geps': 21}
REQUIRED_FIELDS = {
    'gefs': ('r2', 'sp', 'msl', '2t', '10u', '10v', 'r1000', 'r925', 'r850', 'tp', 'gust', 'lcc'),
    'ecmwf_ens': ('sp', 'msl', '2t', '2d', '10u', '10v', 'r1000', 'r925', 'r850', 'tp', 'gust'),
    'aifs_ens': ('sp', 'msl', '2t', '2d', '10u', '10v', 'q1000', 't1000', 'q925', 't925', 'q850', 't850', 'tp', 'lcc'),
    'geps': ('r2', 'sp', 'msl', '2t', '10u', '10v', 'r1000', 'r925', 'r850', 'tp'),
}
UNSUPPORTED_FIELDS = {'gefs': (), 'ecmwf_ens': ('lcc',), 'aifs_ens': ('gust',), 'geps': ('gust', 'lcc')}
MAX_PACKET = 80_000_000


def _now():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError('UTC-aware timestamp required')
    return result.astimezone(UTC)


def members(model):
    return [f'{i:02}' for i in (range(1, 51) if model == 'ecmwf_ens' else range(COUNTS[model]))]


def plan_leads(model, init, start, end):
    """Full six-hour range plus the prior cumulative-rain baseline if nonzero."""
    init, start, end = map(utc, (init, start, end))
    if not init <= start < end or end-start > timedelta(days=16):
        raise ValueError('invalid future range')
    first_hour=start.replace(minute=0,second=0,microsecond=0)
    if first_hour<start:first_hour+=timedelta(hours=1)
    first = max(0, math.floor((first_hour-init).total_seconds()/21600)*6)
    last = max(first, math.ceil((end-timedelta(hours=1)-init).total_seconds()/21600)*6)
    # Only an exact closing endpoint requires an earlier cumulative baseline.
    # At lead six the baseline is the intrinsic zero accumulation, not a
    # nonexistent IFS ensemble analysis catalog.
    if model != 'gefs' and first > 6 and first_hour==init+timedelta(hours=first):
        first -= 6
    return list(range(first, last+1, 6))


def required_keys(model, leads):
    # Analysis has no closing accumulation or interval gust. A zero rain
    # baseline is intrinsic, not a fabricated GRIB point.
    return {(m, f, h) for m in members(model) for h in leads for f in REQUIRED_FIELDS[model]
            if h != 0 or (f != 'tp' and not (model=='gefs' and f=='lcc') and not (model=='ecmwf_ens' and f=='gust'))}


def completeness(packet, start=None, end=None, now=None):
    """True only for one fresh complete declared matrix covering the request.

    This checks matrix/run/clock integrity. load_completed additionally invokes
    the native offline validator for GRIB identity, units, source and bounds.
    start is clamped to now: historical samples are not a producer obligation.
    """
    try:
        model = packet['model']
        init = utc(packet['init'])
        now = utc(now or datetime.now(UTC))
        if not timedelta(0) <= now-init <= timedelta(hours=24):
            return False
        if init.minute or init.second or init.microsecond or init.hour % 6:
            return False
        if packet['offered_members'] != members(model):
            return False
        if packet.get('required_fields') != list(REQUIRED_FIELDS[model]):
            return False
        if packet.get('unsupported_fields') != list(UNSUPPORTED_FIELDS[model]):
            return False
        leads = packet['requested_leads']
        if not isinstance(leads, list) or not leads or any(type(h) is not int or h < 0 or h % 6 for h in leads):
            return False
        if leads != list(range(leads[0], leads[-1]+1, 6)) or leads[-1] > max_lead(model, init):
            return False
        if end is not None:
            needed = plan_leads(model, init, max(utc(start or now), now), utc(end))
            if not set(needed) <= set(leads):
                return False
        expected = required_keys(model, leads)
        seen = set()
        for p in packet['points']:
            key = (p['member'], p['field'], p['lead'])
            if key in seen or key not in expected or p['model'] != model or p['init'] != packet['init']:
                return False
            collected, fetched = utc(p['collected_at']), utc(p['fetched_at'])
            if not init <= collected <= fetched <= now+timedelta(minutes=5) or now-collected > timedelta(hours=12):
                return False
            seen.add(key)
        return seen == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def max_lead(model, init):
    return 384 if model == 'gefs' else 144 if model == 'ecmwf_ens' and init.hour in (6, 18) else 384 if model == 'geps' else 360


def _worker(model):
    return importlib.import_module('.direct_geps_worker' if model == 'geps' else '.direct_ensemble_worker', __package__)


def _validate_packet(packet, now):
    if packet['model'] == 'geps':
        return _worker('geps').validate_packet(packet, now)
    from .direct_ensemble import validate_packet
    return validate_packet(packet, packet['model'], now)


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    fd, name = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)
    return raw


def _run_dir(root, model, init):
    return Path(root) / model / utc(init).strftime('%Y%m%d%H')


def promote(packet, start, end, now, *, root=None):
    if not completeness(packet, start, end, now):
        raise ValueError('incomplete native matrix cannot be promoted')
    _validate_packet(packet, now)
    from .native_rain_precision import enrich
    enrich(packet)
    raw = json.dumps(packet, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory = _run_dir(root or CACHE, packet['model'], packet['init'])
    filename = f'packet-{digest}.json'
    _atomic(directory / filename, packet)
    # Manifest is the commit record. Readers cannot observe a half-written pair.
    _atomic(directory / 'complete.json', dict(schema=1, model=packet['model'], init=packet['init'],
        packet=filename, sha256=digest, points=len(packet['points']),
        original_collected_at=min(p['collected_at'] for p in packet['points']),
        original_fetched_at=max(p['fetched_at'] for p in packet['points'])))
    return packet


def load_completed(model, start, end, now, *, root=None):
    """Purely offline newest valid COMPLETE cycle; never merges or renews clocks."""
    if model not in COUNTS:
        return None
    directory = Path(root or CACHE) / model
    cycle=utc(now).replace(hour=utc(now).hour//6*6,minute=0,second=0,microsecond=0)
    paths=[directory/(cycle-timedelta(hours=6*age)).strftime('%Y%m%d%H')/'complete.json' for age in range(5)]
    for manifest_path in paths:
        try:
            if manifest_path.stat().st_size > 8192: continue
            manifest = json.loads(manifest_path.read_text())
            digest = manifest['sha256']
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest): continue
            if manifest['schema'] != 1 or manifest['model'] != model: continue
            if manifest['packet'] != f'packet-{digest}.json': continue
            path = manifest_path.parent / manifest['packet']
            if path.stat().st_size > MAX_PACKET: continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest: continue
            packet = json.loads(raw)
            if packet['model'] != model or packet['init'] != manifest['init']: continue
            if manifest_path.parent.name != utc(packet['init']).strftime('%Y%m%d%H'): continue
            if manifest['points'] != len(packet['points']): continue
            if manifest['original_collected_at'] != min(p['collected_at'] for p in packet['points']): continue
            if manifest['original_fetched_at'] != max(p['fetched_at'] for p in packet['points']): continue
            if model!='gefs' and packet.get('rain_precision_version')!=1:continue
            if completeness(packet, start, end, now):
                _validate_packet(packet, utc(now))
                return packet
        except (OSError, ValueError, KeyError, TypeError, ImportError, OverflowError):
            continue
    return None


def _catalog(worker, model, init, lead, group, root):
    """Worker owns the validated immutable index cache and original clocks."""
    return worker.index(model, init, lead, group)


def discover(model, now, end, *, worker=None, root=None, workers=12):
    """Probe ALL endpoint catalog groups; never infer complete from one member."""
    worker, root = worker or _worker(model), Path(root or CACHE)
    cycle = utc(now).replace(hour=utc(now).hour//6*6, minute=0, second=0, microsecond=0)
    for age in range(5):
        init = cycle-timedelta(hours=6*age)
        if now-init > timedelta(hours=24) or model == 'geps' and init.hour not in (0, 12): continue
        leads = plan_leads(model, init, now, end)
        if leads[-1] > max_lead(model, init): continue
        try:
            if model == 'geps':
                # Grouped GEPS files have all 21 members. Fetching the endpoint
                # validates catalog completeness and seeds its persistent cache.
                points = worker.fetch_frame(init, leads[-1], stamp(now))
                offered = {(p['member'], p['field'], p['lead']) for p in points}
            else:
                offered = set()
                with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    jobs = [pool.submit(_catalog, worker, model, init, leads[-1], g, root) for g in worker.groups(model)]
                    for job in futures.as_completed(jobs):
                        _, ranges = job.result()
                        offered.update((m, f, leads[-1]) for m, f in ranges)
            if required_keys(model, [leads[-1]]) <= offered:
                return init, leads
            # A provider may still be publishing this catalog. Do not freeze a
            # successfully parsed but incomplete endpoint as immutable forever.
            if model != 'geps':
                if hasattr(worker,'invalidate_index'):
                    for group in worker.groups(model):worker.invalidate_index(model,init,leads[-1],group)
        except Exception:
            continue
    raise ValueError(f'no complete eligible endpoint catalog: {model}')


def produce_run(model, init, now, end, *, worker=None, root=None, workers=12):
    """Resume one cycle, globally index first, then dispatch the entire horizon."""
    worker, root = worker or _worker(model), Path(root or CACHE)
    existing = load_completed(model, now, end, now, root=root)
    if existing and existing['init'] == stamp(init): return existing
    leads = plan_leads(model, init, now, end)
    if leads[-1] > max_lead(model, init): raise ValueError('run does not cover endpoint')
    packet: dict = dict(native_version=3, model=model, init=stamp(init), points=[], offered_members=members(model),
        requested_leads=leads, required_fields=list(REQUIRED_FIELDS[model]),
        unsupported_fields=list(UNSUPPORTED_FIELDS[model]))
    directory = _run_dir(root, model, init)
    expected = required_keys(model, leads)
    errors = []
    last_progress = 0.0
    phase = 'indexes'

    def progress(force=False):
        nonlocal last_progress
        if force or time.monotonic()-last_progress > 2:
            _atomic(directory/'progress.json', dict(model=model, init=stamp(init),
                status='partial', phase=phase, requested_leads=leads, expected_points=len(expected),
                validated_points=len(packet['points']), errors=errors[-20:],
                attempted_at=stamp(now), updated_at=stamp(datetime.now(UTC))))
            last_progress = time.monotonic()

    progress(True)
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        if model == 'geps':
            phase = 'frames'
            jobs = [pool.submit(worker.fetch_frame, init, lead, stamp(now), fields=sorted({f for _,f,_ in required_keys(model,[lead])})) for lead in leads]
            for job in futures.as_completed(jobs):
                try: packet['points'].extend(p for p in job.result() if (p['member'], p['field'], p['lead']) in expected)
                except Exception as exc: errors.append(str(exc)[:200])
                progress()
        else:
            index_jobs = {pool.submit(_catalog, worker, model, init, lead, group, root): lead
                    for lead in leads for group in worker.groups(model)}
            work = {}
            ambiguous = set()
            for job in futures.as_completed(index_jobs):
                lead = index_jobs[job]
                try:
                    url, ranges = job.result()
                    for (member, field), span in ranges.items():
                        key = (member, field, lead)
                        if key in expected:
                            if key in work and work[key] != (url, span):
                                ambiguous.add(key)
                                raise ValueError('ambiguous catalog field')
                            work[key] = (url, span)
                except Exception as exc: errors.append(str(exc)[:200])
                progress()
            phase = 'points'
            for key in ambiguous:
                work.pop(key, None)
            for lead in leads:
                if not required_keys(model, [lead]) <= work.keys():
                    if hasattr(worker,'invalidate_index'):
                        for group in worker.groups(model):worker.invalidate_index(model,init,lead,group)
            jobs = [pool.submit(worker.point, model, init, lead, member, field, url, span, stamp(now))
                    for (member, field, lead), (url, span) in sorted(work.items(), key=lambda item: (item[0][2], item[0][0], item[0][1]))]
            for job in futures.as_completed(jobs):
                try: packet['points'].append(job.result())
                except Exception as exc: errors.append(str(exc)[:200])
                progress()
    packet['points'].sort(key=lambda p: (p['lead'], p['member'], p['field']))
    progress(True)
    verified_at = max(utc(now), _now())
    if completeness(packet, now, end, verified_at):
        try:
            result = promote(packet, now, end, verified_at, root=root)
            _atomic(directory/'progress.json', dict(model=model, init=stamp(init), status='complete',
                validated_points=len(packet['points']), expected_points=len(expected)))
            return result
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc)[:200])
            progress(True)
    return None


def default_end(now):
    """Use actual near-term event display end, otherwise nine future days."""
    from .events import upcoming_events
    from .event_ensemble import event_range
    ends = [utc(event_range(event, now)[1]) for event in upcoming_events(now)
            if 0 <= event.days_out(now) <= 12]
    return max(ends) if ends else now+timedelta(days=9)


def _resume_target(model, now, end, previous):
    """Pin useful unfinished work until complete/expired rather than chase cycles."""
    cycle=now.replace(hour=now.hour//6*6,minute=0,second=0,microsecond=0)
    for age in range(5):
        init=cycle-timedelta(hours=6*age)
        if previous and init<=utc(previous['init']):continue
        path=_run_dir(CACHE,model,init)/'progress.json'
        try:
            if path.stat().st_size>16384:continue
            progress=json.loads(path.read_text())
            if progress.get('status')!='partial' or progress.get('validated_points',0)<=0:continue
            if progress['init']!=stamp(init) or progress['model']!=model:continue
            if now-init>timedelta(hours=24):continue
            if plan_leads(model,init,now,end)[-1]>max_lead(model,init):continue
            return init
        except (OSError,KeyError,ValueError,TypeError):continue
    return None


def _run_model(model, end, workers):
    now=datetime.now(UTC).replace(microsecond=0)
    try:
        previous=load_completed(model,now,end,now)
        _atomic(CACHE/'producer-progress.json',dict(model=model,phase='discovery',
            attempted_at=stamp(now),requested_end=stamp(end),display_init=previous['init'] if previous else None))
        worker=_worker(model)
        init=_resume_target(model,now,end,previous)
        if init is None:init,_=discover(model,now,end,worker=worker,workers=workers)
        result=produce_run(model,init,now,end,worker=worker,workers=workers)
        if result is None:
            # Retry missing work once under the same hard per-model budget.
            # Validated point caches keep successes and their original clocks.
            result=produce_run(model,init,datetime.now(UTC).replace(microsecond=0),end,worker=worker,workers=workers)
        print(json.dumps(dict(model=model,status='complete' if result else 'partial',
            target_init=stamp(init),display_init=result['init'] if result else previous['init'] if previous else None)),flush=True)
        if result is None:raise SystemExit(1)
    except Exception as exc:
        print(json.dumps(dict(model=model,status='unavailable',error=str(exc)[:300])),flush=True)
        raise SystemExit(1)


def _model_child(model,end,workers):
    signal.signal(signal.SIGTERM,signal.SIG_DFL)
    _run_model(model,end,workers)


def run_models(models,end,*,seconds,workers):
    """Hard per-model process budgets ensure every source receives a turn."""
    models=list(dict.fromkeys(models));budget=seconds/len(models)
    result=0
    def shutdown(signum,frame):
        raise SystemExit(128+signum)
    previous=signal.signal(signal.SIGTERM,shutdown)
    try:
        for model in models:
            process=multiprocessing.get_context('fork').Process(target=_model_child,args=(model,end,workers))
            try:
                process.start()
                process.join(budget)
                if process.is_alive():
                    process.terminate();process.join(2)
                    if process.is_alive():process.kill();process.join(2)
                    print(json.dumps(dict(model=model,status='deadline',resumable=True)),flush=True)
                    result=124
                elif process.exitcode and result!=124:result=1
            finally:
                # Reap the child before releasing its inherited global flock.
                # A parent-only SIGTERM must not leave unsupervised downloads.
                handler=signal.signal(signal.SIGTERM,signal.SIG_IGN)
                try:
                    if process.pid is not None and process.is_alive():process.kill();process.join(2)
                    process.close()
                finally:signal.signal(signal.SIGTERM,handler)
        return result
    finally:signal.signal(signal.SIGTERM,previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', nargs='+', choices=list(COUNTS), default=list(COUNTS))
    parser.add_argument('--end', type=utc, help='UTC-aware exclusive range end; default actual event end or now+9d')
    parser.add_argument('--seconds', type=int, default=2700)
    parser.add_argument('--workers', type=int, default=12)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 2700 or not 1 <= args.workers <= 32:
        parser.error('--seconds must be 1..2700 and --workers 1..32')
    now = datetime.now(UTC).replace(microsecond=0)
    end = args.end or default_end(now)
    if not now < end <= now+timedelta(days=16): parser.error('--end must be future and within 16 days')
    os.umask(0o077)
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CACHE/'producer.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('native ensemble producer already active; skipped')
            return 0
        return run_models(args.models,end,seconds=args.seconds,workers=args.workers)


if __name__ == '__main__':
    sys.exit(main())
