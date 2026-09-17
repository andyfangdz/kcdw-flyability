"""Direct NOAA/ECMWF native-run discovery, independent of aggregators.

Probe only bounded native indexes for the required mission samples. An index is
not proof of a complete GRIB: collectors must still download and validate every
required message. Discovery is collection-only; persisted renderers never call it.
"""
from __future__ import annotations

import concurrent.futures
import math
import time
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

MAX_INDEX = 250_000
DISCOVERY_SECONDS = 32
PROCESS_TIMEOUT = 36
MODELS = ('gfs', 'ifs', 'aifs_single')


def _require(ok):
    if not ok:
        raise ValueError('invalid native run discovery')


def _utc(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)
    return value.astimezone(timezone.utc)


def candidate_runs(model, start, end, now, kind='wind'):
    """Newest-first <=24-hour candidates with a whole eligible sample axis."""
    _require(model in MODELS and kind in ('wind', 'ceiling'))
    _require(kind != 'ceiling' or model == 'gfs')
    start, end, now = map(_utc, (start, end, now))
    _require(timedelta(0) < end-start <= timedelta(hours=24))
    cycle=now.replace(hour=(now.hour//6)*6, minute=0, second=0, microsecond=0)
    out=[]
    for age in range(5):
        init=cycle-timedelta(hours=age*6)
        if now-init > timedelta(hours=24):
            continue
        cadence=3 if model=='gfs' else 6
        low=(start-init).total_seconds()/3600/cadence
        high=(end-init).total_seconds()/3600/cadence
        if kind=='ceiling':
            lo,hi=math.ceil(low),math.floor(high)
        else:
            lo,hi=math.floor(low),math.ceil(high)
        maximum=384 if model=='gfs' else 144 if model=='ifs' and init.hour in (6,18) else 360
        leads=list(range(lo*cadence,hi*cadence+1,cadence))
        if not leads or leads[0]<0 or leads[-1]>maximum or len(leads)>(9 if kind=='ceiling' else 3):
            continue
        out.append({'init':init.strftime('%Y-%m-%dT%H:%M:%SZ'), 'leads':leads})
    return out


def _read_index(url, deadline):
    import requests
    remaining=deadline-time.monotonic()
    _require(remaining>0)
    with requests.get(url, timeout=(min(2,remaining),min(4,remaining)),
                      stream=True, allow_redirects=False) as response:
        _require(response.status_code==200)
        content=bytearray()
        for block in response.iter_content(65536):
            content.extend(block)
            _require(len(content)<=MAX_INDEX and time.monotonic()<=deadline)
        _require(content and time.monotonic()<=deadline)
        return content.decode('ascii')


def _probe(model, candidate, kind, deadline):
    from .native_wind_worker import url_for, indexed_ranges
    from .native_ceiling_worker import indexed_ranges as ceiling_ranges
    init=datetime.strptime(candidate['init'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    def one(lead):
        url=url_for(model,init,lead)
        index_url=url+'.idx' if model=='gfs' else url[:-6]+'.index'
        text=_read_index(index_url,deadline)
        if kind=='ceiling':
            ceiling_ranges(text,init,lead)
        else:
            indexed_ranges(model,text,init,lead)
        return True
    # Three bounded concurrent requests; no public bucket enumeration or GRIB
    # bulk download. All required samples must validate, including middle hours.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(one,lead) for lead in candidate['leads']]
        return all(future.result() for future in futures)


def _discover_run(model, start, end, now, kind='wind'):
    """Newest eligible native index set, or None; source failures stay local."""
    try:
        candidates=candidate_runs(model,start,end,now,kind)
    except (ValueError,TypeError,OverflowError):
        return None
    deadline=time.monotonic()+DISCOVERY_SECONDS
    for candidate in candidates:
        if time.monotonic()>=deadline:
            break
        try:
            if _probe(model,candidate,kind,deadline) and time.monotonic()<=deadline:
                return candidate
        except Exception:
            continue
    return None


def discover_run(model, start, end, now, kind='wind'):
    """Hard process deadline includes slow/dripping HTTP and worker shutdown."""
    try:
        candidates=candidate_runs(model,start,end,now,kind)
        if not candidates:
            return None
        request=dict(model=model,start=_utc(start).isoformat(),end=_utc(end).isoformat(),
                     now=_utc(now).isoformat(),kind=kind)
        result=subprocess.run([sys.executable,'-m','kcdw.native_runs'],
                              cwd=Path(__file__).resolve().parents[1],
                              input=json.dumps(request),capture_output=True,text=True,
                              timeout=PROCESS_TIMEOUT,check=True)
        _require(len(result.stdout)<=4096)
        selected=json.loads(result.stdout)
        if selected is None:
            return None
        _require(isinstance(selected,dict) and set(selected)=={'init','leads'})
        _require(isinstance(selected['leads'],list) and all(type(h) is int for h in selected['leads']))
        _require(selected in candidates)
        return selected
    except Exception:
        return None


if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.read(4097))
        _require(isinstance(request,dict) and set(request)=={'model','start','end','now','kind'})
        for key in ('start','end','now'):
            request[key]=datetime.fromisoformat(request[key])
        print(json.dumps(_discover_run(**request),separators=(',',':')))
    except Exception:
        sys.exit(1)
