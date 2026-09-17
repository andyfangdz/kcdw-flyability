"""Optional native GFS ceiling diagnostic. No ecCodes dependency in the service.

New runs are discovered directly from NOAA; decoded native messages bind the
diagnostic to that run. Legacy v1 packets retain advertised-metadata validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .common import UTC, iso_z
from .events import _event, TZ
from .gfs_guidance import validate_gfs, _timestamp, _datetime
from .native_runs import discover_run

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / 'var/native-weather-venv/bin/python'
NOTES = ('Native operational GFS 0.25-degree, instantaneous three-hour samples; closing endpoint included when exact. '
         'HGT cloud ceiling is MSL geopotential height (gpm); approximate AGL feet subtract colocated model terrain (m), '
         'then multiply by 3.280839895. Not an observed airport ceiling or calibrated probability. '
         'Missing/sentinel ceiling is unknown, never clear. LCDC is instantaneous low-cloud fraction, not ceiling. '
         'Neighborhood: 25 grid cells within +/-0.5 degree of the nearest cell; counts are spatial samples, not probabilities.')
MAX_BYTES = 64000


def _check(value):
    if not value:
        raise ValueError('invalid native ceiling')


def _source_url(init, lead):
    return (f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{init:%Y%m%d}/{init:%H}/atmos/'
            f'gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}')


def _context(snapshot, now):
    now = _datetime(now)
    collected = _timestamp(snapshot['collected_at'])
    _check(timedelta(0) <= now-collected <= timedelta(hours=24))
    source = snapshot['gfs']
    _check(validate_gfs(source, now)['available'])
    init = _timestamp(source['data']['metadata']['datasets']['ncep_gfs025']['latest_advertised_init'])
    event = _event(snapshot['event'])
    midnight = datetime.combine(event.day, datetime.min.time(), TZ)
    start = (midnight+timedelta(hours=event.start_hour)).astimezone(UTC)
    end = (midnight+timedelta(hours=event.end_hour)).astimezone(UTC)
    leads = [h for h in range(0,385,3) if start <= init+timedelta(hours=h) <= end]
    _check(0 < len(leads) <= 9)
    return init, {'slug':event.slug, 'start':iso_z(start), 'end':iso_z(end)}, leads


def _native_window(snapshot, now):
    collected = _timestamp(snapshot['collected_at'])
    _check(timedelta(0) <= _datetime(now)-collected <= timedelta(hours=24))
    event = _event(snapshot['event'])
    midnight = datetime.combine(event.day, datetime.min.time(), TZ)
    start = (midnight+timedelta(hours=event.start_hour)).astimezone(UTC)
    end = (midnight+timedelta(hours=event.end_hour)).astimezone(UTC)
    return {'slug':event.slug, 'start':iso_z(start), 'end':iso_z(end)}


def _native_context(snapshot, model_init, now):
    """Pure persisted run/window checks, with no rolling metadata or discovery."""
    now = _datetime(now)
    event = _native_window(snapshot, now)
    init = _timestamp(model_init)
    _check(init.hour in (0,6,12,18) and init.minute == init.second == init.microsecond == 0)
    _check(timedelta(0) <= now-init <= timedelta(hours=24))
    _check(timedelta(0) <= _timestamp(snapshot['collected_at'])-init <= timedelta(hours=24))
    start, end = _timestamp(event['start']), _timestamp(event['end'])
    lo = math.ceil((start-init).total_seconds()/10800)*3
    hi = math.floor((end-init).total_seconds()/10800)*3
    _check(0 <= lo <= hi <= 384)
    leads = list(range(lo,hi+1,3))
    _check(0 < len(leads) <= 9)
    return init,event,leads


def _keys(obj, keys):
    _check(isinstance(obj,dict) and set(obj)==set(keys.split()))


def _number(value, low, high, nullable=False):
    if nullable and value is None:
        return
    _check(type(value) in (int,float) and math.isfinite(value) and low <= value <= high)


def validate_ceiling(envelope, snapshot, now):
    """Return the original allowlisted envelope, or None; never trust persisted data."""
    try:
        e=envelope
        _keys(e,'version model model_init fetched_at snapshot_collected_at event notes samples')
        _check(type(e['version']) is int and e['version'] in (1,2) and e['model']=='gfs_native_025')
        init,event,leads = (_context(snapshot,now) if e['version']==1 else
                            _native_context(snapshot,e['model_init'],now))
        _check(e['model_init']==iso_z(init) and e['event']==event and e['notes']==NOTES)
        _check(e['snapshot_collected_at']==snapshot['collected_at'])
        fetched=_timestamp(e['fetched_at'])
        _check(init <= fetched <= _datetime(now) and _datetime(now)-fetched <= timedelta(hours=24))
        if e['version']==2:
            _check(fetched <= _timestamp(snapshot['collected_at']))
        _check(isinstance(e['samples'],list) and len(e['samples'])==len(leads))
        for s,lead in zip(e['samples'],leads):
            _keys(s,'lead_hour valid_at source_url latitude longitude ceiling_msl_gpm terrain_m ceiling_agl_ft low_cloud_pct neighborhood fields')
            _check(type(s['lead_hour']) is int and s['lead_hour']==lead)
            _check(s['valid_at']==iso_z(init+timedelta(hours=lead)) and s['source_url']==_source_url(init,lead))
            _number(s['latitude'],40.75,41.0); _number(s['longitude'],-74.5,-74.25)
            _check(s['latitude']==41.0 and s['longitude']==-74.25)
            for field,lo,hi in [('ceiling_msl_gpm',-500,30000),('terrain_m',-500,9000),('ceiling_agl_ft',0,100000),('low_cloud_pct',0,100)]:
                _number(s[field],lo,hi,True)
            c,t,a=s['ceiling_msl_gpm'],s['terrain_m'],s['ceiling_agl_ft']
            if c is None or t is None:
                _check(a is None)
            else:
                _check(a is not None and abs(a-max(0,(c-t)*3.280839895))<0.01)
            n=s['neighborhood']
            _keys(n,'cells ceiling_valid ceiling_missing under_1000_ft agl_ft_min agl_ft_max low_cloud_valid low_cloud_pct_min low_cloud_pct_max')
            for k in ('cells','ceiling_valid','ceiling_missing','under_1000_ft','low_cloud_valid'):
                _check(type(n[k]) is int and 0 <= n[k] <= 25)
            _check(n['cells']==25 and n['ceiling_missing']==25-n['ceiling_valid'] and n['under_1000_ft']<=n['ceiling_valid'])
            for count,prefix,high in [('ceiling_valid','agl_ft',100000),('low_cloud_valid','low_cloud_pct',100)]:
                lo,hi=n[prefix+'_min'],n[prefix+'_max']
                if not n[count]:
                    _check(lo is None and hi is None)
                else:
                    _number(lo,0,high); _number(hi,0,high); _check(lo<=hi)
            _keys(s['fields'],'ceiling terrain low_cloud')
            for proof in s['fields'].values():
                _keys(proof,'start end total bytes sha256')
                for k in ('start','end','total','bytes'):
                    _check(type(proof[k]) is int and 0 <= proof[k] <= 1_000_000_000)
                _check(0 < proof['bytes']==proof['end']-proof['start']+1 <= 8_000_000 and proof['end']<proof['total'])
                _check(isinstance(proof['sha256'],str) and re.fullmatch('[0-9a-f]{64}',proof['sha256']) is not None)
        _check(len(json.dumps(e,allow_nan=False))<=MAX_BYTES)
        return e
    except Exception:
        return None


def _safe_path(path):
    path=path.absolute()
    _check(not any(p.is_symlink() for p in (path,*path.parents)))
    return path


def _run_worker(request):
    # All URLs are constructed by the worker, never accepted from input.
    result=subprocess.run([str(PYTHON),str(ROOT/'kcdw/native_ceiling_worker.py')],
                          input=json.dumps(request),text=True,capture_output=True,timeout=88,check=True)
    _check(len(result.stdout)<=MAX_BYTES)
    return json.loads(result.stdout)


def collect_ceiling(snapshot, cache_dir: Path, now):
    """Best effort, bounded <=90s subprocess; reuse same run/event for 12h.

    Fetch time remains actual retrieval time; snapshot binding is refreshed on
    reuse. Display fetch TTL is 24h, independent of the 12h reuse policy.
    """
    try:
        now=_datetime(now)
        event=_native_window(snapshot,now)
        selected=discover_run('gfs',_timestamp(event['start']),_timestamp(event['end']),now,kind='ceiling')
        if not isinstance(selected,dict):
            return None
        init,event,leads=_native_context(snapshot,selected['init'],now)
        _check(isinstance(selected['leads'],list) and all(type(h) is int for h in selected['leads'])
               and selected['leads']==leads)
        directory=_safe_path(Path(cache_dir)); directory.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(json.dumps([iso_z(init),event],sort_keys=True).encode()).hexdigest()[:24]
        path=_safe_path(directory/f'ceiling-{key}.json')
        if path.exists() and path.stat().st_size<=MAX_BYTES:
            try:
                cached=json.loads(path.read_text())
                # Validate its original immutable collection before rebinding.
                # Never upgrade v1 provenance, or trust the filename as run proof.
                _check(cached['version']==2 and cached['model_init']==iso_z(init))
                original=dict(snapshot,collected_at=cached['snapshot_collected_at'])
                if validate_ceiling(cached,original,now) and now-_timestamp(cached['fetched_at'])<=timedelta(hours=12):
                    cached['snapshot_collected_at']=snapshot['collected_at']
                    if validate_ceiling(cached,snapshot,now):
                        return cached
            except (ValueError,TypeError,KeyError):
                pass
        samples=_run_worker({'model_init':iso_z(init),'leads':leads})
        # 'now' is the collector's retrieval clock, not the snapshot clock.
        envelope=dict(version=2,model='gfs_native_025',model_init=iso_z(init),fetched_at=iso_z(now),
                      snapshot_collected_at=snapshot['collected_at'],event=event,notes=NOTES,samples=samples)
        _check(validate_ceiling(envelope,snapshot,now) is not None)
        _safe_path(path)
        fd,name=tempfile.mkstemp(prefix='.ceiling-',dir=directory)
        try:
            with os.fdopen(fd,'w') as out:
                json.dump(envelope,out,allow_nan=False,separators=(',',':'))
                out.flush(); os.fsync(out.fileno())
            _safe_path(path); os.replace(name,path)
        finally:
            if os.path.exists(name): os.unlink(name)
        return envelope
    except Exception:
        return None
