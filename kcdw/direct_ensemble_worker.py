"""Bounded native ensemble point producer. Cache only validated exact GRIB points.

IFS 50r1 advertises enfo-ef with 50 perturbations; control is not substituted
from the deterministic product. AIFS provides pf + cf; GEFS c00 + p01..p30.
"""
from __future__ import annotations
import concurrent.futures as futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT/'var/direct-ensemble-points-v2'
MAX_FIELD = 8_000_000
COUNTS = {'gefs':31, 'ecmwf_ens':50, 'aifs_ens':51}
# name: paramId, native units, typeOfLevel, level, physical bounds
SPECS = {'r2':(157,'%', 'heightAboveGround',2,0,float('inf')),
 'sp':(134,'Pa','surface',0,30000,110000), 'msl':(151,'Pa','meanSea',0,75000,115000),
 '2t':(167,'K','heightAboveGround',2,153.15,353.15), '2d':(168,'K','heightAboveGround',2,130,353.15),
 '10u':(165,'m s**-1','heightAboveGround',10,-160,160), '10v':(166,'m s**-1','heightAboveGround',10,-160,160),
 'lcc':(186,'(0 - 1)','surface',0,0,1),
 'tp':(228,'m','surface',0,0,5)}
for level in (1000,925,850):
 for p,pid,unit,lo,hi in [('r',157,'%',0,float('inf')),('q',133,'kg kg**-1',0,.1),('t',130,'K',150,350)]:
  SPECS[p+str(level)]=(pid,unit,'isobaricInhPa',level,lo,hi)


def require(ok):
 if not ok: raise ValueError('native ensemble validation failed')


def stamp(t):return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def url_for(model,init,lead,group):
 if model=='gefs':
  member=int(group);name='gec00' if member==0 else f'gep{member:02}'
  return f'https://noaa-gefs-pds.s3.amazonaws.com/gefs.{init:%Y%m%d}/{init:%H}/atmos/pgrb2ap5/{name}.t{init:%H}z.pgrb2a.0p50.f{lead:03}'
 name={'ecmwf_ens':'ifs','aifs_ens':'aifs-ens'}[model]
 return f'https://data.ecmwf.int/forecasts/{init:%Y%m%d}/{init:%H}z/{name}/0p25/enfo/{init:%Y%m%d%H}0000-{lead}h-enfo-{group}.grib2'


def fetch(url,limit,byte_range=None):
 import requests
 headers={'Range':f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
 with requests.get(url,headers=headers,timeout=(3,7),stream=True,allow_redirects=False) as r:
  require(r.status_code==(206 if byte_range else 200));content=bytearray()
  if byte_range:
   m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.headers.get('Content-Range',''));require(m is not None)
   a,b,n=map(int,m.groups());require((a,b)==tuple(byte_range) and b<n<=20_000_000_000)
  for block in r.iter_content(65536):content.extend(block);require(len(content)<=limit)
  if byte_range:require(len(content)==byte_range[1]-byte_range[0]+1)
  return bytes(content)


def indexed_ranges(model,text,init,lead,group,object_size=None):
 selected={}
 if model=='gefs':
  rows=[line.split(':') for line in text.splitlines()];require(1<len(rows)<=1500 and all(len(r)>=7 for r in rows))
  offsets=[int(r[1]) for r in rows];require(offsets==sorted(set(offsets)) and offsets[0]==0)
  names={('RH','2 m above ground'):'r2',('PRES','surface'):'sp',('PRMSL','mean sea level'):'msl',('TMP','2 m above ground'):'2t',('UGRD','10 m above ground'):'10u',('VGRD','10 m above ground'):'10v'}
  names[('APCP','surface')]='tp'
  for level in (1000,925,850):names[('RH',f'{level} mb')]='r'+str(level)
  for i,row in enumerate(rows):
   if i==len(rows)-1 and object_size is None:continue
   name=names.get(tuple(row[3:5]))
   if not name:continue
   require(row[2]==f'd={init:%Y%m%d%H}')
   if name=='tp':
    if lead==0:continue
    require(row[5]==f'{lead-6}-{lead} hour acc fcst')
   else:require(row[5]==('anl' if lead==0 else f'{lead} hour fcst'))
   next_offset=offsets[i+1] if i+1<len(rows) else int(object_size)
   require(0<next_offset-offsets[i]<=MAX_FIELD)
   key=(f'{int(group):02}',name);require(key not in selected);selected[key]=(offsets[i],next_offset-1)
 else:
  rows=[json.loads(line) for line in text.splitlines()];require(1<len(rows)<=20000)
  for row in rows:
   require(row.get('date')==init.strftime('%Y%m%d') and row.get('time')==init.strftime('%H%M') and row.get('step')==str(lead))
   require(row.get('domain')=='g' and row.get('stream')=='enfo' and row.get('class')==('ai' if model=='aifs_ens' else 'od'))
   require(row.get('type')==('cf' if group=='cf' else 'pf'))
   if model=='aifs_ens':require(row.get('model')=='aifs-ens')
   member=int(row.get('number','0'));require((member==0) if group=='cf' else 1<=member<=50)
   param=row.get('param');level=row.get('levelist');name=param+str(level) if param in ('r','q','t') else param
   if name not in SPECS:continue
   if model=='ecmwf_ens' and (name.startswith('q') or name.startswith('t') and name!='tp'):continue
   require(row.get('levtype')==('pl' if SPECS[name][2]=='isobaricInhPa' else 'sfc'))
   a,n=row['_offset'],row['_length'];require(type(a) is int and type(n) is int)
   key=(f'{member:02}',name);require(key not in selected);selected[key]=(a,a+n-1)
 require(selected)
 for a,b in selected.values():require(0<=a<=b<20_000_000_000 and b-a+1<=MAX_FIELD)
 return selected


def decode(content,model,init,lead,member,name):
 import eccodes as ec
 require(content[:4]==b'GRIB' and content[-4:]==b'7777' and int.from_bytes(content[8:16],'big')==len(content))
 g=ec.codes_new_from_message(content)
 try:
  pid,unit,kind,level,lo,hi=field_spec(model,name);valid=init+timedelta(hours=lead)
  expected=dict(edition=2,centre='kwbc' if model=='gefs' else 'ecmf',paramId=pid,units=unit,typeOfLevel=kind,level=level,dataDate=int(init.strftime('%Y%m%d')),dataTime=int(init.strftime('%H%M')),validityDate=int(valid.strftime('%Y%m%d')),validityTime=int(valid.strftime('%H%M')),startStep=lead,endStep=lead,stepType='instant',gridType='regular_ll',Ni=720 if model=='gefs' else 1440,Nj=361 if model=='gefs' else 721,number=int(member))
  if name=='tp':expected.update(stepType='accum',startStep=lead-6 if model=='gefs' else 0)
  if model!='gefs':expected.update(marsClass='ai' if model=='aifs_ens' else 'od',marsStream='enfo',marsType='cf' if member=='00' else 'pf')
  require(all(ec.codes_get(g,k)==v for k,v in expected.items()))
  point=ec.codes_grib_find_nearest(g,40.8752,285.7186)[0];v=float(point['value']);lat=float(point['lat']);lon=(float(point['lon'])+180)%360-180
  require(lat==41 and lon==(-74.5 if model=='gefs' else -74.25) and math.isfinite(v) and lo<=v<=hi and v!=ec.codes_get(g,'missingValue'))
  if ec.codes_get(g,'bitmapPresent'):require(ec.codes_get_array(g,'bitmap')[int(point['index'])])
  return dict(value=v,latitude=lat,longitude=lon,identity=expected,sha256=hashlib.sha256(content).hexdigest())
 finally:ec.codes_release(g)


def field_spec(model,name):
 if name=='msl' and model=='gefs':return (260074,'Pa','meanSea',0,75000,115000)
 if name=='r2' and model=='gefs':return (260242,'%', 'heightAboveGround',2,0,float('inf'))
 if name=='tp' and model!='ecmwf_ens':return (228228,'kg m**-2','surface',0,0,5000)
 return SPECS[name]


def path_for(model,init,lead,member,name):return CACHE/model/init.strftime('%Y%m%d%H')/f'{member}-{lead:03}-{name}.json'


def point(model,init,lead,member,name,url,span,collected):
 path=path_for(model,init,lead,member,name)
 try:
  p=json.loads(path.read_text());require(p['model']==model and p['init']==stamp(init) and p['lead']==lead and p['member']==member and p['field']==name)
  require(p['url']==url and p['range']==list(span));require(math.isfinite(p['value']) and field_spec(model,name)[4]<=p['value']<=field_spec(model,name)[5])
  original=datetime.fromisoformat(p['collected_at'].replace('Z','+00:00'));fetched=datetime.fromisoformat(p['fetched_at'].replace('Z','+00:00'));current=datetime.fromisoformat(collected.replace('Z','+00:00'))
  require(init<=original<=fetched<=current and current-original<=timedelta(hours=12))
  return p
 except (OSError,ValueError,KeyError,TypeError):pass
 raw=fetch(url,MAX_FIELD,span);p=decode(raw,model,init,lead,member,name)
 p.update(model=model,init=stamp(init),lead=lead,member=member,field=name,url=url,range=list(span),collected_at=collected,fetched_at=stamp(datetime.now(timezone.utc)))
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(f'.{os.getpid()}.tmp');tmp.write_text(json.dumps(p,allow_nan=False));os.replace(tmp,path)
 return p


def groups(model):return [f'{i:02}' for i in range(31)] if model=='gefs' else ['ef'] if model=='ecmwf_ens' else ['cf','pf']


def index(model,init,lead,group):
 url=url_for(model,init,lead,group);idx=url+'.idx' if model=='gefs' else url[:-6]+'.index'
 text=fetch(idx,4_000_000).decode('ascii');size=None
 if model=='gefs':
  import requests
  with requests.head(url,timeout=(4,10),allow_redirects=False) as response:
   require(response.status_code==200);size=int(response.headers['Content-Length']);require(0<size<200_000_000)
 return url,indexed_ranges(model,text,init,lead,group,object_size=size)


def run(request):
 model=request['model'];require(model in COUNTS)
 now=datetime.fromisoformat(request['now'].replace('Z','+00:00'));start=datetime.fromisoformat(request['start'].replace('Z','+00:00'));end=datetime.fromisoformat(request['end'].replace('Z','+00:00'))
 require(now.tzinfo is not None and start.tzinfo is not None and end.tzinfo is not None and timedelta(0)<end-start<=timedelta(days=16))
 deadline=time.monotonic()+min(175,max(10,int(request.get('seconds',175))))
 cycle=now.replace(hour=now.hour//6*6,minute=0,second=0,microsecond=0);chosen=None
 for age in range(5):
  init=cycle-timedelta(hours=6*age);maxlead=384 if model=='gefs' else 144 if model=='ecmwf_ens' and init.hour in (6,18) else 360
  first=max(0,math.floor((start-init).total_seconds()/21600)*6);last=math.ceil((end-timedelta(hours=1)-init).total_seconds()/21600)*6
  if last>maxlead or now-init>timedelta(hours=24):continue
  try:
   # Actual catalog discovery, including requested far-horizon and full IDs.
   _,catalog=index(model,init,last,groups(model)[-1]);offered={m for m,n in catalog}
   require(len(offered)==(1 if model=='gefs' else 50))
   chosen=(init,list(range(first,last+1,6)));break
  except Exception:
   if time.monotonic()>deadline:break
 require(chosen is not None);init,leads=chosen
 result=dict(model=model,init=stamp(init),points=[],offered_members=[f'{i:02}' for i in (range(1,51) if model=='ecmwf_ens' else range(COUNTS[model]))],requested_leads=leads)
 if request.get('focus'):
  focus=datetime.fromisoformat(request['focus'].replace('Z','+00:00'))
  h=math.floor((focus-init).total_seconds()/21600)*6
  leads=sorted(leads,key=lambda x:(0 if h<=x<=h+6 else 1 if x==h+12 else 2 if x==h-6 else 3,x))
 # Emit partial verified progress on the hard deadline, retaining cache clocks.
 def finish(*_):
  print(json.dumps(result,allow_nan=False,separators=(',',':')),flush=True);os._exit(0)
 signal.signal(signal.SIGALRM,finish);signal.alarm(max(1,math.ceil(deadline-time.monotonic())))
 with futures.ThreadPoolExecutor(max_workers=12) as pool:
  for batch in [leads[:2], *[[lead] for lead in leads[2:]]]:
   tasks={pool.submit(index,model,init,lead,g):lead for lead in batch for g in groups(model)}
   work=[]
   for task in futures.as_completed(tasks):
    try:
     url,ranges=task.result()
     for (member,name),span in ranges.items():work.append((member,name,url,span,tasks[task]))
    except Exception:continue
   # RH850 prioritized, then dependencies, other levels, broad charts.
   priority={'r850':0,'q850':0,'t850':1,'sp':2,'2t':3,'2d':4,'r2':4,'r925':5,'q925':5,'t925':6,'r1000':7,'q1000':7,'t1000':8}
   work.sort(key=lambda x:(priority.get(x[1],9),x[0]))
   tasks=[pool.submit(point,model,init,lead,m,n,u,s,stamp(now)) for m,n,u,s,lead in work]
   for task in futures.as_completed(tasks):
    try:result['points'].append(task.result())
    except Exception:pass
   if time.monotonic()>=deadline:break
 signal.alarm(0);return result

if __name__=='__main__':
 try:
  signal.signal(signal.SIGALRM,lambda *_:os._exit(2));signal.alarm(180)
  print(json.dumps(run(json.loads(sys.stdin.read(4097))),allow_nan=False,separators=(',',':')))
 except Exception:sys.exit(1)
