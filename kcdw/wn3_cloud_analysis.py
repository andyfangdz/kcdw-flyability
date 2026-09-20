"""WN3 cloud-layer timing from validated hourly marginal ensemble statistics."""
from datetime import timedelta
from html import escape

from .common import iso_z, parse_time
from .event_model_matrix import flight_window
from .events import local_clock
from .weathernext3 import validate_weather_next3

LAYERS = {'low_cloud_cover': 'Low', 'medium_cloud_cover': 'Middle',
          'high_cloud_cover': 'High', 'total_cloud_cover': 'Total'}
LIMITS = ('Cloud coverage is not cloud-base height, ceiling or visibility. Layers overlap and must not be added. '
          'p10–p90 describes the ensemble distribution at each hour, not a probability of clearing or a band for a whole-window mean. '
          'Successive hourly percentiles do not track the same members. Timing several days ahead remains uncertain.')


def cloud_evidence(snapshot, now):
    try:
        source = snapshot.get('weathernext3', {})
        if source.get('ok') is not True:
            return None
        validate_weather_next3(source['data'], now)
        forecast = source['data']['forecast']
        start, end, kind = flight_window(snapshot)
        axis = {parse_time(t): i for i, t in enumerate(forecast['valid_time_utc'])}
        # Instantaneous clouds sample [start,end); include a separately named
        # three-hour context period before and after the expected flight.
        first, last = start-timedelta(hours=3), end+timedelta(hours=3)
        times = [first+timedelta(hours=h) for h in range(int((last-first).total_seconds()/3600))]
        if not all(t in axis for t in times):
            return None
        rows = []
        for t in times:
            row = {'at': iso_z(t), 'period': 'before' if t < start else 'flight' if t < end else 'after', 'layers': {}}
            for key in LAYERS:
                field = forecast['fields'][key]
                row['layers'][key] = {s: round(field[s][axis[t]], 1) for s in ('mean', 'p10', 'p90')}
            rows.append(row)
        periods = {}
        for period in ('before', 'flight', 'after'):
            selected = [r for r in rows if r['period'] == period]
            periods[period] = {key: {
                'mean_cover_pct': round(sum(r['layers'][key]['mean'] for r in selected)/len(selected), 1),
                'highest_hourly_p90_pct': max(r['layers'][key]['p90'] for r in selected),
                'widest_hourly_p10_p90_pp': round(max(r['layers'][key]['p90']-r['layers'][key]['p10'] for r in selected), 1),
            } for key in LAYERS}
        return {'init_time': forecast['response_init_utc'], 'grid_point': forecast['grid_point'],
                'window': {'start': iso_z(start), 'end': iso_z(end), 'kind': kind},
                'sampling': 'Hourly instants, opening included and closing excluded; three hours before and after.',
                'periods': periods, 'hours': rows, 'limits': LIMITS,
                'source_url': 'https://developers.google.com/weathernext/guides/bigquery'}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
        return None


def render_clouds(data):
    if not data:
        return '<h3>WN3 cloud layers</h3><p>Fresh WN3 cloud guidance covering the flight and adjacent hours is unavailable.</p>'
    low = [data['periods'][p]['low_cloud_cover']['mean_cover_pct'] for p in ('before', 'flight', 'after')]
    window = data['window']
    summary = (f'Low-cloud mean coverage: {low[0]:g}% in the three hours before, {low[1]:g}% during the '
               f'{escape(window["kind"])}, and {low[2]:g}% in the three hours after.')
    rows = []
    for row in data['hours']:
        values = ''.join(f'<td>{row["layers"][key]["mean"]:g}%<small> {row["layers"][key]["p10"]:g}–{row["layers"][key]["p90"]:g}%</small></td>' for key in LAYERS)
        rows.append(f'<tr><th scope="row">{escape(local_clock(row["at"]))} · {row["period"]}</th>{values}</tr>')
    return (f'<div id="wn3-cloud-layers"><h3>WN3 cloud layers around the flight</h3><p><strong>{summary}</strong></p>'
            f'<p>Flight window: {escape(local_clock(window["start"]))}–{escape(local_clock(window["end"]))} Eastern. '
            f'Actual model initialization: {escape(data["init_time"])}.</p>'
            '<details><summary>Hourly cloud coverage and uncertainty</summary><div class="chart-scroll" tabindex="0" role="region" aria-label="WN3 hourly cloud layers">'
            '<table><caption>Mean coverage; smaller numbers show the hourly p10–p90 range. All values are percent of the grid cell.</caption>'
            '<thead><tr><th scope="col">Eastern · period</th>'+''.join(f'<th scope="col">{v} cloud</th>' for v in LAYERS.values())+
            '</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
            f'<p class="small">{escape(LIMITS)} Samples include each period’s opening hour and exclude its closing hour. '
            f'<a href="{data["source_url"]}">Google WN3 ensemble statistics via BigQuery</a>.</p></details></div>')


def collect_previous(snapshot, runs_dir, now, *, backfill=False):
    """Retain a bounded, independently validated older run for identical hours."""
    import hashlib
    import json
    from pathlib import Path
    current = cloud_evidence(snapshot, now)
    if not current:
        return None
    candidates, used = [], 0
    from itertools import islice
    paths = sorted((p for p in islice(Path(runs_dir).iterdir(), 4096) if not p.is_symlink() and p.is_dir()), reverse=True)[:64]
    for directory in paths:
        path = directory / 'snapshot.json'
        try:
            if path.is_symlink() or path.stat().st_size > 6*1024**2:
                continue
            used += path.stat().st_size
            if used > 32*1024**2:
                break
            raw = path.read_bytes()
            old = json.loads(raw)
            collected = parse_time(old['collected_at'])
            if old['event'] != snapshot['event'] or not now-timedelta(hours=54) <= collected < now:
                continue
            data = cloud_evidence(old, collected)
            if data and data['window'] == current['window'] and data['grid_point'] == current['grid_point'] and now-timedelta(hours=54) <= parse_time(data['init_time']) < parse_time(current['init_time']):
                candidates.append(dict(init_time=data['init_time'], grid_point=data['grid_point'], window=data['window'],
                                       hours=data['hours'], collected_at=iso_z(collected), source_sha256=hashlib.sha256(raw).hexdigest()))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if candidates:
        return max(candidates, key=lambda x:(x['init_time'],x['collected_at']))
    if backfill:
        from .weathernext3 import collect_weather_next3
        from .weathernext3_bigquery import BigQueryStore
        previous_init = parse_time(current['init_time'])-timedelta(hours=6)
        class PreviousRun(BigQueryStore):
            def candidates(self, clock):
                return [previous_init]
        envelope = collect_weather_next3(now, [parse_time(r['at']) for r in current['hours']], store=PreviousRun())
        prior = cloud_evidence(dict(snapshot, weathernext3={'ok': True, 'data': envelope}), now)
        if prior:
            raw = json.dumps(envelope, sort_keys=True).encode()
            return dict(init_time=prior['init_time'], grid_point=prior['grid_point'], window=prior['window'],
                        hours=prior['hours'], collected_at=iso_z(now), source_sha256=hashlib.sha256(raw).hexdigest(),
                        provenance={'kind': 'BigQuery previous-run backfill', 'query': envelope['forecast']['query']})
    return None


def run_comparison(snapshot, current, now):
    """Validate saved reductions, then recompute paired same-hour differences."""
    import math
    import re
    try:
        old = snapshot['wn3_cloud_previous']
        init, collected = parse_time(old['init_time']), parse_time(old['collected_at'])
        if not (now-timedelta(hours=54) <= init < parse_time(current['init_time']) and init <= collected <= parse_time(snapshot['collected_at'])+timedelta(minutes=20)):
            return None
        if old['window'] != current['window'] or old['grid_point'] != current['grid_point'] or not re.fullmatch('[a-f0-9]{64}', old['source_sha256']):
            return None
        if [(r['at'],r['period']) for r in old['hours']] != [(r['at'],r['period']) for r in current['hours']]:
            return None
        for row in old['hours']:
            if set(row['layers']) != set(LAYERS):
                return None
            for fan in row['layers'].values():
                if set(fan) != {'mean','p10','p90'} or any(type(v) not in (int,float) or not math.isfinite(v) or not 0 <= v <= 100 for v in fan.values()) or fan['p10'] > fan['p90']:
                    return None
        old_flight = [r for r in old['hours'] if r['period']=='flight']
        layers = {}
        for key in LAYERS:
            before = sum(r['layers'][key]['mean'] for r in old_flight)/len(old_flight)
            after = current['periods']['flight'][key]['mean_cover_pct']
            layers[key] = dict(previous_mean_pct=round(before,1),current_mean_pct=after,change_pp=round(after-before,1))
        return dict(previous_init=old['init_time'],current_init=current['init_time'],layers=layers,
                    meaning='Same flight hours and grid; change in mean cloud coverage in percentage points, not observations or independent confirmations.')
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, ZeroDivisionError):
        return None


def render_comparison(comparison):
    if not comparison:
        return '<p>Run-to-run cloud change unavailable: no validated distinct earlier run covers the same flight hours and grid.</p>'
    rows=''.join(f'<tr><th scope="row">{label}</th><td>{comparison["layers"][key]["previous_mean_pct"]:g}%</td>'
                 f'<td>{comparison["layers"][key]["current_mean_pct"]:g}%</td><td>{comparison["layers"][key]["change_pp"]:+g} pp</td></tr>'
                 for key,label in LAYERS.items())
    return ('<h4>What changed since the previous WN3 run?</h4>'
            f'<p>{escape(comparison["previous_init"])} → {escape(comparison["current_init"])}</p>'
            '<div class="chart-scroll"><table><caption>Mean coverage over identical flight-window hours</caption>'
            '<thead><tr><th scope="col">Cloud layer</th><th scope="col">Previous</th><th scope="col">Current</th><th scope="col">Change</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div><p class="small">{escape(comparison["meaning"])}</p>')
