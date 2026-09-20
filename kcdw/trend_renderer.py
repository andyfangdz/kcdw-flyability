"""Small, dependency-free SVG history of a fixed event forecast target."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import math
from zoneinfo import ZoneInfo

COLORS = {'gefs': '#b54a2b', 'ecmwf_ens': '#1f5f8b', 'aifs_ens': '#26764e',
          'geps': '#6b4f9e', 'wn3': '#c02679', 'gfs': '#172b3a'}
METRICS = {'pressure': ('PRESSURE', 'hPa', 750, 1150),
           'wind': ('WIND', 'kt', 0, 300), 'rain': ('RAIN', 'mm', 0, 12000)}


def _text(value, limit=100):
    return escape(str(value)[:limit], quote=True)


def _time(value):
    if not isinstance(value, str) or len(value) > 60:
        return None
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _number(value, lower, upper):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value) and lower <= value <= upper:
                return float(value)
        except (ValueError, OverflowError):
            pass
    return None


def _models(raw, now, as_of):
    """Whitelist keys and bound every archive value before interpolation."""
    result = {}
    if not isinstance(raw, dict):
        return result
    for key in COLORS:
        source = raw.get(key)
        if not isinstance(source, dict) or not isinstance(source.get('points'), list):
            continue
        points = []
        for item in source['points'][-256:]:
            if not isinstance(item, dict):
                continue
            at = _time(item.get('collected_at'))
            if at is None or not now-timedelta(hours=72) <= at <= now:
                continue
            metrics = item.get('metrics')
            if not isinstance(metrics, dict):
                continue
            clean = {}
            for metric, (_, _, lower, upper) in METRICS.items():
                entry = metrics.get(metric)
                if not isinstance(entry, dict):
                    clean[metric] = None
                    continue
                center = _number(entry.get('center'), lower, upper)
                lo = _number(entry.get('low'), lower, upper)
                hi = _number(entry.get('high'), lower, upper)
                if key == 'gfs' or (key == 'wn3' and metric == 'rain') or lo is None or hi is None or lo > hi:
                    lo = hi = None
                clean[metric] = (center, lo, hi) if center is not None else None
            run = _time(item.get('run_time')) if item.get('run_binding') == 'response-bound' else None
            points.append(dict(at=at, metrics=clean, run=run, is_current=item.get('is_current') is True, advertised=_time(item.get('run_time')), invalid_run=item.get('run_time') is not None and _time(item.get('run_time')) is None))
        points.sort(key=lambda p: p['at'])
        # Collapse consecutive identical payloads only: A→B→A is real history.
        unique = []
        previous = None
        for p in points:
            identity = ('run', p['run']) if p['run'] else tuple(p['metrics'].values())
            if unique and identity == previous:
                unique[-1] = p
            else:
                unique.append(p)
            previous = identity
        points = unique[-8:]
        if points:
            advertised = points[-1]['advertised']
            expired = points[-1]['invalid_run'] or (advertised is not None and not timedelta(0) <= now-advertised <= timedelta(hours=18 if key == 'wn3' else 24))
            result[key] = dict(label=_text(source.get('label', key), 48),
                               statistic='mean' if key == 'wn3' else 'deterministic' if key == 'gfs' else 'median',
                               available=source.get('current_available') is True and not expired and points[-1]['is_current'] and points[-1]['at'] == as_of and timedelta(0) <= now-points[-1]['at'] <= timedelta(hours=8), expired=expired,
                               provenance=_text(source.get('provenance', ''), 100), points=points)
    return result


def _summary(models, raw_pairs, now, fresh, compact=False):
    if compact and any(key in models for key in ('wn3', 'aifs_ens')):
        models = {key: models[key] for key in ('wn3', 'aifs_ens', 'gfs') if key in models}
    gfs = models.get('gfs')
    comparisons = []
    raw_pairs = raw_pairs if isinstance(raw_pairs, dict) else {}
    for key, model in models.items():
        if key == 'gfs' or not gfs:
            continue
        records = raw_pairs.get(key, [])
        pairs = []
        for row in records[-256:] if isinstance(records, list) else []:
            if not isinstance(row, dict):
                continue
            at = _time(row.get('collected_at'))
            vals = [_number(row.get(k), 750, 1150) for k in ('gfs', 'center', 'low', 'high')]
            if at is None or not now-timedelta(hours=72) <= at <= now or vals[0] is None or vals[1] is None:
                continue
            if vals[2] is None or vals[3] is None or vals[2] > vals[3]:
                vals[2] = vals[3] = None
            pairs.append((at, vals, row.get('is_current') is True))
        pairs = sorted({p[0]: p for p in pairs}.values(), key=lambda p: p[0])
        distinct = []
        for pair in pairs:
            if distinct and distinct[-1][1] == pair[1]:
                distinct[-1] = pair
            else:
                distinct.append(pair)
        pairs = distinct[-8:]
        if len(pairs) >= 2:
            before, after = pairs[-2][1], pairs[-1][1]
            old, new = before[1]-before[0], after[1]-after[0]
            direction = 'narrowing' if abs(new) < abs(old) else 'widening' if abs(new) > abs(old) else 'unchanged'
            line = f"{model['label']}: central gap {old:.1f} → {new:.1f} hPa ({direction})"
            if not compact and before[2] is not None and after[2] is not None:
                old_edge, new_edge = before[2]-before[0], after[2]-after[0]
                edge_direction = 'narrowing' if abs(new_edge) < abs(old_edge) else 'widening' if abs(new_edge) > abs(old_edge) else 'unchanged'
                line += f'; p10-edge gap {old_edge:.1f} → {new_edge:.1f} hPa ({edge_direction})'
            current = fresh and pairs[-1][2] and now-pairs[-1][0] <= timedelta(hours=8) and model['available'] and gfs['available']
            if current and after[2] is not None and after[0] < after[2]:
                line += '; current GFS below p10'
            elif not current:
                line += '; historical comparison, not current'
            comparisons.append(line)
        else:
            gp = [p for p in gfs['points'] if p['metrics']['pressure']]
            mp = [p for p in model['points'] if p['metrics']['pressure']]
            if gp and mp:
                central, edge, _ = mp[-1]['metrics']['pressure']
                value = gp[-1]['metrics']['pressure'][0]
                line = f"{model['label']}: latest available central gap {central-value:.1f} hPa"
                if edge is not None:
                    line += f'; p10-edge gap {edge-value:.1f} hPa'
                comparisons.append(line + '; no change comparison (no paired history)')
    prefix = 'GFS pressure — ' if comparisons else 'No history yet for a paired GFS pressure comparison; no change comparison.'
    if comparisons and not any(isinstance(v, list) and len(v) >= 2 for v in raw_pairs.values()):
        prefix = 'No history yet for paired changes. GFS pressure — '
    text = prefix + ' · '.join(comparisons)
    unavailable = [m['label'] for m in models.values() if not m['available']]
    if unavailable:
        text += '. Historical only: ' + ', '.join(unavailable) + ' (current unavailable).'
    if compact:
        return text + '. Positive gap = GFS below comparator; same-fetch pairs, not synchronized cycles. Pressure is not a flight verdict.'
    return text + '. Positive gap = GFS below comparator. Gap changes use same-fetch pairs; asynchronous model updates, not cycle-synchronized. Raw pressure, not a flight verdict.'


def _chart(metric, models, start, end, axis="retrieval/update time"):
    label, unit, _, _ = METRICS[metric]
    values = [v for model in models.values() for p in model['points']
              if p['metrics'][metric] for v in p['metrics'][metric] if v is not None]
    low, high = (min(values), max(values)) if values else (0, 1)
    pad = max((high-low)*.12, .5)
    low, high = low-pad, high+pad
    def x(at):
        return 42 + (at-start).total_seconds()/max((end-start).total_seconds(), 1)*496
    def y(value):
        return 123-(value-low)/(high-low)*104
    out = [f'<svg data-metric="{metric}" viewBox="0 0 560 165" role="img" aria-label="{label}: {axis} in UTC">',
           f'<title>{label} ({unit}), fixed forecast target; x = {axis} UTC</title>']
    for val in (low+pad, (low+high)/2, high-pad):
        yy = y(val)
        out.append(f'<path class="grid" d="M42 {yy:.1f}H538"/><text x="39" y="{yy+3:.1f}" text-anchor="end">{val:.1f}</text>')
    for at, anchor in ((start, 'start'), (start+(end-start)/2, 'middle'), (end, 'end')):
        out.append(f'<text x="{x(at):.1f}" y="144" text-anchor="{anchor}">{at:%m/%d %HZ}</text>')
    for key, model in models.items():
        out.append(f'<g data-model="{key}" fill="{COLORS[key]}" stroke="{COLORS[key]}"><title>{model["label"]} / {model["statistic"]}</title>')
        line, bands, dots = [], [], []
        connected = False
        for p in model['points']:
            entry = p['metrics'][metric]
            if entry is None:
                connected = False
                continue
            center, lo, hi = entry
            xx, yy = x(p['at']), y(center)
            line.append(f'{"L" if connected else "M"}{xx:.1f} {yy:.1f}')
            connected = True
            if lo is not None:
                bands.append(f'M{xx:.1f} {y(lo):.1f}V{y(hi):.1f}')
            tip = f'{key.replace("_ens", "").upper()} {model["statistic"]} {center:.1f} {unit}; {p["at"]:%m/%d %H:%MZ}'
            if lo is not None:
                tip += f'; p10–p90 {lo:.1f}–{hi:.1f}'
            dots.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}"><title>{tip}</title></circle>')
        if bands:
            out.append('<path class="band" d="'+' '.join(bands)+'"/>')
        if line:
            out.append('<path class="central" d="'+' '.join(line)+'"/>')
        out.extend(dots)
        out.append('</g>')
    if not values:
        out.append('<text x="280" y="75" text-anchor="middle">No available samples</text>')
    out.append('</svg>')
    return ''.join(out)


def render_trends(trends: dict | None, event: dict, now: datetime) -> str:
    """Render bounded historical data; absence is visible and never a zero."""
    opening = '<section id="ensemble-trends"><h2>Ensemble forecast trends</h2>'
    if not isinstance(trends, dict) or trends.get('version') not in (1, 2):
        return opening + '<p>Trend history unavailable.</p></section>'
    if trends['version'] == 2:
        from .ensemble_trends import validate_trends
        if not validate_trends(trends, event):
            return opening + '<p>Trend history unavailable: invalid native provenance.</p></section>'
    archived_event = trends.get('event')
    if not isinstance(event, dict) or not isinstance(archived_event, dict) or any(
            not event.get(k) or archived_event.get(k) != event.get(k) for k in ('slug', 'date', 'window')):
        return opening + '<p>Trend history unavailable: event mismatch.</p></section>'
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    sample = _time(trends.get('sample_time'))
    if sample is None:
        return opening + '<p>Trend history unavailable: missing fixed sample time.</p></section>'
    local = sample.astimezone(ZoneInfo('America/New_York'))
    if local.date().isoformat() != event['date'] or (local.hour, local.minute, local.second) != (12, 0, 0):
        return opening + '<p>Trend history unavailable: fixed noon target mismatch.</p></section>'
    models = _models(trends.get('models'), now, _time(trends.get('as_of')))
    if not models:
        return opening + '<p>Trend history unavailable: no valid samples in the last 72 hours.</p></section>'
    dates = [p['at'] for m in models.values() for p in m['points']]
    start, end = min(dates), max(dates)
    if start == end:
        start, end = start-timedelta(hours=1), end+timedelta(hours=1)
    css = ('#ensemble-trends{margin:1.5rem 0}#ensemble-trends .trend-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:.8rem}'
           '#ensemble-trends svg{width:100%;height:auto}#ensemble-trends svg text{font-size:10px;fill:currentColor;stroke:none}'
           '#ensemble-trends .grid{stroke:currentColor;opacity:.12}#ensemble-trends .central{fill:none;stroke-width:1.8}'
           '#ensemble-trends .band{fill:none;stroke-width:7;opacity:.18}#ensemble-trends circle{stroke:none;r:2.8px}'
           '#ensemble-trends label{margin-right:.8rem}#ensemble-trends h3{font-size:1rem;margin:.5rem 0}'
           '#ensemble-trends .trend-detail{overflow-x:auto}#ensemble-trends th,#ensemble-trends td{padding:.3rem;text-align:left}'
           '#ensemble-trends [data-model="gfs"] .central{stroke-dasharray:5 3;stroke-width:2.5}'
           '#ensemble-trends [data-model="wn3"] .central{stroke-width:2.5}')
    for key in models:
        css += f'#trend-{key}:not(:checked)~.trend-charts [data-model="{key}"]{{display:none}}'
        css += f'#ensemble-trends label[for="trend-{key}"]::before{{content:"━ ";color:{COLORS[key]}}}'
    out = [opening, '<style>'+css+'</style>']
    if trends['version'] == 2:
        from .ensemble_trends import SOURCE_CHANGE
        out.append('<p><small>'+_text(SOURCE_CHANGE, 400)+'</small></p>')
    as_of = _time(trends.get('as_of'))
    if as_of is None or now-as_of > timedelta(hours=8) or as_of > now:
        out.append('<p><strong>Outdated or unverified collection time; this is not current guidance.</strong></p>')
    out.append(f'<p>Same target each update: pressure and wind at noon Eastern {_text(event["date"])}, rain over the full window {_text(event["window"])} Eastern. X-axis: forecast retrieval/update times (UTC), not valid time. Up to 8 distinct updates/model, last 72 hours; missing values stay gaps.</p>')
    fresh = as_of is not None and timedelta(0) <= now-as_of <= timedelta(hours=8)
    out.append('<p class="trend-summary">'+_summary(models, trends.get('pressure_comparisons'), now, fresh, compact=True)+'</p>')
    for key, m in models.items():
        status = ' · historical only' if not m['available'] else ''
        if m['expired']:
            status += ' (outdated run)'
        binding = 'response-bound cycles only when provided' if any(p['run'] for p in m['points']) else 'rolling; cycle not bound'
        out.append(f'<input id="trend-{key}" type="checkbox" checked><label for="trend-{key}" title="{m["provenance"]}; {binding}">{m["label"]} / {m["statistic"]}{status}</label>')
    out.append('<div class="trend-charts">')
    for metric, (label, unit, _, _) in METRICS.items():
        out.append(f'<div><h3>{label} · {unit}</h3>'+_chart(metric, models, start, end)+'</div>')
    out.append('</div><details><summary>All pressure-gap comparisons</summary><p>'+_summary(models, trends.get('pressure_comparisons'), now, fresh)+'</p></details>')
    out.append('<p><small>Faint vertical bars: per-update p10–p90 where supplied, not confidence probabilities. WN3 mean may lie outside its band; no WN3 rain band when unavailable. GFS is deterministic, without a band. Separate model updates are not interpolated onto shared cycles.</small></p>')
    out.append('<details class="trend-detail"><summary>Latest values, changes and source timing</summary><table><thead><tr><th>Model / statistic</th><th>Pressure hPa</th><th>Rain mm</th><th>Wind kt</th><th>Latest retrieval UTC / cycle</th></tr></thead><tbody>')
    for key, m in models.items():
        out.append(f'<tr><th>{m["label"]} / {m["statistic"]}</th>')
        for metric in ('pressure', 'rain', 'wind'):
            pts = [p for p in m['points'] if p['metrics'][metric]]
            value = 'missing'
            if pts:
                value = f'{pts[-1]["metrics"][metric][0]:.1f}'
                value += f' (Δ {pts[-1]["metrics"][metric][0]-pts[-2]["metrics"][metric][0]:+.1f})' if len(pts) > 1 else ' (No history yet)'
                if pts[-1]['at'] != m['points'][-1]['at']:
                    value += ' — historical; latest missing'
            out.append('<td>'+value+'</td>')
        p = m['points'][-1]
        cycle = p['run'].strftime('%m/%d %H:%MZ')+' response-bound' if p['run'] else 'rolling; cycle not bound' if key != 'wn3' else 'cycle unavailable'
        out.append(f'<td>{p["at"]:%m/%d %H:%MZ} / {cycle}</td></tr>')
    out.append('</tbody></table></details></section>')
    return ''.join(out)
