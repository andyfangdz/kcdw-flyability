"""Week-ahead context: WPC's days 3-7 reasoning and how each model evolves toward the flight.

Rows are 12-hourly and anchored to the expected flight start, so the flight hour
is always a row. Every value is re-read from already validated snapshot sources
(run-pinned deterministic guidance, the page's GFS, WN3 and the NWS grid); no
new fetching happens here. Missing values stay missing, never calm or dry.
"""
from __future__ import annotations

import math
from datetime import timedelta
from html import escape

from .common import iso_z, parse_time
from .events import TZ

ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)
KNOTS = 3600 / 1852
MAX_ROWS = 18
NOTES = [
    'Knots and true degrees; pressure is sea-level hPa; rain is the 12 hours ending at the row time; low cloud is percent of the grid cell, not a ceiling.',
    'Deterministic rows are single runs; WN3 is the ensemble mean (low cloud also shows p90). Blank means the source does not cover or provide that value.',
    'Rows fall every 12 hours on the flight start time, so a front or wind shift between rows can be missed.',
]


def anchors(snapshot, now):
    """Flight-aligned 12-hourly times from now through a day after the flight."""
    from .event_gusts import domain
    from .event_model_matrix import flight_window
    start, _, _ = flight_window(snapshot)
    lo, hi = domain(snapshot)
    first = max(lo, now.replace(minute=0, second=0, microsecond=0))
    times = [start + timedelta(hours=12 * k) for k in range(-40, 3)]
    return [t for t in times if first <= t < hi][-MAX_ROWS:]


def _sum12(values, index):
    window = values[max(0, index - 11):index + 1]
    return round(sum(window), 1) if len(window) == 12 and all(v is not None for v in window) else None


def _cell(**values):
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in values.items() if v is not None}


def columns(snapshot, now):
    """[(key, label, run, {iso time: cell})] for each source with any value."""
    from .event_gusts import MODELS, domain, nws_series, validate_gusts
    times = anchors(snapshot, now)
    if not times:
        return times, []
    out = []
    try:
        nws = nws_series(snapshot, now, times)
    except ERRORS:
        nws = None
    if nws:
        out.append(('nws', 'NWS grid', nws['issued_at'], {iso_z(t): _cell(dir=nws['direction'][i], wind=nws['wind'][i], gust=nws['gust'][i])
                                                          for i, t in enumerate(times)}))
    try:
        packet = validate_gusts(snapshot.get('event_gusts'), snapshot)
    except ERRORS:
        packet = None
    lo, hi = domain(snapshot)
    hours = [lo + timedelta(hours=h) for h in range(int((hi - lo).total_seconds() // 3600))]
    index = {t: i for i, t in enumerate(hours)}
    labels = {m[0]: m[1] for m in MODELS}
    for row in (packet or {}).get('models', []):
        if not row['ok']:
            continue
        cells = {}
        for t in times:
            i = index.get(t)
            if i is None:
                continue
            cells[iso_z(t)] = _cell(dir=row['direction'][i], wind=row['wind'][i], gust=row['gust'][i],
                                    pressure=(row.get('pressure') or [None] * len(hours))[i],
                                    rain=_sum12(row['rain'], i) if 'rain' in row else None,
                                    low_cloud=(row.get('low_cloud') or [None] * len(hours))[i])
        out.append((row['key'], labels[row['key']], row['run'], cells))
    try:
        from .event_renderer import gfs_status
        if gfs_status(snapshot, now)['available']:
            h = snapshot['gfs']['data']['hourly']
            axis = {parse_time(t): i for i, t in enumerate(h['time'])}
            get = lambda field, i: h[field][i] if field in h else None
            rain = h.get('precipitation')
            out.append(('gfs', 'GFS', None, {iso_z(t): _cell(wind=get('wind_speed_10m', axis[t]), gust=get('wind_gusts_10m', axis[t]),
                                                             pressure=get('pressure_msl', axis[t]),
                                                             rain=_sum12(rain, axis[t]) if rain else None,
                                                             low_cloud=get('cloud_cover_low', axis[t]))
                                             for t in times if t in axis}))
    except ERRORS:
        pass
    try:
        from .event_ensemble import weathernext3_diagnostic
        if weathernext3_diagnostic(snapshot, now)['available']:
            f = snapshot['weathernext3']['data']['forecast']
            F = f['fields']
            axis = {parse_time(t): i for i, t in enumerate(f['valid_time_utc'])}
            rain = F['precipitation_1h']['mean']
            cells = {}
            for t in times:
                i = axis.get(t)
                if i is None:
                    continue
                u, v = F['u_component_of_wind_10m']['mean'][i], F['v_component_of_wind_10m']['mean'][i]
                hourly = all(axis.get(t - timedelta(hours=k)) == i - k for k in range(12))
                cells[iso_z(t)] = _cell(dir=round(math.degrees(math.atan2(-u, -v)) % 360), wind=F['wind_speed_10m']['mean'][i] * KNOTS,
                                        pressure=F['sea_level_pressure']['mean'][i] / 100,
                                        rain=_sum12(rain, i) if hourly else None,
                                        low_cloud=F['low_cloud_cover']['mean'][i], low_cloud_p90=F['low_cloud_cover']['p90'][i])
            out.insert(1 if out and out[0][0] == 'nws' else 0, ('wn3', 'WN3 mean', f.get('response_init_utc'), cells))
    except ERRORS:
        pass
    return times, [c for c in out if any(c[3].values())]


def wn3_flight_hours(snapshot, now):
    """Hourly WN3 mean and spread from an hour before to an hour after the flight."""
    from .event_ensemble import weathernext3_diagnostic
    from .event_model_matrix import flight_window
    if not weathernext3_diagnostic(snapshot, now)['available']:
        return []
    start, end, _ = flight_window(snapshot)
    f = snapshot['weathernext3']['data']['forecast']
    F = f['fields']
    rows = []
    for i, stamp in enumerate(f['valid_time_utc']):
        t = parse_time(stamp)
        if start - timedelta(hours=1) <= t <= end + timedelta(hours=1):
            u, v = F['u_component_of_wind_10m']['mean'][i], F['v_component_of_wind_10m']['mean'][i]
            rows.append({'at': iso_z(t), 'wind': [round(F['wind_speed_10m'][p][i] * KNOTS, 1) for p in ('mean', 'p10', 'p90')],
                         'dir': round(math.degrees(math.atan2(-u, -v)) % 360),
                         'temp_c': round(F['temperature_2m']['mean'][i]), 'dewpoint_c': round(F['dewpoint_temperature_2m']['mean'][i]),
                         'low_cloud': [round(F['low_cloud_cover'][p][i]) for p in ('mean', 'p90')],
                         'rain_mm': round(F['precipitation_1h']['mean'][i], 2)})
    return rows


def extended(snapshot, now):
    from .synoptic_pattern import extended_discussion
    try:
        envelope = snapshot['synoptic_pattern']
        if envelope.get('snapshot_collected_at') != snapshot['collected_at']:
            return None
        return extended_discussion(envelope, now)
    except ERRORS:
        return None


def week_evidence(snapshot, now):
    """Compact narrative packet: WPC excerpt plus rows within a day of the flight."""
    from .event_model_matrix import flight_window
    try:
        times, cols = columns(snapshot, now)
        start, _, _ = flight_window(snapshot)
        near = [iso_z(t) for t in times if abs(t - start) <= timedelta(hours=24)]
        wpc = extended(snapshot, now)
        packet = {'notes': NOTES,
                  'rows': {at: {c[0]: c[3][at] for c in cols if c[3].get(at)} for at in near},
                  'runs': {c[0]: c[2] for c in cols}, 'wn3_flight_hours': wn3_flight_hours(snapshot, now)}
        if wpc:
            packet['wpc_extended'] = {'issued_at': wpc['issued_at'], 'valid': wpc['valid'],
                                      'overview': [p[:700] for p in wpc['overview']],
                                      'regional': [p['text'][:500] for p in wpc['regional']]}
        return packet if packet['rows'] or wpc else None
    except ERRORS:
        return None


def _when(t):
    return t.astimezone(TZ).strftime('%a %-d · %-I %p')


def _render_cell(c):
    if not c:
        return '<td class="empty">—</td>'
    wind = ''
    if 'wind' in c or 'gust' in c:
        wind = (f'{c["dir"]:03.0f}° ' if 'dir' in c else '') + (f'{c["wind"]:.0f}' if 'wind' in c else '—') + (f'G{c["gust"]:.0f}' if 'gust' in c else '')
    extra = []
    if 'pressure' in c:
        extra.append(f'{c["pressure"]:.0f} hPa')
    if 'rain' in c:
        extra.append(f'{c["rain"]:g} mm')
    if 'low_cloud' in c:
        extra.append(f'low {c["low_cloud"]:.0f}%' + (f' (p90 {c["low_cloud_p90"]:.0f})' if 'low_cloud_p90' in c else ''))
    return f'<td>{escape(wind)}<small>{escape(" · ".join(extra))}</small></td>'


def render_week(snapshot, now):
    from .event_model_matrix import flight_window
    try:
        times, cols = columns(snapshot, now)
        start, _, _ = flight_window(snapshot)
        wpc = extended(snapshot, now)
        if not cols and not wpc:
            return ''
        parts = ['<section id="week-ahead" class="weather-pattern week-ahead" aria-labelledby="week-ahead-title">'
                 '<p class="eyebrow">Week ahead · official reasoning and each model through the flight</p>'
                 '<h2 id="week-ahead-title">How the lead-up evolves</h2>']
        if wpc:
            parts.append(f'<h3>WPC extended forecast discussion</h3><p class="small">Issued {escape(_when(parse_time(wpc["issued_at"])))} {escape(parse_time(wpc["issued_at"]).astimezone(TZ).strftime("%Z"))}'
                         f'{" · " + escape(wpc["valid"]) if wpc["valid"] else ""} · quoted verbatim; national scope, regional paragraphs selected.</p>')
            parts += [f'<blockquote><p>{escape(p)}</p></blockquote>' for p in wpc['overview']]
            parts += [f'<blockquote><p><strong>{escape(r["section"])}:</strong> {escape(r["text"])}</p></blockquote>' for r in wpc['regional']]
        if cols:
            stamp = lambda key, run: ('page source' if not run else ('issued ' if key == 'nws' else '') + parse_time(run).strftime('%b %-d %HZ'))
            head = ''.join(f'<th scope="col">{escape(label)}<small>{escape(stamp(key, run))}</small></th>' for key, label, run, _ in cols)
            body = ''.join(f'<tr{" data-flight" if t == start else ""}><th scope="row">{escape(_when(t))}{"<small>flight start</small>" if t == start else ""}</th>'
                           + ''.join(_render_cell(cells.get(iso_z(t))) for _, _, _, cells in cols) + '</tr>' for t in times)
            parts.append('<h3>Each model at the flight hour, every 12 hours</h3><div class="table-wrap"><table class="week-table">'
                         '<caption>Wind from · sustained G gust (kt); then pressure, 12-hour rain and low cloud.</caption>'
                         f'<thead><tr><th scope="col">Eastern</th>{head}</tr></thead><tbody>{body}</tbody></table></div>')
        hours = wn3_flight_hours(snapshot, now)
        if hours:
            rows = ''.join(f'<tr{" data-flight" if parse_time(h["at"]) == start else ""}><th scope="row">{escape(parse_time(h["at"]).astimezone(TZ).strftime("%-I %p"))}</th>'
                           f'<td>{h["wind"][0]:.1f}<small>{h["wind"][1]:.0f}–{h["wind"][2]:.0f}</small></td><td>{h["dir"]:03d}°</td>'
                           f'<td>{h["temp_c"]} / {h["dewpoint_c"]} °C</td><td>{h["low_cloud"][0]}%<small>p90 {h["low_cloud"][1]}%</small></td>'
                           f'<td>{h["rain_mm"]:g} mm</td></tr>' for h in hours)
            parts.append('<h3>WN3 around the flight</h3><div class="table-wrap"><table><caption>Ensemble mean with p10–p90 for wind and p90 for low cloud; '
                         'direction from mean wind components. WN3 has no gust field.</caption><thead><tr><th scope="col">Eastern</th>'
                         '<th scope="col">Sustained kt</th><th scope="col">From</th><th scope="col">Temp / dew point</th>'
                         f'<th scope="col">Low cloud</th><th scope="col">Rain</th></tr></thead><tbody>{rows}</tbody></table></div>')
        parts.append('<details><summary>Week-ahead sources &amp; limits</summary><ul>' + ''.join(f'<li>{escape(n)}</li>' for n in NOTES)
                     + '</ul><p><a href="https://www.wpc.ncep.noaa.gov/medr/5dayfcst_wbg_conus.gif">WPC days 3–7 surface maps</a> · '
                       '<a href="#deterministic-wind">Hourly gust charts</a></p></details></section>')
        return ''.join(parts)
    except ERRORS:
        return ''
