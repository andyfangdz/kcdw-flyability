"""Explicitly historical, initialization-indexed backfill; no fake fetch times."""
from __future__ import annotations

import json
import math
import os
import stat
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .common import UTC, iso_z
from .ensemble_trends import AIRPORT, LABELS, _event
from .event_ensemble import MODELS
from .trend_renderer import COLORS, METRICS, _chart

UNITS = {'pressure': 'hPa', 'wind': 'kn', 'rain': 'mm'}
IDS = {m.key: m.model_id for m in MODELS} | {'wn3': 12, 'gfs': 'gfs_global'}
MAX_BYTES, MAX_POINTS = 2_000_000, 64
MAX_INPUT_POINTS = 2 * MAX_POINTS * len(IDS)


def _time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('UTC required')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _point(raw, sample, rain_times, now):
    key = raw['model_key']
    if raw['model_id'] != IDS[key]:
        raise ValueError('model identity')
    run, retrieved = _time(raw['run_time']), _time(raw['retrieved_at'])
    if run.hour % 6 or run.minute or run.second or run.microsecond or not run <= retrieved <= now+timedelta(minutes=5):
        raise ValueError('chronology')
    horizon = 360 if key in ('wn3','ecmwf_ens','aifs_ens') else 384 if key == 'gfs' else 840
    if not run < sample <= run+timedelta(hours=horizon) or _time(raw['sample_time']) != sample:
        raise ValueError('fixed sample coverage')
    if [_time(t) for t in raw['rain_times']] != rain_times or rain_times[-1] > run+timedelta(hours=horizon):
        raise ValueError('window coverage')
    grid = raw['grid_point']
    if any(not _finite(grid[k]) or abs(grid[k]-AIRPORT[k]) > 1 for k in ('latitude','longitude')):
        raise ValueError('grid')
    source = urlparse(raw['source_url'])
    if source.scheme != 'https' or source.username or source.password or source.fragment:
        raise ValueError('source URL')
    binding = raw['run_binding']
    if key == 'wn3':
        # Preserve already archived Weather Lab points, while all new points
        # are sourced from the official immutable statistics store.
        legacy = source.netloc == 'deepmind.google.com' and source.path == '/science/weatherlab/'
        official = (source.netloc == 'storage.googleapis.com' and
                    source.path == '/weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/')
        if binding != 'response-bound' or not (legacy or official):
            raise ValueError('WN3 binding')
    elif binding == 'archive-request-bound':
        # Only this archive/model path has been verified. Conventional member
        # archives must get their own validated contract before being accepted.
        if key != 'gfs' or source.netloc != 'single-runs-api.open-meteo.com' or source.path != '/v1/forecast':
            raise ValueError('archive endpoint')
        query = parse_qs(source.query, keep_blank_values=True)
        expected = {'models': str(IDS[key]), 'latitude': str(AIRPORT['latitude']),
                    'longitude': str(AIRPORT['longitude']), 'wind_speed_unit': 'kn',
                    'precipitation_unit': 'mm', 'timezone': 'UTC'}
        if any(query.get(k) != [v] for k,v in expected.items()):
            raise ValueError('archive request model/location/units')
        selected = query.get('run', [])
        if len(selected) != 1:
            raise ValueError('archive run selection')
        chosen = datetime.fromisoformat(selected[0])
        if chosen.tzinfo is not None and chosen.utcoffset() != timedelta(0):
            raise ValueError('archive run must be UTC')
        if chosen.replace(tzinfo=UTC) != run:
            raise ValueError('archive run selection')
    else:
        raise ValueError('unsupported binding')
    metrics = raw['metrics']
    if set(metrics) != set(UNITS):
        raise ValueError('metric keys')
    clean = {}
    for name, value in metrics.items():
        if value is None:
            clean[name] = None
            continue
        bounds = {'pressure': (750,1150), 'wind': (0,300), 'rain': (0,12000)}[name]
        if set(value) != {'center','low','high'} or not _finite(value['center']):
            raise ValueError('metric shape')
        if any(v is not None and (not _finite(v) or not bounds[0] <= v <= bounds[1]) for v in value.values()):
            raise ValueError('metric bounds')
        low, high = value['low'], value['high']
        if (low is None) != (high is None) or (low is not None and low > high):
            raise ValueError('band')
        if (key == 'gfs' or key == 'wn3' and name == 'rain') and low is not None:
            raise ValueError('invalid marginal total band')
        if key not in ('wn3','gfs') and low is not None and not low <= value['center'] <= high:
            raise ValueError('median order')
        clean[name] = dict(value)
    if not any(v is not None for v in clean.values()):
        raise ValueError('no metrics')
    return dict(model_key=key, model_id=IDS[key],run_time=iso_z(run),retrieved_at=iso_z(retrieved),
                source_url=raw['source_url'],grid_point={k:grid[k] for k in ('latitude','longitude')},
                sample_time=iso_z(sample),rain_times=[iso_z(t) for t in rain_times],metrics=clean,run_binding=binding)


def validate_run_history(data, event, now):
    """Return bounded, isolated valid records, or None for a bad root identity."""
    try:
        identity, sample, rain_times = _event(event)
        if not isinstance(data,dict) or data.get('version') != 1 or data.get('event') != identity or data.get('airport') != AIRPORT or data.get('units') != UNITS:
            return None
        raw = data['points']
        if not isinstance(raw,list) or len(raw)>MAX_INPUT_POINTS:
            return None
        groups = defaultdict(list)
        for record in raw:
            try:
                p = _point(record,sample,rain_times,now)
                groups[(p['model_key'],p['run_time'])].append(p)
            except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
                continue
        models = defaultdict(list)
        for (key,_), records in groups.items():
            # Exact duplicates are harmless; conflicting values at one cycle are
            # not ordered into a trend by their later download timestamp.
            if len({json.dumps([p['metrics'],p['grid_point']],sort_keys=True) for p in records}) == 1:
                models[key].append(max(records,key=lambda p:p['retrieved_at']))
        points = []
        for key in IDS:
            records = sorted(models[key],key=lambda p:p['run_time'])
            if records:
                grid = records[-1]['grid_point']
                if key == 'wn3':
                    compatible = [p for p in records if all(abs(p['grid_point'][axis]-grid[axis]) <= .1
                                                             for axis in ('latitude','longitude'))]
                else:
                    compatible = [p for p in records if p['grid_point']==grid]
                points.extend(compatible[-MAX_POINTS:])
        return dict(version=1,event=identity,airport=dict(AIRPORT),units=dict(UNITS),points=points,
                    notes=[str(n)[:600] for n in data.get('notes',[])[:8]] if isinstance(data.get('notes',[]),list) else [])
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        return None


def load_run_history(path: Path, event, now):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:
                return None
            raw=handle.read(MAX_BYTES+1)
        return validate_run_history(json.loads(raw),event,now) if len(raw)<=MAX_BYTES else None
    except (OSError,ValueError,TypeError):
        return None


def render_run_history(data, event, now):
    history=validate_run_history(data,event,now)
    if not history or not history['points']:
        return ''
    models={}
    for key in COLORS:
        points=[p for p in history['points'] if p['model_key']==key]
        if not points:
            continue
        models[key]=dict(label=LABELS[key],statistic='mean' if key=='wn3' else 'deterministic' if key=='gfs' else 'median',
                         points=[dict(at=_time(p['run_time']),metrics={k:tuple(v[n] for n in ('center','low','high')) if v else None for k,v in p['metrics'].items()}) for p in points])
    dates=[p['at'] for m in models.values() for p in m['points']]
    start,end=min(dates),max(dates)
    if start==end:
        start,end=start-timedelta(hours=1),end+timedelta(hours=1)
    css=('#ensemble-run-history .run-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:.8rem}'
         '#ensemble-run-history svg{width:100%;height:auto}#ensemble-run-history svg text{font-size:10px;fill:currentColor;stroke:none}'
         '#ensemble-run-history .grid{stroke:currentColor;opacity:.12}#ensemble-run-history .central{fill:none;stroke-width:2}'
         '#ensemble-run-history .band{fill:none;stroke-width:7;opacity:.18}#ensemble-run-history circle{stroke:none;r:2.8px}'
         '#ensemble-run-history label{margin-right:.8rem}#ensemble-run-history [data-model="gfs"] .central{stroke-width:2.8;stroke-dasharray:5 3}')
    for key in models:
        css+=f'#run-{key}:not(:checked)~.run-charts [data-model="{key}"]{{display:none}}#ensemble-run-history label[for="run-{key}"]::before{{content:"━ ";color:{COLORS[key]}}}'
    retrieved=max(_time(p['retrieved_at']) for p in history['points'])
    out=['<section id="ensemble-run-history"><h2>Model-run history</h2><style>'+css+'</style>',
         f'<p>Cycles {min(dates):%b %d %HZ}–{max(dates):%b %d %HZ}. X-axis: model initialization UTC, not retrieval time. Same target: pressure/wind at noon Eastern {escape(event["date"])}, rain over {escape(event["window"])} Eastern.</p>']
    pairs=[]
    gfs={p['at']:p['metrics']['pressure'] for p in models.get('gfs',{}).get('points',[]) if p['metrics']['pressure']}
    for key in ('wn3','aifs_ens','gefs'):
        ensemble={p['at']:p['metrics']['pressure'] for p in models.get(key,{}).get('points',[]) if p['metrics']['pressure']}
        common=sorted(set(gfs)&set(ensemble))
        if len(common)>=2:
            a,b=common[-2:];before=ensemble[a][0]-gfs[a][0];after=ensemble[b][0]-gfs[b][0]
            direction='narrowing' if abs(after)<abs(before) else 'widening' if abs(after)>abs(before) else 'unchanged'
            pairs.append(f'{LABELS[key]} central pressure minus GFS: {before:.1f} → {after:.1f} hPa ({direction}; {a:%m/%d %HZ} → {b:%m/%d %HZ})')
    if pairs:
        out.append('<p><strong>Same initialization:</strong> '+' · '.join(pairs[:2])+'. Positive gap means GFS is lower; pressure alone is not a flight verdict.</p>')
    for key,model in models.items():
        out.append(f'<input id="run-{key}" type="checkbox" checked><label for="run-{key}">{model["label"]} / {model["statistic"]} · {len(model["points"])} runs</label>')
    out.append('<div class="run-charts">')
    for metric,(label,unit,_,_) in METRICS.items():
        out.append(f'<div><h3>{label} · {unit}</h3>'+_chart(metric,models,start,end,axis='model initialization')+'</div>')
    out.append(f'</div><p><small>Archived guidance recovered {retrieved:%b %d %H:%MZ}; not current-source availability. Faint bars are p10–p90, not all-member ranges. WN3 rain sums means, without a fabricated window band. GFS has no band.</small></p>')
    out.append('<details><summary>Run provenance and coverage</summary><p>WeatherNext 3 initialization is response-bound. Open-Meteo Single Runs selects the archived initialization in the request; its JSON does not echo a run identifier. Native model output may be interpolated to hourly values. These recovered records do not alter earlier page-fetch timestamps or aviation readiness.</p>')
    for key in models:
        records=[p for p in history['points'] if p['model_key']==key]
        grid=records[-1]['grid_point']
        out.append(f'<p>{LABELS[key]}: {len(records)} runs, {records[0]["run_time"]} to {records[-1]["run_time"]}; grid {grid["latitude"]:.4f}, {grid["longitude"]:.4f}. <a href="{escape(records[-1]["source_url"],quote=True)}" rel="noreferrer">Source</a></p>')
    for note in history['notes']:
        out.append('<p>'+escape(note)+'</p>')
    out.append('</details></section>')
    return ''.join(out)
