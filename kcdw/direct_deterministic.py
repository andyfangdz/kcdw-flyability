"""Direct deterministic profiles, optional isolated ecCodes collection.

Public integration: collect_native_profile(model,start,end,now,timeout=165) returns
an immutable native proof packet. normalize_profile(packet,start,end,purpose)
returns existing hourly schemas (purpose='moisture' or 'gfs'). validate_direct_data
reconstructs normalized values offline. Production activation belongs to caller;
fixtures remain legacy unless client.direct_native is exactly True.
"""
from __future__ import annotations
import bisect
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

UTC=timezone.utc
ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'var/direct-deterministic'
FALLBACK='Direct native source unavailable or invalid'
PROVENANCE='direct-native'
SAMPLING=('Native nearest-point GRIB; instantaneous fields linearly interpolated to the hourly display only between adjacent selected steps, never across missing selected samples. GFS sampled every 3 hours (native hourly through 120h then 3-hourly); IFS 3-hourly through 144h then 6-hourly; AIFS 6-hourly. Rain is uniformly allocated across its native accumulation interval for hourly display: estimated hourly and partial-window amounts, not independent hourly forecasts. Native interval amounts and bounds are retained. GFS low cloud uses the exact instantaneous field, not its interval-average duplicate. Native and derived RH supersaturation is retained without clamping.')

def require(ok):
    if not ok: raise ValueError('invalid direct deterministic evidence')

def stamp(t): return t.astimezone(UTC).isoformat().replace('+00:00','Z')
def parse(s):
    t=datetime.fromisoformat(s.replace('Z','+00:00'))
    require(t.tzinfo is not None and stamp(t)==s)
    return t

def candidate_runs(model,now,end):
    require(model in ('gfs','ifs','aifs_single') and now.tzinfo is not None)
    base=now.astimezone(UTC).replace(hour=now.hour//6*6,minute=0,second=0,microsecond=0)
    return [t for i in range(5) if (t:=base-timedelta(hours=6*i))<=now and
            end<=t+timedelta(hours=384 if model=='gfs' else 144 if model=='ifs' and t.hour in (6,18) else 360)]

def native_leads(model,first,last):
    limit=384 if model=='gfs' else 360
    leads=list(range(0,385,3)) if model=='gfs' else list(range(0,145,3))+list(range(150,361,6)) if model=='ifs' else list(range(0,361,6))
    require(0<=last<=limit)
    a=max(0,bisect.bisect_right(leads,max(0,first))-1)
    b=min(len(leads),bisect.bisect_left(leads,last)+1)
    return leads[a:b]

def align_hourly(axis,values,target):
    out=[]
    for t in target:
        i=bisect.bisect_left(axis,t)
        if i<len(axis) and axis[i]==t: out.append(values[i]); continue
        if not 0<i<len(axis) or values[i-1] is None or values[i] is None:
            out.append(None); continue
        out.append(values[i-1]+(values[i]-values[i-1])*(t-axis[i-1])/(axis[i]-axis[i-1]))
    return out

def rain_hourly(samples,target):
    intervals=[]; previous={}
    for s in samples:
        f=s['fields'].get('rain')
        if not f or f['value'] is None: continue
        a,b=f['identity']['startStep'],f['identity']['endStep']; value=f['value']
        old=previous.get(a)
        previous[a]=(b,value)
        if old and old[0]<b:
            a,value=old[0],value-old[1]
        if b>a and value>=-0.001:
            require(not intervals or a>=intervals[-1]['end_lead'])
            intervals.append(dict(start_lead=a,end_lead=b,amount_mm=max(0,value)))
    hourly={h:r['amount_mm']/(r['end_lead']-r['start_lead']) for r in intervals for h in range(r['start_lead']+1,r['end_lead']+1)}
    return [hourly.get(t) for t in target],intervals

def url_for(model,init,lead):
    if model=='gfs':
        return f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{init:%Y%m%d}/{init:%H}/atmos/gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}'
    name='ifs' if model=='ifs' else 'aifs-single'
    return f'https://data.ecmwf.int/forecasts/{init:%Y%m%d}/{init:%H}z/{name}/0p25/oper/{init:%Y%m%d%H}0000-{lead}h-oper-fc.grib2'

def field_specs(model):
    # name -> index parameter, index level, GRIB level type, level, units, identifier, bounds
    gfs=model=='gfs'
    out={}
    def add(name,gparam,eparam,label,kind,level,units,category,number,param,low,high):
        out[name]=(gparam if gfs else eparam,label,kind,level,units,(category,number) if gfs else param,low,high)
    for level in (1000,925,850):
        if model=='aifs_single':
            add('q'+str(level),'','q',f'{level} mb','isobaricInhPa',level,'kg kg**-1',1,0,133,0,.1)
        add('r'+str(level),'RH','r',f'{level} mb','isobaricInhPa',level,'%',1,1,157,0,float('inf'))
        add('h'+str(level),'HGT','gh',f'{level} mb','isobaricInhPa',level,'gpm',3,5,156,-1000,10000)
        add('t'+str(level),'TMP','t',f'{level} mb','isobaricInhPa',level,'K',0,0,130,150,350)
    add('sp','PRES','sp','surface','surface',0,'Pa',3,0,134,50000,110000)
    add('t2','TMP','2t','2 m above ground','heightAboveGround',2,'K',0,0,167,150,350)
    add('d2','DPT','2d','2 m above ground','heightAboveGround',2,'K',0,6,168,150,350)
    add('u10','UGRD','10u','10 m above ground','heightAboveGround',10,'m s**-1',2,2,165,-150,150)
    add('v10','VGRD','10v','10 m above ground','heightAboveGround',10,'m s**-1',2,3,166,-150,150)
    if gfs:
        add('r2','RH','', '2 m above ground','heightAboveGround',2,'%',1,1,0,0,float('inf'))
        add('msl','PRMSL','', 'mean sea level','meanSea',0,'Pa',3,1,0,75000,115000)
        add('gust','GUST','', 'surface','surface',0,'m s**-1',2,22,0,0,150)
        add('low','LCDC','', 'low cloud layer','lowCloudLayer',0,'%',6,3,0,0,100)
        add('rain','APCP','', 'surface','surface',0,'kg m**-2',1,8,0,0,2000)
    return out

def expected_identity(model,name,init,lead,start=None,step=None):
    _,_,kind,level,units,param,_,_=field_specs(model)[name]
    end=init+timedelta(hours=lead)
    d=dict(edition=2,centre='kwbc' if model=='gfs' else 'ecmf',typeOfLevel=kind,level=level,units=units,
           dataDate=int(init.strftime('%Y%m%d')),dataTime=int(init.strftime('%H%M')),
           validityDate=int(end.strftime('%Y%m%d')),validityTime=int(end.strftime('%H%M')),
           stepType=step or ('accum' if name=='rain' else 'instant'),startStep=lead if start is None else start,endStep=lead,
           gridType='regular_ll',Ni=1440,Nj=721,iDirectionIncrementInDegrees=.25,jDirectionIncrementInDegrees=.25)
    if model=='gfs': d.update(discipline=0,parameterCategory=param[0],parameterNumber=param[1])
    else: d.update(paramId=param,marsClass='ai' if model=='aifs_single' else 'od',marsStream='oper',marsType='fc')
    return d

def derived_rh(fields,level):
    """Specific humidity -> vapor pressure -> RH over liquid water, not ice."""
    q=fields.get('q'+str(level),{}).get('value')
    t=fields.get('t'+str(level),{}).get('value')
    value=None
    if q is not None and t is not None:
        c=t-273.15
        vapor=q*(level*100)/(.622+.378*q)
        saturation=610.94*math.exp(17.625*c/(243.04+c))
        value=100*vapor/saturation
    return dict(value=value,method='RH over liquid water: q*p/(0.622+0.378*q), Magnus 610.94 Pa, 17.625, 243.04 C; supersaturation retained',
                inputs=['q'+str(level),'t'+str(level)],pressure_hpa=level)

def validate_packet(packet,now):
    require(packet['version']==2 and packet['kind']=='direct-deterministic')
    model=packet['model']; init=parse(packet['initialization_time']); collected=parse(packet['collected_at'])
    require(init.hour in (0,6,12,18) and init.minute==init.second==init.microsecond==0)
    begun=parse(packet['collection_started_at'])
    require(init<=begun<=collected<=begun+timedelta(seconds=180))
    require(timedelta(0)<=now-collected<=timedelta(hours=12) and timedelta(0)<=now-init<=timedelta(hours=24))
    require(packet['source_provider']==('NOAA' if model=='gfs' else 'ECMWF'))
    samples=packet['samples']; require(isinstance(samples,list) and len(samples)>0)
    leads=[s['lead'] for s in samples]; require(leads==sorted(set(leads)))
    specs=field_specs(model)
    for s in samples:
        lead=s['lead']; require(type(lead) is int and (model=='gfs' and 0<=lead<=120 or lead in native_leads(model,0,384 if model=='gfs' else 360)))
        require(not(model=='ifs' and init.hour in (6,18) and lead>144))
        require(s['url']==url_for(model,init,lead) and s['at']==stamp(init+timedelta(hours=lead)))
        fetched=parse(s['fetched_at']); require(init<=fetched<=collected)
        if 'collection_origin' not in s:require(begun<=fetched)
        if 'collection_origin' in s:
            origin=s['collection_origin']; a=parse(origin['started_at']); b=parse(origin['completed_at'])
            require(init<=a<=fetched<=b<=a+timedelta(seconds=180) and b<=collected)
        require(s['fields'] and set(s['fields'])<=set(specs))
        for name,f in s['fields'].items():
            if model=='aifs_single' and name in ('r1000','r925','r850'):
                require(f==derived_rh(s['fields'],int(name[1:])))
                continue
            identity=f['identity']; a=identity['startStep']; step=identity['stepType']
            require(type(a) is int and 0<=a<=lead)
            require(step==('accum' if name=='rain' else 'instant'))
            require(name=='rain' or a==lead)
            require(identity==expected_identity(model,name,init,lead,a,step))
            require(f['latitude']==41.0 and f['longitude']==-74.25)
            value=f['value']; low,high=specs[name][-2:]
            require(value is None or type(value) in (float,int) and math.isfinite(value) and low<=value<=high)
            p=f['proof']; require(all(type(p[k]) is int for k in ('start','end','total','bytes')))
            require(0<=p['start']<=p['end']<p['total']<=1_000_000_000 and p['bytes']==p['end']-p['start']+1<=8_000_000)
            require(len(p['sha256'])==64 and all(c in '0123456789abcdef' for c in p['sha256']))
    require(any(s['fields'].get('r850',{}).get('value') is not None for s in samples))
    return packet

def collect_native_profile(model,start,end,now,*,timeout=165,cache_dir=None):
    """Whole requested range; hard process deadline includes discovery/download.

    Cache packets retain original collection and per-sample fetch clocks; reading
    cache never updates either. A new packet may reuse independently validated
    native samples, whose original fetch clocks and byte proofs remain unchanged.
    """
    require(now.tzinfo is not None and start.tzinfo is not None and end.tzinfo is not None)
    require(timedelta(0)<end-start<=timedelta(days=16))
    directory=Path(cache_dir) if cache_dir else CACHE
    directory.mkdir(parents=True,exist_ok=True)
    source_now=datetime.now(UTC)
    cached=[]
    for path in directory.glob(model+'-*.json'):
        try:
            old=json.loads(path.read_text())
            validate_packet(old,parse(old['collected_at']))
            cached.append(validate_packet(old,source_now))
        except Exception: pass
    request=dict(model=model,start=stamp(start),end=stamp(end),now=stamp(source_now),cached=cached,timeout=min(165,max(5,timeout)))
    python=ROOT/'var/native-weather-venv/bin/python'
    command=[str(python),'-m','kcdw.direct_deterministic_worker']
    try:
        result=subprocess.run(command,input=json.dumps(request),text=True,capture_output=True,cwd=ROOT,timeout=min(170,max(6,timeout+2)))
        output=result.stdout
    except subprocess.TimeoutExpired as exc:
        output=exc.stdout or b''
        if isinstance(output,bytes): output=output.decode()
    lines=output.splitlines(); require(lines)
    header=json.loads(lines[0]); samples=[]
    for line in lines[1:]:
        try: samples.append(json.loads(line))
        except Exception: pass
    merged={}
    for old in cached:
        if old['initialization_time']==header['initialization_time']:
            for s in old['samples']:
                item=copy.deepcopy(s)
                item.setdefault('collection_origin',dict(started_at=old['collection_started_at'],completed_at=old['collected_at']))
                merged[s['lead']]=item
    # Worker echoes cached records; don't overwrite their original collection span.
    for s in samples:
        if s['lead'] not in merged or s['fetched_at']!=merged[s['lead']]['fetched_at']:
            merged[s['lead']]=s
    packet=dict(header,samples=sorted(merged.values(),key=lambda s:s['lead']))
    # Completion is a real wall clock, not the caller's earlier collection clock.
    packet['collected_at']=stamp(datetime.now(UTC))
    for s in packet['samples']:
        s.setdefault('collection_origin',dict(started_at=packet['collection_started_at'],completed_at=packet['collected_at']))
    for old in cached:
        if old['initialization_time']==packet['initialization_time'] and old['samples']==packet['samples']:
            return old
    validate_packet(packet,datetime.now(UTC))
    path=directory/(model+'-'+packet['initialization_time'].replace(':','')+'.json')
    with tempfile.NamedTemporaryFile(mode='w',dir=directory,prefix=path.stem,suffix='.tmp',delete=False) as f:
        json.dump(packet,f,separators=(',',':'),allow_nan=False)
        temporary=Path(f.name)
    temporary.replace(path)
    return packet

def normalize_profile(packet,start,end,purpose='moisture'):
    from .event_moisture import MODELS, VARIABLES as MV, UNITS as MU, INTERPRETATION
    from .gfs_guidance import VARIABLES as GV, UNITS as GU, MODEL as GM
    model=packet['model']; init=parse(packet['initialization_time'])
    require(start.minute==end.minute==start.second==end.second==0 and end>start)
    times=[start+timedelta(hours=i) for i in range(int((end-start).total_seconds()/3600))]
    target=[int((t-init).total_seconds()/3600) for t in times]
    leads=native_leads(model,max(0,min(target)),max(target))
    bylead={s['lead']:s for s in packet['samples']}
    require(set(leads)<=set(bylead))
    needed={'r1000','r925','r850','sp'} if purpose=='moisture' else {'msl','rain','u10','v10','gust','low','t2'}
    for lead in leads:
        require((needed-({'rain'} if lead==0 else set()))<=set(bylead[lead]['fields']))
        require(bylead[lead]['fields'].get('r850',{}).get('value') is not None)
        fields=bylead[lead]['fields']
        if purpose=='moisture':
            pressure=fields['sp']['value']; require(pressure is not None)
            for level in (1000,925,850):
                require(pressure<level*100 or fields['r'+str(level)]['value'] is not None)
        else:
            require(all(fields[n]['value'] is not None for n in needed if n!='rain' or lead!=0))
    def native(name): return [bylead.get(h,{}).get('fields',{}).get(name,{}).get('value') for h in leads]
    def values(name,scale: float=1,offset: float=0): return [None if v is None else v*scale+offset for v in align_hourly(leads,native(name),target)]
    h={'time':[stamp(t) for t in times]}
    if purpose=='moisture':
        h['surface_pressure']=values('sp',.01)
        h['relative_humidity_2m']=values('r2') if model=='gfs' else [None if t is None or d is None else 100*math.exp(17.625*d/(243.04+d)-17.625*t/(243.04+t)) for t,d in zip(values('t2',1,-273.15),values('d2',1,-273.15))]
        for level in (1000,925,850):
            for var,n in [('relative_humidity','r'),('geopotential_height','h')]:
                h[f'{var}_{level}hPa']=[v if p is not None and p>=level else None for v,p in zip(values(n+str(level)),h['surface_pressure'])]
        units=dict(MU); title=MODELS[model]['model']
    else:
        require(model=='gfs')
        h.update(pressure_msl=values('msl',.01),temperature_2m=values('t2',1,-273.15),cloud_cover_low=values('low'),wind_gusts_10m=values('gust',1.9438444924406))
        h['wind_speed_10m']=[None if u is None or v is None else math.hypot(u,v)*1.9438444924406 for u,v in zip(values('u10'),values('v10'))]
        h['precipitation'],_=rain_hourly(packet['samples'],target)
        units=dict(GU,time='iso8601'); title=GM
    _,intervals=rain_hourly(packet['samples'],target)
    metadata=dict(provenance=PROVENANCE,model_init_is_response_bound=True,initialization_time=packet['initialization_time'],source_provider=packet['source_provider'],sampling=SAMPLING,
                  provenance_limits='Exact GRIB initialization, field identities and native byte-range checksums validated offline; not a rolling API run attribution.',
                  precipitation_semantics='Uniform allocation of native accumulation intervals; hourly and partial-window amounts are estimates; native totals conserved',
                  null_semantics='missing/unknown; never clear weather',interpretation=INTERPRETATION,
                  collected_at=packet['collected_at'],collection_started_at=packet['collection_started_at'],native_proof=copy.deepcopy(packet),
                  precipitation_intervals=[dict(start=stamp(init+timedelta(hours=r['start_lead'])),end=stamp(init+timedelta(hours=r['end_lead'])),amount_mm=r['amount_mm']) for r in intervals],
                  surface_rh_method='native RH2m' if model=='gfs' else 'Magnus water saturation formula from interpolated native 2m temperature and dewpoint',
                  pressure_rh_method='Derived from native specific humidity, temperature and pressure using liquid-water Magnus saturation; not ice saturation' if model=='aifs_single' else 'native GRIB relative humidity',
                  hourly_provenance=['native step' if h in leads else 'linear interpolation of adjacent selected native steps (or null)' for h in target])
    return dict(schema_version=2,model_id=MODELS[model]['model_id'],model=title,endpoint=url_for(model,init,0).rsplit('/',1)[0]+'/',
                fetched_at=packet['collected_at'],requested_start=stamp(start),requested_end=stamp(end),
                grid_point=dict(latitude=41.0,longitude=-74.25,requested_latitude=40.8752,requested_longitude=-74.2814),hourly_units=units,hourly=h,metadata=metadata)

def validate_direct_data(data,now,purpose='moisture'):
    packet=validate_packet(data['metadata']['native_proof'],now)
    expected=normalize_profile(packet,parse(data['requested_start']),parse(data['requested_end']),purpose)
    require(data==expected)
    return copy.deepcopy(data)

def source_binding(data):
    """Small UI contract; never infers a binding for legacy Open-Meteo data."""
    meta=data.get('metadata',{})
    return {k:meta.get(k) for k in ('provenance','model_init_is_response_bound','initialization_time','source_provider','sampling','direct_fallback_reason')}
