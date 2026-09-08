"""Minimum usable evidence for a current two-hour planning assessment."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .common import UTC, parse_time


def timestamp(value, tz=UTC):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    result = parse_time(value)
    return result.replace(tzinfo=tz) if result.tzinfo is None else result


def covers_session(intervals, now):
    end = now
    for start, stop in sorted(intervals):
        if start > end:
            break
        if stop > end:
            end = stop
    return end >= now + timedelta(hours=2)


def usable_forecast(key, data, now):
    try:
        if key in ("nws_hourly", "nws_forecast"):
            return covers_session([
                (timestamp(p["startTime"]), timestamp(p["endTime"])) for p in data
                if p.get("shortForecast") or p.get("detailedForecast")
            ], now)
        if key == "nws_grid":
            # Require both wind and an aviation constraint, not just temperatures.
            def covered(field):
                return covers_session([
                    (timestamp(p["time"]), timestamp(p["time"]) + timedelta(hours=1))
                    for p in data.get(field, {}).get("values", []) if p.get("value") is not None
                ], now)
            return covered("windSpeed") and (covered("ceilingHeight") or covered("visibility"))
        if key == "open_meteo":
            hourly = data["hourly"]
            times = hourly["time"]
            required = ("wind_speed_10m", "visibility", "precipitation_probability")
            if any(len(hourly.get(field, [])) != len(times) for field in required):
                return False
            return covers_session([
                (timestamp(t, ZoneInfo("America/New_York")), timestamp(t, ZoneInfo("America/New_York")) + timedelta(hours=1))
                for i, t in enumerate(times) if all(hourly[field][i] is not None for field in required)
            ], now)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return False
    return False


def usable_context(key, data, now):
    try:
        if key == "awc_metars":
            return any(p.get("rawOb") and p.get("icaoId") in ("KCDW", "KMMU", "KTEB", "KEWR")
                       and -timedelta(minutes=5) <= now - timestamp(p["obsTime"]) <= timedelta(hours=3)
                       for p in data if p.get("obsTime") is not None)
        if key == "awc_tafs":
            return any(p.get("rawTAF") and p.get("icaoId") in ("KTEB", "KEWR")
                       and timestamp(p["validTimeFrom"]) <= now
                       and timestamp(p["validTimeTo"]) >= now + timedelta(hours=2)
                       for p in data if p.get("validTimeFrom") is not None and p.get("validTimeTo") is not None)
        if key == "okx_afd":
            return bool(data.get("excerpt", "").strip()) and timedelta(0) <= now - timestamp(data["issuanceTime"]) <= timedelta(hours=18)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return False
    return False


def evidence_readiness(snapshot):
    now = timestamp(snapshot["collected_at"])
    sources = snapshot.get("sources", {})
    forecast = [key for key in ("nws_hourly", "nws_forecast", "nws_grid", "open_meteo")
                if sources.get(key, {}).get("ok") and usable_forecast(key, sources[key].get("data"), now)]
    context = [key for key in ("awc_metars", "awc_tafs", "okx_afd")
               if sources.get(key, {}).get("ok") and usable_context(key, sources[key].get("data"), now)]
    return forecast, context
