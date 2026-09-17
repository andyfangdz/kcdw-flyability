"""Forward-fill fixed-event, initialization-indexed weather history.

Caller must serialize updates (including publication) with its event lock.
Fetchers are injectable: GFS takes one UTC datetime and returns
{raw, source_url, retrieved_at}; WN3 takes <=8 UTC datetimes and returns
{records: [{context, raw, forecast, retrieved_at}], errors: [{run_time, error}]}.
No live-source freshness test or rolling metadata establishes archive identity.
"""
from __future__ import annotations

import copy
import json
import math
import os
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import UTC, atomic_write, iso_z
from .ensemble_trends import AIRPORT, _event
from .run_history import MAX_BYTES, UNITS, _finite, _point, _time, validate_run_history
from .weathernext3 import SPECS, _number

MAX_CATCHUP = 8
SOURCE_BYTES = 3 * 1024 * 1024
HELPER = Path(__file__).resolve().parents[1]/'services/weatherlab/recover-runs.mjs'
GFS_BASE = 'https://single-runs-api.open-meteo.com/v1/forecast'
SAFE_ERRORS = {'auth_required','upstream_error','validation_error','cdp_unavailable',
               'timeout','stale','no_data','cache_error','source_write_error'}


def gfs_source_url(run: datetime) -> str:
    from urllib.parse import urlencode
    return GFS_BASE+'?'+urlencode(dict(latitude=str(AIRPORT['latitude']), longitude=str(AIRPORT['longitude']),
        hourly='pressure_msl,wind_speed_10m,precipitation', models='gfs_global', wind_speed_unit='kn',
        precipitation_unit='mm', timezone='UTC', timeformat='iso8601', forecast_days='16',
        run=run.strftime('%Y-%m-%dT%H:%M')))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('upstream_error')


def fetch_gfs_run(run: datetime) -> dict:
    url = gfs_source_url(run)
    with build_opener(_NoRedirect()).open(Request(url,headers={'Accept':'application/json'}),timeout=30) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError('upstream_error')
        body = response.read(SOURCE_BYTES+1)
    retrieved = iso_z(datetime.now(UTC))
    if len(body)>SOURCE_BYTES:
        raise ValueError('upstream_error')
    return dict(raw=json.loads(body),source_url=url,retrieved_at=retrieved)


def fetch_wn3_runs(runs: list[datetime]) -> dict:
    if len(runs)>MAX_CATCHUP:
        raise ValueError('validation_error')
    # Helper owns authenticated browser/cache access; no credentials or stderr
    # are returned, persisted, or included in exception text here.
    proc = subprocess.run(['node',str(HELPER)],input=json.dumps({'runs':[r.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z') for r in runs]}),
                          text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=135,check=False)
    if proc.returncode or len(proc.stdout)>MAX_CATCHUP*SOURCE_BYTES:
        raise ValueError('upstream_error')
    return json.loads(proc.stdout)


def _read(path: Path, limit: int) -> Any:
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
            raise ValueError('validation_error')
        body = handle.read(limit+1)
    if len(body)>limit:
        raise ValueError('validation_error')
    return json.loads(body)


def _write(path: Path, data):
    atomic_write(path,json.dumps(data,allow_nan=False,separators=(',',':'))+'\n')


def _safe_error(value):
    return value if isinstance(value,str) and value in SAFE_ERRORS else 'upstream_error'


def _require(condition):
    if not condition:
        raise ValueError('validation_error')


def _base(key, run, record, sample, rain_times, grid, metrics):
    return dict(model_key=key,model_id=12 if key=='wn3' else 'gfs_global',run_time=iso_z(run),
                retrieved_at=record['retrieved_at'],source_url='https://deepmind.google.com/science/weatherlab/' if key=='wn3' else record['source_url'],
                grid_point=grid,sample_time=iso_z(sample),rain_times=[iso_z(t) for t in rain_times],metrics=metrics,
                run_binding='response-bound' if key=='wn3' else 'archive-request-bound')


def _metric(value,low=None,high=None):
    return dict(center=value,low=low,high=high) if value is not None else None


def _gfs_point(record,run,sample,rain_times,now):
    from urllib.parse import parse_qs, urlparse
    # Exact query semantics, independent of parameter order/escaping.
    source=urlparse(record['source_url']);expected=urlparse(gfs_source_url(run))
    _require((source.scheme,source.netloc,source.path,source.fragment)==(expected.scheme,expected.netloc,expected.path,''))
    _require(parse_qs(source.query)==parse_qs(expected.query))
    d=record['raw']
    _require(isinstance(d,dict) and not d.get('error'))
    _require(d['timezone'] in ('GMT','UTC','Etc/UTC') and type(d['utc_offset_seconds']) is int and d['utc_offset_seconds']==0)
    # API does not echo model/run; reject any contradictory optional echo.
    _require(d.get('model','gfs_global')=='gfs_global')
    _require(d['hourly_units']==dict(time='iso8601',pressure_msl='hPa',wind_speed_10m='kn',precipitation='mm'))
    grid={k:d[k] for k in ('latitude','longitude')}
    _require(all(_finite(v) and abs(v-AIRPORT[k])<=.3 for k,v in grid.items()))
    hourly=d['hourly']; axis=hourly['time']
    _require(isinstance(axis,list) and 384<=len(axis)<=385)
    times=[]
    for text in axis:
        _require(isinstance(text,str))
        instant=datetime.fromisoformat(text.replace('Z','+00:00'))
        _require(instant.tzinfo is None or instant.utcoffset()==timedelta(0))
        times.append(instant.replace(tzinfo=UTC))
    _require(times==[run+timedelta(hours=i) for i in range(len(times))])
    for field,bounds in (('pressure_msl',(800,1100)),('wind_speed_10m',(0,250)),('precipitation',(0,500))):
        values=hourly[field]
        _require(isinstance(values,list) and len(values)==len(times))
        _require(all(v is None or _number(v,*bounds) for v in values))
    i=times.index(sample)
    pressure,wind=hourly['pressure_msl'][i],hourly['wind_speed_10m'][i]
    rain=[hourly['precipitation'][times.index(t)] for t in rain_times]
    _require(pressure is not None and wind is not None and all(v is not None for v in rain))
    metrics=dict(pressure=_metric(pressure),wind=_metric(wind),rain=_metric(round(math.fsum(rain),6)))
    return _point(_base('gfs',run,record,sample,rain_times,grid,metrics),sample,rain_times,now)


def _wn3_point(record,run,sample,rain_times,now):
    f=record['forecast']; context=record['context']; raw=record['raw']
    _require(context==dict(latitude=AIRPORT['latitude'],longitude=AIRPORT['longitude'],model_id=12,
                          init_seconds=int(run.timestamp()),fields=[1,5,2,3]))
    _require(f['model_id']==12 and type(f['model_id']) is int and f['model']=='WeatherNext 3')
    _require(f['source']=='Weather Lab GetForecastStatistics')
    _require(_time(f['response_init_utc'])==run and _time(f['requested_init_utc'])==run)
    grid={k:f[k] for k in ('latitude','longitude')}
    _require(all(_finite(v) and v==AIRPORT[k] for k,v in grid.items()))
    times=[_time(t) for t in f['valid_time_utc']]
    _require(times==[run+timedelta(hours=i) for i in range(1,361)])
    fields=f['fields'];_require(set(fields)==set(SPECS))
    for name,(unit,lo,hi) in SPECS.items():
        field=fields[name]
        _require(set(field)=={'unit','mean','p10','p90'} and field['unit']==unit)
        for statistic in ('mean','p10','p90'):
            values=field[statistic]
            _require(isinstance(values,list) and len(values)==360 and all(_number(v,lo,hi) for v in values))
        _require(all(a<=b for a,b in zip(field['p10'],field['p90'])))
    # Cross-check weather-only originals, rather than trusting a normalized
    # forecast pasted beside an unrelated response.
    _require(isinstance(raw,list) and len(raw)==4 and raw[0]==[int(run.timestamp())])
    _require(raw[1]==[[int(t.timestamp())] for t in times])
    ids={1:'temperature_2m',5:'precipitation_1h',2:'sea_level_pressure',3:'wind_speed_10m'}
    _require(len(raw[2])==4 and len(raw[3])==8)
    means={r[0]:r[1] for r in raw[2]};quantiles={(r[0],r[1]):r[2] for r in raw[3]}
    _require(means=={k:fields[v]['mean'] for k,v in ids.items()})
    _require(quantiles=={(k,q):fields[v][f'p{q}'] for k,v in ids.items() for q in (10,90)})
    i=times.index(sample);metrics={}
    for name,field,scale in (('pressure','sea_level_pressure',.01),('wind','wind_speed_10m',3600/1852)):
        v=fields[field];metrics[name]=_metric(*(v[s][i]*scale for s in ('mean','p10','p90')))
    metrics['rain']=_metric(math.fsum(fields['precipitation_1h']['mean'][times.index(t)] for t in rain_times))
    return _point(_base('wn3',run,record,sample,rain_times,grid,metrics),sample,rain_times,now)


def _candidates(key,points,sample,rain_times,now):
    horizon=360 if key=='wn3' else 384
    existing=[_time(p['run_time']) for p in points if p['model_key']==key]
    earliest=min(existing) if existing else now-timedelta(hours=48)
    earliest=max(earliest,rain_times[-1]-timedelta(hours=horizon))
    latest=min(now-timedelta(hours=5),sample-timedelta(seconds=1),rain_times[0]-timedelta(hours=1))
    run=latest.replace(hour=latest.hour//6*6,minute=0,second=0,microsecond=0)
    result=[]
    while run>=earliest:
        if run not in existing:result.append(run)
        run-=timedelta(hours=6)
    return result


def refresh_run_history(event_dir: Path,event: dict,now: datetime,*,wn3_fetcher=None,gfs_fetcher=None) -> dict|None:
    """Append valid missing runs atomically; failures never replace old history.

    Seed's earliest cycle is the lower bound. Without a seed start at 48h.
    Least-recently checked holes prevent permanent recent misses starving older
    cycles; never-checked cycles are selected newest-first. Status is bounded.
    Returns normalized history (or None for an invalid existing root).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware now required')
    now=now.astimezone(UTC)
    identity,sample,rain_times=_event(event)
    root=Path(event_dir);path=root/'backfill.json';status_path=root/'run-history-status.json'
    status: dict[str, Any] = dict(version=1,event=identity,checked_at=iso_z(now),models={})
    try:
        try:original=_read(path,MAX_BYTES)
        except FileNotFoundError:
            original=dict(version=1,event=identity,airport=dict(AIRPORT),units=dict(UNITS),points=[],notes=[])
        history=validate_run_history(original,event,now)
        # Do not turn an update into a lossy repair of a malformed seed.
        if history is None or len(history['points'])!=len(original['points']):
            raise ValueError('validation_error')
    except (OSError,ValueError,TypeError,KeyError):
        status['error']='invalid_existing_history'
        try:_write(status_path,status)
        except OSError:pass
        return None
    try:
        previous=_read(status_path,100_000)
        if previous.get('event')!=identity or not isinstance(previous.get('models'),dict):previous={}
    except (OSError,ValueError,TypeError,AttributeError):previous={}
    updated=copy.deepcopy(original)
    sources: list[tuple[str, Callable[..., Any], Callable[..., Any]]] = [
        ('wn3',wn3_fetcher or fetch_wn3_runs,_wn3_point),('gfs',gfs_fetcher or fetch_gfs_run,_gfs_point)]
    for key,fetcher,convert in sources:
        candidates=_candidates(key,history['points'],sample,rain_times,now)
        model_state=previous.get('models',{}).get(key,{})
        prior=model_state.get('runs',{}) if isinstance(model_state,dict) else {}
        if not isinstance(prior,dict):prior={}
        states={}
        for run in candidates:
            old=prior.get(iso_z(run),{})
            try:
                checked=_time(old['checked_at'])
                _require(checked<=now)
                states[iso_z(run)]=dict(checked_at=iso_z(checked),error=_safe_error(old.get('error')))
            except (ValueError,KeyError,TypeError):pass
        chosen=sorted(candidates,key=lambda r:(states.get(iso_z(r),{}).get('checked_at',''),-r.timestamp()))[:MAX_CATCHUP]
        records={};helper_errors={};needed=[]
        for run in chosen:
            target=root/'backfill-sources'/key/(run.strftime('%Y%m%dT%H%MZ')+'.json')
            try:records[iso_z(run)]=_read(target,SOURCE_BYTES)
            except FileNotFoundError:needed.append(run)
            except (OSError,ValueError,TypeError):helper_errors[iso_z(run)]='cache_error'
        if key=='wn3' and needed:
            try:
                response=fetcher(needed)
                _require(isinstance(response,dict) and isinstance(response.get('records'),list) and len(response['records'])<=MAX_CATCHUP)
                wanted={iso_z(r) for r in needed}
                for record in response['records']:
                    try:
                        stamp=iso_z(_time(record['forecast']['response_init_utc']))
                        if stamp not in wanted:continue
                        if stamp in records:
                            records[stamp]=None;helper_errors[stamp]='validation_error'
                        else:records[stamp]=record
                    except (ValueError,TypeError,KeyError):continue
                for error in response.get('errors',[]):
                    if not isinstance(error,dict):continue
                    try:stamp=iso_z(_time(error.get('run_time')))
                    except (ValueError,TypeError):continue
                    if stamp in wanted:helper_errors[stamp]=_safe_error(error.get('error'))
            except Exception:
                for run in needed:helper_errors[iso_z(run)]='upstream_error'
        acquired=0
        grid=next((p['grid_point'] for p in history['points'] if p['model_key']==key),None)
        for run in chosen:
            stamp=iso_z(run);state=dict(checked_at=iso_z(now),error=None);states[stamp]=state
            try:
                if key=='gfs' and run in needed:
                    try:records[stamp]=fetcher(run)
                    except Exception:
                        state['error']='upstream_error';continue
                record=records.get(stamp)
                if record is None:
                    state['error']=helper_errors.get(stamp,'no_data');continue
                point=convert(record,run,sample,rain_times,now)
                _require(grid is None or point['grid_point']==grid)
                # Only allowlisted weather data; never headers, RPC envelopes,
                # arbitrary helper metadata, or exception bodies.
                original_record={k:record[k] for k in (('context','raw','forecast','retrieved_at') if key=='wn3' else ('raw','source_url','retrieved_at'))}
                target=root/'backfill-sources'/key/(run.strftime('%Y%m%dT%H%MZ')+'.json')
                if not target.exists():
                    try:_write(target,original_record)
                    except OSError:
                        state['error']='source_write_error';continue
                updated['points'].append(point);grid=point['grid_point'];acquired+=1
            except Exception:state['error']='validation_error'
        status['models'][key]=dict(attempted=len(chosen),acquired=acquired,missing=len(candidates)-acquired,runs=states)
    clean=validate_run_history(updated,event,now)
    if clean is not None and len(clean['points'])==len(updated['points']):
        if updated['points']!=original['points']:
            # Preserve seed values/provenance verbatim; stable schema order.
            order={(p['model_key'],p['run_time']):i for i,p in enumerate(clean['points'])}
            updated['points'].sort(key=lambda p:order[(p['model_key'],p['run_time'])])
            try:_write(path,updated);history=clean
            except OSError:status['error']='history_write_error'
    else:status['error']='history_validation_error'
    try:_write(status_path,status)
    except OSError:pass
    return history
