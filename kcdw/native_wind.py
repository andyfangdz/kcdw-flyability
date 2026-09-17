"""Snapshot-bound optional native wind profiles; no service ecCodes dependency.

Packet: version/snapshot_collected_at/window/models (each source or null).
V2 discovers upstream runs independently; v1 keeps archived aggregator binding.
Validation returns a sanitized packet or None for a broken outer binding.
Source failures never alter publication readiness or another model's evidence.
"""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import UTC, iso_z
from .event_timing import timing_evidence
from .native_runs import discover_run
from .native_wind_worker import CORE, identity, url_for

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'var/native-weather-venv/bin/python'
DATASETS={'gfs':'ncep_gfs025','ifs':'ecmwf_ifs025','aifs_single':'ecmwf_aifs025_single'}
LABELS={'gfs':'Native GFS','ifs':'Native IFS','aifs_single':'Native AIFS Single'}
MAX_BYTES=100_000
NOTES=['Native 0.25-degree nearest grid 41.0N, 74.25W, approximately 14 km from KCDW; not airport observations.',
       '925/850 hPa are pressure levels, not precise AGL heights; terrain masking unavailable. These sparse profiles do not diagnose formal LLWS, turbulence or a low-level jet.',
       'ECMWF six-hour brackets are not direct flight-hour samples; GFS three-hour samples are not continuous coverage. Directions are FROM true north. Missing gust is unknown.']


def _check(ok):
    if not ok: raise ValueError('invalid native wind')


def _time(v):
    t=datetime.fromisoformat(v.replace('Z','+00:00')) if isinstance(v,str) else v
    _check(isinstance(t,datetime) and t.tzinfo is not None and t.utcoffset() is not None)
    return t.astimezone(UTC)


def _fresh(value,now,hours):
    t=_time(value); _check(timedelta(0)<=now-t<=timedelta(hours=hours)); return t


def _window(snapshot,now):
    _fresh(snapshot['collected_at'],_time(now),24)
    e=snapshot['event']; t=timing_evidence(snapshot)
    if t is None or t.get('flight_end') is None:
        raise ValueError('validated flight timing required')
    for k in ('flight_start','flight_end'): _check(re.fullmatch(r'\d{2}:\d{2}',t[k]) is not None)
    a=datetime.fromisoformat(t['date']+'T'+t['flight_start']).replace(tzinfo=ZoneInfo(t['timezone'])).astimezone(UTC)
    b=datetime.fromisoformat(t['date']+'T'+t['flight_end']).replace(tzinfo=ZoneInfo(t['timezone'])).astimezone(UTC)
    _check(timedelta(0)<b-a<=timedelta(hours=3) and type(t['flight_duration_minutes']) is int and (b-a).total_seconds()==t['flight_duration_minutes']*60)
    return dict(slug=e['slug'],start=iso_z(a),end=iso_z(b))


def _selection(snapshot,key,now):
    """Archived v1 selection only: retain historical Open-Meteo semantics."""
    now=_time(now); window=_window(snapshot,now)
    source=snapshot['gfs'] if key=='gfs' else snapshot['event_moisture']['models'][key]
    _check(source.get('ok') is True and not source.get('explicit_last_good'))
    data=source['data']; _check(not data.get('explicit_last_good'))
    fetched=_fresh(data['fetched_at'],now,12)
    collection_limit=_time(snapshot['collected_at'])+timedelta(minutes=5)
    _check(fetched<=collection_limit)
    meta=data['metadata']['datasets'][DATASETS[key]]
    _check(meta['endpoint']==f'https://api.open-meteo.com/data/{DATASETS[key]}/static/meta.json')
    _check(_fresh(meta['fetched_at'],now,12)<=collection_limit)
    init=_fresh(meta['latest_advertised_init'],now,24)
    _check(init.minute==init.second==init.microsecond==0 and init.hour in (0,6,12,18))
    _check(init<=_time(meta['latest_advertised_available_at'])<=fetched<=now)
    end=_time(meta['data_end_time']); _check(end>init)
    a,b=_time(window['start']),_time(window['end'])
    # Only infer a candidate preceding long IFS cycle when actual short-cycle
    # metadata cannot reach the mission; the GRIB/index must then prove it.
    if key=='ifs' and init.hour in (6,18) and b>end:
        _check(timedelta(hours=144)<=end-init<=timedelta(hours=147))
        init-=timedelta(hours=6)
    else:
        _check(b<=end)
    _fresh(init,now,24)
    cadence=3 if key=='gfs' else 6
    lo=math.floor((a-init).total_seconds()/3600/cadence)*cadence
    hi=math.ceil((b-init).total_seconds()/3600/cadence)*cadence
    leads=list(range(lo,hi+1,cadence))
    maxlead=384 if key=='gfs' else 144 if key=='ifs' and init.hour in (6,18) else 360
    _check(1<=len(leads)<=3 and 0<=lo<=hi<=maxlead)
    return init,leads


def _keys(obj,keys):
    _check(isinstance(obj,dict) and set(obj)==set(keys.split()))


def _persisted_selection(snapshot,key,stamp,now):
    """V2 uses persisted run + immutable snapshot clock; never discovers runs."""
    clock=_time(snapshot['collected_at'])
    init=_fresh(stamp,clock,24)
    _fresh(init,_time(now),24)
    _check(stamp==iso_z(init) and init.minute==init.second==init.microsecond==0 and init.hour in (0,6,12,18))
    window=_window(snapshot,now)
    a,b=_time(window['start']),_time(window['end'])
    cadence=3 if key=='gfs' else 6
    lo=math.floor((a-init).total_seconds()/3600/cadence)*cadence
    hi=math.ceil((b-init).total_seconds()/3600/cadence)*cadence
    leads=list(range(lo,hi+1,cadence))
    maxlead=384 if key=='gfs' else 144 if key=='ifs' and init.hour in (6,18) else 360
    _check(1<=len(leads)<=3 and 0<=lo<=hi<=maxlead)
    return init,leads


def _validate_source(m,snapshot,key,now,version=1):
    _keys(m,'model init fetched_at samples')
    init,leads=(_selection(snapshot,key,now) if version==1 else _persisted_selection(snapshot,key,m['init'],now))
    _check(m['model']==key and m['init']==iso_z(init))
    fetched=_fresh(m['fetched_at'],_time(now),12)
    _check(init<=fetched<=_time(snapshot['collected_at'])+(timedelta(minutes=5) if version==1 else timedelta(0)))
    _check(isinstance(m['samples'],list) and len(m['samples'])==len(leads))
    for s,lead in zip(m['samples'],leads):
        _keys(s,'at lead url fields')
        _check(type(s['lead']) is int and s['lead']==lead and s['at']==iso_z(init+timedelta(hours=lead)))
        _check(s['url']==url_for(key,init,lead,legacy=version==1))
        fs=s['fields']; _check(isinstance(fs,dict) and set(CORE)<=set(fs)<=set(CORE)|({'gust'} if key!='aifs_single' else set()))
        for name,f in fs.items():
            _keys(f,'identity value latitude longitude proof')
            _check(f['identity']==identity(key,name,init,lead,legacy=version==1) and f['latitude']==41.0 and f['longitude']==-74.25)
            v=f['value']; _check(type(v) in (int,float) and math.isfinite(v) and (-200 if name!='gust' else 0)<=v<=200)
            p=f['proof']; _keys(p,'start end total bytes sha256')
            _check(all(type(p[k]) is int for k in ('start','end','total','bytes')))
            _check(0<=p['start']<=p['end']<p['total']<=1_000_000_000 and 0<p['bytes']==p['end']-p['start']+1<=8_000_000)
            _check(isinstance(p['sha256'],str) and re.fullmatch('[0-9a-f]{64}',p['sha256']) is not None)
    _check(len(json.dumps(m,allow_nan=False))<=MAX_BYTES)
    return m


def validate_native_wind(packet,snapshot,now):
    try:
        _keys(packet,'version snapshot_collected_at window models')
        _check(type(packet['version']) is int and packet['version'] in (1,2))
        _check(packet['snapshot_collected_at']==snapshot['collected_at'] and packet['window']==_window(snapshot,now))
        _keys(packet['models'],'gfs ifs aifs_single')
        out=dict(packet,models={})
        for key in DATASETS:
            try: out['models'][key]=_validate_source(packet['models'][key],snapshot,key,now,packet['version'])
            except Exception: out['models'][key]=None
        return out
    except Exception:
        return None


def _safe(path):
    path=Path(path).absolute(); _check(not any(p.is_symlink() for p in (path,*path.parents))); return path


def _run_worker(request):
    r=subprocess.run([str(PYTHON),str(ROOT/'kcdw/native_wind_worker.py')],input=json.dumps(request),text=True,capture_output=True,check=True,timeout=112)
    _check(len(r.stdout)<=MAX_BYTES)
    return json.loads(r.stdout)


def collect_native_wind(snapshot,cache_dir,now):
    """Bounded upstream discovery then three isolated <=112s worker invocations.

    Cache identity includes actual run + mission. Rebinding never renews fetched_at.
    Both fetch TTL (12h) and run TTL (24h) apply during collection AND rendering.
    """
    try:
        window=_window(snapshot,now)
        directory=_safe(cache_dir); directory.mkdir(parents=True,exist_ok=True)
    except Exception: return None
    def one(key):
        try:
            # Always rediscover before cache lookup; Open-Meteo is not an input.
            run=discover_run(key,_time(window['start']),_time(window['end']),_time(now),kind='wind')
            if run is None: return None
            _keys(run,'init leads')
            init,leads=_persisted_selection(snapshot,key,run['init'],now)
            _check(isinstance(run['leads'],list) and all(type(h) is int for h in run['leads']) and run['leads']==leads)
            digest=hashlib.sha256(json.dumps([key,iso_z(init),window],sort_keys=True).encode()).hexdigest()[:24]
            path=_safe(directory/f'wind-v2-{digest}.json')
            if path.exists() and path.stat().st_size<=MAX_BYTES:
                try:
                    cached=json.loads(path.read_text())
                    _keys(cached,'version snapshot_collected_at source')
                    _check(type(cached['version']) is int and cached['version']==2)
                    original=dict(snapshot,collected_at=cached['snapshot_collected_at'])
                    source=_validate_source(cached['source'],original,key,now,2)
                    _check(source['init']==run['init'])
                    return _validate_source(source,snapshot,key,now,2)
                except Exception: pass
            samples=_run_worker(dict(model=key,init=iso_z(init),leads=leads))
            m=dict(model=key,init=iso_z(init),fetched_at=iso_z(_time(now)),samples=samples)
            _validate_source(m,snapshot,key,now,2)
            fd,name=tempfile.mkstemp(prefix='.wind-',dir=directory)
            try:
                with os.fdopen(fd,'w') as f:
                    json.dump(dict(version=2,snapshot_collected_at=snapshot['collected_at'],source=m),f,allow_nan=False,separators=(',',':')); f.flush(); os.fsync(f.fileno())
                _safe(path); os.replace(name,path)
            finally:
                if os.path.exists(name): os.unlink(name)
            return m
        except Exception: return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        models=dict(zip(DATASETS,pool.map(one,DATASETS)))
    return dict(version=2,snapshot_collected_at=snapshot['collected_at'],window=window,models=models)


def native_wind_evidence(snapshot,now):
    packet=validate_native_wind(snapshot.get('native_wind'),snapshot,now)
    if packet is None: return None
    result=dict(window={k:packet['window'][k] for k in ('start','end')},models={},notes=NOTES)
    for key,m in packet['models'].items():
        if m is None:
            result['models'][key]=None; continue
        out=dict(label=LABELS[key],init=m['init'],fetched_at=m['fetched_at'],samples=[],notes=['GRIB/index run-bound; six-hour brackets.' if key!='gfs' else 'GRIB/index run-bound; three-hour samples.'])
        for s in m['samples']:
            f=s['fields']; row={'at':s['at']}
            for label,u,v in [('surface','10u','10v'),('925','u925','v925'),('850','u850','v850')]:
                x,y=f[u]['value'],f[v]['value']
                row[label]=dict(speed_kt=round(math.hypot(x,y)*1.943844492,1),from_true_deg=round(math.degrees(math.atan2(-x,-y))%360,1)%360)
            row['gust']=None
            if 'gust' in f:
                g=f['gust']; ident=g['identity']; init=_time(m['init'])
                row['gust']=dict(kt=round(g['value']*1.943844492,1),start=iso_z(init+timedelta(hours=ident['startStep'])),end=s['at'],semantics='six-hour maximum' if key=='ifs' else 'instantaneous gust diagnostic')
            out['samples'].append(row)
        result['models'][key]=out
    _check(len(json.dumps(result,allow_nan=False).encode())<=5000)
    return result
