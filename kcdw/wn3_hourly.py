"""Supplemental 48-hour WN3 initializations, separate from 15-day guidance."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from .common import iso_z, parse_time
from .weathernext3 import FIELD_SPECS, OPTIONAL_FIELD_SPECS, STATISTICS, wind_direction
from .weathernext3_bigquery import BigQueryStore, SOURCE_URL, initialization, validate_rows, validate_provenance

SPECS = FIELD_SPECS | OPTIONAL_FIELD_SPECS
ARRAYS = [f'{s.array}_{p}' for s in SPECS.values() for p in STATISTICS]
NOTES = ('Supplemental hourly initialization with 48-hour forecast horizon, not an independent model. '
         'Compare only identical valid times with the six-hourly WN3 run; never extend past coverage. '
         'p10/p90 are hourly marginal ensemble quantiles, not event probabilities. '
         'Cloud fraction is not ceiling. Winds at 100 m are not gust forecasts or gust limits. '
         'Ceiling, visibility, gusts, convection and pressure-level profiles are unavailable.')


def validate(packet, now):
    try:
        init = initialization(packet['init_time'])
        fetched = parse_time(packet['collected_at'])
        if (packet['version'] != 1 or packet['horizon_hours'] != 48 or init.hour % 6 == 0
                or not timedelta(0) <= now-init <= timedelta(hours=24)
                or not init <= fetched <= now+timedelta(minutes=5)):
            raise ValueError('invalid hourly cycle')
        times = [iso_z(init+timedelta(hours=h)) for h in range(1,49)]
        validate_rows(packet['rows'], packet['init_time'], times, ARRAYS)
        validate_provenance(packet['query'])
        if not init <= parse_time(packet['query']['retrieved_at']) <= fetched+timedelta(minutes=5):
            raise ValueError('invalid hourly provenance')
        return packet
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError('Invalid or stale WN3 hourly guidance') from None


def collect(now, *, store=None):
    """Newest published interim run; unavailable fields never become zeros."""
    try:
        store = store or BigQueryStore()
        candidates = [r for r in store.candidates(now, include_interim=True) if r.hour % 6]
        for run in candidates[:2]:
            try:
                result = store.fetch(iso_z(run), ARRAYS)
                break
            except LookupError:
                continue
        else:
            raise ValueError('no hourly run')
        packet = dict(version=1, init_time=iso_z(run), collected_at=iso_z(now),
                      horizon_hours=48, rows=result['rows'], query=result['provenance'])
        return validate(packet, now)
    except Exception:
        raise ValueError('WN3 hourly collection unavailable') from None


def summarize(packet, now, start=None, end=None):
    """Validated compact arrays with explicit coverage and accumulation timing."""
    validate(packet, now)
    first, last = (parse_time(packet['rows'][i]['valid_time']) for i in (0,-1))
    start, end = start or now, end or last
    rows = [r for r in packet['rows'] if start <= parse_time(r['valid_time']) <= end]
    result = dict(model='WeatherNext 3 interim hourly run', source_url=SOURCE_URL,
                  init_time=packet['init_time'], collected_at=packet['collected_at'],
                  horizon_hours=48, coverage_start=iso_z(first), coverage_end=iso_z(last),
                  requested_start=iso_z(start), requested_end=iso_z(end),
                  coverage='complete' if first <= start <= end <= last else 'partial' if rows else 'none',
                  notes=NOTES)
    for name,spec in SPECS.items():
        selected = [r for r in rows if spec.step_type != 'accumulated' or parse_time(r['valid_time'])-timedelta(hours=1) >= start]
        factor = 3600/1852 if 'wind' in name else .01 if name == 'sea_level_pressure' else 1
        unit = 'kt' if 'wind' in name else 'hPa' if name == 'sea_level_pressure' else spec.unit
        group = 'precipitation' if spec.step_type == 'accumulated' else 'instantaneous'
        # One time axis per sampling convention avoids repeating all timestamps
        # for every variable in the bounded assessment request.
        target = result.setdefault(group, dict(time=[r['valid_time'] for r in selected], fields={}))
        target['fields'][name] = dict(unit=unit, step_type=spec.step_type,
            **{p:[round(spec.convert(r[f'{spec.array}_{p}'])*factor,3) for r in selected] for p in STATISTICS})
    result['wind_direction'] = dict(unit='degrees true', statistic='direction of mean components',
        time=[r['valid_time'] for r in rows], values=[wind_direction(r['u_component_of_wind_10m_mean'],r['v_component_of_wind_10m_mean']) for r in rows])
    result['precipitation_note'] = 'Each precipitation time ends a one-hour interval wholly inside the requested window. No hourly percentile sums.'
    return result


def event_evidence(snapshot, now):
    from .event_timing import timing_evidence
    from .events import _event, TZ
    timing = timing_evidence(snapshot)
    event = _event(snapshot['event'])
    if timing and timing.get('flight_end_utc'):
        start,end = (parse_time(timing[k]) for k in ('flight_start_utc','flight_end_utc'))
        kind = 'expected flight, including return sample'
    else:
        start = datetime.combine(event.day, datetime.min.time(), TZ)+timedelta(hours=event.start_hour)
        end = start+timedelta(hours=event.end_hour-event.start_hour)
        kind = 'forecast context; flight timing unavailable'
    result = summarize(snapshot['wn3_hourly'], now, start, end)
    result['window_kind'] = kind
    # Direction is derived above; marginal U/V bands add no flight-window detail.
    for name in ('u_component_of_wind_10m','v_component_of_wind_10m'):
        result['instantaneous']['fields'].pop(name)
    if result['coverage'] == 'none':
        result.pop('instantaneous')
        result.pop('precipitation')
        result.pop('wind_direction')
    return result


def render_event(snapshot, now):
    if 'wn3_hourly' not in snapshot:
        return ''
    try:
        data = event_evidence(snapshot, now)
    except (ValueError, KeyError, TypeError):
        return '<p class="small">Hourly WN3 guidance unavailable.</p>'
    tz = ZoneInfo('America/New_York')
    end = parse_time(data['coverage_end']).astimezone(tz).strftime('%b %-d, %H:%M %Z')
    init = parse_time(data['init_time']).strftime('%b %-d %HZ')
    text = f'Run {init}; 48-hour guidance through {end}. '
    text += {'none':'This run does not yet reach the flight window.',
             'partial':'Only part of the requested window is covered.',
             'complete':'The requested window is fully covered.'}[data['coverage']]
    body = '<p>'+escape(text)+'</p>'
    if data['coverage'] != 'none':
        fields = data['instantaneous']['fields']
        times = data['instantaneous']['time']
        body += '<div class="table-scroll"><table><thead><tr><th>Eastern</th><th>10 m wind · kt</th><th>100 m wind · kt</th><th>Low cloud · %</th></tr></thead><tbody>'
        for i,t in enumerate(times):
            body += '<tr><td>'+escape(parse_time(t).astimezone(tz).strftime('%H:%M'))+'</td>'
            for name in ('wind_speed_10m','wind_speed_100m','low_cloud_cover'):
                f = fields[name]
                body += f'<td>{f["mean"][i]:.1f} ({f["p10"][i]:.1f}–{f["p90"][i]:.1f})</td>'
            body += '</tr>'
        body += '</tbody></table></div><p class="small">Mean (p10–p90). Cloud coverage is not ceiling; 100 m wind is not gust.</p>'
    return '<details class="report-detail"><summary>Latest hourly WN3 run</summary>'+body+'</details>'
