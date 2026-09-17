"""Bounded official Atlantic NHC guidance; independent of long-range models.

Three fixed requests plus at most eight Atlantic forecast advisories; retries
belong to Client (normally two). No GIS binary requests: TCM provides the actual
forecast centers/intensities, including OUTLOOK VALID and dissipation rows.
Freshness limits are conservative application policies, not NHC expiration times.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .events import local_clock

UTC = timezone.utc
INVENTORY_URL = 'https://www.nhc.noaa.gov/CurrentStorms.json'
OUTLOOK_URL = 'https://www.nhc.noaa.gov/text/MIATWOAT.shtml'
RSS_URL = 'https://www.nhc.noaa.gov/index-at.xml'
MAX_TEXT = 160_000
MAX_STORMS = 8
MAX_AGE = timedelta(hours=8)
INVENTORY_AGE = timedelta(hours=3)


def _dt(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone required')
    return d.astimezone(UTC)


def _iso(value):
    return _dt(value).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _text(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_TEXT:
        raise ValueError('invalid/oversize NHC text')
    pre = re.search(r'<pre\b[^>]*>(.*?)</pre>', raw, re.I | re.S)
    raw = pre[1] if pre else raw
    return unescape(re.sub(r'<[^>]*>', '', raw)).replace('\r', '')


def _issued(text):
    months = {calendar.month_abbr[n].upper(): n for n in range(1, 13)}
    m = re.search(r'(?mi)^\s*(\d{3,4}) UTC \w{3} (\w{3}) (\d{1,2}) (\d{4})\s*$', text)
    if m:
        return datetime(int(m[4]), months[m[2].upper()], int(m[3]), int(m[1][:-2]), int(m[1][-2:]), tzinfo=UTC)
    m = re.search(r'(?mi)^\s*(\d{1,4}) (AM|PM) (EDT|EST|AST|UTC) \w{3} (\w{3}) (\d{1,2}) (\d{4})\s*$', text)
    if not m:
        raise ValueError('missing NHC issue timestamp')
    hhmm = m[1].zfill(4)
    hour, minute = int(hhmm[:-2]), int(hhmm[-2:])
    if not 1 <= hour <= 12:
        raise ValueError('invalid NHC clock')
    hour = hour % 12 + (12 if m[2].upper() == 'PM' else 0)
    offset = {'EDT': -4, 'EST': -5, 'AST': -4, 'UTC': 0}[m[3].upper()]
    return datetime(int(m[6]), months[m[4].upper()], int(m[5]), hour, minute,
                    tzinfo=timezone(timedelta(hours=offset))).astimezone(UTC)


def parse_outlook(raw):
    text = _text(raw)
    if not re.search(r'(?m)^TWOAT\s*$', text) or 'For the North Atlantic' not in text:
        raise ValueError('not an Atlantic TWO')
    issued = _issued(text)
    body = text.split('For the North Atlantic', 1)[1].split(':', 1)[-1].split('$$', 1)[0].strip()
    areas = []
    # Pair each paragraph with both horizons; preserve qualifiers such as near.
    pat = r'\* Formation chance through 48 hours[^\n]*?((?:near )?\d{1,3} percent)\.\s*\n\* Formation chance through 7 days[^\n]*?((?:near )?\d{1,3} percent)\.'
    last = 0
    for m in re.finditer(pat, body, re.I):
        if len(areas) >= 8:
            raise ValueError('too many formation areas')
        narrative = ' '.join(body[last:m.start()].split())
        if any(int(re.findall(r'\d+', m[n])[0]) > 100 for n in (1, 2)):
            raise ValueError('invalid formation probability')
        areas.append({'narrative': narrative[:1800], 'chance_48h': m[1], 'chance_7day': m[2]})
        last = m.end()
    none = bool(re.search(r'Tropical cyclone formation is not expected during the next 7 days', body, re.I))
    if not areas and not none:
        raise ValueError('unrecognized seven-day formation outlook')
    return {'issued_at': _iso(issued), 'valid_start': _iso(issued),
            'valid_end': _iso(issued + timedelta(days=7)), 'areas': areas,
            'no_formation_expected': none, 'narrative': ' '.join(body.split())[:6000],
            'source_url': OUTLOOK_URL}


def _day_time(token, issued):
    day, clock = token.split('/')
    # Search actual calendar days, not a guessed month: handles Dec/Jan rollover.
    options = [issued.replace(hour=int(clock[:2]), minute=int(clock[2:4]), second=0, microsecond=0) + timedelta(days=i) for i in range(-1, 8)]
    matches = [d for d in options if d.day == int(day) and issued <= d <= issued + timedelta(hours=126)]
    if len(matches) != 1:
        raise ValueError('forecast valid time outside NHC horizon')
    return matches[0]


def parse_advisory(raw, storm_id=None):
    text = _text(raw)
    if 'FORECAST/ADVISORY' not in text.upper():
        raise ValueError('not a forecast advisory')
    if storm_id and not re.search(r'\b' + re.escape(storm_id) + r'\b', text, re.I):
        raise ValueError('advisory storm identity mismatch')
    issued = _issued(text)
    track = []
    pattern = r'(?m)^(?:FORECAST|OUTLOOK) VALID (\d{2}/\d{4})Z([^\n]*)(.*?)(?=^(?:FORECAST|OUTLOOK) VALID|^NEXT ADVISORY|^REQUEST|\Z)'
    for m in re.finditer(pattern, text, re.S | re.M):
        valid = _day_time(m[1], issued)
        if track and valid <= _dt(track[-1]['valid_at']):
            raise ValueError('unordered forecast track')
        loc = re.search(r'(\d{1,2}\.\d+)([NS])\s+(\d{1,3}\.\d+)([EW])', m[2])
        wind = re.search(r'MAX WIND\s+(\d+) KT(?:\.*GUSTS\s+(\d+) KT)?', m[3])
        status = m[2].split('...', 1)[1].strip()[:120] if '...' in m[2] else ''
        point = {'valid_at': _iso(valid), 'latitude': None, 'longitude': None,
                 'wind_kt': None, 'gust_kt': None, 'status': status}
        if loc:
            lat, lon = float(loc[1]), float(loc[3])
            if lat > 90 or lon > 180 or not wind or int(wind[1]) > 250:
                raise ValueError('invalid forecast coordinate/intensity')
            point.update(latitude=lat * (-1 if loc[2] == 'S' else 1), longitude=lon * (-1 if loc[4] == 'W' else 1), wind_kt=int(wind[1]), gust_kt=int(wind[2]) if wind[2] else None)
        elif not re.search(r'DISSIPATED|ABSORBED', status):
            raise ValueError('missing forecast center')
        track.append(point)
    if not track or len(track) > 12:
        raise ValueError('missing/oversize official forecast track')
    next_match = re.search(r'NEXT ADVISORY AT (\d{2}/\d{4})Z', text)
    next_at = _day_time(next_match[1], issued) if next_match else issued + timedelta(hours=6)
    return {'issued_at': _iso(issued), 'valid_start': _iso(issued),
            'valid_end': track[-1]['valid_at'], 'next_advisory_at': _iso(next_at),
            'track': track, 'narrative': ' '.join(text.split())[:6500]}


def _advisory_url(value):
    if not isinstance(value, str):
        raise ValueError('missing advisory URL')
    u = urlsplit(value)
    if (u.scheme != 'https' or u.netloc not in ('www.nhc.noaa.gov', 'nhc.noaa.gov')
            or u.query or u.fragment or not re.fullmatch(r'/text/MIATCMAT[1-5]\.shtml', u.path)):
        raise ValueError('unapproved NHC advisory URL')
    return value


def _fresh(issued, now, limit=MAX_AGE):
    try:
        age = _dt(now) - _dt(issued)
        return timedelta(0) <= age <= limit
    except (ValueError, TypeError, AttributeError):
        return False


def collect_nhc(client, now):
    """Return partial evidence on failures; ok means all components usable now."""
    now = _dt(now)
    data = {'provider': 'NOAA/NWS National Hurricane Center', 'basin': 'Atlantic',
            'outlook': None, 'inventory': None, 'storms': [], 'errors': []}
    errors = data['errors']
    try:
        outlook = parse_outlook(client.get_text(OUTLOOK_URL, maximum=MAX_TEXT))
        data['outlook'] = outlook
        if not _fresh(outlook['issued_at'], now):
            raise ValueError('outlook expired or future-issued')
    except Exception as exc:
        errors.append('Formation outlook: ' + str(exc)[:240])
    # RSS supplies inventory provenance, not access to independent advisories.
    refreshed = None
    try:
        rss = ET.fromstring(client.get_text(RSS_URL, maximum=MAX_TEXT))
        refreshed = _iso(parsedate_to_datetime(rss.findtext('./channel/pubDate') or ''))
    except Exception as exc:
        errors.append('Inventory RSS provenance unavailable: ' + type(exc).__name__)
    try:
        getter = getattr(client, 'getJSON', None) or client.get
        inventory = getter(INVENTORY_URL)
        rows = inventory.get('activeStorms') if isinstance(inventory, dict) else None
        if not isinstance(rows, list) or len(rows) > 40:
            raise ValueError('invalid CurrentStorms inventory')
        if any(not isinstance(s, dict) or not re.fullmatch(r'(?:al|ep|cp)\d{6}', str(s.get('id', ''))) for s in rows):
            raise ValueError('invalid storm identity in inventory')
        atlantic = [s for s in rows if s['id'].startswith('al')]
        if len({s['id'] for s in rows}) != len(rows):
            raise ValueError('duplicate inventory storm')
        data['inventory'] = {'fetched_at': _iso(now), 'feed_refreshed_at': refreshed,
                             'atlantic_count': len(atlantic), 'source_url': INVENTORY_URL}
        if not _fresh(refreshed, now, INVENTORY_AGE):
            errors.append('Inventory feed refresh expired or future-issued')
        if len(atlantic) > MAX_STORMS:
            errors.append('Atlantic storm cap exceeded; inventory incomplete below')
        for storm in atlantic[:MAX_STORMS]:
            entry = {'id': storm['id'], 'name': str(storm.get('name', storm['id']))[:100],
                     'classification': str(storm.get('classification', ''))[:30],
                     'advisory': None, 'error': None}
            data['storms'].append(entry)
            try:
                meta = storm.get('forecastAdvisory') or {}
                url = _advisory_url(meta.get('url'))
                a = parse_advisory(client.get_text(url, maximum=MAX_TEXT), storm['id'])
                if _dt(meta.get('issuance')) != _dt(a['issued_at']):
                    raise ValueError('inventory/advisory issue mismatch; product rollover')
                a['source_url'] = url
                entry['advisory'] = a
                if not _fresh(a['issued_at'], now) or now > _dt(a['next_advisory_at']) + timedelta(hours=2):
                    raise ValueError('advisory expired or future-issued')
            except Exception as exc:
                entry['error'] = str(exc)[:240]
                errors.append(entry['id'] + ': ' + entry['error'])
    except Exception as exc:
        errors.append('Active cyclone inventory: ' + str(exc)[:240])
    return {'ok': not errors, 'fetched_at': _iso(now), 'data': data,
            'error': '; '.join(errors)[:1200] if errors else None}


def _coverage(start, end, first, last):
    if start > last:
        return 'not yet covered by this official product'
    if end < first:
        return 'outside this product’s valid window (before issuance)'
    if start >= first and end <= last:
        return 'within this product’s time horizon (not a KCDW impact forecast)'
    return 'only partly covered; remainder outside this official product’s horizon'


def render_nhc(source, start, end, now):
    """Self-contained escaped HTML; recompute freshness on every render."""
    e = lambda value: escape(str(value), quote=True)
    parts = ['<section aria-label="Official NHC tropical guidance"><h3 class="nhc-title">Official NHC · Atlantic tropics</h3>']
    try:
        start, end, now = _dt(start), _dt(end), _dt(now)
        if end < start:
            raise ValueError('reversed mission window')
        parts.append(f'<p>Mission: {e(local_clock(start, True))} – {e(local_clock(end, True))}.</p>')
        data = source.get('data') or {}
        fetch_fresh = _fresh(source.get('fetched_at'), now, INVENTORY_AGE)
        parts.append(f'<p><small>Retrieved {e(local_clock(_dt(source.get("fetched_at")), True))} · collection cache {"fresh" if fetch_fresh else "expired"}.</small></p>')
        inv = data.get('inventory')
        if inv and fetch_fresh and _fresh(inv.get('feed_refreshed_at'), now, INVENTORY_AGE):
            count = inv['atlantic_count']
            inventory_text = 'no active Atlantic cyclones listed' if count == 0 else f'{count} active Atlantic cyclone(s) listed'
            parts.append(f'<p>CurrentStorms inventory: {e(inventory_text)}; Atlantic RSS refreshed {e(local_clock(_dt(inv["feed_refreshed_at"]), True))}.</p>')
        else:
            parts.append('<p>Active Atlantic cyclone inventory unavailable or expired; absence cannot be inferred.</p>')
        products = [('7-day formation outlook', data.get('outlook'), None)]
        for s in data.get('storms', [])[:MAX_STORMS]:
            products.append((f'{s.get("name", "Unknown")} ({s.get("id", "")}) · official track/intensity (up to ~5 days)', s.get('advisory'), s.get('error')))
        for label, p, error in products:
            parts.append(f'<h4>{e(label)}</h4>')
            if not p:
                parts.append(f'<p>unavailable: {e(error or "product not retrieved")}</p>')
                continue
            try:
                issued, first, last = _dt(p['issued_at']), _dt(p['valid_start']), _dt(p['valid_end'])
                if first != issued or last <= first or last > first + timedelta(days=7):
                    raise ValueError('invalid product valid window')
                fresh = fetch_fresh and _fresh(issued, now)
                if 'next_advisory_at' in p:
                    fresh = fresh and now <= _dt(p['next_advisory_at']) + timedelta(hours=2)
                # Validate all cached timestamps before emitting any fresh label
                # or opening a table; malformed cached rows must fail closed.
                if 'track' in p:
                    track = p['track']
                    if not isinstance(track, list) or not 1 <= len(track) <= 12:
                        raise ValueError('invalid cached forecast track')
                    previous = first
                    for point in track:
                        valid = _dt(point['valid_at'])
                        if not previous < valid <= last:
                            raise ValueError('invalid track valid time')
                        previous = valid
                    if previous != last:
                        raise ValueError('track horizon mismatch')
                url = OUTLOOK_URL if label == '7-day formation outlook' else _advisory_url(p.get('source_url'))
                state = 'fresh' if fresh and not error else 'expired / source issue'
                parts.append(f'<p>— {state}; issued {e(_iso(issued))}; valid {e(_iso(first))} – {e(_iso(last))}. Mission: {e(_coverage(start,end,first,last))}{"; expired guidance is not current coverage" if not fresh else ""}.</p>')
                for area in p.get('areas', [])[:8]:
                    title = area.get('narrative', '').split(':', 1)[0][:100]
                    parts.append(f'<p>{e(title)} — 48 h: {e(area.get("chance_48h"))}; 7 days: {e(area.get("chance_7day"))}.</p>')
                if p.get('no_formation_expected'):
                    parts.append('<p>NHC: tropical cyclone formation is not expected during the next 7 days (at issuance).</p>')
                if p.get('track'):
                    parts.append('<details><summary>Official forecast centers / intensity (UTC; not a cone)</summary><table><thead><tr><th>Valid UTC</th><th>Center °N, °E</th><th>Max sustained wind</th><th>Status</th></tr></thead><tbody>')
                    for point in p['track'][:12]:
                        valid = _dt(point['valid_at'])
                        if not first <= valid <= last:
                            raise ValueError('invalid track valid time')
                        center = '—' if point.get('latitude') is None else f'{point["latitude"]}, {point["longitude"]}'
                        wind = '—' if point.get('wind_kt') is None else f'{point["wind_kt"]} kt'
                        parts.append(f'<tr><td>{e(_iso(valid))}</td><td>{e(center)}</td><td>{e(wind)}</td><td>{e(point.get("status", ""))}</td></tr>')
                    parts.append('</tbody></table></details>')
                parts.append(f'<details><summary>Official narrative (bounded excerpt)</summary><p>{e(str(p.get("narrative", ""))[:6500])}</p></details>')
                url = OUTLOOK_URL if label == '7-day formation outlook' else _advisory_url(p.get('source_url'))
                parts.append(f'<p><a href="{e(url)}">Read official product</a></p>')
            except (ValueError, TypeError, KeyError) as exc:
                parts.append(f'<p>unavailable / source issue: {e(exc)}</p>')
        if source.get('error'):
            parts.append(f'<details><summary>Source issues</summary><p>{e(str(source["error"])[:1200])}</p></details>')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        parts.append(f'<p>NHC guidance unavailable / source issue: {e(exc)}</p>')
    parts.append('<p><small>Atlantic basin guidance is not a KCDW ceiling, wind or rain forecast. Formation percentages are not probabilities of KCDW impact. Track centers omit the uncertainty cone and hazards extending beyond it. Official NHC guidance is separate from WN3 long-range model output: lack of official horizon coverage does not rule out longer-range tropical risk.</small></p><p><a href="https://www.nhc.noaa.gov/gtwo.php?basin=atlc">Atlantic formation outlook</a> · <a href="https://www.nhc.noaa.gov/cyclones/">Official advisories and cones</a></p></section>')
    return ''.join(parts)
