"""Private WeatherNext 3 adapter. Raw and compact values are internal-only.

The 08:00–20:00 Eastern planning window is a daylight *proxy*, not solar
sunrise/sunset and not a determination that VFR conditions exist.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

ENDPOINT = "http://127.0.0.1:8796/v1/forecast/KCDW"
MAX_BYTES = 3 * 1024 * 1024
EASTERN = ZoneInfo("America/New_York")
SPECS = {
    "temperature_2m": ("degC", -120, 80),
    "precipitation_1h": ("mm", 0, 1500),
    "sea_level_pressure": ("Pa", 75000, 115000),
    "wind_speed_10m": ("m/s", 0, 160),
}


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError("weathernext3_validation_error")


def _time(value: object) -> datetime:
    _require(isinstance(value, str) and bool(re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z", str(value))))
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _number(value: object, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def _validate(data: dict[str, Any], now: datetime) -> None:
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    _require(type(data) is dict and set(data) == {"explicit_last_good", "status", "forecast"})
    _require(data["explicit_last_good"] is False)
    s, f = data["status"], data["forecast"]
    _require(type(s) is dict and type(f) is dict)
    _require(s["available"] is True and s["freshness"] == "fresh" and s["error_code"] is None)
    _require(s["authentication"] == "last_refresh_succeeded" and s["station"] == "KCDW")
    for obj in (s, f):
        _require(type(obj["model_id"]) is int and obj["model_id"] == 12 and obj["model"] == "WeatherNext 3")
        _require(_number(obj["latitude"], 40.8752, 40.8752) and _number(obj["longitude"], -74.2814, -74.2814))
    _require(f["source"] == "Weather Lab GetForecastStatistics")
    actual = _time(f["response_init_utc"])
    requested = _time(s["requested_init_utc"])
    fetched = _time(s["fetched_at"])
    _require(actual.timestamp() > 0 and actual.timestamp() % 21600 == 0 and requested.timestamp() % 21600 == 0)
    _require(timedelta(0) <= now - actual <= timedelta(hours=18))
    _require(actual <= requested <= fetched <= now + timedelta(minutes=5))
    _require(requested - actual <= timedelta(hours=24))
    for key in ("actual_run_utc", "attempted_init_utc"):
        _require(_time(s[key]) == actual)
    _require(_time(f["requested_init_utc"]) == actual)
    _require(_time(s["last_good_requested_init_utc"]) == requested)
    _require(type(s["fallback"]) is bool and s["fallback"] == (requested != actual))
    _require(s["state"] == ("degraded" if s["fallback"] else "ready"))
    times: Any = f["valid_time_utc"]
    _require(type(times) is list and len(times) == 360)
    for i, instant in enumerate(times, 1):
        _require(_time(instant) == actual + timedelta(hours=i))
    fields: Any = f["fields"]
    _require(type(fields) is dict and set(fields) == set(SPECS))
    for name, (unit, low, high) in SPECS.items():
        field: Any = fields[name]
        _require(type(field) is dict and set(field) == {"unit", "mean", "p10", "p90"} and field["unit"] == unit)
        for statistic in ("mean", "p10", "p90"):
            values = field[statistic]
            _require(type(values) is list and len(values) == 360 and all(_number(v, low, high) for v in values))
        lower: Any = field["p10"]
        upper: Any = field["p90"]
        _require(all(a <= b for a, b in zip(lower, upper)))


def validate_weather_next3(data: dict, now: datetime) -> None:
    """Fail closed on malformed, stale, unavailable, or last-good data."""
    try:
        _validate(data, now)
    except Exception:
        raise ValueError("weathernext3_validation_error") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("weathernext3_request_error")


def _read_token() -> str:
    try:
        path = Path(os.environ.get("WEATHERLAB_TOKEN_FILE", "~/.local/state/weatherlab-feed/api-token")).expanduser()
        # O_NOFOLLOW closes the lstat/open symlink race; nonblocking rejects FIFOs
        # without hanging, and fstat validates the opened object, not the pathname.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise ValueError
            raw = file.read(4097)
        if len(raw) > 4096:
            raise ValueError
        token = raw.decode("ascii").strip()
        if not token or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
            raise ValueError
        return token
    except Exception:
        raise ValueError("weathernext3_token_error") from None


def collect_weather_next3(now: datetime) -> dict:
    """Read only the fixed loopback endpoint; never return/cache credentials."""
    token = _read_token()
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = Request(ENDPOINT, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError
        data = json.loads(body)
        # Reject reflected credentials as well as never adding them ourselves.
        if token.encode() in body:
            raise ValueError
    except Exception:
        raise ValueError("weathernext3_request_error") from None
    finally:
        del token
    validate_weather_next3(data, now)
    return data


def summarize_weather_next3(data: dict, now: datetime) -> dict:
    """Compact internal guidance; hourly marginal bands are NOT daily quantiles."""
    validate_weather_next3(data, now)
    s, f = data["status"], data["forecast"]
    times = [_time(t) for t in f["valid_time_utc"]]
    first = now.astimezone(EASTERN).date()
    days = []
    for offset in range(15):
        date = first + timedelta(days=offset)
        start = datetime.combine(date, datetime.min.time(), EASTERN) + timedelta(hours=8)
        end = start + timedelta(hours=12)
        instant_indices = [i for i, t in enumerate(times) if max(start, now) <= t < end]
        # Only complete remaining preceding-hour intervals, not the hour before opening.
        rain_indices = [i for i, t in enumerate(times) if t - timedelta(hours=1) >= max(start, now) and t <= end]
        fields = {}
        for name, field in f["fields"].items():
            indices = rain_indices if name == "precipitation_1h" else instant_indices
            if not indices:
                fields[name] = None
                continue
            mean = [field["mean"][i] for i in indices]
            fields[name] = {"unit": field["unit"], "sample_count": len(indices), "mean_min": round(min(mean), 3),
                            "mean_max": round(max(mean), 3), "mean_average": round(sum(mean) / len(mean), 3),
                            "hourly_p10_min": round(min(field["p10"][i] for i in indices), 3),
                            "hourly_p90_max": round(max(field["p90"][i] for i in indices), 3)}
            if name == "precipitation_1h":
                fields[name]["mean_sum_mm"] = round(sum(mean), 3)
        days.append({"date": date.isoformat(), "coverage": {"hours": len(instant_indices), "precipitation_hours": len(rain_indices), "expected_hours": 12,
                     "fraction": len(instant_indices) / 12, "complete": len(instant_indices) == 12 and len(rain_indices) == 12}, "fields": fields})
    return {"station": "KCDW", "model": f["model"], "model_id": f["model_id"],
            "latitude": f["latitude"], "longitude": f["longitude"], "source": f["source"],
            "actual_run_utc": s["actual_run_utc"], "requested_init_utc": s["requested_init_utc"],
            "fetched_at": s["fetched_at"], "fallback": s["fallback"],
            "timezone": "America/New_York", "planning_window_local": "08:00–20:00 (instant samples end exclusive; precipitation interval end inclusive)",
            "missing_fields": ["cloud", "gust", "ceiling", "visibility", "wind_direction", "convection"],
            "semantics": {
                "use": "Experimental planning guidance; not official aviation weather or a VFR determination.",
                "window": "Fixed 08–20 local planning window, not sunrise/sunset. Instant samples use start-inclusive/end-exclusive times at or after now. Rain uses complete preceding-hour intervals wholly within the remaining window, excluding the opening endpoint and including closing; per-field sample counts identify coverage.",
                "quantiles": "p10/p90 are hourly marginal ensemble quantiles, not bounds on the mean. You cannot sum hourly quantiles into daily quantiles; hourly extrema are not daily confidence intervals.",
                "precipitation": "mean_sum_mm sums mean preceding-hour precipitation only for covered timestamps, not a full-calendar-day total.",
                "uncertainty": "Hourly p10 minimum/p90 maximum describe marginal spread, not joint daily probability. Uncertainty grows with lead time; missing cloud/gust/ceiling/visibility preclude stand-alone flyability assessment."},
            "days": days}
