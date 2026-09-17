"""Bounded public GRIB worker; stdout is flushed native sample JSON lines.

Parent hard timeout kills all downloader threads. Partial validated samples are
retained as gaps, never extrapolated, and reused with original fetch timestamps.
"""
import concurrent.futures as futures
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta
from .direct_deterministic import (UTC, require, parse, stamp, candidate_runs, native_leads,
    field_specs, expected_identity, url_for, derived_rh)
from .native_wind_worker import fetch


def indexed_ranges(model,text,init,lead):
    specs=field_specs(model); found={}; rain_start=None
    if model=='gfs':
        rows=[r.split(':') for r in text.splitlines()]
        require(1<len(rows)<=1500 and all(len(r)>=6 for r in rows))
        offsets=[int(r[1]) for r in rows]; require(offsets==sorted(set(offsets)) and offsets[0]==0)
        for i,row in enumerate(rows[:-1]):
            for name,spec in specs.items():
                if row[3:5]==list(spec[:2]):
                    if name=='rain':
                        match=re.fullmatch(r'(\d+)-(\d+) (hour|day) acc fcst',row[5])
                        if not match: continue
                        factor=24 if match[3]=='day' else 1
                        if int(match[2])*factor!=lead: continue
                        begin=int(match[1])*factor; require(0<=begin<lead)
                        if rain_start is not None and begin>=rain_start: continue
                        rain_start=begin
                        found.pop(name,None)
                    elif row[5] != (f'{lead} hour fcst' if lead else 'anl'):
                        continue
                    require(row[2]==f'd={init:%Y%m%d%H}' and name not in found)
                    found[name]=(offsets[i],offsets[i+1]-1)
    else:
        rows=[json.loads(r) for r in text.splitlines()]; require(1<len(rows)<=1500)
        for row in rows:
            for name,spec in specs.items():
                if row.get('param')!=spec[0]: continue
                if spec[2]=='isobaricInhPa' and row.get('levelist')!=str(spec[3]): continue
                require(row.get('date')==init.strftime('%Y%m%d') and row.get('time')==init.strftime('%H%M'))
                require(row.get('step')==str(lead) and row.get('type')=='fc' and row.get('domain')=='g')
                require(row.get('class')==('ai' if model=='aifs_single' else 'od') and row.get('stream')=='oper')
                require(row.get('levtype')==('pl' if spec[2]=='isobaricInhPa' else 'sfc'))
                if model=='aifs_single': require(row.get('model')=='aifs-single')
                require(name not in found)
                a,n=row['_offset'],row['_length']; require(type(a) is int and type(n) is int)
                found[name]=(a,a+n-1)
    require(('q850' if model=='aifs_single' else 'r850') in found and 'sp' in found)
    for a,b in found.values(): require(0<=a<=b<1_000_000_000 and b-a+1<=8_000_000)
    return found


def decode(raw,model,name,init,lead):
    import eccodes as ec
    require(raw[:4]==b'GRIB' and raw[-4:]==b'7777' and int.from_bytes(raw[8:16],'big')==len(raw))
    g=ec.codes_new_from_message(raw)
    try:
        start=int(ec.codes_get(g,'startStep')); step=ec.codes_get(g,'stepType')
        require(0<=start<=lead and (name=='rain' or start==lead))
        require(step==('accum' if name=='rain' else 'instant'))
        identity=expected_identity(model,name,init,lead,start,step)
        require(all(ec.codes_get(g,k)==v for k,v in identity.items()))
        p=ec.codes_grib_find_nearest(g,40.8752,285.7186)[0]
        lat=float(p['lat']); lon=(float(p['lon'])+180)%360-180
        require(lat==41.0 and lon==-74.25)
        v=float(p['value']); low,high=field_specs(model)[name][-2:]
        if v==ec.codes_get(g,'missingValue') or not math.isfinite(v) or not low<=v<=high:
            v=None
        if ec.codes_get(g,'bitmapPresent') and not ec.codes_get_array(g,'bitmap')[int(p['index'])]: v=None
        return dict(identity=identity,value=v,latitude=lat,longitude=lon)
    finally: ec.codes_release(g)


def index(model,init,lead):
    url=url_for(model,init,lead)
    text,_=fetch(url+'.idx' if model=='gfs' else url[:-6]+'.index',300_000)
    return indexed_ranges(model,text.decode('ascii'),init,lead)


def sample(model,init,lead,ranges=None):
    ranges=ranges or index(model,init,lead); url=url_for(model,init,lead)
    fields={}
    # Required moisture first; optional diagnostics cannot erase valid RH.
    names=sorted(ranges,key=lambda n:(n not in ('r850','r925','r1000','sp'),n))
    for name in names:
        try:
            raw,proof=fetch(url,8_000_000,ranges[name])
            fields[name]=dict(decode(raw,model,name,init,lead),proof=proof)
        except Exception: pass
    if model=='aifs_single':
        for level in (1000,925,850):
            if 'q'+str(level) in fields and 't'+str(level) in fields:
                fields['r'+str(level)]=derived_rh(fields,level)
    require(fields)
    return dict(lead=lead,at=stamp(init+timedelta(hours=lead)),url=url,
                fetched_at=stamp(datetime.now(UTC)),fields=fields)


def collection_leads(model,first,last):
    leads=native_leads(model,first,last)
    if model=='gfs' and leads[0]>0:leads.insert(0,leads[0]-3)
    return leads


def humidity_complete(sample):
    fields=sample['fields']; pressure=fields.get('sp',{}).get('value')
    return pressure is not None and all(pressure<level*100 or fields.get('r'+str(level),{}).get('value') is not None
                                       for level in (1000,925,850))


def main():
    request=json.loads(sys.stdin.read(20_000_000))
    model=request['model']; now=parse(request['now']); start=parse(request['start']); end=parse(request['end'])
    deadline=time.monotonic()+request['timeout']-3
    chosen=None; discovered=None
    for init in candidate_runs(model,now,end):
        try:
            leads=collection_leads(model,(start-init).total_seconds()/3600,(end-timedelta(hours=1)-init).total_seconds()/3600)
            discovered=index(model,init,leads[-1]); chosen=(init,leads); break
        except Exception: pass
        if time.monotonic()>deadline: return
    require(chosen is not None)
    init,leads=chosen
    header=dict(version=2,kind='direct-deterministic',model=model,initialization_time=stamp(init),
                collected_at=stamp(now),collection_started_at=stamp(now),source_provider='NOAA' if model=='gfs' else 'ECMWF')
    print(json.dumps(header),flush=True)
    cached={}
    for packet in request.get('cached',[]):
        if packet['initialization_time']==stamp(init):
            cached.update({s['lead']:s for s in packet['samples']})
    # A previously interrupted lead must be retried, not permanently cached as a gap.
    cached={h:s for h,s in cached.items()
            if (set(field_specs(model))-({'rain'} if h==0 else set()))<=set(s['fields'])
            and humidity_complete(s)
            and (model!='gfs' or h==0 or s['fields'].get('rain',{}).get('identity',{}).get('startStep')==0)}
    for lead in leads:
        if lead in cached: print(json.dumps(cached[lead]),flush=True)
    missing=[h for h in leads if h not in cached]
    # Event near end minus two days gets first priority, then whole-range anchors.
    mission=(end-timedelta(days=2)-init).total_seconds()/3600
    missing=sorted(missing,key=lambda h:(0 if abs(h-mission)<=24 else 1 if h in (leads[0],leads[-1]) else 2,abs(h-mission)))
    pool=futures.ThreadPoolExecutor(max_workers=12)
    jobs={pool.submit(sample,model,init,h,discovered if h==leads[-1] else None):h for h in missing}
    try:
        for job in futures.as_completed(jobs,timeout=max(.1,deadline-time.monotonic())):
            try: print(json.dumps(job.result(),allow_nan=False),flush=True)
            except Exception: pass
    except futures.TimeoutError: pass
    pool.shutdown(wait=False,cancel_futures=True)
    # Do not wait for running requests during interpreter thread cleanup.
    sys.stdout.flush()
    import os
    os._exit(0)

if __name__=='__main__':
    try: main()
    except Exception: sys.exit(1)
