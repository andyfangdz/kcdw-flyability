"""Isolated ecCodes downloader/decoder. Input: init and <=9 native leads only.

No cache paths, arbitrary URLs, credentials or application imports accepted.
Invoked by cloud_ceiling using the persistent native-weather virtualenv.
"""
import concurrent.futures
import hashlib
import json
import math
import re
import signal
import sys
from datetime import datetime, timedelta, timezone

FIELDS = {'ceiling': ('HGT','cloud ceiling','cloudCeiling','gpm',3,5),
          'terrain': ('HGT','surface','surface','m',3,5),
          'low_cloud': ('LCDC','low cloud layer','lowCloudLayer','%',6,3)}


def require(ok):
    if not ok: raise ValueError('native GRIB validation failed')


def url_for(init, lead):
    return (f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{init:%Y%m%d}/{init:%H}/atmos/'
            f'gfs.t{init:%H}z.pgrb2.0p25.f{lead:03d}')


def fetch(url, limit, byte_range=None):
    import requests
    headers={'Range':f'bytes={byte_range[0]}-{byte_range[1]}'} if byte_range else {}
    with requests.get(url,headers=headers,timeout=(5,12),stream=True,allow_redirects=False) as response:
        require(response.status_code==(206 if byte_range else 200))
        proof=None
        if byte_range:
            a,b=byte_range
            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
            require(match is not None)
            x,y,total=map(int,match.groups())
            require((x,y)==(a,b) and b<total<=1_000_000_000)
            proof=dict(start=a,end=b,total=total,bytes=b-a+1)
        content=bytearray()
        for block in response.iter_content(65536):
            content.extend(block); require(len(content)<=limit)
        if proof:
            require(len(content)==proof['bytes'])
            proof['sha256']=hashlib.sha256(content).hexdigest()
        return bytes(content),proof


def indexed_ranges(text, init, lead):
    rows=[line.split(':') for line in text.splitlines()]
    require(0<len(rows)<=1500)
    offsets=[int(row[1]) for row in rows]
    require(offsets==sorted(set(offsets)) and offsets[0]==0)
    selected={}
    for i,row in enumerate(rows[:-1]):
        require(len(row)>=6)
        for name,(var,level,*_) in FIELDS.items():
            if row[3:5]==[var,level] and row[5]==('anl' if lead==0 else f'{lead} hour fcst'):
                require(row[2]==f'd={init:%Y%m%d%H}' and name not in selected)
                a,b=offsets[i],offsets[i+1]-1
                require(0<b-a+1<=8_000_000)
                selected[name]=(a,b)
    require(set(selected)==set(FIELDS))
    return selected


def decode(content, name, init, lead):
    import eccodes as ec
    require(content[:4]==b'GRIB' and content[-4:]==b'7777' and int.from_bytes(content[8:16],'big')==len(content))
    g=ec.codes_new_from_message(content)
    try:
        var,level,kind,units,category,parameter=FIELDS[name]
        valid=init+timedelta(hours=lead)
        expected={'edition':2,'discipline':0,'parameterCategory':category,'parameterNumber':parameter,
                  'typeOfLevel':kind,'units':units,'dataDate':int(init.strftime('%Y%m%d')),
                  'dataTime':int(init.strftime('%H%M')),'validityDate':int(valid.strftime('%Y%m%d')),
                  'validityTime':int(valid.strftime('%H%M')),'stepType':'instant','startStep':lead,'endStep':lead,
                  'Ni':1440,'Nj':721,'numberOfDataPoints':1038240,
                  'iDirectionIncrementInDegrees':0.25,'jDirectionIncrementInDegrees':0.25}
        require(all(ec.codes_get(g,k)==v for k,v in expected.items()))
        nearest=ec.codes_grib_find_nearest(g,40.8752,285.7186)[0]
        lat,lon=float(nearest['lat']),float(nearest['lon'])
        require(abs(lat-40.8752)<=0.25 and abs(lon-285.7186)<=0.25)
        # Small fixed coordinate requests avoid serializing a global field.
        missing=ec.codes_get(g,'missingValue')
        bitmap=ec.codes_get_array(g,'bitmap') if ec.codes_get(g,'bitmapPresent') else None
        cells={}
        for dy in (-.5,-.25,0,.25,.5):
            for dx in (-.5,-.25,0,.25,.5):
                p=ec.codes_grib_find_nearest(g,lat+dy,lon+dx)[0]
                require(abs(p['lat']-(lat+dy))<1e-6 and abs(p['lon']-(lon+dx))<1e-6)
                v=float(p['value']); idx=int(p['index'])
                low,high=(-500,30000) if name=='ceiling' else (-500,9000) if name=='terrain' else (0,100)
                if not math.isfinite(v) or v==missing or (bitmap is not None and not bitmap[idx]) or not low<=v<=high:
                    v=None
                cells[(float(p['lat']),float(p['lon']))]=v
        require(len(cells)==25)
        return (lat,lon),cells
    finally:
        ec.codes_release(g)


def sample(init,lead):
    url=url_for(init,lead)
    text,_=fetch(url+'.idx',200000)
    selected=indexed_ranges(text.decode('ascii'),init,lead)
    fields={}; cells={}; center=None
    for name,byte_range in selected.items():
        content,proof=fetch(url,8_000_000,byte_range)
        point,values=decode(content,name,init,lead)
        require(center is None or point==center)
        center=point; fields[name]=proof; cells[name]=values
    require(set(cells['ceiling'])==set(cells['terrain'])==set(cells['low_cloud']))
    def agl(c,t):
        return None if c is None or t is None else max(0,(c-t)*3.280839895)
    a=[agl(c,cells['terrain'][p]) for p,c in cells['ceiling'].items()]
    a=[v for v in a if v is not None]
    low=[v for v in cells['low_cloud'].values() if v is not None]
    c,t,l=[cells[n][center] for n in ('ceiling','terrain','low_cloud')]
    return dict(lead_hour=lead,valid_at=(init+timedelta(hours=lead)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                source_url=url,latitude=center[0],longitude=center[1]-360,
                ceiling_msl_gpm=c,terrain_m=t,ceiling_agl_ft=agl(c,t),low_cloud_pct=l,fields=fields,
                neighborhood=dict(cells=25,ceiling_valid=len(a),ceiling_missing=25-len(a),
                    under_1000_ft=sum(v<1000 for v in a),agl_ft_min=min(a) if a else None,agl_ft_max=max(a) if a else None,
                    low_cloud_valid=len(low),low_cloud_pct_min=min(low) if low else None,low_cloud_pct_max=max(low) if low else None))


def main():
    # Hard wall-clock cap includes decoding, all requests, and executor shutdown.
    signal.signal(signal.SIGALRM,lambda *_: sys.exit(2)); signal.alarm(85)
    request=json.loads(sys.stdin.read(4097))
    require(set(request)=={'model_init','leads'})
    init=datetime.strptime(request['model_init'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    require(init.hour in (0,6,12,18) and init.minute==init.second==0)
    leads=request['leads']
    require(isinstance(leads,list) and 0<len(leads)<=9 and all(type(h) is int and 0<=h<=384 and h%3==0 for h in leads))
    require(leads==sorted(set(leads)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        out=list(pool.map(lambda h:sample(init,h),leads))
    print(json.dumps(out,allow_nan=False,separators=(',',':')))


if __name__=='__main__':
    try: main()
    except Exception:
        sys.exit(1)
