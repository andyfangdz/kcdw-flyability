"""Calibrated KCDW wind for the event day, from the daily research model (research/mos).

The model is trained on KCDW's own 1-minute ASOS record and METARs against the
archived forecasts of GFS, GEFS, ECMWF ENS, AIFS (single and ensemble), HRRR,
WeatherNext 2/3, ICON, ECMWF HRES and GEM (NBM is deliberately excluded and used
only as the benchmark). A separate job (scripts/mos_update.sh) writes
var/mos/forecast.json; this module only reads it, validates every value,
binds the event day to the snapshot and renders it. Nothing here trains or
imports numerical libraries.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from .common import UTC, iso_z, parse_time
from .events import TZ, Event

VERSION = 1
MAX_AGE = timedelta(hours=36)
HOURS = range(6, 22)
TABLE_HOURS = range(9, 19)
NOTES = [
    'Trained on every hour from 6 a.m. to 9 p.m. since mid-2021 (about 190,000 hours), comparing each model\'s archived forecast with what KCDW actually measured.',
    '"Peak gust" is the strongest 5-second gust in the hour from the ASOS 1-minute record, so it counts gusts too small for a METAR to report. "METAR peak" is what the hourly and special reports would show: the reported gust, or the wind when no gust is reported.',
    'Ranges are the 10th–90th percentiles, widened by conformal calibration so about 80% of hours fall inside them. Probabilities come from separate classifiers.',
    'Each hour uses the shortest-lead 00Z runs already archived; lead day is how many days before the date those runs were issued.',
    'NBM is not an input. It is NOAA\'s own station-calibrated guidance, so it serves as the benchmark this model has to beat.',
]
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)


def _require(ok):
    if not ok:
        raise ValueError('invalid calibrated wind')


def _num(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _check_hour(h, day):
    _require(isinstance(h, dict) and h.get('hour') in HOURS)
    valid = parse_time(h['valid_utc'])
    local = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=h['hour'])
    _require(valid == local.astimezone(UTC))
    init = parse_time(h['init_utc'])
    _require(init.hour == 0 and init < valid and type(h['lead_day']) is int and 1 <= h['lead_day'] <= 7)
    _require((local.date() - init.astimezone(UTC).date()).days == h['lead_day'])
    for key, high in (('sust_kt', 80), ('gust_kt', 120), ('metar_peak_kt', 120)):
        q = h[key]
        _require(isinstance(q, list) and len(q) == 3 and all(_num(v, 0, high) for v in q) and q[0] <= q[1] <= q[2])
    _require(h['gust_kt'][1] >= h['sust_kt'][1] - 0.5)
    _require(type(h['dir_deg']) is int and 0 <= h['dir_deg'] < 360 and _num(h['dir_confidence'], 0, 1))
    for key in ('p_gust_ge20', 'p_spread_ge10', 'p_metar_gust'):
        _require(_num(h[key], 0, 1))


def collect_calibrated(snapshot, path, now):
    """The event day's hours from the research forecast, bound to this snapshot, or None when unavailable."""
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    _require(isinstance(data, dict) and data.get('version') == VERSION and data.get('station') == 'KCDW')
    generated = parse_time(data['generated_at'])
    now = now.astimezone(UTC)
    _require(generated <= now + timedelta(minutes=5) and parse_time(data['trained_at']) <= generated + timedelta(minutes=5))
    if now - generated > MAX_AGE:
        return None
    day = Event(**snapshot['event']).day
    hours = data['days'].get(day.isoformat())
    if not hours:
        return None
    for h in hours:
        _check_hour(h, day)
    return {'version': VERSION, 'snapshot_collected_at': snapshot['collected_at'],
            'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')},
            'generated_at': data['generated_at'], 'trained_at': data['trained_at'], 'training_period': data['training_period'],
            'coverage_target': data['coverage_target'], 'skill': data.get('skill'), 'hours': hours}


def validate_calibrated(packet, snapshot):
    _require(isinstance(packet, dict) and packet.get('version') == VERSION)
    _require(packet.get('snapshot_collected_at') == snapshot['collected_at'])
    _require(packet.get('event') == {k: snapshot['event'][k] for k in ('slug', 'date', 'window')})
    day = Event(**snapshot['event']).day
    hours = packet['hours']
    _require(isinstance(hours, list) and hours and [h['hour'] for h in hours] == sorted({h['hour'] for h in hours}))
    for h in hours:
        _check_hour(h, day)
    _require(_num(packet['coverage_target'], 0.5, 0.95))
    return packet


def _flight(packet, snapshot):
    from .event_model_matrix import flight_window
    start, end, _ = flight_window(snapshot)
    return [h for h in packet['hours'] if start <= parse_time(h['valid_utc']) < end] or \
        [min(packet['hours'], key=lambda h: abs(parse_time(h['valid_utc']) - start))]


def _limit(snapshot):
    from .event_personal import personal
    return (personal(snapshot) or {}).get('gust_limit_kt', 20)


def summary(packet, snapshot):
    rows = _flight(packet, snapshot)
    gust = [h['gust_kt'] for h in rows]
    sust = [h['sust_kt'] for h in rows]
    return {'hours_local': [h['hour'] for h in rows], 'lead_day': rows[0]['lead_day'], 'runs': rows[0]['init_utc'],
            'sustained_kt': [min(s[0] for s in sust), max(s[1] for s in sust), max(s[2] for s in sust)],
            'peak_gust_kt': [min(g[0] for g in gust), max(g[1] for g in gust), max(g[2] for g in gust)],
            'metar_peak_kt_median': max(h['metar_peak_kt'][1] for h in rows),
            'direction_deg': [h['dir_deg'] for h in rows],
            'max_p_gust_ge20': max(h['p_gust_ge20'] for h in rows), 'max_p_spread_ge10': max(h['p_spread_ge10'] for h in rows),
            'max_p_metar_gust': max(h['p_metar_gust'] for h in rows)}


def calibrated_evidence(snapshot, now):
    try:
        packet = validate_calibrated(snapshot.get('calibrated_wind'), snapshot)
    except ERRORS:
        return None
    s = summary(packet, snapshot)
    skill = packet.get('skill') or {}
    return {'flight_window': s, 'gust_limit_kt': _limit(snapshot), 'probability_threshold_kt': 20,
            'skill_1_4pm': {'gust_mae_kt': skill.get('gust_mae_kt'), 'sust_mae_kt': skill.get('sust_mae_kt'),
                            'hours': skill.get('afternoon_hours'), 'period': skill.get('period')},
            'notes': NOTES[:3]}


def _q(q):
    return f'{q[1]:.0f}<small>{q[0]:.0f}–{q[2]:.0f}</small>'


def _pct(p):
    return '&lt;1%' if p < 0.005 else f'{p:.0%}'


def render_calibrated(snapshot, now):
    try:
        packet = validate_calibrated(snapshot.get('calibrated_wind'), snapshot)
    except ERRORS:
        return ''
    s = summary(packet, snapshot)
    flight = set(s['hours_local'])
    limit = _limit(snapshot)
    first, last = min(flight), max(flight) + 1
    clock = lambda h: datetime(2000, 1, 1, h).strftime('%-I %p').lower()
    dirs = s['direction_deg']
    lead = 'tomorrow\'s' if s['lead_day'] == 1 else f'{s["lead_day"]}-day-ahead'
    heading = f'{min(dirs):03d}°' if min(dirs) == max(dirs) else f'{min(dirs):03d}–{max(dirs):03d}°'
    story = (f'<p>For {clock(first)}–{clock(last)}, the calibrated model expects about {s["sustained_kt"][1]:.0f} kt sustained from '
             f'{heading}, with a true peak gust around {s["peak_gust_kt"][1]:.0f} kt '
             f'(80% range {s["peak_gust_kt"][0]:.0f}–{s["peak_gust_kt"][2]:.0f}). The METAR would likely show about '
             f'{s["metar_peak_kt_median"]:.0f} kt; the chance it reports any gust is {_pct(s["max_p_metar_gust"])}. '
             f'Chance of a gust of 20 kt or more: {_pct(s["max_p_gust_ge20"])}'
             f'{"" if limit == 20 else f" (your limit is {limit} kt)"}; of a gust spread of 10 kt or more: {_pct(s["max_p_spread_ge10"])}. '
             f'Based on {lead} 00Z runs.</p>')
    body = ''.join(
        f'<tr{" data-flight" if h["hour"] in flight else ""}><th scope="row">{escape(clock(h["hour"]))}'
        f'{"<small>flight</small>" if h["hour"] in flight else ""}</th>'
        f'<td>{h["dir_deg"]:03d}°</td><td>{_q(h["sust_kt"])}</td><td>{_q(h["gust_kt"])}</td><td>{_q(h["metar_peak_kt"])}</td>'
        f'<td>{_pct(h["p_gust_ge20"])}</td><td>{_pct(h["p_spread_ge10"])}</td><td>{_pct(h["p_metar_gust"])}</td></tr>'
        for h in packet['hours'] if h['hour'] in TABLE_HOURS)
    skill = packet.get('skill') or {}
    g, sus = skill.get('gust_mae_kt') or {}, skill.get('sust_mae_kt') or {}
    check = (f'<p class="small">Checked against KCDW on {skill["afternoon_hours"]:,} afternoon hours (1–4 p.m.) from '
             f'{escape(skill["period"][0][:7])} to {escape(skill["period"][1][:7])}, each scored by a model that never saw its month: '
             f'typical peak-gust error {g["calibrated"]:.1f} kt versus {g["nbm"]:.1f} kt for NBM and {g["raw_ecmwf_ens"]:.1f} kt for the raw ECMWF '
             f'ensemble; sustained {sus["calibrated"]:.1f} kt versus {sus["nbm"]:.1f} kt for NBM.</p>') if g and sus else ''
    trained = parse_time(packet['trained_at']).astimezone(TZ).strftime('%b %-d')
    return (f'<section id="calibrated-wind" class="weather-pattern" aria-labelledby="calibrated-wind-title">'
            f'<p class="eyebrow">Calibrated KCDW wind · learned from five years of the airport\'s own record · retrained {escape(trained)}</p>'
            f'<h2 id="calibrated-wind-title">What KCDW is likely to report</h2>{story}'
            f'<div class="table-wrap"><table><caption>Median with the 80% range beneath it, in knots. Peak gust is the true 1-hour maximum; '
            f'METAR peak is what the reports would show.</caption><thead><tr><th scope="col">Local</th><th scope="col">From</th>'
            f'<th scope="col">Sustained</th><th scope="col">Peak gust</th><th scope="col">METAR peak</th><th scope="col">Gust ≥ 20</th>'
            f'<th scope="col">Spread ≥ 10</th><th scope="col">METAR gust</th></tr></thead><tbody>{body}</tbody></table></div>{check}'
            f'<details><summary>How this is calibrated</summary><ul>{"".join("<li>" + escape(n) + "</li>" for n in NOTES)}</ul></details></section>')
