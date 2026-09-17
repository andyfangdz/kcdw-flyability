"""Direct-native ensemble adapter; offline binding and conservative interpolation.

Exact native values/proofs stay in the private point cache. Published records keep
normalized fans and compact validation metadata, never full global/member arrays.
RH derivations use Bolton (1980) saturation vapor pressure over liquid water:
es=6.112 exp(17.67 Tc/(Tc+243.5)); e=q*p/(.622+.378*q).
Supersaturation is retained, not treated as invalid data or clipped.
Rain uses explicit uniform interval disaggregation; no synthetic gust/low cloud.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import subprocess

from .common import UTC, iso_z
from .direct_ensemble_worker import SPECS, COUNTS, url_for, field_spec
ROOT=Path(__file__).resolve().parents[1]
SAMPLING=('Native 6-hour samples; linear memberwise interpolation to hourly only between adjacent samples; '
          'u/v interpolated before paired speed/direction and quantiles. No extrapolation. '
          'Native 6-hour precipitation uniformly disaggregated to closing-hour amounts after same-member cumulative differencing; no added timing skill. '
          'Gust and low cloud remain missing unless independently supplied by the native catalog.')
DERIVATION=('RH2m from native T/dewpoint; AIFS pressure-level RH from native q,T,p using Bolton (1980) '
            'liquid-water saturation vapor pressure; RH supersaturation retained without clamping. Pressure levels masked using same-member surface pressure.')
FALLBACK='Direct native source unavailable or insufficient validated coverage; explicit Open-Meteo fallback.'


def require(ok):
    if not ok:raise ValueError('direct ensemble validation failed')


def time(value):
    require(isinstance(value,str) and value.endswith('Z'))
    dt=datetime.fromisoformat(value.replace('Z','+00:00'));require(iso_z(dt)==value);return dt


def number(v,lo,hi):
    require(type(v) in (int,float) and math.isfinite(v) and lo<=v<=hi);return v


def saturation(t):
    number(t,130,360);c=t-273.15;return 6.112*math.exp(17.67*c/(c+243.5))


def rh_from_q(q,t,p):
    number(q,0,.1);number(p,100,1100)
    return 100*q*p/(.622+.378*q)/saturation(t)


def rh_from_dewpoint(t,d):return 100*saturation(d)/saturation(t)


def interpolate(samples,targets):
    out=[]
    for at in targets:
        if at in samples:out.append(samples[at]);continue
        a=math.floor(at/6)*6;b=a+6
        x,y=samples.get(a),samples.get(b)
        out.append(None if x is None or y is None else x+(y-x)*(at-a)/6)
    return out


def validate_metadata(meta,model,now):
    require(model in COUNTS and isinstance(meta,dict))
    require(meta.get('direct_native') is True and meta.get('provenance')=='direct-native' and meta.get('model_init_is_response_bound') is True)
    require(meta['native_model']==model and meta['source_provider']==('NOAA' if model=='gefs' else 'ECMWF'))
    init=time(meta['initialization_time']);require(init.hour in (0,6,12,18) and init.minute==init.second==0)
    require(-timedelta(minutes=5)<=now-init<=timedelta(hours=24))
    first,last=time(meta['original_collected_at']),time(meta['original_fetched_at'])
    require(init<=first<=last<=now+timedelta(minutes=5) and now-first<=timedelta(hours=12))
    require(meta.get('availability_time') is None)
    require(meta['sampling']==SAMPLING and meta['native_timestep_hours']==6 and meta['derivation']==DERIVATION)
    expected=[f'{i:02}' for i in (range(1,51) if model=='ecmwf_ens' else range(COUNTS[model]))]
    require(meta['member_ids']==expected and meta['offered_members']==len(expected))
    require(meta['control_member']==(None if model=='ecmwf_ens' else '00'))
    end=time(meta['data_end_time']);require(init<=end<=init+timedelta(hours=384 if model=='gefs' else 360))
    require(type(meta['validated_points']) is int and 0<meta['validated_points']<=100000)
    require(meta['ok'] is True and meta['fresh'] is True and type(meta['covers_display']) is bool)
    return meta


def validate_packet(packet,model,now):
    require(packet['model']==model);init=time(packet['init']);require(now-timedelta(hours=24)<=init<=now)
    expected=[f'{i:02}' for i in (range(1,51) if model=='ecmwf_ens' else range(COUNTS[model]))]
    require(packet['offered_members']==expected)
    points=packet['points'];require(isinstance(points,list) and 0<len(points)<=100000)
    seen=set()
    for p in points:
        member,name,lead=p['member'],p['field'],p['lead'];require(member in expected and name in SPECS and type(lead) is int and 0<=lead<=384 and lead%6==0)
        require((member,name,lead) not in seen);seen.add((member,name,lead))
        require(p['model']==model and p['init']==packet['init'])
        group=member if model=='gefs' else 'ef' if model=='ecmwf_ens' else 'cf' if member=='00' else 'pf'
        require(p['url']==url_for(model,init,lead,group))
        require(p['latitude']==41 and p['longitude']==(-74.5 if model=='gefs' else -74.25))
        pid,unit,kind,level,lo,hi=field_spec(model,name);number(p['value'],lo,hi)
        ident=p['identity'];valid=init+timedelta(hours=lead)
        expected_identity=dict(edition=2,centre='kwbc' if model=='gefs' else 'ecmf',paramId=pid,units=unit,typeOfLevel=kind,level=level,dataDate=int(init.strftime('%Y%m%d')),dataTime=int(init.strftime('%H%M')),validityDate=int(valid.strftime('%Y%m%d')),validityTime=int(valid.strftime('%H%M')),startStep=lead,endStep=lead,stepType='instant',gridType='regular_ll',Ni=720 if model=='gefs' else 1440,Nj=361 if model=='gefs' else 721,number=int(member))
        if name=='tp':expected_identity.update(stepType='accum',startStep=lead-6 if model=='gefs' else 0)
        if model!='gefs':expected_identity.update(marsClass='ai' if model=='aifs_ens' else 'od',marsStream='enfo',marsType='cf' if member=='00' else 'pf')
        require(ident==expected_identity)
        a,b=p['range'];require(type(a) is int and type(b) is int and 0<=a<=b<20_000_000_000 and b-a<8_000_000)
        require(isinstance(p['sha256'],str) and len(p['sha256'])==64 and all(c in '0123456789abcdef' for c in p['sha256']))
        collected,fetched=time(p['collected_at']),time(p['fetched_at']);require(init<=collected<=fetched<=now+timedelta(minutes=5) and now-collected<=timedelta(hours=12))
    return packet


def collect_native(client,model,start,end,now):
    """One hard-bounded producer per model/range/client; shared by chart and RH."""
    require(getattr(client,'direct_native',False) is True and model in COUNTS)
    cache=getattr(client,'_direct_ensemble_cache',None)
    if cache is None:cache={};setattr(client,'_direct_ensemble_cache',cache)
    key=(model,iso_z(end))
    if key not in cache:
        request=dict(model=model,start=iso_z(start),end=iso_z(end),now=iso_z(datetime.now(UTC)),seconds=175,focus=iso_z(end-timedelta(days=2)+timedelta(hours=12)))
        try:
            run=subprocess.run([str(ROOT/'var/native-weather-venv/bin/python'),'-m','kcdw.direct_ensemble_worker'],cwd=ROOT,input=json.dumps(request),capture_output=True,text=True,timeout=185,check=True)
            require(len(run.stdout)<=80_000_000);cache[key]=validate_packet(json.loads(run.stdout),model,datetime.now(UTC))
        except Exception:cache[key]=None
    packet=cache[key];require(packet is not None)
    validate_packet(packet,model,datetime.now(UTC))
    return packet


def hourly_members(packet,axis):
    """Pair native dependencies within actual member/run/lead before reduction."""
    from .event_ensemble import VARIABLES
    from .event_moisture_ensemble import VARIABLES as RH
    init=time(packet['init']);targets=[(time(t)-init).total_seconds()/3600 for t in axis]
    native={}
    for p in packet['points']:native.setdefault(p['member'],{}).setdefault(p['field'],{})[p['lead']]=p['value']
    out={f:{} for f in (*VARIABLES,*RH,'wind_direction_10m')}
    for member,fields in native.items():
        samples={f:{} for f in out}
        leads=sorted({h for row in fields.values() for h in row})
        for h in leads:
            vals={n:row[h] for n,row in fields.items() if h in row}
            for n,f,scale,offset in [('sp','surface_pressure',.01,0),('msl','pressure_msl',.01,0),('2t','temperature_2m',1,-273.15),('lcc','cloud_cover_low',100,0),('r2','relative_humidity_2m',1,0)]:
                if n in vals:samples[f][h]=vals[n]*scale+offset
            if 'r2' not in vals and {'2t','2d'}<=vals.keys():samples['relative_humidity_2m'][h]=rh_from_dewpoint(vals['2t'],vals['2d'])
            for level in (1000,925,850):
                n='r'+str(level);q='q'+str(level);t='t'+str(level);f=f'relative_humidity_{level}hPa'
                if n in vals:samples[f][h]=vals[n]
                elif {q,t}<=vals.keys():samples[f][h]=rh_from_q(vals[q],vals[t],level)
        for f in out:
            out[f][member]=interpolate(samples[f],targets)
        # Amounts belong to (start,end]; never interpolate accumulated totals.
        rain = {}
        for lead, value in sorted(fields.get('tp', {}).items()):
            if lead == 0:
                continue
            previous = lead-6
            if packet['model'] == 'gefs':
                amount = value
            else:
                before = fields['tp'].get(previous, 0 if previous == 0 else None)
                if before is None:
                    continue
                amount = (value-before)*(1000 if packet['model']=='ecmwf_ens' else 1)
            if amount < -1e-6:
                continue
            for hour in range(previous+1, lead+1):
                rain[hour] = max(0., amount)/6
        out['precipitation'][member] = [rain.get(at) for at in targets]
        # Vector interpolation avoids direction wrap and pairs member components.
        u,v=[interpolate(fields.get(n,{}),targets) for n in ('10u','10v')]
        out['wind_speed_10m'][member]=[None if x is None or y is None else math.hypot(x,y)*3600/1852 for x,y in zip(u,v)]
        out['wind_direction_10m'][member]=[None if x is None or y is None or math.hypot(x,y)<.01 else (math.degrees(math.atan2(-x,-y))+360)%360 for x,y in zip(u,v)]
    return out


def metadata(packet,axis,now):
    model=packet['model'];points=packet['points'];init=time(packet['init']);last=init+timedelta(hours=max(p['lead'] for p in points))
    return dict(ok=True,fresh=True,direct_native=True,provenance='direct-native',model_init_is_response_bound=True,native_model=model,source_provider='NOAA' if model=='gefs' else 'ECMWF',initialization_time=packet['init'],availability_time=None,data_end_time=iso_z(last),native_timestep_hours=6,covers_display=last>=time(axis[-1]),sampling=SAMPLING,derivation=DERIVATION,binding_note='Exact native GRIB model/init/member/lead/field binding; partial coverage remains null.',member_ids=packet['offered_members'],offered_members=len(packet['offered_members']),control_member=None if model=='ecmwf_ens' else '00',control_note='IFS open ef catalog provides 50 perturbations; no control advertised.' if model=='ecmwf_ens' else 'Member 00 is the separately identified native control.',original_collected_at=min(p['collected_at'] for p in points),original_fetched_at=max(p['fetched_at'] for p in points),validated_points=len(points),license='NOAA public domain' if model=='gefs' else 'ECMWF open data CC BY 4.0')


def label(spec):return f'{spec.name} direct {"NOAA" if spec.key=="gefs" else "ECMWF"} native members; descriptive spread, not calibrated flight odds.'


def seal(data):
    data['metadata']['normalized_sha256']=digest(data)
    return data


def digest(data):
    # Binding checksum is an integrity guard, not an authenticity signature.
    payload=dict(data)
    payload['metadata']={k:v for k,v in data['metadata'].items() if k!='normalized_sha256'}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_normalized(data,spec,now):
    meta=validate_metadata(data['metadata'],spec.key,now)
    require(data['model_id']==spec.model_id and data['model']==spec.name and data['members']==COUNTS[spec.key])
    require(data['label']==label(spec) and data['endpoint']==('https://noaa-gefs-pds.s3.amazonaws.com/' if spec.key=='gefs' else 'https://data.ecmwf.int/forecasts/'))
    require(meta['normalized_sha256']==digest(data))
    require(data['grid_point']=={'latitude':41.,'longitude':-74.5 if spec.key=='gefs' else -74.25})
    require(data['fetched_at']==meta['original_fetched_at'])
    return data


def base_data(spec,packet,axis,now) -> dict:
    return dict(model_id=spec.model_id,model=spec.name,label=label(spec),members=COUNTS[spec.key],fetched_at=max(p['fetched_at'] for p in packet['points']),endpoint='https://noaa-gefs-pds.s3.amazonaws.com/' if spec.key=='gefs' else 'https://data.ecmwf.int/forecasts/',metadata=metadata(packet,axis,now),grid_point={'latitude':41.,'longitude':-74.5 if spec.key=='gefs' else -74.25})


def collect_rh(client,spec,start,end,now):
    from .event_moisture_ensemble import _fans,VARIABLES,UNITS
    axis=[iso_z(start+timedelta(hours=i)) for i in range(int((end-start).total_seconds()/3600))]
    packet=collect_native(client,spec.key,start,end,now);members=hourly_members(packet,axis)
    members={f:members[f] for f in VARIABLES};data=base_data(spec,packet,axis,now)
    data.update(hourly_units=dict(UNITS),member_ids={f:sorted(m for m,row in rows.items() if any(v is not None for v in row)) for f,rows in members.items()},hourly=_fans(members,axis))
    focus=end-timedelta(days=2)+timedelta(hours=10)
    indices=[i for i,t in enumerate(axis) if focus<=time(t)<=focus+timedelta(hours=2)]
    counts=data['hourly']['relative_humidity_850hPa']['sample_counts']
    require(all(counts[i]==COUNTS[spec.key] for i in indices) if indices else any(counts))
    return seal(data)


def collect_chart(client,spec,event,start,end,now):
    from .event_ensemble import VARIABLES,UNITS,_hourly_stats
    from .event_ensemble import _window_scenarios
    axis=[iso_z(start+timedelta(hours=i)) for i in range(int((end-start).total_seconds()/3600))]
    packet=collect_native(client,spec.key,start,end,now);members=hourly_members(packet,axis)
    data=base_data(spec,packet,axis,now);data.update(key=spec.key,provider=data['metadata']['source_provider'],hourly_units=dict(UNITS)|{'time':'iso8601 UTC'},hourly=_hourly_stats([time(t) for t in axis],{f:members[f] for f in VARIABLES}),window=_window_scenarios(event,[time(t) for t in axis],members) if event is not None else None)
    require(any(data['hourly']['wind_speed_10m']['sample_counts']))
    return seal(data)
