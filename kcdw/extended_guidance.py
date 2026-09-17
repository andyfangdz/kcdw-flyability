"""Bounded official CPC/WPC context, never a flyability or rain-chance score.

collect_extended returns an envelope; ok means ANY usable product. data.cpc and
 data.wpc independently contain {ok, products, error}. Products retain issue and
half-open valid windows, source links, excerpts, and optional polygon categories.
CPC date-only validity is interpreted as inclusive local calendar dates at KCDW;
GIS fcst_date is date precision, not a fabricated issuance hour. WPC uses UTC.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

UTC = timezone.utc
LOCAL = ZoneInfo('America/New_York')
MAX_TEXT_BYTES = 150_000
CPC_URL = 'https://www.cpc.ncep.noaa.gov/products/predictions/610day/fxus06.html'
WPC_URL = 'https://www.wpc.ncep.noaa.gov/discussions/pmdepd.html'
ERO_URL = 'https://www.wpc.ncep.noaa.gov/discussions/qpferd.html'
AGE_HOURS = {'cpc': 48, 'wpc': 36}
MONTHS = {m: i for i, m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(), 1)}
REGION = re.compile(r'Northeast|New England|New Jersey|Mid[- ]Atlantic|Eastern U\.S\.|East Coast|Great Lakes', re.I)


def _iso(d):
    return d.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _dt(s):
    d = s if isinstance(s, datetime) else datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone required')
    return d.astimezone(UTC)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag in ('br', 'p'): self.parts.append('\n')


def _plain(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_TEXT_BYTES:
        raise ValueError('invalid or oversized bulletin')
    pre = re.search(r'<pre\b[^>]*>(.*?)</pre>', text, re.I | re.S)
    p = _Text()
    p.feed(pre[1] if pre else text)
    return ''.join(p.parts).replace('\r', '')


def _issued(text):
    m = re.search(r'\b(\d{3,4})\s+(AM|PM)\s+(EDT|EST|UTC|GMT)\s+\w{3}\s+([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})', text, re.I)
    if not m: raise ValueError('missing issue timestamp')
    clock = m[1].zfill(4)
    hour, minute = int(clock[:2]), int(clock[2:])
    if not 1 <= hour <= 12: raise ValueError('invalid issue hour')
    hour = hour % 12 + (12 if m[2].upper() == 'PM' else 0)
    zone = timezone(timedelta(hours={'EDT': -4, 'EST': -5, 'UTC': 0, 'GMT': 0}[m[3].upper()]))
    return datetime(int(m[6]), MONTHS[m[4][:3].upper()], int(m[5]), hour, minute, tzinfo=zone).astimezone(UTC)


def _excerpt(text):
    # Select region-naming sentences before truncation so a long national
    # paragraph cannot bury Northeast precipitation behind western temperatures.
    paras = [' '.join(x.split()) for x in re.split(r'\n\s*\n', text) if x.strip()]
    chosen = []
    for index, paragraph in enumerate(paras):
        if paragraph.startswith('http'):
            continue
        paragraph = re.sub(r'\.{3}[^.]+\.{3}', '', paragraph).strip()
        regional = [s for s in re.split(r'(?<=[.!?])\s+(?=[A-Z])', paragraph) if REGION.search(s)]
        chosen.extend(regional)
        if not paragraph and REGION.search(paras[index]) and index + 1 < len(paras):
            chosen.append(paras[index] + ' ' + paras[index + 1])
    chosen = list(dict.fromkeys(chosen))
    chosen.sort(key=lambda p: not bool(re.search(r'precipitation|rainfall|rain|showers|thunderstorms', p, re.I)))
    if not chosen:
        return 'No Northeast-specific passage identified; national context: ' + ' '.join(paras)[:600]
    out = ' '.join(chosen[:4])
    return out[:1000] + (' … [excerpt truncated]' if len(out) > 1000 else '')


def _dates(month, first, end_month, last, year):
    m1, m2 = MONTHS[month[:3].upper()], MONTHS[(end_month or month)[:3].upper()]
    y2 = int(year)
    a = datetime(y2 - (m1 > m2), m1, int(first), tzinfo=LOCAL)
    b = datetime(y2, m2, int(last), tzinfo=LOCAL) + timedelta(days=1)
    return a, b


def parse_cpc(text):
    text = _plain(text)
    if 'Prognostic Discussion' not in text and 'PROGNOSTIC DISCUSSION' not in text:
        raise ValueError('not a CPC discussion')
    issued = _issued(text)
    matches = list(re.finditer(r'(6-10|8-14) DAY OUTLOOK FOR\s+([A-Z]+)\s+(\d{1,2})\s*-\s*(?:([A-Z]+)\s+)?(\d{1,2})\s*,?\s*(\d{4})', text, re.I))
    if len(matches) != 2 or {m[1] for m in matches} != {'6-10', '8-14'}:
        raise ValueError('missing CPC valid windows')
    products = []
    for i, m in enumerate(matches):
        a, b = _dates(*m.groups()[1:])
        days = 5 if m[1] == '6-10' else 7
        if (b.date() - a.date()).days != days or not 0 <= (a.date() - issued.date()).days <= 15:
            raise ValueError('invalid CPC valid window')
        body = text[m.end():matches[i+1].start() if i+1 < len(matches) else len(text)]
        body = re.split(r'FORECASTER:|Notes:|6-10 DAY OUTLOOK TABLE', body, flags=re.I)[0]
        table = re.search(re.escape(m[1]) + r' DAY OUTLOOK TABLE(.*?)(?=8-14 DAY OUTLOOK TABLE|LEGEND|$)', text, re.I | re.S)
        row = re.search(r'NEW JERSEY\s+([ANB])\s+([ANB])\b', table[1], re.I) if table else None
        products.append({'id': 'cpc_' + m[1], 'title': 'CPC ' + m[1] + ' day outlook', 'issued_at': _iso(issued),
                         'valid_start': _iso(a), 'valid_end': _iso(b),
                         'valid_label': f'{a:%b %d}–{b-timedelta(days=1):%b %d, %Y} (inclusive local dates)',
                         'source_url': CPC_URL, 'excerpt': _excerpt(body), 'scope': 'New Jersey state-average categories; regional narrative, not a site-specific forecast',
                         'regional_categories': {'temperature': row[1].upper(), 'precipitation': row[2].upper()} if row else None})
    return products


def parse_wpc(text, kind):
    text = _plain(text)
    title = {'medium_range': 'Extended Forecast Discussion', 'excessive_rainfall': 'Excessive Rainfall Discussion'}[kind]
    if title.lower() not in text.lower(): raise ValueError('wrong WPC bulletin')
    issued = _issued(text)
    pat = r'Valid\s+(\d{2})Z\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})\s*-\s*(\d{2})Z\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})'
    matches = list(re.finditer(pat, text, re.I))
    if not matches: raise ValueError('missing WPC valid window')
    products = []
    for i, m in enumerate(matches):
        def date(offset): return datetime(int(m[4+offset]), MONTHS[m[2+offset].upper()], int(m[3+offset]), int(m[1+offset]), tzinfo=UTC)
        a, b = date(0), date(4)
        if not timedelta(0) < b-a <= timedelta(days=8) or abs(a-issued) > timedelta(days=10):
            raise ValueError('invalid WPC valid window')
        body = text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)]
        body = body.split('Additional 3-7 Day Hazard')[0]
        heading = re.search(r'(Day \d+(?: and Day \d+)?)\s*$', text[:m.start()], re.I)
        if kind == 'excessive_rainfall' and not heading:
            raise ValueError('missing excessive rainfall day label')
        day_label = heading[1] if heading else ''
        products.append({'id': f'wpc_{kind}_{i+1}', 'title': 'WPC ' + title + (f' · {day_label}' if kind == 'excessive_rainfall' else ''),
                         'issued_at': _iso(issued), 'valid_start': _iso(a), 'valid_end': _iso(b),
                         'source_url': ERO_URL if kind == 'excessive_rainfall' else WPC_URL,
                         'excerpt': _excerpt(body), 'scope': 'Regional narrative, not a site-specific forecast; no KCDW risk category inferred'})
    return products


def point_url(period, variable):
    if period not in ('6-10', '8-14') or variable not in ('temperature', 'precipitation'):
        raise ValueError('unsupported CPC point product')
    base = 'https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/'
    return base + f'cpc_{period.replace("-", "_")}_day_outlk/MapServer/{0 if variable == "temperature" else 1}/query?' + urlencode({
        'f': 'json', 'geometry': '-74.2814,40.8752', 'geometryType': 'esriGeometryPoint', 'inSR': 4326,
        'spatialRel': 'esriSpatialRelIntersects', 'outFields': 'fcst_date,start_date,end_date,cat,prob',
        'returnGeometry': 'false', 'resultRecordCount': 5})


def parse_cpc_point(data, period, variable):
    url = point_url(period, variable)
    if not isinstance(data, dict) or data.get('error') or data.get('exceededTransferLimit') or len(data.get('features', [])) != 1:
        raise ValueError('missing/ambiguous CPC polygon intersection')
    try:
        a = data['features'][0]['attributes']
        dates = [datetime.fromtimestamp(a[k]/1000, UTC) for k in ('fcst_date', 'start_date', 'end_date')]
        issue, first, last = dates
        prob, cat = float(a['prob']), a['cat']
        if not math.isfinite(prob) or cat not in ('Above', 'Below', 'Normal') or prob not in (33, 36, 40, 50, 60, 70, 80, 90):
            raise ValueError('unsupported CPC probability/category')
        if (cat == 'Normal') != (prob == 36): raise ValueError('invalid CPC normal category')
        expected = 6 if period == '6-10' else 8
        if (first-issue).days != expected or (last-first).days != (4 if period == '6-10' else 6):
            raise ValueError('invalid GIS date relationship')
        first_local = datetime.combine(first.date(), datetime.min.time(), LOCAL)
        end_local = datetime.combine(last.date()+timedelta(days=1), datetime.min.time(), LOCAL)
    except (KeyError, TypeError, OverflowError, OSError) as exc:
        raise ValueError('invalid CPC GIS schema') from exc
    label = 'Near normal (CPC category; numeric probability not displayed)' if cat == 'Normal' else f'{cat} normal; CPC map probability contour {prob:g}%'
    return {'id': f'cpc_{period}_{variable}_point', 'title': f'CPC {period} day KCDW {variable}', 'issued_at': _iso(issue), 'issue_precision': 'date',
            'valid_start': _iso(first_local), 'valid_end': _iso(end_local), 'source_url': url,
            'scope': 'CPC polygon intersecting KCDW (40.8752, -74.2814); period-average tercile outlook, not a site-specific forecast',
            'category': cat, 'map_probability': prob, 'probability_label': label,
            'excerpt': label + '. Map contour/category, not a calibrated exact airport probability.'}


def _state(p, provider, now):
    try:
        issue, a, b = (_dt(p[k]) for k in ('issued_at', 'valid_start', 'valid_end'))
        if a >= b: return 'invalid valid window'
        if issue > now: return 'future issue'
        if now-issue > timedelta(hours=AGE_HOURS[provider]): return 'stale'
        if b <= now: return 'expired'
        return 'current'
    except (ValueError, TypeError, KeyError):
        return 'unknown dates'


def collect_extended(client, now):
    now = _dt(now)
    data = {k: {'ok': False, 'products': [], 'error': None} for k in ('cpc', 'wpc')}
    errors = {'cpc': [], 'wpc': []}
    jobs = [('cpc', CPC_URL, lambda t: parse_cpc(t)),
            ('wpc', WPC_URL, lambda t: parse_wpc(t, 'medium_range')),
            ('wpc', ERO_URL, lambda t: parse_wpc(t, 'excessive_rainfall'))]
    for provider, url, parser in jobs:
        try: data[provider]['products'].extend(parser(client.get_text(url, maximum=MAX_TEXT_BYTES)))
        except Exception as exc: errors[provider].append(f'{url}: {type(exc).__name__}: {exc}'[:350])
    for period in ('6-10', '8-14'):
        for variable in ('temperature', 'precipitation'):
            try: data['cpc']['products'].append(parse_cpc_point(client.get(point_url(period, variable)), period, variable))
            except Exception as exc: errors['cpc'].append(f'{period} {variable} GIS unavailable: {type(exc).__name__}: {exc}'[:200])
    for provider, block in data.items():
        for product in block['products']:
            product['status_at_fetch'] = _state(product, provider, now)
        block['ok'] = any(p['status_at_fetch'] == 'current' for p in block['products'])
        if not block['ok']: errors[provider].append('No current usable products')
        block['error'] = '; '.join(errors[provider]) or None
    return {'ok': any(b['ok'] for b in data.values()), 'fetched_at': _iso(now), 'data': data,
            'error': '; '.join(f'{k}: {b["error"]}' for k, b in data.items() if b['error']) or None}


def _coverage(p, start, end):
    a, b = _dt(p['valid_start']), _dt(p['valid_end'])
    if b <= start:
        return f'does not yet cover {start.astimezone(LOCAL):%b %d}; shorter-range product ends before mission'
    if a >= end:
        return 'no mission overlap; product begins after mission'
    if a <= start and b >= end:
        return 'full mission overlap (broad period, not hourly timing)'
    return 'partial mission overlap only'


def _visible_summary(data, start, end, now):
    e = lambda value: escape(str(value), quote=True)
    parts = ['<div class="extended-highlights">']
    cpc = [p for p in data.get('cpc', {}).get('products', []) if _state(p, 'cpc', now) == 'current']
    for period in ('6-10', '8-14'):
        selected = [p for p in cpc if p['id'].startswith('cpc_' + period) and _dt(p['valid_start']) < end and _dt(p['valid_end']) > start]
        if not selected:
            continue
        labels = []
        for variable in ('temperature', 'precipitation'):
            point = next((p for p in selected if p['id'] == f'cpc_{period}_{variable}_point'), None)
            if point:
                labels.append(variable + ': ' + point['probability_label'])
            else:
                regional = next((p.get('regional_categories', {}) for p in selected if p.get('regional_categories')), {})
                names = {'A': 'above normal', 'N': 'near normal', 'B': 'below normal'}
                labels.append(variable + ': ' + names.get(regional.get(variable, ""), 'unavailable') + ' (New Jersey state average)')
        p = selected[0]
        dates = f'{_dt(p["valid_start"]).astimezone(LOCAL):%b %d}–{(_dt(p["valid_end"]).astimezone(LOCAL)-timedelta(days=1)):%b %d}'
        parts.append(f'<p><strong>CPC {period} day · {e(dates)}</strong><br>{e("; ".join(labels))}.<br><small>{e(_coverage(p,start,end))}; period averages, not daily rain chance.</small></p>')
    if len(parts) == 1:
        parts.append('<p><strong>CPC:</strong> no current outlook overlaps this mission window; see product dates below.</p>')
    wpc = [p for p in data.get('wpc', {}).get('products', []) if _state(p, 'wpc', now) == 'current']
    overlapping = [p for p in wpc if _dt(p['valid_start']) < end and _dt(p['valid_end']) > start]
    chosen = overlapping[:2] if overlapping else sorted(wpc, key=lambda p: p['valid_end'], reverse=True)[:1]
    for p in chosen:
        parts.append(f'<p><strong>{e(p["title"])}</strong><br>{e(_coverage(p,start,end))}. '
                     f'<small>Valid {e(p["valid_start"])} – {e(p["valid_end"])}.</small></p>')
        if p in overlapping:
            parts.append(f'<p>{e(p["excerpt"][:420])}{" … (excerpt)" if len(p["excerpt"]) > 420 else ""}</p>')
    if not wpc:
        parts.append('<p><strong>WPC:</strong> no current usable guidance; missing is not favorable.</p>')
    return ''.join(parts) + '</div>'


def render_extended(source, start, end, now):
    """Recheck issue age AND mission coverage; never trust stored ok/status flags."""
    e = lambda value: escape(str(value), quote=True)
    parts = ['<section aria-label="CPC and WPC extended guidance" style="border:1px solid #718096;padding:0.7rem;margin:0.6rem 0;border-radius:0.4rem;font:inherit">',
             '<h3 style="margin:0 0 0.4rem">CPC / WPC extended context</h3>']
    try:
        start, end, now = map(_dt, (start, end, now))
        if start >= end: raise ValueError('invalid mission window')
    except (ValueError, TypeError, AttributeError):
        return ''.join(parts) + '<p>Unavailable: invalid mission window or current time.</p></section>'
    source = source if isinstance(source, dict) else {}
    data = source.get('data') or {}
    parts.append(_visible_summary(data, start, end, now))
    parts.append('<p><small>CPC: <strong>not probability of rain</strong>; period-average tercile outlook. WPC excessive rainfall: flash-flood guidance exceedance within 25 miles, <strong>not ordinary rain chance</strong>.</small></p>')
    try:
        fetched = _dt(source['fetched_at']).astimezone(LOCAL).strftime('%b %d, %H:%M %Z')
    except (KeyError, ValueError, TypeError, AttributeError):
        fetched = 'unknown'
    parts.append(f'<p><small>Retrieved {e(fetched)}. Individual issue and valid times below.</small></p>')
    for provider in ('cpc', 'wpc'):
        block = data.get(provider) or {}
        products = block.get('products') or []
        parts.append(f'<details><summary>{provider.upper()} · {sum(_state(p, provider, now) == "current" for p in products)} current products · dates &amp; narrative</summary>')
        if not products: parts.append('<p>Guidance unavailable; missing evidence is not favorable.</p>')
        for p in products:
            status = _state(p, provider, now)
            coverage = 'mission coverage unknown / unusable'
            if status == 'current':
                coverage = _coverage(p, start, end)
            issue = p.get('issued_at', 'unknown')
            if p.get('issue_precision') == 'date': issue = str(issue)[:10] + ' (forecast date only; issuance hour unavailable)'
            url = str(p.get('source_url', ''))
            parsed = urlsplit(url)
            link = f'<a href="{e(url)}" rel="noopener noreferrer">Official product</a>' if parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.noaa.gov') else 'Official link unavailable'
            parts.append(f'<div style="margin:0.5rem 0"><strong>{e(p.get("title", "Product"))}</strong> — {e(status)}; {e(coverage)}<br><small>Issued {e(issue)} · Valid {e(p.get("valid_label") or str(p.get("valid_start", "unknown")) + " – " + str(p.get("valid_end", "unknown")) + " (end exclusive)")} · {link}</small>')
            cats = p.get('regional_categories')
            if cats:
                names = {'A': 'above normal', 'N': 'near normal', 'B': 'below normal'}
                parts.append('<p style="margin:0.2rem 0">New Jersey state-average categories: ' + e('; '.join(f'{k}: {names.get(v, "unknown")}' for k,v in cats.items())) + '; numerical probabilities unavailable in state table.</p>')
            parts.append(f'<p style="margin:0.2rem 0">{e(p.get("excerpt", "Excerpt unavailable"))}</p><small>{e(p.get("scope", "Scope unknown"))}</small></div>')
        if block.get('error'): parts.append(f'<p><small>Partial availability / collection notes: {e(block["error"])}</small></p>')
        parts.append('</details>')
    return ''.join(parts) + '</section>'
