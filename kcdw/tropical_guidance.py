"""Public WNV3 cyclone-track screening, independent of operational quorum.

Exports collect_wn3_cyclones(client, now), validate_wn3_cyclones(source, now),
render_wn3_cyclones(source, start, end, now), and parse_wn3_cyclones(text, url).
All timestamps in the JSON-safe envelope are UTC ISO8601. No credentials,
private feed, disk cache, raw CSV redistribution, or implicit network access.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from functools import lru_cache
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .events import local_clock
from html import escape

MODEL_ID = "WNV3"
MODEL_NAME = "WeatherNext 3 Cyclones (r0)"
MODEL_DOC = "https://developers.google.com/weathernext/guides/models"
TERMS_URL = "https://storage.googleapis.com/weathernext-public/terms-of-use.pdf"
BASE_URL = "https://deepmind.google.com/science/weatherlab/download/cyclones/WNV3/ensemble/cyclogenesis/csv/"
# First-party WN3 model documentation specifies 64; NOT counted from tracks.
ENSEMBLE_SIZE = 64
MAX_BYTES = 16_000_000
MAX_ROWS = 60_000
MAX_CANDIDATES = 8
MAX_AGE = timedelta(hours=36)
KCDW = (40.8752, -74.2814)
# Display/count focus, not an impact mask or the full NHC basin graphic.
MAP_WEST, MAP_EAST, MAP_SOUTH, MAP_NORTH = -100, -45, 5, 60
URL_RE = re.compile(re.escape(BASE_URL) + r"WNV3_(\d{4}_\d{2}_\d{2}T\d{2}_00)_cyclogenesis\.csv\Z")
FIELDS = {"init_time", "track_id", "sample", "valid_time", "lead_time_hours", "lat", "lon", "minimum_sea_level_pressure_hpa", "maximum_sustained_wind_speed_knots"}


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("invalid timestamp")
    # Public CSV's naive times are explicitly UTC; callers use UTC if naive.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat().replace("+00:00", "Z")


def _run(url):
    match = URL_RE.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise ValueError("not the public WNV3 ensemble cyclogenesis CSV identity")
    run = datetime.strptime(match[1], "%Y_%m_%dT%H_%M").replace(tzinfo=timezone.utc)
    if run.hour % 6:
        raise ValueError("not a six-hour WNV3 cycle")
    return run


def _number(value, low, high):
    if isinstance(value, bool):
        raise ValueError("boolean is not numerical forecast data")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError("nonfinite or out-of-bounds numerical forecast data")
    return result


def _integer(value, low, high):
    result = _number(value, low, high)
    if not result.is_integer():
        raise ValueError("noninteger member/track identity")
    return int(result)


def _track(value):
    # Paired observed storms/invests coexist with numerical genesis identities.
    # Preserve the source token, including leading zeroes and invest suffixes.
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9]{1,8}|[A-Z]{2}[0-9]{6}(?:\([0-9]{1,3}\))?)", value):
        raise ValueError("invalid track identity")
    return value


def _atlantic(lat, lon):
    # Explicit broad geographic sector, not an official storm-basin assignment.
    return 0 <= lat <= 70 and -100 <= lon <= 20


def _atlantic_track(point):
    """Atlantic-only display screen; numerical genesis IDs have no basin tag.

    Exclude explicitly non-AL named tracks. For unassigned numerical tracks,
    use a geographic screen east of an approximate Central American divide.
    This is not an official basin classifier or a land/ocean impact mask.
    """
    lat, lon, track = point['lat'], point['lon'], point['track_id']
    if not _atlantic(lat, lon) or (not track.isdigit() and not track.startswith('AL')):
        return False
    # Western boundary (lat, lon), keeping the Gulf and Caribbean and
    # excluding the eastern Pacific corner of the old rectangular sector.
    boundary = [(0, -77), (7, -77), (8, -77), (9, -79.5),
                (9.5, -82.5), (11, -84.5), (15, -87), (17, -93),
                (18, -95), (20, -97.5), (22, -99), (25, -100), (70, -100)]
    for (a, x), (b, y) in zip(boundary, boundary[1:]):
        if a <= lat <= b:
            return lon >= x + (y-x) * (lat-a) / (b-a)
    return False


def _north_america_point(point):
    """Keep Gulf/Caribbean/western Atlantic centers, including eastern Canada."""
    return (_atlantic_track(point)
            and MAP_WEST <= point['lon'] <= MAP_EAST
            and MAP_SOUTH <= point['lat'] <= MAP_NORTH)


@lru_cache(maxsize=1)
def _land_polygons():
    return json.loads((Path(__file__).parent / 'assets/atlantic-land.json').read_text())['polygons']


def parse_wn3_cyclones(text: str, url: str) -> dict:
    """Strict CSV -> compact data; reject any malformed row, dedupe exact rows.

    data: schema_version, model_id/name, product, source_url, ensemble_size,
    ensemble_size_source, initialization_time, valid_start/end (observed GLOBAL
    track-point extent, not a promise of continuous coverage), global_point_count,
    global_valid_times, points (Atlantic-sector only), selection_method.
    point: sample, track_id, valid_time, lat/lon, pressure_hpa, wind_kt.
    Header-only files cannot confirm initialization and are unavailable; a valid
    file with non-Atlantic tracks is an explicit no-Atlantic-track result.
    """
    run = _run(url)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("CSV exceeds size bound")
    lines = (line for line in io.StringIO(text.lstrip("\ufeff")) if not line.lstrip().startswith("#") and line.strip())
    reader = csv.DictReader(lines, strict=True)
    if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("missing/duplicate WNV3 CSV columns")
    seen, points, times = {}, [], set()
    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            raise ValueError("CSV exceeds row bound")
        if None in row or any(row.get(key) is None for key in reader.fieldnames):
            raise ValueError("malformed CSV row")
        if _time(row["init_time"]) != run:
            raise ValueError("CSV initialization does not match requested WNV3 run")
        valid = _time(row["valid_time"])
        lead = _number(row["lead_time_hours"], 0, 360)
        if valid != run + timedelta(hours=lead) or lead % 6:
            raise ValueError("invalid lead/valid time or non-six-hour track cadence")
        sample = _integer(row["sample"], 0, ENSEMBLE_SIZE - 1)
        track = _track(row["track_id"])
        lat, lon = _number(row["lat"], -90, 90), _number(row["lon"], -180, 180)
        pressure = _number(row["minimum_sea_level_pressure_hpa"], 800, 1100)
        wind = _number(row["maximum_sustained_wind_speed_knots"], 0, 250)
        # Blank optional radii occur in real files. Validate supplied values but
        # omit radii: track-center distance must never imply airport wind radii.
        for key, value in row.items():
            if key.startswith("radius_") and value.strip():
                _number(value, 0, 5000)
        point = dict(sample=sample, track_id=track, valid_time=_iso(valid), lat=lat, lon=lon, pressure_hpa=pressure, wind_kt=wind)
        identity = (sample, track, point["valid_time"])
        if identity in seen:
            if seen[identity] != point:
                raise ValueError("conflicting duplicate track point")
            continue
        seen[identity] = point
        times.add(point["valid_time"])
        if _atlantic_track(point):
            points.append(point)
    if not seen:
        raise ValueError("empty track CSV: cannot verify response initialization or coverage")
    points.sort(key=lambda p: (p["sample"], p["track_id"], p["valid_time"]))
    return dict(schema_version=1, model_id=MODEL_ID, model_name=MODEL_NAME,
                product="ensemble/cyclogenesis", source_url=url, ensemble_size=ENSEMBLE_SIZE,
                ensemble_size_source=MODEL_DOC, initialization_time=_iso(run),
                valid_start=min(times), valid_end=max(times), global_valid_times=sorted(times),
                global_point_count=len(seen), points=points,
                selection_method="Newest validated response among at most 8 six-hour UTC cycle candidates; not a public run listing")


def validate_wn3_cyclones(source, now) -> dict:
    """Return {ok, status, error}; recheck stored content and current age."""
    try:
        now = _time(now)
        if not isinstance(source, dict) or source.get("ok") is not True:
            raise ValueError(str(source.get("error") or "source missing")[:300] if isinstance(source, dict) else "source missing")
        data = source["data"]
        if data["schema_version"] != 1 or data["model_id"] != MODEL_ID or data["model_name"] != MODEL_NAME or data["product"] != "ensemble/cyclogenesis" or data["ensemble_size"] != ENSEMBLE_SIZE or data["ensemble_size_source"] != MODEL_DOC:
            raise ValueError("wrong cyclone model/schema/member denominator")
        run = _run(data["source_url"])
        fetched = _time(source["fetched_at"])
        if _time(data["initialization_time"]) != run or run > now or fetched > now + timedelta(minutes=5) or fetched < run:
            raise ValueError("invalid run/fetch timestamp")
        if now - run > MAX_AGE or now - fetched > MAX_AGE:
            return dict(ok=False, status="stale", error="Stale: run or retrieval older than 36 hours")
        times = data["global_valid_times"]
        if not isinstance(times, list) or not 1 <= len(times) <= 61 or times != sorted(set(times)):
            raise ValueError("invalid global track time axis")
        for value in times:
            delta = (_time(value) - run).total_seconds()
            if delta < 0 or delta > 360 * 3600 or delta % (6 * 3600):
                raise ValueError("invalid track validity")
        if data["valid_start"] != min(times) or data["valid_end"] != max(times):
            raise ValueError("track validity extent mismatch")
        if not isinstance(data["selection_method"], str) or len(data["selection_method"]) > 400:
            raise ValueError("invalid selection provenance")
        count = _integer(data["global_point_count"], 1, MAX_ROWS)
        points = data["points"]
        if not isinstance(points, list) or len(points) > count:
            raise ValueError("invalid track point count")
        seen = set()
        for p in points:
            if any(type(p[key]) not in (int, float) for key in ("sample", "lat", "lon", "pressure_hpa", "wind_kt")):
                raise ValueError("invalid cached numeric type")
            sample = _integer(p["sample"], 0, 63)
            track = _track(p["track_id"])
            if p["valid_time"] not in times or not _atlantic(_number(p["lat"], -90, 90), _number(p["lon"], -180, 180)):
                raise ValueError("invalid Atlantic track point")
            _number(p["pressure_hpa"], 800, 1100)
            _number(p["wind_kt"], 0, 250)
            identity = (sample, track, p["valid_time"])
            if identity in seen:
                raise ValueError("duplicate stored point")
            seen.add(identity)
        return dict(ok=True, status="fresh", error=None)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
        return dict(ok=False, status="unavailable", error=str(exc)[:300])


def collect_wn3_cyclones(client, now) -> dict:
    """Bounded latest-cycle search; all I/O uses supplied client's get_text."""
    now = _time(now)
    cycle = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    errors = []
    for offset in range(MAX_CANDIDATES):
        run = cycle - timedelta(hours=6 * offset)
        url = BASE_URL + f"WNV3_{run:%Y_%m_%dT%H}_00_cyclogenesis.csv"
        try:
            data = parse_wn3_cyclones(client.get_text(url, maximum=MAX_BYTES), url)
            envelope = dict(ok=True, fetched_at=_iso(now), data=data, error=None)
            check = validate_wn3_cyclones(envelope, now)
            if not check["ok"]:
                raise ValueError(check["error"])
            return envelope
        except Exception as exc:
            # Exception type only: upstream exception text may echo HTML/body.
            errors.append(f"{run:%m-%d %HZ}: {type(exc).__name__}")
    return dict(ok=False, fetched_at=_iso(now), data=None,
                error="No fresh verified WNV3 CSV in bounded cycle search (" + "; ".join(errors) + ")")


def _distance(point):
    lat1, lon1, lat2, lon2 = map(math.radians, (*KCDW, point["lat"], point["lon"]))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


def _map(points):
    """North America/western Atlantic focus; preserve real geography and gaps."""
    def xy(lat, lon):
        return ((lon - MAP_WEST) * 600 / (MAP_EAST - MAP_WEST) + 30,
                (MAP_NORTH - lat) * 600 / (MAP_NORTH - MAP_SOUTH) + 20)
    chunks = ['<svg viewBox="0 0 660 650" role="img" aria-label="North America and western Atlantic mission-window cyclone tracks and KCDW" style="width:100%;max-width:760px;background:#102539;border-radius:8px">', '<title>North America / western Atlantic mission-window track centers over Natural Earth land outlines; not a navigation map</title>']
    chunks.append('<defs><clipPath id="wn3-atlantic-clip"><rect x="30" y="20" width="600" height="600"/></clipPath></defs>')
    chunks.append('<g data-layer="land" clip-path="url(#wn3-atlantic-clip)" fill="#304858" stroke="#afc3c9" stroke-width="0.8" fill-rule="evenodd">')
    for polygon in _land_polygons():
        parts = []
        for ring in polygon:
            coords = [xy(lat, lon) for lon, lat in ring]
            parts.append('M' + 'L'.join(f'{x:.1f},{y:.1f}' for x, y in coords) + 'Z')
        chunks.append('<path d="' + ''.join(parts) + '"/>')
    chunks.append('</g>')
    for lon in (-100, -90, -80, -70, -60, -50, -45):
        x, _ = xy(0, lon)
        chunks.append(f'<path d="M{x} 20V620" stroke="#496077" stroke-width="0.5"/><text x="{x}" y="639" fill="#ccd9e5" font-size="10" text-anchor="middle">{abs(lon)}°{"W" if lon < 0 else "E"}</text>')
    for lat in (5, 10, 20, 30, 40, 50, 60):
        _, y = xy(lat, 0)
        chunks.append(f'<path d="M30 {y}H630" stroke="#496077" stroke-width="0.5"/><text x="2" y="{y}" fill="#ccd9e5" font-size="10">{lat}N</text>')
    chunks.append('<g data-layer="tracks" clip-path="url(#wn3-atlantic-clip)">')
    groups = defaultdict(list)
    for point in points:
        if not _north_america_point(point):
            continue
        groups[(point["sample"], point["track_id"])].append(point)
    # Bound SVG independently; nearest tracks first, counts always use all data.
    ordered = sorted(groups.values(), key=lambda group: min(_distance(p) for p in group))
    for group in ordered[:160]:
        previous = None
        for p in sorted(group, key=lambda p: p["valid_time"]):
            x, y = xy(p["lat"], p["lon"])
            if previous is not None and _time(p["valid_time"]) - _time(previous["valid_time"]) == timedelta(hours=6):
                px, py = xy(previous["lat"], previous["lon"])
                chunks.append(f'<path d="M{px:.1f} {py:.1f}L{x:.1f} {y:.1f}" fill="none" stroke="#68bddc" stroke-opacity="0.4" stroke-width="1"/>')
            else:
                chunks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.3" fill="#68bddc"/>')
            previous = p
    chunks.append('</g>')
    for lat, lon, label in [(40.8752, -74.2814, "KCDW / NYC"), (25.8, -80.2, "Florida"), (18.2, -66.5, "Puerto Rico"), (32.3, -64.8, "Bermuda"), (48, -56, "Newfoundland")]:
        x, y = xy(lat, lon)
        chunks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="#ffd07a"/><text x="{x+5:.1f}" y="{y-5:.1f}" fill="#fff0d5" font-size="11">{label}</text>')
    chunks.append('</svg><p style="font-size:0.8em">North America / western Atlantic mission-window tracks only (including Gulf, Caribbean and eastern Canada). Land outlines: Natural Earth 1:110m, public domain; minor islands may be omitted. Plate-carree geographic view, not a navigation map. Map shows up to 160 nearest tracks; counts use all retained points inside the same regional view.</p>')
    return ''.join(chunks)


def render_wn3_cyclones(source, start, end, now) -> str:
    """Escaped, self-contained, non-operational HTML; never trust cached freshness."""
    esc = lambda value: escape(str(value), quote=True)
    chunks = ['<section style="border:1px solid #526579;border-radius:10px;padding:14px;margin:12px 0"><h3 style="margin:0 0 8px">WN3 tropical guidance</h3>']
    check = validate_wn3_cyclones(source, now)
    caveat = '<p style="font-size:0.85em">Experimental model screening, not landfall probability, airport wind/rain probability, or checkride completion probability. Track centers do not describe storm size, remote rain, or remnants. Use NHC/NWS official forecasts and warnings for decisions.</p>'
    if not check['ok']:
        chunks.append(f'<p><strong>{"Stale" if check["status"] == "stale" else "Unavailable"}</strong>: {esc(check["error"])}. No favorable inference.</p>')
        chunks.append(f'<p>{esc(MODEL_NAME)} · WNV3 ensemble cyclogenesis. Source/run/validity/mission-overlap not usable.</p>')
        stored = source.get('data') if isinstance(source, dict) else None
        if isinstance(stored, dict):
            chunks.append('<p style="font-size:0.85em">Unusable stored metadata: ' + '; '.join(esc(key) + ': ' + esc(str(stored.get(key, 'unknown'))[:200]) for key in ('initialization_time', 'valid_start', 'valid_end')) + '</p>')
        return ''.join(chunks) + caveat + '</section>'
    data = source['data']
    chunks.append(f'<p style="font-size:0.85em"><a href="{esc(data["source_url"])}">Google DeepMind · {MODEL_NAME}</a> · WNV3 ensemble/cyclogenesis, distinct from generic WeatherNext Cyclones and the WN3 point forecast.<br>Run {esc(data["initialization_time"])} · fetched {esc(local_clock(source["fetched_at"], True))} · track points valid {esc(local_clock(data["valid_start"], True))} → {esc(local_clock(data["valid_end"], True))}.</p>')
    try:
        start, end = _time(start), _time(end)
        if end <= start:
            raise ValueError("mission end must follow start")
    except (ValueError, TypeError, OverflowError) as exc:
        return ''.join(chunks) + f'<p>Unavailable mission overlap: {esc(exc)}</p>' + caveat + '</section>'
    lo, hi = _time(data['valid_start']), _time(data['valid_end'])
    chunks.append(f'<p>Mission window: {esc(_iso(start))} → {esc(_iso(end))}.</p>')
    global_times = [t for t in data['global_valid_times'] if start <= _time(t) <= end]
    points = [p for p in data['points'] if start <= _time(p['valid_time']) <= end and _north_america_point(p)]
    if end < lo or start > hi:
        chunks.append('<p><strong>Out-of-range</strong>: no mission overlap with supplied track-point validity; no proximity assessment.</p>')
    elif not global_times:
        chunks.append('<p><strong>No sampled track points</strong> at mission times (six-hour sampling); no proximity assessment or interpolation.</p>')
    else:
        coverage = 'Partial' if start < lo or end > hi else 'Within'
        chunks.append(f'<p><strong>{coverage} mission overlap</strong> with sampled validity; only valid points inside the requested window contribute.</p>')
        if not points:
            chunks.append('<p>No North America / western Atlantic track points in this mission window. This is not evidence of no tropical impacts.</p>')
        else:
            near500 = {p['sample'] for p in points if _distance(p) <= 500}
            near1000 = {p['sample'] for p in points if _distance(p) <= 1000}
            nearest = min(points, key=_distance)
            members = len({p['sample'] for p in points})
            tracks = len({(p['sample'], p['track_id']) for p in points})
            chunks.append(f'<p><strong>NYC-region screening:</strong> {len(near500)} / 64 unique members have a sampled center within 500 km of KCDW; {len(near1000)} / 64 within 1,000 km. North America / western Atlantic window: {tracks} member-tracks in {members} unique members.<br>Closest sampled center: {_distance(nearest):.0f} km at {esc(nearest["valid_time"])}; storm-center sustained wind {nearest["wind_kt"]:.0f} kt, pressure {nearest["pressure_hpa"]:.0f} hPa (not airport conditions).</p>')
            chunks.append(_map(points))
    chunks.append(f'<details><summary>Source, sampling and limitations</summary><p>Display/count region: 5–60°N, 100–45°W, narrower than the <a href="https://www.nhc.noaa.gov/gtwo.php?basin=atl&amp;fdays=7">NHC full-Atlantic overview</a>. This is a geographic focus, not a North American impact prediction; remote systems and storm-size effects can matter outside the view. Broad source Atlantic screen: 0–70°N, 100°W–20°E with the eastern Pacific excluded by an approximate Central American divide; explicitly non-Atlantic named tracks are excluded. Numerical genesis tracks have no basin tag, so geographic screening is not an official basin assignment. Member identity is sample, track identity is (sample, track_id); multiple storms in one member count once per distance band. <a href="{MODEL_DOC}">Documented WN3 ensemble size: 64</a>, not inferred from tracks. Six-hour points only; no between-point closest-approach interpolation. {esc(data["selection_method"])}. Freshness limit: 36 hours. <a href="{TERMS_URL}">Google real-time data terms</a>; derived screening, not raw CSV redistribution.</p></details>')
    return ''.join(chunks) + caveat + '</section>'
