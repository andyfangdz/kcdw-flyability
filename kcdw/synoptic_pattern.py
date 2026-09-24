"""Surface pattern around a dated event from WPC coded analyses and forecasts.

WPC publishes its hand-analyzed surface map (CODSUS) and 12–48 h forecast maps
(CODSRP, two products per cycle) as coded text: pressure centers to whole
degrees plus fronts. We retain the original product text, decode the strongest
high and deepest low within RADIUS_KM of the airport at each valid time, and
recompute every derived value on render. Centers are WPC's; distances,
bearings and the pressure difference are ours. A bigger difference over a
shorter distance means a tighter gradient and stronger wind in general, not a
KCDW wind forecast.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from html import escape

from .common import UTC, iso_z, parse_time
from .events import TZ, Event

VERSION = 1
ROOT = 'https://api.weather.gov/products'
LISTINGS = {'analysis': ROOT + '/types/COD/locations/SUS', 'forecast': ROOT + '/types/COD/locations/SRP',
            'extended': ROOT + '/types/PMD/locations/EPD'}
MAX_EXTENDED_TEXT = 20_000
MAX_EXTENDED_AGE = timedelta(hours=36)
REGIONAL = re.compile(r'Northeast|Mid-Atlantic|New England|East Coast|the East\b|coastal|Appalachian|Atlantic', re.I)
MAX_TEXT = 12_000
MAX_FORECAST_PRODUCTS = 4
RADIUS_KM = 1500
MAX_ANALYSIS_AGE = timedelta(hours=12)
MAX_FORECAST_AGE = timedelta(hours=30)
KCDW = (40.8752, -74.2814)
KEYWORDS = ('HIGHS', 'LOWS', 'COLD', 'WARM', 'STNRY', 'OCFNT', 'TROF', 'DRYLINE', 'SQLN')
NOTES = [
    'Pressure centers are WPC hand analyses/forecasts, decoded to whole degrees; distance, bearing and pressure difference are derived here.',
    'Only the strongest high and deepest low within 1,500 km of KCDW are listed; other centers and fronts are on WPC maps.',
    'A larger high–low difference over a shorter distance means a tighter pressure gradient, which generally drives stronger wind; it is not a KCDW wind or gust forecast.',
    'WPC 36/48-hour forecasts come from a separate product from the 12/24-hour forecasts; forecast times are fixed synoptic hours, not the flight hour.',
]
ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError)
COMPASS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')


def _require(ok):
    if not ok:
        raise ValueError('invalid synoptic pattern')


def _distance_bearing(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    d = 2 * 6371 * math.asin(math.sqrt(math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def _position(code):
    """WPC whole-degree code: two latitude digits, then west longitude (e.g. 4978 = 49N 78W)."""
    _require(re.fullmatch(r'\d{4,5}', code) is not None)
    lat, lon = int(code[:2]), int(code[2:])
    _require(0 < lat < 90 and 0 < lon < 180)
    return lat, -lon


def _centers(tokens):
    _require(len(tokens) % 2 == 0)
    out = []
    for pressure, code in zip(tokens[::2], tokens[1::2]):
        _require(re.fullmatch(r'\d{3,4}', pressure) is not None and 900 <= int(pressure) <= 1090)
        out.append((int(pressure), *_position(code)))
    return out


def _valid(stamp, issued, analysis):
    """CODSUS gives MMDDHHZ; CODSRP gives DDHHMMZ. Resolve month/year from issuance."""
    _require(re.fullmatch(r'\d{6}Z', stamp) is not None)
    if analysis:
        month, day, hour, minute = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6]), 0
        candidates = [datetime(issued.year + dy, month, day, hour, tzinfo=UTC) for dy in (-1, 0)]
    else:
        day, hour, minute = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
        first = issued.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months = [(first - timedelta(days=1)).replace(day=1), first, (first + timedelta(days=32)).replace(day=1)]
        candidates = []
        for m in months:
            try:
                candidates.append(m.replace(day=day, hour=hour, minute=minute))
            except ValueError:
                pass
    return min(candidates, key=lambda c: abs(c - issued))


def decode(text, issued, analysis):
    """[(valid, forecast_hours|None, highs, lows)] from one coded product."""
    _require(isinstance(text, str) and len(text) <= MAX_TEXT)
    lines = [line.strip() for line in text.replace('\r', '').split('\n')]
    blocks, current = [], None
    for line in lines:
        header = re.fullmatch(r'VALID (\d{6}Z)', line) if analysis else re.fullmatch(r'(\d{2})HR PROG VALID (\d{6}Z)', line)
        if header:
            current = {'valid': _valid(header[1] if analysis else header[2], issued, analysis),
                       'hours': None if analysis else int(header[1]), 'HIGHS': [], 'LOWS': [], 'key': None}
            blocks.append(current)
            continue
        if current is None or not line:
            continue
        words = line.split()
        if words[0] in KEYWORDS:
            current['key'] = words[0]
            words = words[1:]
            if words and words[0] == 'WK':
                words = words[1:]
        if current['key'] in ('HIGHS', 'LOWS'):
            current[current['key']] += words
    _require(blocks)
    return [(b['valid'], b['hours'], _centers(b['HIGHS']), _centers(b['LOWS'])) for b in blocks]


def _nearest(centers, strongest):
    near = []
    for pressure, lat, lon in centers:
        distance, bearing = _distance_bearing(KCDW, (lat, lon))
        if distance <= RADIUS_KM:
            near.append({'hPa': pressure, 'lat': lat, 'lon': lon, 'distance_km': round(distance),
                         'bearing_deg': round(bearing), 'direction': COMPASS[round(bearing / 22.5) % 16]})
    if not near:
        return None
    return (max if strongest else min)(near, key=lambda c: (c['hPa'], -c['distance_km']) if strongest else (c['hPa'], c['distance_km']))


def _row(valid, hours, highs, lows, issued, kind):
    high, low = _nearest(highs, True), _nearest(lows, False)
    row = {'valid': iso_z(valid), 'kind': kind, 'forecast_hours': hours, 'issued_at': iso_z(issued), 'high': high, 'low': low,
           'difference_hPa': None, 'separation_km': None, 'gradient_hPa_per_100km': None}
    if high and low:
        separation, _ = _distance_bearing((high['lat'], high['lon']), (low['lat'], low['lon']))
        row.update(difference_hPa=high['hPa'] - low['hPa'], separation_km=round(separation),
                   gradient_hPa_per_100km=round((high['hPa'] - low['hPa']) / separation * 100, 2) if separation else None)
    return row


def _event_end(snapshot):
    day = Event(**snapshot['event']).day
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ).astimezone(UTC)


def _product(record, analysis, now):
    _require(isinstance(record, dict) and re.fullmatch(r'[0-9a-f-]{36}', record['id']) is not None)
    _require(record['url'] == f'{ROOT}/{record["id"]}' and record['productCode'] == 'COD')
    _require(record['issuingOffice'] == 'KWNH' or record['issuingOffice'] == 'KWBC')
    issued = parse_time(record['issuanceTime'])
    fetched = parse_time(record['fetched_at'])
    _require(issued <= fetched + timedelta(minutes=5) and fetched <= now + timedelta(minutes=5))
    _require(now - issued <= (MAX_ANALYSIS_AGE if analysis else MAX_FORECAST_AGE))
    text = record['productText']
    _require(('CODSUS' if analysis else 'CODSRP') in text[:200])
    return issued, decode(text, issued, analysis)


def validate_pattern(envelope, snapshot, now):
    """Re-decode retained WPC text; newest issuance wins for each forecast valid time."""
    _require(isinstance(envelope, dict) and envelope.get('version') == VERSION)
    _require(envelope.get('snapshot_collected_at') == snapshot['collected_at'])
    _require(envelope.get('event') == {k: snapshot['event'][k] for k in ('slug', 'date')})
    now = now.astimezone(UTC)
    issued, blocks = _product(envelope['analysis'], True, now)
    _require(len(blocks) == 1)
    valid, _, highs, lows = blocks[0]
    rows = [_row(valid, None, highs, lows, issued, 'analysis')]
    forecasts = {}
    products = envelope['forecasts']
    _require(isinstance(products, list) and len(products) <= MAX_FORECAST_PRODUCTS)
    for record in products:
        try:
            when, decoded = _product(record, False, now)
        except ERRORS:
            continue
        for at, hours, h, l in decoded:
            if max(valid, parse_time(snapshot['collected_at'])) < at <= _event_end(snapshot) and (at not in forecasts or forecasts[at][0] < when):
                forecasts[at] = (when, hours, h, l)
    rows += [_row(at, hours, h, l, when, 'forecast') for at, (when, hours, h, l) in sorted(forecasts.items())]
    return {'analysis_url': envelope['analysis']['url'], 'rows': rows}


def collect_pattern(client, snapshot, now):
    """Listing + newest analysis + up to four forecast products: at most seven requests."""
    now = now.astimezone(UTC)
    fetched = iso_z(now)

    def detail(entry):
        _require(re.fullmatch(r'[0-9a-f-]{36}', entry['id']) is not None)
        url = f'{ROOT}/{entry["id"]}'
        raw = client.get(url)
        _require(len(raw['productText']) <= (MAX_EXTENDED_TEXT if raw.get('productCode') == 'PMD' else MAX_TEXT))
        return {'id': entry['id'], 'url': url, 'fetched_at': fetched,
                **{k: raw[k] for k in ('productCode', 'issuingOffice', 'issuanceTime', 'productText')}}

    analysis = detail(client.get(LISTINGS['analysis'])['@graph'][0])
    forecasts = []
    for entry in client.get(LISTINGS['forecast'])['@graph'][:MAX_FORECAST_PRODUCTS]:
        try:
            forecasts.append(detail(entry))
        except Exception:
            continue
    envelope = {'version': VERSION, 'snapshot_collected_at': snapshot['collected_at'],
                'event': {k: snapshot['event'][k] for k in ('slug', 'date')},
                'analysis': analysis, 'forecasts': forecasts}
    try:
        # Optional days 3-7 discussion; its absence never invalidates the surface analysis.
        envelope['extended'] = detail(client.get(LISTINGS['extended'])['@graph'][0])
        extended_discussion(envelope, now)
    except Exception:
        envelope.pop('extended', None)
    validate_pattern(envelope, snapshot, now)
    return envelope


def extended_discussion(envelope, now):
    """Verbatim WPC days 3-7 overview plus regional paragraphs; raise ValueError if invalid."""
    record = envelope['extended']
    _require(isinstance(record, dict) and re.fullmatch(r'[0-9a-f-]{36}', record['id']) is not None)
    _require(record['url'] == f'{ROOT}/{record["id"]}' and record['productCode'] == 'PMD')
    _require(record['issuingOffice'] in ('KWNH', 'KWBC'))
    issued, fetched = parse_time(record['issuanceTime']), parse_time(record['fetched_at'])
    _require(issued <= fetched + timedelta(minutes=5) and fetched <= now.astimezone(UTC) + timedelta(minutes=5))
    _require(now.astimezone(UTC) - issued <= MAX_EXTENDED_AGE)
    text = record['productText']
    _require(isinstance(text, str) and len(text) <= MAX_EXTENDED_TEXT and 'PMDEPD' in text[:200])
    valid = re.search(r'Valid (\d{2}Z \w{3} \w{3} \d{1,2} \d{4}) - (\d{2}Z \w{3} \w{3} \d{1,2} \d{4})', text)
    sections = re.split(r'\n\.\.\.([A-Za-z/ ]+)\.\.\.\n', text)
    named = {sections[i].strip(): sections[i + 1] for i in range(1, len(sections) - 1, 2)}
    paragraphs = lambda body: [' '.join(p.split()) for p in re.split(r'\n\s*\n', body or '') if p.strip()]
    overview = paragraphs(named.get('Overview'))[:2]
    regional = [(name, p) for name in ('Guidance/Predictability Assessment', 'Weather/Hazards Highlights')
                for p in paragraphs(named.get(name)) if REGIONAL.search(p)][:4]
    _require(overview or regional)
    return {'issued_at': iso_z(issued), 'url': record['url'], 'valid': valid.group(0) if valid else None,
            'overview': overview, 'regional': [{'section': n, 'text': p} for n, p in regional]}


def _key_messages(snapshot, now):
    """OKX 'What has changed' and 'Key messages', verbatim from the retained AFD."""
    try:
        from .event_afd import _sections, validate_afds
        envelope = validate_afds(snapshot.get('event_afds'), snapshot, now)
        source = envelope['offices']['OKX']
        sections = {code: body for code, _, body in _sections(source['productText'])}
        found = {k: ' '.join(sections[k].split()) for k in ('WHAT HAS CHANGED', 'KEY MESSAGES') if sections.get(k)}
        if not found:
            return None
        return {'issued_at': source['issued_at'], 'url': source['product_url'], **found}
    except ERRORS:
        return None


def pattern_evidence(snapshot, now):
    try:
        pattern = validate_pattern(snapshot.get('synoptic_pattern'), snapshot, now)
    except ERRORS:
        return None
    return {'rows': pattern['rows'], 'notes': list(NOTES)}


def _center(c):
    return f'{c["hPa"]} hPa · {c["lat"]}°N {-c["lon"]}°W · {c["distance_km"] * 0.621371:,.0f} mi {c["direction"]}' if c else 'None within 1,500 km'


def _when(row):
    local = parse_time(row['valid']).astimezone(TZ)
    return local.strftime('%a %-I %p %Z').replace(' 0', ' ')


def _story(rows, flight_start):
    now = rows[0]
    parts = []
    if now['high'] and now['low']:
        parts.append(f'Now ({_when(now)} analysis): high pressure of {now["high"]["hPa"]} hPa sits {now["high"]["distance_km"] * 0.621371:,.0f} mi '
                     f'{now["high"]["direction"]} of KCDW, and a {now["low"]["hPa"]} hPa low sits {now["low"]["distance_km"] * 0.621371:,.0f} mi '
                     f'{now["low"]["direction"]}. KCDW sits between them; air is pushed from the high toward the low, and the {now["difference_hPa"]} hPa difference '
                     f'across about {now["separation_km"] * 0.621371:,.0f} mi sets how hard the wind blows.')
    target = min((r for r in rows[1:] if r['high'] and r['low']), key=lambda r: abs(parse_time(r['valid']) - flight_start), default=None)
    if target and now['difference_hPa'] is not None:
        change = target['difference_hPa'] - now['difference_hPa']
        trend = ('tightens' if change >= 2 else 'eases' if change <= -2 else 'holds about steady')
        move = _distance_bearing((now['low']['lat'], now['low']['lon']), (target['low']['lat'], target['low']['lon']))
        drift = ('stays about in place' if move[0] < 150 else f'moves about {move[0] * 0.621371:,.0f} mi {COMPASS[round(move[1] / 22.5) % 16]}')
        parts.append(f'By {_when(target)} (WPC {target["forecast_hours"]}-h forecast), the low is {target["low"]["hPa"]} hPa and {drift}; '
                     f'the high is {target["high"]["hPa"]} hPa. The difference is {target["difference_hPa"]} hPa, so the gradient {trend} '
                     f'({now["gradient_hPa_per_100km"]:.2f} → {target["gradient_hPa_per_100km"]:.2f} hPa per 100 km).')
    return parts


def render_pattern(snapshot, now):
    try:
        pattern = validate_pattern(snapshot.get('synoptic_pattern'), snapshot, now)
    except ERRORS:
        return ''
    from .event_model_matrix import flight_window
    flight_start, _, _ = flight_window(snapshot)
    rows = pattern['rows']
    story = ''.join(f'<p>{escape(p)}</p>' for p in _story(rows, flight_start))
    body = ''.join(
        f'<tr{" data-flight" if r["kind"] == "forecast" and abs(parse_time(r["valid"]) - flight_start) <= timedelta(hours=6) else ""}>'
        f'<th scope="row">{escape(_when(r))}<small>{"WPC analysis" if r["kind"] == "analysis" else "WPC " + str(r["forecast_hours"]) + "-h forecast"}'
        f'{" · nearest the flight" if r["kind"] == "forecast" and abs(parse_time(r["valid"]) - flight_start) <= timedelta(hours=6) else ""}</small></th>'
        f'<td>{escape(_center(r["high"]))}</td><td>{escape(_center(r["low"]))}</td>'
        f'<td>{"—" if r["difference_hPa"] is None else str(r["difference_hPa"]) + " hPa"}'
        f'<small>{"" if r["gradient_hPa_per_100km"] is None else format(r["gradient_hPa_per_100km"], ".2f") + " hPa / 100 km"}</small></td></tr>'
        for r in rows)
    messages = _key_messages(snapshot, now)
    official = ''
    if messages:
        official = (f'<h3>What NWS New York expects</h3><p class="small">OKX discussion issued {escape(parse_time(messages["issued_at"]).astimezone(TZ).strftime("%a %-d %b %H:%M %Z"))}, quoted verbatim.</p>'
                    + ''.join(f'<blockquote><p><strong>{escape(k.capitalize())}:</strong> {escape(v)}</p></blockquote>'
                              for k, v in messages.items() if k in ('WHAT HAS CHANGED', 'KEY MESSAGES')))
    return (f'<section id="weather-pattern" class="weather-pattern" aria-labelledby="weather-pattern-title">'
            f'<p class="eyebrow">Weather pattern · now and through the checkride</p><h2 id="weather-pattern-title">Surface pattern</h2>{story}'
            f'<div class="table-wrap"><table><caption>Strongest high and deepest low within 1,500 km (930 mi) of KCDW. Distance and direction are from KCDW.</caption>'
            f'<thead><tr><th scope="col">Time · Eastern</th><th scope="col">High</th><th scope="col">Low</th><th scope="col">Difference · gradient</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>{official}'
            f'<details><summary>Pattern sources &amp; limits</summary><ul>{"".join("<li>" + escape(n) + "</li>" for n in NOTES)}</ul>'
            f'<p><a href="https://www.wpc.ncep.noaa.gov/html/sfc-zoom.php">WPC surface analysis</a> · '
            f'<a href="https://www.wpc.ncep.noaa.gov/basicwx/basicwx_ndfd.php">WPC forecast maps</a> · '
            f'<a href="{escape(pattern["analysis_url"], quote=True)}">Coded analysis text</a> · <a href="#coastal-low">Model pressure maps</a></p></details></section>')
