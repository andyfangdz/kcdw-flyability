"""Official SPC outlook context, never an aviation-readiness input.

Eight fixed layered GeoJSON requests and one bounded national discussion.
SPC schema/URLs: https://www.spc.noaa.gov/gis/ . Age limits below are
conservative application policies. Empty collections without explicit SPC
labels are unknown, not evidence of absent thunderstorms or severe weather.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape, unescape
import math
import re
from zoneinfo import ZoneInfo
from .geometry import geometry_contains

UTC = timezone.utc
LOCAL = ZoneInfo('America/New_York')
BASE = 'https://www.spc.noaa.gov/products/'
GEO_URLS = {d: BASE + (f'outlook/day{d}otlk_cat.lyr.geojson' if d < 4 else
                     f'exper/day4-8/day{d}prob.lyr.geojson') for d in range(1, 9)}
TEXT_URL = BASE + 'exper/day4-8/'
CATEGORIES = {2: 'General thunder (not a severe-risk category)', 3: 'Marginal severe risk',
              4: 'Slight severe risk', 5: 'Enhanced severe risk',
              6: 'Moderate severe risk', 8: 'High severe risk'}
MAX_TEXT = 100_000
FETCH_AGE = timedelta(hours=6)


def _dt(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone required')
    return d.astimezone(UTC)


def _iso(d):
    return _dt(d).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _stamp(p, key):
    raw = p.get(key)
    if not isinstance(raw, str) or not re.fullmatch(r'\d{12}', raw):
        raise ValueError('missing/malformed ' + key)
    d = datetime.strptime(raw, '%Y%m%d%H%M').replace(tzinfo=UTC)
    if key + '_ISO' in p and _dt(p[key + '_ISO']) != d:
        raise ValueError('conflicting ' + key + ' timestamps')
    return d


def _fresh(value, now, limit):
    if not timedelta(0) <= now - _dt(value) <= limit:
        raise ValueError('stale or future timestamp')


def _contains(g):
    if not isinstance(g, dict) or g.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('unsupported/missing polygon geometry')
    polys = [g.get('coordinates')] if g['type'] == 'Polygon' else g.get('coordinates')
    if not isinstance(polys, list) or not polys or len(polys) > 1000:
        raise ValueError('empty/oversize polygons')
    total = 0
    inside = False
    for poly in polys:
        if not isinstance(poly, list) or not poly:
            raise ValueError('missing polygon rings')
        for ring in poly:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError('invalid/unclosed polygon ring')
            total += len(ring)
            if total > 100_000:
                raise ValueError('geometry vertex cap')
            for point in ring:
                if (not isinstance(point, list) or len(point) != 2 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in point) or
                    not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
                    raise ValueError('invalid polygon coordinates')
            if len({tuple(p) for p in ring}) < 3:
                raise ValueError('degenerate polygon ring')
        # Shared geometry helper ignores holes; subtract them explicitly here.
        if (geometry_contains({'type': 'Polygon', 'coordinates': [poly[0]]}, -74.2814, 40.8752)
            and not any(geometry_contains({'type': 'Polygon', 'coordinates': [hole]}, -74.2814, 40.8752)
                        for hole in poly[1:])):
            inside = True
    return inside


def _parse(raw, day, now):
    if not isinstance(raw, dict) or raw.get('type') != 'FeatureCollection' or raw.get('crs'):
        raise ValueError('invalid GeoJSON or unsupported CRS')
    features = raw.get('features')
    if not isinstance(features, list) or not features or len(features) > 100:
        raise ValueError('empty/oversize feature collection; coverage unknown')
    stamps = None
    hits, explicit = [], []
    for f in features:
        if not isinstance(f, dict) or f.get('type') != 'Feature' or not isinstance(f.get('properties'), dict):
            raise ValueError('invalid feature')
        p, g = f['properties'], f.get('geometry')
        issued, first, last = (_stamp(p, k) for k in ('ISSUE', 'VALID', 'EXPIRE'))
        if stamps is not None and stamps != (issued, first, last):
            raise ValueError('mixed product issuance/validity')
        stamps = issued, first, last
        _fresh(issued, now, timedelta(hours=12 if day == 1 else 30))
        if not (0 < (last-first).total_seconds() <= 86400 and issued < last and now < last):
            raise ValueError('expired/invalid valid window')
        # NWSI 10-512 SPC schedule: Days 2–8 are noon-to-noon UTC;
        # Day 1's overnight 01Z update ends at noon of the issuance date.
        noon = issued.replace(hour=12, minute=0, second=0, microsecond=0)
        if day == 1:
            expected_end = noon if issued.hour < 5 else noon + timedelta(days=1)
            valid_day = last == expected_end
        else:
            expected_start = noon + timedelta(days=day-1)
            valid_day = first == expected_start and last == expected_start + timedelta(days=1)
        if not valid_day:
            raise ValueError('valid window inconsistent with outlook day')
        dn = p.get('DN')
        if type(dn) not in (int, float) or dn not in (CATEGORIES if day < 4 else (0, 15, 30)):
            raise ValueError('unknown category/probability')
        if day >= 4 and dn == 0:
            label = p.get('LABEL')
            if label not in ('Potential Too Low', 'Predictability Too Low'):
                raise ValueError('unrecognized no-area status')
            if g != {'type': 'GeometryCollection', 'geometries': []}:
                raise ValueError('invalid no-area geometry')
            explicit.append(label)
        elif _contains(g):
            hits.append(dn)
    if explicit and (len(explicit) != len(features) or len(set(explicit)) != 1):
        raise ValueError('contradictory no-area features')
    if hits:
        status = CATEGORIES[max(hits)] if day < 4 else f'{max(hits):g}% severe probability area'
    elif explicit:
        status = 'SPC national status: ' + explicit[0] + (' — no 15%/30% area delineated' if explicit[0] == 'Potential Too Low' else ' — uncertainty prevents delineation')
    else:
        status = 'outside the depicted ' + ('thunder/severe polygons' if day < 4 else '15%/30% severe polygons') + '; not zero risk'
    if stamps is None:
        raise ValueError('missing product timestamps')
    issued, first, last = stamps
    return {'issued_at': _iso(issued), 'valid_start': _iso(first), 'valid_end': _iso(last),
            'status': status, 'feature_count': len(features)}


def _discussion(raw, products, now):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_TEXT:
        raise ValueError('invalid/oversize discussion')
    m = re.search(r'<pre\b[^>]*>(.*?)</pre>', raw, re.I | re.S)
    text = unescape(re.sub(r'<[^>]*>', '', m[1] if m else raw))
    if 'Day 4-8 Convective Outlook' not in text:
        raise ValueError('wrong discussion product')
    # Match full date in signed footer and UTC issue in SPC header.
    date = re.search(r'\.\.[^\n]+\.\.\s+(\d{2}/\d{2}/\d{4})', text)
    stamp = re.search(r'SPC AC (\d{2})(\d{2})(\d{2})', text)
    if not date or not stamp:
        raise ValueError('missing discussion issuance')
    issued = datetime.strptime(date[1], '%m/%d/%Y').replace(hour=int(stamp[2]), minute=int(stamp[3]), tzinfo=UTC)
    if issued.day != int(stamp[1]):
        raise ValueError('discussion issue mismatch')
    _fresh(issued, now, timedelta(hours=30))
    valid = re.search(r'Valid (\d{6})Z - (\d{6})Z', text)
    if not valid:
        raise ValueError('missing discussion valid window')
    def resolve(token):
        candidates = [issued.replace(hour=int(token[2:4]), minute=int(token[4:])) + timedelta(days=i) for i in range(10)]
        matches = [d for d in candidates if d.day == int(token[:2])]
        if len(matches) != 1:
            raise ValueError('discussion valid date outside horizon')
        return matches[0]
    first, last = resolve(valid[1]), resolve(valid[2])
    if not timedelta(days=4) <= last-first <= timedelta(days=5) or not issued < first < last:
        raise ValueError('invalid discussion window')
    return {'issued_at': _iso(issued), 'valid_start': _iso(first), 'valid_end': _iso(last),
            'text': '\n'.join(line.rstrip() for line in text.strip().splitlines())}


def collect_spc(client, now):
    """Independent fixed requests; partial products survive all other outages."""
    now = _dt(now)
    data = {'provider': 'NOAA/NWS Storm Prediction Center', 'products': [], 'discussion': None}
    errors = []
    for day, url in GEO_URLS.items():
        entry = {'day': day, 'source_url': url, 'geojson': None, 'error': None}
        data['products'].append(entry)
        try:
            entry['geojson'] = client.get(url)
            entry.update(_parse(entry['geojson'], day, now))
        except Exception as exc:
            entry['error'] = str(exc)[:200]
            errors.append(f'Day {day}: ' + entry['error'])
    try:
        text = client.get_text(TEXT_URL, maximum=MAX_TEXT)
        data['discussion'] = _discussion(text, data['products'], now)
    except Exception as exc:
        data['discussion_error'] = str(exc)[:200]
        errors.append('National discussion: ' + str(exc)[:200])
    return {'ok': not errors, 'fetched_at': _iso(now), 'data': data,
            'error': '; '.join(errors)[:1800] if errors else None}


def render_spc(source, start, end, now):
    """Escaped standalone fragment. Revalidate persisted geometry and freshness."""
    e = lambda v: escape(str(v), quote=True)
    parts = ['<section class="spc-context" aria-label="Official SPC severe-convection outlook"><h3>Official SPC · KCDW severe-convection context</h3>',
             '<p>General thunder is distinct from severe risk. Days 4–8 depict 15%/30% probabilities of severe weather within 25 miles of a point, not a point rain probability or flyability forecast. No delineated area is not a safe-to-fly verdict.</p>']
    try:
        now, start, end = _dt(now), _dt(start), _dt(end)
        if start >= end:
            raise ValueError('invalid requested window')
        _fresh(source['fetched_at'], now, FETCH_AGE)
        data = source['data']
        rows = data['products']
        if not isinstance(rows, list) or len(rows) > 8:
            raise ValueError('invalid products')
    except (KeyError, TypeError, ValueError, AttributeError):
        return ''.join(parts) + '<p>SPC coverage unknown: unavailable, stale, future-dated or invalid data/window. Not a weather clearance.</p></section>'
    parts.append('<p>Requested window: ' + e(start.astimezone(LOCAL).strftime('%b %d %H:%M %Z')) + ' – ' + e(end.astimezone(LOCAL).strftime('%b %d %H:%M %Z')) + '. Outlook days are valid periods, not local midnight-to-midnight days.</p><ul>')
    intervals = []
    for day, url in GEO_URLS.items():
        label = f'Day {day}'
        try:
            matches = [r for r in rows if isinstance(r, dict) and r.get('day') == day]
            if len(matches) != 1 or matches[0].get('error'):
                raise ValueError('product unavailable')
            row = _parse(matches[0].get('geojson'), day, now)
            first, last = _dt(row['valid_start']), _dt(row['valid_end'])
            label += ' · ' + first.astimezone(LOCAL).strftime('%a %b %d %H:%M %Z') + ' – ' + last.astimezone(LOCAL).strftime('%b %d %H:%M %Z')
            if last <= start or first >= end:
                message = 'not covered by this product: outside requested window'
            else:
                intervals.append((max(first,start),min(last,end)))
                message = 'KCDW: ' + row['status'] + '. Issued ' + row['issued_at']
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            message = 'coverage unknown — unavailable, malformed, stale or future-issued product'
        parts.append('<li><strong>' + e(label) + '</strong>: ' + e(message) + ' <a href="' + e(url) + '">SPC GIS</a></li>')
    cursor = start
    for a,b in sorted(intervals):
        if a > cursor:
            break
        cursor = max(cursor,b)
    parts.append('</ul><p>' + ('Requested window covered by current outlook valid periods; this is not aviation readiness.' if cursor >= end else 'Requested window not covered in full: gaps are unknown, not benign weather.') + '</p>')
    discussion = data.get('discussion')
    try:
        parsed = _discussion(discussion['text'], rows, now)
        if any(parsed[k] != discussion[k] for k in ('issued_at', 'valid_start', 'valid_end')):
            raise ValueError('persisted discussion timestamp mismatch')
        _fresh(discussion['issued_at'], now, timedelta(hours=30))
        first, last = _dt(discussion['valid_start']), _dt(discussion['valid_end'])
        if not _dt(discussion['issued_at']) < first < last or last <= start or first >= end or not timedelta(days=4) <= last-first <= timedelta(days=5):
            raise ValueError('out-of-window discussion')
        parts.append('<details><summary>SPC national Day 4–8 discussion (not a KCDW forecast)</summary><p>Issued ' + e(discussion['issued_at']) + '; valid ' + e(discussion['valid_start']) + ' – ' + e(discussion['valid_end']) + '</p><pre style="white-space:pre-wrap">' + e(discussion['text'][:9000]) + '</pre></details>')
    except (KeyError, TypeError, ValueError, AttributeError):
        parts.append('<details><summary>National discussion unavailable or outside requested window</summary><p>Individual GIS products above remain independent.</p></details>')
    parts.append('<p><a href="https://www.spc.noaa.gov/products/outlook/">SPC Day 1–3 graphics</a> · <a href="' + TEXT_URL + '">SPC Day 4–8 graphics and discussion</a></p></section>')
    return ''.join(parts)
