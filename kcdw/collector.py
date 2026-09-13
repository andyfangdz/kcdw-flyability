from __future__ import annotations

import argparse
import json
import math
import re
from io import BytesIO
from PIL import Image
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import UTC, atomic_write, iso_z, parse_time
from .ensemble_guidance import WEATHER_NEXT_ENDPOINT, collect_aifs_ens, collect_weather_next
from .geometry import geometry_contains
from .nbm_guidance import collect_nbm
from .weathernext3 import collect_weather_next3
from .intervals import expand_grid_values

LAT, LON = 40.8752, -74.2814
TZ = ZoneInfo("America/New_York")
USER_AGENT = "kcdw-flyability/1.0 (weather report; contact: local-operator)"
GRID_FIELDS = ("ceilingHeight", "visibility", "probabilityOfThunder", "probabilityOfPrecipitation", "windSpeed", "windGust", "windDirection", "skyCover", "weather")
RADAR_SERVICE = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/radar_base_reflectivity_time/ImageServer"
RADAR_BBOX = (-82.0, 38.0, -73.0, 43.0)
RADAR_SIZE = (900, 500)
RADAR_FRAMES = 6
RADAR_MAX_AGE = timedelta(minutes=30)
AFD_OFFICES = {
    "OKX": "New York/Upton", "PHI": "Philadelphia/Mount Holly",
    "BGM": "Binghamton", "ALY": "Albany", "BOX": "Boston/Norton",
    "CTP": "State College",
}


class Client:
    def __init__(self, timeout: float = 15, retries: int = 2):
        self.timeout, self.retries = timeout, retries

    def get(self, url: str):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json, text/plain"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(4_000_001)
                    if len(raw) > 4_000_000:
                        raise ValueError("response exceeds 4 MB")
                    if getattr(response, "status", None) == 204 or not raw.strip():
                        return []
                    return json.loads(raw)
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last))

    def get_text(self, url: str, maximum: int = 1_000_000) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(maximum + 1)
                    if len(raw) > maximum:
                        raise ValueError(f"text response exceeds {maximum} bytes")
                    return raw.decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last))

    def get_bytes(self, url: str, maximum: int) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/png"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(maximum + 1)
                    if len(raw) > maximum:
                        raise ValueError(f"binary response exceeds {maximum} bytes")
                    if response.headers.get_content_type() != "image/png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("radar response is not a PNG image")
                    return raw
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last))


def _periods(data: dict, limit: int) -> list[dict]:
    keep = ("number", "name", "startTime", "endTime", "isDaytime", "temperature", "temperatureUnit", "probabilityOfPrecipitation", "windSpeed", "windDirection", "shortForecast", "detailedForecast")
    return [{k: p[k] for k in keep if k in p} for p in data.get("properties", {}).get("periods", [])[:limit]]



def _source(fetch, now: datetime) -> dict:
    try:
        value = fetch()
        return {"ok": True, "fetched_at": iso_z(now), "data": value, "error": None}
    except Exception as exc:
        return {"ok": False, "fetched_at": iso_z(now), "data": None, "error": f"{type(exc).__name__}: {exc}"[:300]}


def collect_afd(client: Client, now: datetime, office: str) -> dict:
    listing = client.get(f"https://api.weather.gov/products/types/AFD/locations/{office}")
    candidates = []
    for product in listing.get("@graph", []):
        try:
            issued = parse_time(product["issuanceTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if issued <= now:
            candidates.append((issued, product))
    if not candidates:
        raise ValueError(f"no {office} AFD issued by collection time")
    issued, product = max(candidates, key=lambda item: item[0])
    url = f"https://api.weather.gov/products/{product['id']}"
    detail = client.get(url)
    text = detail.get("productText", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"empty {office} AFD")
    if not re.search(rf"(?m)^AFD{office}\s*$", text):
        raise ValueError(f"AFD office mismatch for {office}")
    return {"id": product["id"], "office": office, "office_name": AFD_OFFICES[office],
            "issuanceTime": iso_z(issued), "source_url": url,
            "age_seconds": int((now - issued).total_seconds()),
            "excerpt": text[:14000], "truncated": len(text) > 14000}


def collect_afds(client: Client, now: datetime) -> dict:
    return {f"{office.lower()}_afd": _source(lambda: collect_afd(client, now, office), now)
            for office in AFD_OFFICES}


def report_dates(now: datetime) -> list[str]:
    local_date = now.astimezone(TZ).date()
    return [(local_date + timedelta(days=i)).isoformat() for i in range(7)]


def _decode_rgba_png(image: bytes) -> tuple[int, int, list[bytes]]:
    """Decode only the bounded RGBA PNG format emitted by NOAA."""
    if not 0 < len(image) <= 1_000_000:
        raise ValueError("radar PNG exceeds size limit")
    with Image.open(BytesIO(image), formats=["PNG"]) as picture:
        if picture.size != RADAR_SIZE or picture.mode != "RGBA":
            raise ValueError("unexpected radar PNG format or dimensions")
        picture.verify()
    with Image.open(BytesIO(image), formats=["PNG"]) as picture:
        picture.load()
        raw = picture.tobytes()
    width, height = RADAR_SIZE
    stride = width * 4
    return width, height, [raw[y * stride:(y + 1) * stride] for y in range(height)]


def _radar_spatial_summary(image: bytes) -> dict:
    width, height, rows = _decode_rgba_png(image)
    west, south, east, north = RADAR_BBOX
    longitude_nm = 60 * math.cos(math.radians(LAT))
    nearest = None
    echo_pixels = 0
    within_25 = False
    within_50 = False
    for y, row in enumerate(rows):
        latitude = north - (y + 0.5) / height * (north - south)
        north_nm = (latitude - LAT) * 60
        for x in range(width):
            if row[x * 4 + 3] == 0:
                continue
            echo_pixels += 1
            longitude = west + (x + 0.5) / width * (east - west)
            east_nm = (longitude - LON) * longitude_nm
            distance = math.hypot(east_nm, north_nm)
            if distance <= 25:
                within_25 = True
            if distance <= 50:
                within_50 = True
            if nearest is None or distance < nearest[0]:
                nearest = (distance, east_nm, north_nm, longitude, latitude)

    result = {
        "displayed_echo_pixel_fraction_percent": round(echo_pixels / (width * height) * 100, 3),
        "local_echo_within_25_nm": within_25,
        "local_echo_within_50_nm": within_50,
        "nearest_displayed_echo_nm": None,
        "nearest_displayed_echo_direction": None,
        "nearest_displayed_echo_position": None,
    }
    if nearest is not None:
        distance, east_nm, north_nm, longitude, latitude = nearest
        bearing = (math.degrees(math.atan2(east_nm, north_nm)) + 360) % 360
        directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        result.update({
            "nearest_displayed_echo_nm": round(distance, 1),
            "nearest_displayed_echo_direction": directions[round(bearing / 45) % 8],
            "nearest_displayed_echo_position": {"longitude": round(longitude, 3), "latitude": round(latitude, 3)},
        })
    return result


def collect_radar_loop(client: Client, now: datetime, radar_dir: Path) -> dict:
    """Download a bounded, time-ordered regional MRMS reflectivity loop."""
    now = now.astimezone(UTC)
    query = urllib.parse.urlencode({
        "where": "name LIKE 'CONUS_L2_BREF_QCD_%'",
        "outFields": "objectid,name,idp_validtime,idp_ingestdate",
        "orderByFields": "idp_validtime DESC",
        "resultRecordCount": str(RADAR_FRAMES * 2),
        "returnGeometry": "false",
        "f": "json",
    })
    listing = client.get(f"{RADAR_SERVICE}/query?{query}")
    if not isinstance(listing, dict) or not isinstance(listing.get("features"), list):
        raise RuntimeError("NOAA radar service returned an invalid frame listing")
    candidates = []
    seen_times = set()
    for feature in listing["features"]:
        if not isinstance(feature, dict):
            continue
        attributes = feature.get("attributes", {})
        if not isinstance(attributes, dict):
            continue
        try:
            object_id = int(attributes["objectid"])
            valid = datetime.fromtimestamp(int(attributes["idp_validtime"]) / 1000, UTC)
            ingest = datetime.fromtimestamp(int(attributes["idp_ingestdate"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if valid > now + timedelta(minutes=5) or valid in seen_times:
            continue
        seen_times.add(valid)
        candidates.append((valid, ingest, object_id, str(attributes.get("name", ""))[:200]))
    candidates = sorted(candidates)[-RADAR_FRAMES:]
    if not candidates:
        raise RuntimeError("NOAA radar service returned no usable CONUS frames")
    latest_age = now - candidates[-1][0]
    if latest_age > RADAR_MAX_AGE:
        raise RuntimeError(f"latest NOAA radar frame is stale by {int(latest_age.total_seconds())} seconds")

    radar_dir.mkdir(parents=True, exist_ok=True)
    bbox = ",".join(str(value) for value in RADAR_BBOX)
    size = ",".join(str(value) for value in RADAR_SIZE)
    frames = []
    for attachment, (valid, ingest, object_id, name) in enumerate(candidates, start=1):
        mosaic_rule = json.dumps(
            {"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [object_id]},
            separators=(",", ":"),
        )
        export = urllib.parse.urlencode({
            "bbox": bbox,
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": size,
            "format": "png32",
            "interpolation": "RSP_NearestNeighbor",
            "mosaicRule": mosaic_rule,
            "f": "image",
        })
        image = client.get_bytes(f"{RADAR_SERVICE}/exportImage?{export}", 1_000_000)
        path = radar_dir / f"frame-{attachment:02d}.png"
        temporary = path.with_suffix(".png.tmp")
        temporary.write_bytes(image)
        temporary.replace(path)
        frames.append({
            "attachment": attachment,
            "valid_at": iso_z(valid),
            "ingested_at": iso_z(ingest),
            "object_id": object_id,
            "name": name,
            **_radar_spatial_summary(image),
        })

    width, height = RADAR_SIZE
    west, south, east, north = RADAR_BBOX
    kcdw_pixel = {
        "x": round((LON - west) / (east - west) * width),
        "y": round((north - LAT) / (north - south) * height),
    }
    return {
        "provider": "NOAA/NWS MRMS time-enabled base reflectivity ImageServer",
        "service_url": RADAR_SERVICE,
        "product": "CONUS 1 km quality-controlled base reflectivity",
        "bbox": list(RADAR_BBOX),
        "image_size": list(RADAR_SIZE),
        "kcdw_pixel": kcdw_pixel,
        "frame_order": "oldest_to_newest",
        "latest_age_seconds": max(0, int(latest_age.total_seconds())),
        "stale": False,
        "frames": frames,
        "interpretation_note": "Attached images contain radar reflectivity only, without a basemap. Black or transparent areas have no displayed reflectivity; use the supplied bounding box and KCDW pixel location.",
    }


def collect(now: datetime | None = None, client: Client | None = None, radar_dir: Path | None = None, cache_dir: Path | None = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    client = client or Client()
    points_url = f"https://api.weather.gov/points/{LAT},{LON}"
    points = _source(lambda: client.get(points_url), now)
    props = points.get("data", {}).get("properties", {}) if points["ok"] else {}
    radar_dir = radar_dir or Path("var/radar")
    sources: dict[str, dict] = {
        "radar_mosaic": _source(lambda: collect_radar_loop(client, now, radar_dir), now),
        "nws_points": points,
        "nbm_nbh": _source(lambda: collect_nbm(client, now, "NBH", cache_dir), now),
        "nbm_nbs": _source(lambda: collect_nbm(client, now, "NBS", cache_dir), now),
        "weather_next3": _source(lambda: collect_weather_next3(now), now),
        "weather_next": _source(lambda: collect_weather_next(client, now), now),
        "aifs_ens": _source(lambda: collect_aifs_ens(client, now), now),
    }
    sources["nws_hourly"] = _source(lambda: _periods(client.get(props["forecastHourly"]), 180), now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)
    sources["nws_forecast"] = _source(lambda: _periods(client.get(props["forecast"]), 16), now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)

    def grid():
        raw = client.get(props["forecastGridData"])["properties"]
        return {field: {"uom": raw.get(field, {}).get("uom"), "values": expand_grid_values(raw.get(field, {}).get("values", []), limit=240)} for field in GRID_FIELDS if field in raw}
    sources["nws_grid"] = _source(grid, now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)

    sources.update(collect_afds(client, now))

    stations = "KCDW,KMMU,KTEB,KEWR,KABE,KAVP,KRDG"
    sources["awc_metars"] = _source(lambda: client.get(f"https://aviationweather.gov/api/data/metar?ids={stations}&format=json&hours=3"), now)
    def tafs():
        ids = "KTEB%2CKEWR%2CKABE%2CKAVP"
        parsed = client.get(f"https://aviationweather.gov/api/data/taf?ids={ids}&format=json&time=valid")
        if parsed:
            return parsed
        # AWC sometimes returns an empty valid-time JSON response while its raw
        # endpoint already has the newly issued TAFs. Preserve that live fallback.
        raw = client.get_text(f"https://aviationweather.gov/api/data/taf?ids={ids}&format=raw")
        blocks = [block.strip() for block in re.split(r"(?m)(?=^TAF\s+K[A-Z0-9]{3}\s)", raw) if block.strip()]
        return [
            {"icaoId": block.split()[1], "rawTAF": block, "sourceFormat": "raw_fallback"}
            for block in blocks
            if len(block.split()) >= 2
        ]
    sources["awc_tafs"] = _source(tafs, now)

    def sigmets():
        # GeoJSON is requested explicitly so every retained product has usable geometry.
        data = client.get("https://aviationweather.gov/api/data/airsigmet?format=geojson")
        items = data.get("features", data if isinstance(data, list) else [])
        out = []
        for item in items:
            prop = item.get("properties", item)
            text = " ".join(str(prop.get(k, "")) for k in ("airSigmetType", "hazard", "rawAirSigmet", "productLabel"))
            if "CONVECTIVE" not in text.upper():
                continue
            geom = item.get("geometry") or prop.get("geometry")
            out.append({"properties": {k: prop.get(k) for k in ("airSigmetType", "hazard", "validTimeFrom", "validTimeTo", "rawAirSigmet", "productLabel") if prop.get(k) is not None}, "geometry": geom, "contains_kcdw": geometry_contains(geom, LON, LAT)})
        return out[:40]
    sources["awc_convective_sigmets"] = _source(sigmets, now)
    sources["nws_alerts"] = _source(lambda: client.get(f"https://api.weather.gov/alerts/active?point={LAT},{LON}").get("features", [])[:30], now)

    def spc_outlooks():
        urls = {
            "day1": "https://www.spc.noaa.gov/products/outlook/day1otlk.txt",
            "day2": "https://www.spc.noaa.gov/products/outlook/day2otlk.txt",
            "day3": "https://www.spc.noaa.gov/products/outlook/day3otlk.txt",
            "day4_8": "https://www.spc.noaa.gov/products/exper/day4-8/day4-8otlk.txt",
        }
        out = {}
        for key, url in urls.items():
            try:
                out[key] = client.get_text(url)[:12000]
            except Exception as exc:
                out[key] = f"UNAVAILABLE: {type(exc).__name__}: {exc}"[:300]
        return out
    sources["spc_outlooks"] = _source(spc_outlooks, now)

    def open_meteo():
        params = {"latitude": LAT, "longitude": LON, "timezone": "America/New_York", "forecast_days": 8, "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch", "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,weather_code,cloud_cover,cloud_cover_low,visibility,wind_speed_10m,wind_gusts_10m,wind_direction_10m,cape", "daily": "sunset"}
        raw = client.get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))
        return {"label": "Model guidance — not official aviation guidance", "hourly_units": raw.get("hourly_units", {}), "hourly": raw.get("hourly", {}), "daily": raw.get("daily", {})}
    sources["open_meteo"] = _source(open_meteo, now)

    local = now.astimezone(TZ)
    dates = report_dates(now)
    return {"schema_version": 1, "airport": {"id": "KCDW", "name": "Essex County Airport", "latitude": LAT, "longitude": LON, "timezone": str(TZ), "taf_note": "KCDW has no routine TAF; KTEB and KEWR are local proxies."}, "collected_at": iso_z(now), "local_date": local.date().isoformat(), "report_dates": dates, "sources": sources}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="var/snapshot.json")
    parser.add_argument("--cache-dir", default="var/cache/nbm", type=Path)
    parser.add_argument("--radar-dir", help="directory for ordered radar PNG attachments")
    args = parser.parse_args(argv)
    output = Path(args.output)
    radar_dir = Path(args.radar_dir) if args.radar_dir else output.with_name(f"{output.stem}.radar")
    atomic_write(output, json.dumps(collect(radar_dir=radar_dir, cache_dir=args.cache_dir), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
