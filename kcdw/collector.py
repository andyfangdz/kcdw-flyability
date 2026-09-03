from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import UTC, atomic_write, iso_z
from .geometry import geometry_contains
from .intervals import expand_grid_values

LAT, LON = 40.8752, -74.2814
TZ = ZoneInfo("America/New_York")
USER_AGENT = "kcdw-flyability/1.0 (weather report; contact: local-operator)"
GRID_FIELDS = ("ceilingHeight", "visibility", "probabilityOfThunder", "probabilityOfPrecipitation", "windSpeed", "windGust", "windDirection", "skyCover", "weather")


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

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise ValueError("text response exceeds 1 MB")
                    return raw.decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last))


def _periods(data: dict, limit: int) -> list[dict]:
    keep = ("number", "name", "startTime", "endTime", "isDaytime", "temperature", "temperatureUnit", "probabilityOfPrecipitation", "windSpeed", "windDirection", "shortForecast", "detailedForecast")
    return [{k: p[k] for k in keep if k in p} for p in data.get("properties", {}).get("periods", [])[:limit]]


def _afd_excerpt(text: str, limit: int = 14000) -> str:
    # Keep the operationally useful major sections, including extended-period
    # reasoning. OKX separates major sections with a standalone && line.
    sections = []
    for heading in ("KEY MESSAGES", "SYNOPSIS", "NEAR TERM", "SHORT TERM", "LONG TERM", "AVIATION"):
        match = re.search(
            rf"(?ims)^\.{re.escape(heading)}[^\n]*\n.*?(?=^\s*&&\s*$|^\s*\$\$\s*$|\Z)",
            text,
        )
        if match:
            sections.append(match.group(0).strip())
    combined = "\n\n".join(sections)
    return (combined or text[:limit])[:limit]


def _source(fetch, now: datetime) -> dict:
    try:
        value = fetch()
        return {"ok": True, "fetched_at": iso_z(now), "data": value, "error": None}
    except Exception as exc:
        return {"ok": False, "fetched_at": iso_z(now), "data": None, "error": f"{type(exc).__name__}: {exc}"[:300]}


def collect(now: datetime | None = None, client: Client | None = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    client = client or Client()
    points_url = f"https://api.weather.gov/points/{LAT},{LON}"
    points = _source(lambda: client.get(points_url), now)
    props = points.get("data", {}).get("properties", {}) if points["ok"] else {}
    sources: dict[str, dict] = {"nws_points": points}
    sources["nws_hourly"] = _source(lambda: _periods(client.get(props["forecastHourly"]), 180), now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)
    sources["nws_forecast"] = _source(lambda: _periods(client.get(props["forecast"]), 16), now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)

    def grid():
        raw = client.get(props["forecastGridData"])["properties"]
        return {field: {"uom": raw.get(field, {}).get("uom"), "values": expand_grid_values(raw.get(field, {}).get("values", []), limit=240)} for field in GRID_FIELDS if field in raw}
    sources["nws_grid"] = _source(grid, now) if props else _source(lambda: (_ for _ in ()).throw(RuntimeError("points unavailable")), now)

    def afd():
        listing = client.get("https://api.weather.gov/products/types/AFD/locations/OKX")
        products = listing.get("@graph", [])
        product = max(products, key=lambda item: item.get("issuanceTime", ""))
        detail = client.get(product.get("@id") or f"https://api.weather.gov/products/{product['id']}")
        return {"id": product.get("id"), "issuanceTime": product.get("issuanceTime"), "excerpt": _afd_excerpt(detail.get("productText", ""))}
    sources["okx_afd"] = _source(afd, now)

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
    dates = [(local.date() + timedelta(days=i)).isoformat() for i in range(1, 8)]
    return {"schema_version": 1, "airport": {"id": "KCDW", "name": "Essex County Airport", "latitude": LAT, "longitude": LON, "timezone": str(TZ), "taf_note": "KCDW has no routine TAF; KTEB and KEWR are local proxies."}, "collected_at": iso_z(now), "local_date": local.date().isoformat(), "report_dates": dates, "sources": sources}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="var/snapshot.json")
    args = parser.parse_args(argv)
    atomic_write(args.output, json.dumps(collect(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
