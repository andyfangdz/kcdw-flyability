"""How the event's forecast compares with past afternoons at KCDW and with each model's 7-day forecasts.

Two reference sets, both summarized for the event's flight window (local hours):
- observed: KCDW METARs (Iowa Environmental Mesonet), max sustained wind, max
  reported gust (a missing gust counts as the sustained wind), gust spread,
  best-runway crosswind and whether the window stayed VFR (ceiling >= 3,000 ft,
  visibility >= 5 SM) and dry;
- forecast: Open-Meteo's archive of runs issued about 7 days before each date
  (168-191 h lead) for GFS, NBM and ECMWF IFS.
The large archives refresh at most daily into a cache; each snapshot keeps only
compact per-day rows so rendering never fetches. Percentiles are "share of days
less favorable", with ties split. Observed METAR gusts are reported only when
the wind varies ~10 kt, so the page ranks forecasts by gust and by sustained
wind and treats the truth as between them.
"""
from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from .common import UTC, atomic_write, iso_z, parse_time
from .events import TZ, Event

VERSION = 1
IEM = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'
PREVIOUS = 'https://previous-runs-api.open-meteo.com/v1/forecast'
FORECAST = 'https://api.open-meteo.com/v1/forecast'
LATLON = (40.8752, -74.2814)
OBS_YEARS, MODEL_YEARS = 3, 2
CACHE_MAX_AGE = timedelta(hours=20)
CACHE_STALE = timedelta(days=7)
SEASON_DAYS = 30
RUNWAYS = (30, 83)  # true headings of the 04/22 and 10/28 axes
ARCHIVE_MODELS = (('nbm', 'NBM', 'ncep_nbm_conus'), ('gfs', 'GFS', 'gfs_global'), ('ifs', 'ECMWF IFS', 'ecmwf_ifs025'))
SCATTER_MODELS = ('nbm', 'gfs', 'ifs')
MIN_ROWS = {'wn3': 100}  # WN3's BigQuery archive begins January 2026
WX = ('RA', 'SN', 'TS', 'DZ', 'PL', 'FZ', 'GR', 'FG')
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, ZeroDivisionError)
NOTES = [
    'Percentile = share of days less favorable than the forecast (ties split). Higher is better.',
    'Observed gusts appear in METARs only when the wind varies about 10 kt, so a light gusty day often shows no gust; rank by gust understates a forecast, rank by sustained overstates it.',
    'Forecast archives are runs issued about 7 days before each date; 7-day forecasts are smoother than reality.',
    'ECMWF comes from native open data (AWS mirror, runs since 2024-11-12; gaps filled from Earth Engine after it matches the GRIB values): 10 m wind at the flight start rounded to six hours and the maximum gust over the following six hours, for past dates and for this event alike.',
    'Season = days within 30 days of the event date in any year. Low cloud is ranked only for WN3, the one archive that keeps it (since January 2026, so no past-season days yet); ceilings are in the observed tiers only.',
]


def _require(ok):
    if not ok:
        raise ValueError('invalid climatology')


def window(snapshot):
    """Flight-window local hours (inclusive samples) and the observed minute range."""
    from .event_model_matrix import flight_window
    start, end, _ = flight_window(snapshot)
    s, e = start.astimezone(TZ), end.astimezone(TZ)
    hours = list(range(s.hour, e.hour + 1))
    return {'hours': hours, 'label': f'{s:%H:%M}–{e:%H:%M}', 'obs_minutes': [s.hour * 60 - 15, e.hour * 60 + 15]}


def _xw(speed, direction):
    return min(speed * abs(math.sin(math.radians(direction - h))) for h in RUNWAYS)


def _number(v):
    return None if v in (None, '', 'null', 'M') else float(v)


def observed_rows(text, win):
    days = {}
    lo, hi = win['obs_minutes']
    for r in csv.DictReader(io.StringIO(text)):
        d, t = r['valid'].split(' ')
        hm = int(t[:2]) * 60 + int(t[3:5])
        if lo <= hm <= hi:
            days.setdefault(d, []).append(r)
    rows = []
    for d, obs in sorted(days.items()):
        if len(obs) < 2:
            continue
        peak = sust = spread = xw = 0.0
        ceiling, vis, wet = 99999.0, 10.0, False
        for r in obs:
            s, g, dr = _number(r['sknt']) or 0.0, _number(r['gust']), _number(r['drct'])
            top = max(s, g or 0.0)
            peak, sust = max(peak, top), max(sust, s)
            if g:
                spread = max(spread, g - s)
            if dr is not None and top > 0:
                xw = max(xw, _xw(top, dr))
            for i in (1, 2, 3):
                if r[f'skyc{i}'] in ('BKN', 'OVC', 'VV') and _number(r[f'skyl{i}']) is not None:
                    ceiling = min(ceiling, _number(r[f'skyl{i}']))
                    break
            if _number(r['vsby']) is not None:
                vis = min(vis, _number(r['vsby']))
            wet = wet or any(k in (r['wxcodes'] or '') for k in WX)
        rows.append([d, round(sust), round(peak), round(spread), round(xw, 1), ceiling >= 3000 and vis >= 5 and not wet])
    return rows


def model_rows(hourly, suffix, hours, use_gust=True):
    days = {}
    for i, t in enumerate(hourly['time']):
        if int(t[11:13]) in hours:
            days.setdefault(t[:10], []).append(i)
    get = lambda name, i: (hourly.get(name + suffix) or [None] * len(hourly['time']))[i]
    rows = []
    for d, ix in sorted(days.items()):
        w, dr = [get('wind_speed_10m', i) for i in ix], [get('wind_direction_10m', i) for i in ix]
        g = [get('wind_gusts_10m', i) if use_gust else None for i in ix]
        p = [get('precipitation', i) for i in ix]
        if len(ix) != len(hours) or None in w or None in dr:
            continue
        gust = None if None in g else max(g)
        rows.append([d, round(max(w), 1), None if gust is None else round(gust, 1),
                     round(max(_xw(gi if gi is not None else wi, di) for wi, gi, di in zip(w, g, dr)), 1),
                     round(sum(x or 0 for x in p), 1)])
    return rows


def refresh_cache(client, snapshot, cache_path, now, ecmwf=None, wn3=None):
    """Rebuild the archive summaries at most daily; keep a recent cache on failure."""
    win = window(snapshot)
    cache = None
    try:
        cache = json.loads(Path(cache_path).read_text(encoding='utf-8'))
        if cache.get('version') != VERSION or cache.get('window') != win:
            cache = None
    except (OSError, ValueError, AttributeError, TypeError):
        cache = None
    if cache and now - parse_time(cache['built_at']) < CACHE_MAX_AGE:
        return cache
    today = now.astimezone(TZ).date()
    fresh = {'version': VERSION, 'window': win, 'built_at': iso_z(now), 'models': {}}
    try:
        start = today - timedelta(days=365 * OBS_YEARS)
        query = [('station', 'CDW'), *[('data', k) for k in ('sknt', 'gust', 'drct', 'vsby', 'skyc1', 'skyc2', 'skyc3', 'skyl1', 'skyl2', 'skyl3', 'wxcodes')],
                 ('year1', start.year), ('month1', start.month), ('day1', start.day),
                 ('year2', today.year), ('month2', today.month), ('day2', today.day),
                 ('tz', 'America/New_York'), ('format', 'onlycomma'), ('latlon', 'no'), ('missing', 'null'),
                 ('trace', 'T'), ('direct', 'yes'), ('report_type', 3), ('report_type', 4)]
        url = IEM + '?' + urlencode(query)
        fresh['observed'] = {'source_url': url, 'period': [start.isoformat(), (today - timedelta(days=1)).isoformat()],
                             'rows': [r for r in observed_rows(client.get_text(url, 12_000_000), win) if r[0] < today.isoformat()]}
        for key, label, model in ARCHIVE_MODELS:
            begin = today - timedelta(days=365 * MODEL_YEARS)
            fields = ','.join(f'{v}_previous_day7' for v in ('wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m', 'precipitation'))
            url = PREVIOUS + '?' + urlencode(dict(latitude=LATLON[0], longitude=LATLON[1], hourly=fields, models=model,
                                                   start_date=begin.isoformat(), end_date=(today - timedelta(days=1)).isoformat(),
                                                   timezone='America/New_York', wind_speed_unit='kn'))
            hourly = client.get(url)['hourly']
            fresh['models'][key] = {'label': label, 'model': model, 'source_url': url, 'period': [begin.isoformat(), (today - timedelta(days=1)).isoformat()],
                                    'rows': model_rows(hourly, '_previous_day7', win['hours'])}
        if ecmwf is not None:
            _native_ifs(fresh, ecmwf, win, today, now)
        if wn3 is not None:
            _wn3(fresh, wn3, win, today, now)
        _require(len(fresh['observed']['rows']) > 300 and all(len(m['rows']) > MIN_ROWS.get(k, 300) for k, m in fresh['models'].items()))
    except Exception:
        if cache and now - parse_time(cache['built_at']) < CACHE_STALE:
            return cache
        raise
    atomic_write(cache_path, json.dumps(fresh, separators=(',', ':'), allow_nan=False) + '\n')
    return fresh


def _native_ifs(fresh, path, win, today, now):
    """Replace Open-Meteo's gust-less ECMWF rows with native open data when enough dates exist."""
    from . import ecmwf_archive
    start = win['label'][:5]
    first, last = today - timedelta(days=365 * MODEL_YEARS), today - timedelta(days=1)
    try:
        ecmwf_archive.update(path, start, first, last, now, limit=20)
    except Exception:
        pass
    try:
        # Earth Engine fills what the throttled AWS mirror could not, after matching native GRIB.
        ecmwf_archive.fill_from_earth_engine(path, start, first, last, now)
    except Exception:
        pass
    cache = ecmwf_archive.load(path)
    native = ecmwf_archive.rows(cache, (today - timedelta(days=365 * MODEL_YEARS)).isoformat())
    rain = {r[0]: r[4] for r in fresh['models'].get('ifs', {}).get('rows', [])}
    native = [[d, s, g, x, rain[d]] for d, s, g, x in native if d in rain]
    if len(native) > 300:
        fresh['models']['ifs'] = {'label': 'ECMWF IFS', 'model': 'ecmwf-open-data', 'source_url': ecmwf_archive.BUCKET,
                                  'period': [native[0][0], native[-1][0]], 'rows': native}


def _wn3(fresh, path, win, today, now):
    """WN3 7-day forecasts from BigQuery (archive since 2026-01); adds a few new dates per day."""
    from . import wn3_climatology
    start, end = win['label'][:5], win['label'][-5:]
    try:
        wn3_climatology.update(path, start, end, today - timedelta(days=365 * MODEL_YEARS), today - timedelta(days=1), now, limit=3)
    except Exception:
        pass
    rows = wn3_climatology.rows(wn3_climatology.load(path))
    if len(rows) > MIN_ROWS['wn3']:
        fresh['models']['wn3'] = {'label': 'WN3 mean', 'model': 'weathernext-3-bigquery', 'source_url': 'https://developers.google.com/weathernext/guides/bigquery',
                                  'period': [rows[0][0], rows[-1][0]], 'rows': rows}


def collect_climatology(client, snapshot, cache_path, now, ecmwf=None, wn3=None):
    """Cached archives plus one request for the archive models' current forecast of the event day."""
    now = now.astimezone(UTC)
    cache = refresh_cache(client, snapshot, cache_path, now, ecmwf, wn3)
    day = snapshot['event']['date']
    url = FORECAST + '?' + urlencode(dict(latitude=LATLON[0], longitude=LATLON[1],
                                          hourly='wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation',
                                          models=','.join(m for _, _, m in ARCHIVE_MODELS), start_date=day, end_date=day,
                                          timezone='America/New_York', wind_speed_unit='kn'))
    current = {}
    try:
        hourly = client.get(url)['hourly']
        for key, _, model in ARCHIVE_MODELS:
            # Match the archive: a model archived without gusts is ranked without them.
            gusty = any(r[2] is not None for r in cache['models'][key]['rows'])
            rows = model_rows(hourly, '_' + model, cache['window']['hours'], gusty)
            if rows and rows[0][0] == day:
                current[key] = rows[0][1:]
    except Exception:
        current = {}
    if cache['models'].get('ifs', {}).get('model') == 'ecmwf-open-data':
        from . import ecmwf_archive
        native = ecmwf_archive.current(Event(**snapshot['event']).day, cache['window']['label'][:5], now, path=ecmwf)
        rain = current.get('ifs', [None, None, None, 0])[3]
        current.pop('ifs', None)
        if native:
            current['ifs'] = [round(native['sust'], 1), round(native['gust'], 1), native['xw'], rain]
    if 'wn3' in cache['models']:
        from . import wn3_climatology
        try:
            wn3_now = wn3_climatology.current(snapshot, cache['window']['label'][:5], cache['window']['label'][-5:])
        except (KeyError, TypeError, ValueError, IndexError):
            wn3_now = None
        if wn3_now:
            current['wn3'] = wn3_now
    packet = {'version': VERSION, 'snapshot_collected_at': snapshot['collected_at'],
              'event': {k: snapshot['event'][k] for k in ('slug', 'date', 'window')}, 'window': cache['window'],
              'built_at': cache['built_at'], 'observed': cache['observed'], 'models': cache['models'],
              'current': {'fetched_at': iso_z(now), 'source_url': url, 'rows': current}}
    return validate_climatology(packet, snapshot)


def validate_climatology(packet, snapshot):
    _require(isinstance(packet, dict) and packet.get('version') == VERSION)
    _require(packet['snapshot_collected_at'] == snapshot['collected_at'])
    _require(packet['event'] == {k: snapshot['event'][k] for k in ('slug', 'date', 'window')})
    _require(packet['window'] == window(snapshot))
    num = lambda v, hi: type(v) in (int, float) and math.isfinite(v) and 0 <= v <= hi
    obs = packet['observed']['rows']
    _require(isinstance(obs, list) and len(obs) > 300)
    for r in obs:
        _require(len(r) == 6 and isinstance(r[0], str) and all(num(v, 150) for v in r[1:5]) and type(r[5]) is bool)
    _require([r[0] for r in obs] == sorted({r[0] for r in obs}))
    _require(set(packet['models']) <= {k for k, _, _ in ARCHIVE_MODELS} | {'wn3'})
    cloud_ok = lambda r: all(num(v, 100) for v in r)
    for key, m in packet['models'].items():
        _require(len(m['rows']) > MIN_ROWS.get(key, 300))
        for r in m['rows']:
            _require(len(r) in (5, 7) and num(r[1], 150) and (r[2] is None or num(r[2], 200)) and num(r[3], 200) and num(r[4], 500) and cloud_ok(r[5:]))
    for key, r in packet['current']['rows'].items():
        _require(key in packet['models'] and len(r) in (4, 6) and num(r[0], 150) and (r[1] is None or num(r[1], 200))
                 and num(r[2], 200) and num(r[3], 500) and cloud_ok(r[4:]))
    return packet


def _season(day, event_day):
    d = date.fromisoformat(day)
    gap = abs((d.replace(year=2001) - event_day.replace(year=2001)).days) if not (d.month == 2 and d.day == 29) else 999
    return min(gap, 365 - gap) <= SEASON_DAYS


def _pct(values, key, target):
    below = sum(1 for v in values if key(v) > target)
    tie = sum(1 for v in values if key(v) == target)
    return round(100 * (below + tie / 2) / len(values)) if values else None


def forecast_points(snapshot, now):
    """Each current forecast source's flight-window maximum sustained wind and gust."""
    from .event_gusts import gust_evidence
    from .event_wind import wind_evidence
    points = []
    wind = wind_evidence(snapshot, now)
    if wind and wind.get('nws'):
        s = wind['nws']['samples']
        if all(p.get('wind_kt') is not None and p.get('gust_kt') is not None for p in s):
            points.append({'name': 'NWS grid', 'kind': 'official', 'sust': max(p['wind_kt'] for p in s), 'gust': max(p['gust_kt'] for p in s)})
    for key, model in ((gust_evidence(snapshot, now) or {}).get('models') or {}).items():
        if model:
            points.append({'name': model['label'].split(' ·')[0].replace(' (page source)', ''), 'kind': 'deterministic',
                           'sust': max(p['wind_kt'] for p in model['samples']), 'gust': max(p['gust_kt'] for p in model['samples'])})
    for key, model in ((wind or {}).get('ensembles') or {}).items():
        if model and model.get('gust_max') and model.get('sustained'):
            for stat, kind in (('p50', 'ensemble median'), ('p90', 'ensemble p90')):
                points.append({'name': f'{model["label"]} {"median" if stat == "p50" else "p90"}', 'kind': kind,
                               'sust': model['sustained'][stat], 'gust': model['gust_max'][stat]})
    try:
        from .week_ahead import wn3_flight_hours
        hours = wn3_flight_hours(snapshot, now)
        if hours:
            points.append({'name': 'WN3 mean', 'kind': 'ensemble median', 'sust': max(h['wind'][0] for h in hours), 'gust': None})
    except ERRORS:
        pass
    return points


def analysis(snapshot, now):
    packet = validate_climatology(snapshot.get('event_climatology'), snapshot)
    from .event_personal import personal
    limit = (personal(snapshot) or {}).get('gust_limit_kt', 20)
    event_day = Event(**snapshot['event']).day
    obs = packet['observed']['rows']
    season = [r for r in obs if _season(r[0], event_day)]

    def tier(r):
        if not r[5] or r[2] > limit + 5:
            return 'D'
        if r[2] > limit or r[4] > 12:
            return 'C'
        return 'B' if r[2] > 12 or r[3] >= 8 else 'A'
    tiers = {name: {t: round(100 * sum(1 for r in rows if tier(r) == t) / len(rows)) for t in 'ABCD'} | {'days': len(rows)}
             for name, rows in (('year', obs), ('season', season))}
    points = []
    for p in forecast_points(snapshot, now):
        row = dict(p)
        for name, rows in (('year', obs), ('season', season)):
            row[f'obs_sust_{name}'] = _pct(rows, lambda r: (0 if r[5] else 1, r[1]), (0, p['sust']))
            row[f'obs_gust_{name}'] = None if p['gust'] is None else _pct(rows, lambda r: (0 if r[5] else 1, r[2]), (0, p['gust']))
        points.append(row)
    models = {}
    for key, cur in packet['current']['rows'].items():
        arch = packet['models'][key]
        bad = lambda r: 1 if r[4] >= 0.2 else 0
        target = [None, *cur]
        entry = {'label': arch['label'], 'current': {'sust': cur[0], 'gust': cur[1], 'xw': cur[2], 'rain': cur[3],
                                                     'low_cloud': cur[4] if len(cur) > 4 else None}}
        for name, rows in (('year', arch['rows']), ('season', [r for r in arch['rows'] if _season(r[0], event_day)])):
            for metric, i in (('sust', 1), ('gust', 2), ('xw', 3), ('low_cloud', 5)):
                if i >= len(target):
                    entry[f'{metric}_{name}'] = None
                    continue
                ok = [r for r in rows if len(r) > i and r[i] is not None]
                entry[f'{metric}_{name}'] = None if target[i] is None or not ok else _pct(ok, lambda r: (bad(r), r[i]), (bad(target), target[i]))
            gusty = any(r[2] is not None for r in rows)  # without gusts "within the gust limit" is undefined
            entry[f'within_{name}'] = round(100 * sum(1 for r in rows if not bad(r) and r[2] is not None and r[2] <= limit) / len(rows)) if gusty else None
        models[key] = entry
    return {'packet': packet, 'limit': limit, 'tiers': tiers, 'points': points, 'models': models, 'season_days': len(season)}


def climatology_evidence(snapshot, now):
    try:
        a = analysis(snapshot, now)
    except ERRORS:
        return None
    keep = ('name', 'kind', 'sust', 'gust', 'obs_sust_year', 'obs_gust_year', 'obs_sust_season', 'obs_gust_season')
    return {'window_local': a['packet']['window']['label'], 'gust_limit_kt': a['limit'], 'tiers_percent': a['tiers'],
            'forecast_vs_observed': [{k: p[k] for k in keep} for p in a['points']],
            'forecast_vs_model_7day': a['models'], 'notes': NOTES}


def _scatter(title, note, rows, points, refs, limit):
    X1, Y1, W, H, PL, PR, PT, PB = 26, 40, 360, 330, 36, 10, 10, 34
    sx = lambda x: PL + min(max(x, 0), X1) / X1 * (W - PL - PR)
    sy = lambda y: PT + (1 - min(max(y, 0), Y1) / Y1) * (H - PT - PB)
    bins = {}
    for s, g in rows:
        bins[(round(s), round(g))] = bins.get((round(s), round(g)), 0) + 1
    out = [f'<figure class="clim-panel"><figcaption><h4>{escape(title)}</h4><p>{escape(note)}</p></figcaption>'
           f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}: sustained wind against gust">']
    for v in range(0, X1 + 1, 5):
        out.append(f'<line class="g" x1="{sx(v):.1f}" x2="{sx(v):.1f}" y1="{PT}" y2="{H - PB}"/><text class="t" x="{sx(v):.1f}" y="{H - PB + 13}" text-anchor="middle">{v}</text>')
    for v in range(0, Y1 + 1, 10):
        out.append(f'<line class="g" x1="{PL}" x2="{W - PR}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/><text class="t" x="{PL - 5}" y="{sy(v) + 4:.1f}" text-anchor="end">{v}</text>')
    if limit:
        out.append(f'<rect class="zone" x="{PL}" y="{sy(limit):.1f}" width="{W - PL - PR}" height="{sy(0) - sy(limit):.1f}"/>'
                   f'<line class="lim" x1="{PL}" x2="{W - PR}" y1="{sy(limit):.1f}" y2="{sy(limit):.1f}"/>'
                   f'<text class="rl" x="{W - PR - 3}" y="{sy(limit) - 4:.1f}" text-anchor="end">{limit}-kt gust limit</text>')
    out.append(f'<line class="ref" x1="{sx(0):.1f}" y1="{sy(0):.1f}" x2="{sx(26):.1f}" y2="{sy(26):.1f}"/>'
               f'<text class="rl" x="{sx(25):.1f}" y="{sy(25) + 13:.1f}" text-anchor="end">no gust</text>'
               f'<line class="ref" x1="{sx(0):.1f}" y1="{sy(10):.1f}" x2="{sx(26):.1f}" y2="{sy(36):.1f}"/>'
               f'<text class="rl" x="{sx(22.5):.1f}" y="{sy(34):.1f}" text-anchor="end">10-kt spread</text>')
    for (s, g), c in sorted(bins.items(), key=lambda kv: -kv[1]):
        out.append(f'<circle class="bg" cx="{sx(s):.0f}" cy="{sy(g):.0f}" r="{1.6 + 2.1 * c ** .5:.1f}"><title>{c}d {s}G{g}</title></circle>')
    for r in refs:
        out.append(f'<circle class="refpt" cx="{sx(r["sustained_kt"]):.1f}" cy="{sy(r["gust_kt"]):.1f}" r="6"><title>{escape(r["label"])}: {r["sustained_kt"]:g} kt, gust {r["gust_kt"]:g}. {escape(r["note"])}</title></circle>'
                   f'<text class="dl" x="{sx(r["sustained_kt"]) + 9:.1f}" y="{sy(r["gust_kt"]) + 4:.1f}">{escape(r["label"])}</text>')
    for p in points:
        if p['gust'] is None:
            continue
        out.append(f'<circle class="fc{" tail" if p["kind"] == "ensemble p90" else ""}" cx="{sx(p["sust"]):.1f}" cy="{sy(p["gust"]):.1f}" r="5">'
                   f'<title>{escape(p["name"])}: {p["sust"]:.0f} kt, gust {p["gust"]:.0f}</title></circle>')
    if any(p['gust'] is not None for p in points):
        low = min((p for p in points if p['gust'] is not None), key=lambda p: p['gust'])
        out.append(f'<text class="dl" x="{min(sx(low["sust"]) + 8, W - 120):.1f}" y="{sy(low["gust"]) + 16:.1f}">Event forecasts</text>')
    out.append(f'<text class="ax" x="{(PL + W - PR) / 2:.0f}" y="{H - 3}" text-anchor="middle">Sustained, kt</text>'
               f'<text class="ax" transform="translate(10 {(PT + H - PB) / 2:.0f}) rotate(-90)" text-anchor="middle">Gust, kt</text></svg>')
    within = sum(1 for _, g in rows if g <= limit) if limit else None
    if within is not None:
        out.append(f'<p class="clim-stat">{round(100 * within / len(rows))}% of {len(rows)} days at or under {limit} kt gust.</p>')
    return ''.join(out) + '</figure>'


def render_climatology(snapshot, now):
    try:
        a = analysis(snapshot, now)
    except ERRORS:
        return ''
    from .event_personal import personal
    refs = (personal(snapshot) or {}).get('references', [])
    p, limit, t = a['packet'], a['limit'], a['tiers']
    event_day = Event(**snapshot['event']).day
    fmt = lambda v: '—' if v is None else f'{v}'
    tier_rows = ''.join(f'<tr><th scope="row">{escape(label)}</th><td>{t["year"][k]}%</td><td>{t["season"][k]}%</td></tr>' for k, label in (
        ('A', 'Easy: VFR, dry, peak 12 kt or less, little gust'), ('B', f'Flyable with gusts: peak 13–{limit} kt'),
        ('C', f'Over the limit: gust {limit + 1}–{limit + 5} kt or crosswind over 12 kt'),
        ('D', f'No-go: ceiling under 3,000 ft, visibility under 5 SM, precipitation, or gust over {limit + 5} kt')))
    point_rows = ''.join(
        f'<tr{" class=tail" if pt["kind"] == "ensemble p90" else ""}><th scope="row">{escape(pt["name"])}</th><td>{pt["sust"]:.0f}{"" if pt["gust"] is None else "G" + format(pt["gust"], ".0f")}</td>'
        f'<td>{fmt(pt["obs_gust_year"])} / {fmt(pt["obs_sust_year"])}</td><td>{fmt(pt["obs_gust_season"])} / {fmt(pt["obs_sust_season"])}</td></tr>'
        for pt in a['points'])
    model_rows_html = ''.join(
        f'<tr><th scope="row">{escape(m["label"])}</th><td>{m["current"]["sust"]:.0f}{"" if m["current"]["gust"] is None else "G" + format(m["current"]["gust"], ".0f")}</td>'
        f'<td>{fmt(m["sust_year"])} ({fmt(m["sust_season"])})</td><td>{fmt(m["gust_year"])} ({fmt(m["gust_season"])})</td>'
        f'<td>{fmt(m["xw_year"])} ({fmt(m["xw_season"])})</td>'
        f'<td>{"—" if m["current"]["low_cloud"] is None else format(m["current"]["low_cloud"], ".0f") + "% · " + fmt(m["low_cloud_year"])}</td>'
        f'<td>{"—" if m["within_year"] is None else str(m["within_year"]) + "%"}</td></tr>' for m in a['models'].values())
    obs_period = p['observed']['period']
    panels = [_scatter('Observed at KCDW', f'{a["packet"]["window"]["label"]} local, {obs_period[0][:7]} to {obs_period[1][:7]}. Many days sit on the no-gust line (METAR gust reporting).',
                       [(r[1], r[2]) for r in p['observed']['rows']], a['points'], refs, limit)]
    for key in SCATTER_MODELS:
        m = p['models'].get(key)
        if m and sum(1 for r in m['rows'] if r[2] is not None) > 300:
            panels.append(_scatter(f'{m["label"]} 7-day forecasts', f'What {m["label"]} predicted about 7 days ahead, {m["period"][0][:7]} to {m["period"][1][:7]}.',
                                   [(r[1], r[2]) for r in m['rows'] if r[2] is not None], a['points'], refs, limit))
    return (f'<section id="climatology" class="weather-pattern climatology" aria-labelledby="climatology-title">'
            f'<p class="eyebrow">How this day compares · {escape(a["packet"]["window"]["label"])} local</p>'
            f'<h2 id="climatology-title">Against past afternoons and each model\'s 7-day forecasts</h2>'
            f'<p>Percentile is the share of days less favorable than the forecast; higher is better. "Season" is the {a["season_days"]} observed days within {SEASON_DAYS} days of {event_day:%b %-d} in past years.</p>'
            f'<div class="table-wrap"><table><caption>Observed KCDW afternoons by tier (your {limit}-kt gust limit).</caption>'
            f'<thead><tr><th scope="col">Tier</th><th scope="col">All year ({t["year"]["days"]} days)</th><th scope="col">Season ({t["season"]["days"]} days)</th></tr></thead><tbody>{tier_rows}</tbody></table></div>'
            f'<div class="table-wrap"><table><caption>Each current forecast ranked against observed afternoons: by gust / by sustained wind. The true rank is between the two.</caption>'
            f'<thead><tr><th scope="col">Forecast</th><th scope="col">Wind kt</th><th scope="col">All year</th><th scope="col">Season</th></tr></thead><tbody>{point_rows}</tbody></table></div>'
            f'<div class="table-wrap"><table><caption>Each model\'s current forecast ranked against its own 7-day forecasts (season in parentheses).</caption>'
            f'<thead><tr><th scope="col">Model</th><th scope="col">Now</th><th scope="col">Sustained</th><th scope="col">Gust</th><th scope="col">Crosswind</th><th scope="col">Low cloud · pct</th><th scope="col">Its 7-day forecasts dry and within limit</th></tr></thead><tbody>{model_rows_html}</tbody></table></div>'
            f'<h3>Sustained wind against gust</h3><p class="small">Gray dots are days, sized by count. Blue dots are this event\'s current forecasts (hollow: ensemble 90th percentile); orange are your reference flights. Hover a dot for its value.</p>'
            f'<div class="clim-panels">{"".join(panels)}</div>'
            f'<details><summary>Climatology sources &amp; limits</summary><ul>{"".join("<li>" + escape(n) + "</li>" for n in NOTES)}</ul>'
            f'<p>Archives refreshed {escape(parse_time(p["built_at"]).astimezone(TZ).strftime("%b %-d %H:%M %Z"))}; current model forecasts fetched {escape(parse_time(p["current"]["fetched_at"]).astimezone(TZ).strftime("%b %-d %H:%M %Z"))} (latest available run, not run-pinned). '
            f'<a href="https://mesonet.agron.iastate.edu/request/download.phtml">IEM ASOS archive</a> · <a href="https://open-meteo.com/en/docs/previous-runs-api">Open-Meteo previous runs (CC BY 4.0)</a></p></details></section>')
