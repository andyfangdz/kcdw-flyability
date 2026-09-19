"""Isolated optional ecCodes worker: bounded native indexes/ranges, no arbitrary URLs.

Importing this module does not import requests/ecCodes. Each invocation owns one
source; the parent runs sources concurrently so one unavailable model is local.
"""
import concurrent.futures
import hashlib
import json
import math
import re
import signal
import sys
from datetime import datetime, timedelta, timezone

CORE = ('10u', '10v', 'u925', 'v925', 'u850', 'v850')
MAX_FIELD = 8_000_000


def require(ok):
    if not ok:
        raise ValueError('native wind validation failed')


def url_for(model, init, lead, *, legacy=False):
    if model == 'gfs':
        return (f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{init:%Y%m%d}/{init:%H}/atmos/'
                f'gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}')
    name = {'ifs':'ifs', 'aifs_single':'aifs-single'}[model]
    # IFS 50r1 uses oper at every cycle; scda is only for archived v1 packets.
    stream = 'scda' if legacy and model == 'ifs' and init.hour in (6,18) else 'oper'
    return (f'https://data.ecmwf.int/forecasts/{init:%Y%m%d}/{init:%H}z/{name}/0p25/{stream}/'
            f'{init:%Y%m%d%H}0000-{lead}h-{stream}-fc.grib2')


GUST3 = ('gust3', 'gust3_prev')


def identity(model, name, init, lead, *, legacy=False):
    if name in GUST3:
        # Inside 144 h ECMWF publishes 10fg3, a three-hour maximum, instead of the six-hour 10fg. gust3 ends at the
        # sample; gust3_prev is the preceding three hours from the previous step's file, so both halves stay verifiable.
        require(model=='ifs' and not legacy and lead>=6)
        end=lead if name=='gust3' else lead-3
        valid=init+timedelta(hours=end)
        return dict(edition=2,centre='ecmf',paramId=228028,typeOfLevel='heightAboveGround',level=10,units='m s**-1',
                    dataDate=int(init.strftime('%Y%m%d')),dataTime=int(init.strftime('%H%M')),
                    validityDate=int(valid.strftime('%Y%m%d')),validityTime=int(valid.strftime('%H%M')),
                    stepType='max',startStep=end-3,endStep=end,gridType='regular_ll',Ni=1440,Nj=721,
                    marsClass='od',marsStream='oper',marsType='fc')
    valid=init+timedelta(hours=lead)
    gust=name=='gust'
    if gust and model=='ifs' and not legacy:
        require(lead>=6)  # Never invent a pre-initialization gust interval.
    param = (260065 if model=='gfs' else 49) if gust else {'10u':165,'10v':166}.get(name,131 if name.startswith('u') else 132)
    kind='surface' if gust and model=='gfs' else 'heightAboveGround' if name.startswith('10') or gust else 'isobaricInhPa'
    level=0 if kind=='surface' else 10 if kind=='heightAboveGround' else int(name[1:])
    return dict(edition=2,centre='kwbc' if model=='gfs' else 'ecmf',paramId=param,
                typeOfLevel=kind,level=level,units='m s**-1',
                dataDate=int(init.strftime('%Y%m%d')),dataTime=int(init.strftime('%H%M')),
                validityDate=int(valid.strftime('%Y%m%d')),validityTime=int(valid.strftime('%H%M')),
                stepType='max' if gust and model=='ifs' else 'instant',
                startStep=lead-6 if gust and model=='ifs' else lead,endStep=lead,
                gridType='regular_ll',Ni=1440,Nj=721,
                **({} if model=='gfs' else dict(marsClass='ai' if model=='aifs_single' else 'od',
                   marsStream='scda' if legacy and model=='ifs' and init.hour in (6,18) else 'oper',marsType='fc')))


def fetch(url, limit, byte_range=None):
    import requests
    headers={'Range':f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    with requests.get(url,headers=headers,timeout=(4,9),stream=True,allow_redirects=False) as r:
        require(r.status_code==(206 if byte_range else 200))
        proof=None
        if byte_range:
            m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.headers.get('Content-Range',''))
            require(m is not None)
            a,b,total=map(int,m.groups())
            require((a,b)==tuple(byte_range) and 0<=a<=b<total<=1_000_000_000)
            proof=dict(start=a,end=b,total=total,bytes=b-a+1)
        content=bytearray()
        for block in r.iter_content(65536):
            content.extend(block); require(len(content)<=limit)
        if proof:
            require(len(content)==proof['bytes'])
            proof['sha256']=hashlib.sha256(content).hexdigest()
        return bytes(content),proof


def indexed_ranges(model,text,init,lead,*,legacy=False):
    selected={}
    if model=='gfs':
        rows=[line.split(':') for line in text.splitlines()]
        require(1<len(rows)<=1500 and all(len(r)>=6 for r in rows))
        offsets=[int(r[1]) for r in rows]
        require(offsets==sorted(set(offsets)) and offsets[0]==0)
        specs={'10u':('UGRD','10 m above ground'),'10v':('VGRD','10 m above ground'),
               'u925':('UGRD','925 mb'),'v925':('VGRD','925 mb'),
               'u850':('UGRD','850 mb'),'v850':('VGRD','850 mb'),'gust':('GUST','surface')}
        for i,row in enumerate(rows[:-1]):
            for name,spec in specs.items():
                if tuple(row[3:5])==spec:
                    require(row[2]==f'd={init:%Y%m%d%H}' and row[5]==('anl' if lead==0 else f'{lead} hour fcst'))
                    require(name not in selected)
                    selected[name]=(offsets[i],offsets[i+1]-1)
    else:
        rows=[json.loads(line) for line in text.splitlines()]
        require(1<len(rows)<=1500)
        for row in rows:
            param=row.get('param'); level=row.get('levelist')
            name=param if param in ('10u','10v') else param+str(level) if param in ('u','v') and str(level) in ('925','850') else 'gust' if param in ('10fg','10fg6') and model=='ifs' else 'gust3' if param=='10fg3' and model=='ifs' and not legacy else None
            if name:
                require(row.get('date')==init.strftime('%Y%m%d') and row.get('time')==init.strftime('%H%M'))
                require(row.get('step')==str(lead) and row.get('type')=='fc' and row.get('domain')=='g')
                require(row.get('class')==('ai' if model=='aifs_single' else 'od'))
                require(row.get('stream')==('scda' if legacy and model=='ifs' and init.hour in (6,18) else 'oper'))
                require(row.get('levtype')==('pl' if name in ('u925','v925','u850','v850') else 'sfc'))
                if model=='aifs_single': require(row.get('model')=='aifs-single')
                require(name not in selected)
                a,n=row['_offset'],row['_length']; require(type(a) is int and type(n) is int)
                selected[name]=(a,a+n-1)
    require(set(CORE)<=set(selected))
    for a,b in selected.values(): require(0<=a<=b<1_000_000_000 and b-a+1<=MAX_FIELD)
    return selected


def decode(content,model,name,init,lead):
    import eccodes as ec
    require(content[:4]==b'GRIB' and content[-4:]==b'7777' and int.from_bytes(content[8:16],'big')==len(content))
    g=ec.codes_new_from_message(content)
    try:
        expected=identity(model,name,init,lead)
        # In particular, an IFS 10fg index entry does not prove a six-hour
        # maximum. Shorter actual intervals fail here and sample omits gust.
        require(all(ec.codes_get(g,k)==v for k,v in expected.items()))
        p=ec.codes_grib_find_nearest(g,40.8752,285.7186)[0]
        value=float(p['value']); lat=float(p['lat']); lon=(float(p['lon'])+180)%360-180
        require(lat==41.0 and lon==-74.25 and math.isfinite(value) and abs(value)<=200)
        require(value!=ec.codes_get(g,'missingValue'))
        if ec.codes_get(g,'bitmapPresent'): require(ec.codes_get_array(g,'bitmap')[int(p['index'])])
        if name=='gust' or name in GUST3: require(value>=0)
        return dict(identity=expected,value=value,latitude=lat,longitude=lon)
    finally:
        ec.codes_release(g)


def previous_gust3(model,init,lead):
    """10fg3 from the file three hours before the sample: the first half of the six-hour bracket."""
    url=url_for(model,init,lead-3)
    text,_=fetch(url[:-6]+'.index',250_000)
    rows=[json.loads(line) for line in text.decode('ascii').splitlines()]
    require(1<len(rows)<=1500)
    found=[row for row in rows if row.get('param')=='10fg3']
    require(len(found)==1)
    row=found[0]
    require(row.get('date')==init.strftime('%Y%m%d') and row.get('time')==init.strftime('%H%M') and row.get('step')==str(lead-3))
    require(row.get('type')=='fc' and row.get('domain')=='g' and row.get('class')=='od' and row.get('stream')=='oper' and row.get('levtype')=='sfc')
    a,n=row['_offset'],row['_length']; require(type(a) is int and type(n) is int and 0<=a and 0<n<=MAX_FIELD)
    raw,proof=fetch(url,MAX_FIELD,(a,a+n-1))
    # decode() validates against the sample lead: gust3_prev's identity is the three hours ending at lead-3.
    return dict(decode(raw,model,'gust3_prev',init,lead),proof=proof,url=url)


def sample(model,init,lead):
    url=url_for(model,init,lead)
    index_url=url+'.idx' if model=='gfs' else url[:-6]+'.index'
    text,_=fetch(index_url,250_000)
    ranges=indexed_ranges(model,text.decode('ascii'),init,lead)
    fields={}
    for name,byte_range in ranges.items():
        try:
            raw,proof=fetch(url,MAX_FIELD,byte_range)
            fields[name]=dict(decode(raw,model,name,init,lead),proof=proof)
        except Exception:
            if name!='gust' and name not in GUST3: raise
            # Gust remains unavailable; never substitute instantaneous wind.
    if 'gust3' in fields and 'gust' not in fields:
        try:
            fields['gust3_prev']=previous_gust3(model,init,lead)
        except Exception:
            pass  # The sample then reports an honest three-hour maximum only.
    return dict(at=(init+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),lead=lead,url=url,fields=fields)


def main():
    signal.signal(signal.SIGALRM,lambda *_: sys.exit(2)); signal.alarm(108)
    request=json.loads(sys.stdin.read(4097))
    require(set(request)=={'model','init','leads'})
    model=request['model']; require(model in ('gfs','ifs','aifs_single'))
    init=datetime.strptime(request['init'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    require(init.hour in (0,6,12,18))
    leads=request['leads']; cadence=3 if model=='gfs' else 6
    require(isinstance(leads,list) and 1<=len(leads)<=3 and leads==sorted(set(leads)))
    require(all(type(h) is int and 0<=h<= (384 if model=='gfs' else 360) and h%cadence==0 for h in leads))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        result=list(pool.map(lambda h:sample(model,init,h),leads))
    print(json.dumps(result,allow_nan=False,separators=(',',':')))


if __name__=='__main__':
    try: main()
    except Exception: sys.exit(1)
