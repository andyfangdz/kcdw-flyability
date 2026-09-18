"""Collect packing-error proofs only where cumulative rain decreases.

Raw totals/clocks stay unchanged. This is collection-time I/O only: normalized
consumers use the persisted error bounds, never download during rendering.
"""
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path

CACHE=Path(__file__).resolve().parents[1]/'var/native-rain-packing'


def _error(point):
    from . import direct_ensemble_worker as worker
    import eccodes as ec
    digest=point['sha256'];path=CACHE/(digest+'.json')
    try:
        saved=json.loads(path.read_text())
        if saved['sha256']==digest and saved['url']==point['url'] and saved['range']==point['range']:
            value=saved['packing_error']
            if type(value) in (int,float) and math.isfinite(value) and value>=0:return value
    except (OSError,KeyError,ValueError,TypeError):pass
    raw=worker.fetch(point['url'],worker.MAX_FIELD,point['range'])
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('rain packing source changed')
    g=ec.codes_new_from_message(raw)
    try:
        try:value=float(ec.codes_get(g,'packingError'))
        except ec.KeyValueNotFoundError:
            # GRIB2 lossless JPEG2000 still quantizes physical values using
            # Y=(R+X*2**E)*10**(-D). With exact R=0 its half-bin error is known.
            if ec.codes_get(g,'packingType')!='grid_jpeg' or ec.codes_get(g,'typeOfCompressionUsed')!=0 or ec.codes_get(g,'referenceValue')!=0:
                raise ValueError('unsupported rain packing precision')
            value=.5*2.**ec.codes_get(g,'binaryScaleFactor')*10.**(-ec.codes_get(g,'decimalScaleFactor'))
    finally:ec.codes_release(g)
    if not math.isfinite(value) or value<0:raise ValueError('invalid GRIB packing error')
    worker._atomic_json(path,dict(sha256=digest,url=point['url'],range=point['range'],packing_error=value))
    return value


def enrich(packet):
    """Add exact native-unit error bounds to negative-difference point pairs."""
    if packet['model']=='gefs':return packet
    rain={(p['member'],p['lead']):p for p in packet['points'] if p['field']=='tp'}
    wanted={}
    for (member,lead),point in rain.items():
        previous=rain.get((member,lead-6))
        if previous is not None and point['value']<previous['value']:
            for p in (previous,point):
                if 'packing_error' not in p:wanted[(p['member'],p['lead'])]=p
    if len(wanted)>2000:raise ValueError('rain precision request budget exceeded')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        jobs={pool.submit(_error,p):p for p in wanted.values()}
        for job in concurrent.futures.as_completed(jobs):jobs[job]['packing_error']=job.result()
    packet['rain_precision_version']=1
    return packet
