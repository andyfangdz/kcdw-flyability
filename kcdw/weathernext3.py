"""Official WeatherNext 3 BigQuery point forecasts for KCDW."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .weathernext3_zarr import KCDW
from .weathernext3_bigquery import BigQueryStore, validate_provenance


EASTERN = ZoneInfo("America/New_York")
STATISTICS = ("mean", "p10", "p90")
SOURCE = "WeatherNext 3 official statistics via BigQuery"
LEGACY_SOURCE = "WeatherNext 3 official statistics Zarr via GCS gRPC"


@dataclass(frozen=True)
class FieldSpec:
    array: str
    source_unit: str
    unit: str
    low: float
    high: float
    factor: float = 1.0
    offset: float = 0.0
    step_type: str = "instant"

    def convert(self, value: float) -> float:
        return value * self.factor + self.offset


# Surface variables useful to local aviation planning that actually exist in
# the official statistics store. Solar, SST, and 100 m wind are intentionally
# excluded; ceiling, visibility, gust, and convection are not published here.
FIELD_SPECS = {
    "temperature_2m": FieldSpec("temperature_2m", "K", "degC", -120, 80, offset=-273.15),
    "dewpoint_temperature_2m": FieldSpec("dewpoint_temperature_2m", "K", "degC", -120, 80, offset=-273.15),
    "precipitation_1h": FieldSpec("total_precipitation_1hr", "m", "mm", 0, 1500, factor=1000, step_type="accumulated"),
    "imerg_precipitation_1h": FieldSpec("imerg_tp_1hr", "m", "mm", 0, 1500, factor=1000, step_type="accumulated"),
    "experimental_precipitation_1h": FieldSpec("experimental_tp_1hr", "m", "mm", 0, 1500, factor=1000, step_type="accumulated"),
    "sea_level_pressure": FieldSpec("mean_sea_level_pressure", "Pa", "Pa", 75000, 115000),
    "wind_speed_10m": FieldSpec("wind_speed_10m", "m s**-1", "m/s", 0, 160),
    "u_component_of_wind_10m": FieldSpec("u_component_of_wind_10m", "m s**-1", "m/s", -160, 160),
    "v_component_of_wind_10m": FieldSpec("v_component_of_wind_10m", "m s**-1", "m/s", -160, 160),
    "low_cloud_cover": FieldSpec("low_cloud_cover", "(0 - 1)", "%", 0, 100, factor=100),
    "medium_cloud_cover": FieldSpec("medium_cloud_cover", "(0 - 1)", "%", 0, 100, factor=100),
    "high_cloud_cover": FieldSpec("high_cloud_cover", "(0 - 1)", "%", 0, 100, factor=100),
    "total_cloud_cover": FieldSpec("total_cloud_cover", "(0 - 1)", "%", 0, 100, factor=100),
}
# Optional diagnostics use separate cache entries without changing archived
# primary forecast envelopes or making their fields mandatory.
OPTIONAL_FIELD_SPECS = {
    "wind_speed_100m": FieldSpec("wind_speed_100m", "m s**-1", "m/s", 0, 160),
}
SPECS = {name: (spec.unit, spec.low, spec.high) for name, spec in FIELD_SPECS.items()}


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError("weathernext3_validation_error")


def _time(value: object) -> datetime:
    _require(isinstance(value, str) and bool(re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", str(value))))
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: object, low: float, high: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and low <= value <= high)


def _validate(data: dict[str, Any], now: datetime) -> None:
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    _require(type(data) is dict and set(data) == {"explicit_last_good", "status", "forecast"})
    _require(data["explicit_last_good"] is False)
    status, forecast = data["status"], data["forecast"]
    _require(type(status) is dict and type(forecast) is dict)
    _require(status.get("available") is True and status.get("freshness") == "fresh" and status.get("error_code") is None)
    legacy = forecast.get("source") == LEGACY_SOURCE
    _require(status.get("authentication") == ("gcs_authenticated_read_succeeded" if legacy else "bigquery_authenticated_query_succeeded"))
    _require(status.get("transport") == ("GCS gRPC whole-object reads" if legacy else "BigQuery REST"))
    _require(status.get("station") == "KCDW" and status.get("model_id") == 12 and status.get("model") == "WeatherNext 3")
    _require(_number(status.get("latitude"), KCDW[0], KCDW[0]) and _number(status.get("longitude"), KCDW[1], KCDW[1]))
    _require(forecast.get("model_id") == 12 and forecast.get("model") == "WeatherNext 3" and forecast.get("source") in (SOURCE, LEGACY_SOURCE))
    actual = _time(forecast["response_init_utc"])
    requested = _time(status["requested_init_utc"])
    fetched = _time(status["fetched_at"])
    _require(actual.timestamp() % 21600 == 0 and requested.timestamp() % 21600 == 0)
    _require(timedelta(0) <= now.astimezone(timezone.utc) - actual <= timedelta(hours=24))
    _require(actual <= requested <= fetched <= now.astimezone(timezone.utc) + timedelta(minutes=5))
    _require(_time(status["actual_run_utc"]) == actual and _time(status["attempted_init_utc"]) == actual)
    _require(_time(forecast["requested_init_utc"]) == requested)
    _require(status.get("fallback") is (actual != requested))
    _require(status.get("state") == ("degraded" if actual != requested else "ready"))
    times = forecast.get("valid_time_utc")
    _require(type(times) is list and 1 <= len(times) <= 360)
    parsed = [_time(value) for value in times]
    _require(parsed == sorted(set(parsed)))
    _require(all((value - actual).total_seconds() % 3600 == 0 and
                 actual + timedelta(hours=1) <= value <= actual + timedelta(hours=360)
                 for value in parsed))
    grid = forecast.get("grid_point")
    _require(type(grid) is dict and set(grid) == {"latitude", "longitude"})
    _require(_number(grid["latitude"], KCDW[0] - .1, KCDW[0] + .1) and
             _number(grid["longitude"], KCDW[1] - .1, KCDW[1] + .1))
    fields = forecast.get("fields")
    _require(type(fields) is dict and set(fields) == set(FIELD_SPECS))
    for name, spec in FIELD_SPECS.items():
        field = fields[name]
        _require(type(field) is dict and set(field) == {
            "unit", "source_unit", "source_array", "step_type", *STATISTICS})
        _require(field["unit"] == spec.unit and field["source_unit"] == spec.source_unit and
                 field["source_array"] == spec.array and field["step_type"] == spec.step_type)
        for statistic in STATISTICS:
            values = field[statistic]
            _require(type(values) is list and len(values) == len(parsed) and
                     all(_number(value, spec.low, spec.high) for value in values))
        _require(all(low <= high for low, high in zip(field["p10"], field["p90"])))
    if legacy:
        _require("query" not in forecast)
        transfer = forecast.get("transfer")
        _require(type(transfer) is dict and set(transfer) == {
            "objects", "network_objects", "cache_objects", "object_bytes", "network_bytes"})
        _require(all(type(transfer[key]) is int and transfer[key] >= 0 for key in transfer))
        _require(transfer["objects"] == len(FIELD_SPECS) * len(STATISTICS) * len(parsed))
        _require(transfer["network_objects"] + transfer["cache_objects"] == transfer["objects"])
    else:
        _require("transfer" not in forecast)
        validate_provenance(forecast.get("query"))
        _require(actual <= _time(forecast["query"]["retrieved_at"]) <= fetched + timedelta(minutes=5))


def validate_weather_next3(data: dict, now: datetime) -> None:
    """Fail closed on malformed, stale, unavailable, or partial field data."""
    try:
        _validate(data, now)
    except Exception:
        raise ValueError("weathernext3_validation_error") from None


def event_valid_times(event: Any, now: datetime | None = None) -> list[datetime]:
    """Opening through closing plus three hours for the scorecard outlook."""
    start = datetime.combine(event.day, datetime.min.time(), EASTERN) + timedelta(hours=event.start_hour)
    selected = {start.astimezone(timezone.utc) + timedelta(hours=hour)
                for hour in range(event.end_hour - event.start_hour + 4)}
    if now is not None:
        first = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        end = datetime.combine(now.astimezone(EASTERN).date() + timedelta(days=7),
                               datetime.min.time(), EASTERN).astimezone(timezone.utc)
        selected.update(first + timedelta(hours=h) for h in range(int((end-first).total_seconds()/3600) + 1))
    return sorted(selected)


def relevant_valid_times(now: datetime, init: datetime) -> list[datetime]:
    """Full seven-calendar-day hourly guidance plus upcoming event windows."""
    from .events import upcoming_events
    day = now.astimezone(EASTERN).date()
    start = datetime.combine(day, datetime.min.time(), EASTERN).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=7), datetime.min.time(), EASTERN).astimezone(timezone.utc)
    selected = {start + timedelta(hours=h) for h in range(int((end-start).total_seconds()/3600) + 1)}
    for event in upcoming_events(now, horizon_days=16):
        selected.update(value.astimezone(timezone.utc) for value in event_valid_times(event))
    lower, upper = init + timedelta(hours=1), init + timedelta(hours=360)
    selected = {value for value in selected if lower <= value <= upper}
    if not selected:
        next_hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        selected.add(max(lower, next_hour))
    return sorted(selected)


def _normalize_times(values: Iterable[datetime], init: datetime) -> list[datetime]:
    result = []
    for value in values:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("WeatherNext 3 valid times must be timezone-aware")
        result.append(value.astimezone(timezone.utc))
    result = sorted(set(result))
    if not result or any(value.minute or value.second or value.microsecond or
                         not init + timedelta(hours=1) <= value <= init + timedelta(hours=360)
                         for value in result):
        raise ValueError("WeatherNext 3 valid times are outside the selected run")
    return result


def collect_weather_next3(now: datetime, valid_times: Iterable[datetime] | None = None,
                          *, store: BigQueryStore | None = None) -> dict:
    """Read a complete cached BigQuery run, selecting report hours locally."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("weathernext3_request_error")
    now = now.astimezone(timezone.utc)
    try:
        store = store or BigQueryStore()
        requested = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
        # Permission, billing, malformed rows and incomplete runs fail closed.
        # Only an empty, not-yet-published run permits trying an older cycle.
        for actual in store.candidates(now)[:2]:
            try:
                result = store.fetch(_iso(actual))
                break
            except LookupError:
                continue
        else:
            raise ValueError("no recent WeatherNext 3 BigQuery run")
        times = _normalize_times(valid_times if valid_times is not None else relevant_valid_times(now, actual), actual)
        rows = {_time(row["valid_time"]): row for row in result["rows"]}
        selected = [rows[t] for t in times]
        fields = {
            name: {"unit": spec.unit, "source_unit": spec.source_unit,
                   "source_array": spec.array, "step_type": spec.step_type,
                   **{stat: [spec.convert(row[f"{spec.array}_{stat}"]) for row in selected]
                      for stat in STATISTICS}}
            for name, spec in FIELD_SPECS.items()
        }
        latitude, longitude = selected[0]['latitude'], selected[0]['longitude']
        envelope = {
            "explicit_last_good": False,
            "status": {
                "state": "degraded" if actual != requested else "ready",
                "available": True, "freshness": "fresh", "error_code": None,
                "authentication": "bigquery_authenticated_query_succeeded",
                "transport": "BigQuery REST", "station": "KCDW",
                "model": "WeatherNext 3", "model_id": 12,
                "latitude": KCDW[0], "longitude": KCDW[1],
                "actual_run_utc": _iso(actual), "requested_init_utc": _iso(requested),
                "attempted_init_utc": _iso(actual), "fetched_at": _iso(now),
                "fallback": actual != requested,
            },
            "forecast": {
                "source": SOURCE, "model": "WeatherNext 3", "model_id": 12,
                "latitude": latitude, "longitude": longitude,
                "grid_point": {"latitude": latitude, "longitude": longitude},
                "requested_init_utc": _iso(requested), "response_init_utc": _iso(actual),
                "valid_time_utc": [_iso(value) for value in times],
                "fields": fields, "query": result['provenance'],
            },
        }
        validate_weather_next3(envelope, now)
        return envelope
    except ValueError as error:
        if str(error) == "weathernext3_validation_error":
            raise
        raise ValueError("weathernext3_request_error") from None
    except Exception:
        raise ValueError("weathernext3_request_error") from None


def wind_direction(u: float, v: float) -> float | None:
    """Meteorological direction FROM mean components; calm has no direction.

    Marginal component percentiles cannot produce direction percentiles.
    """
    return None if math.hypot(u, v) < 1e-6 else math.degrees(math.atan2(-u, -v)) % 360


def mean_hourly(data: dict, now: datetime) -> dict:
    """Validated common field names/units for mean guidance and scorecards."""
    validate_weather_next3(data, now)
    forecast = data['forecast']
    fields = forecast['fields']
    mapping = {
        'temperature_2m': ('temperature_2m', 1),
        'dew_point_2m': ('dewpoint_temperature_2m', 1),
        'precipitation': ('precipitation_1h', 1),
        'wind_speed_10m': ('wind_speed_10m', 3600/1852),
        'pressure_msl': ('sea_level_pressure', .01),
        'cloud_cover_low': ('low_cloud_cover', 1),
        'cloud_cover_mid': ('medium_cloud_cover', 1),
        'cloud_cover_high': ('high_cloud_cover', 1),
        'cloud_cover': ('total_cloud_cover', 1),
    }
    hourly = {'time': list(forecast['valid_time_utc'])}
    hourly.update({key: [v * factor for v in fields[name]['mean']]
                   for key, (name, factor) in mapping.items()})
    hourly['wind_direction_10m'] = [wind_direction(u, v) for u, v in zip(
        fields['u_component_of_wind_10m']['mean'], fields['v_component_of_wind_10m']['mean'])]
    hourly['wind_gusts_10m'] = [None] * len(hourly['time'])
    return dict(forecast['grid_point'], utc_offset_seconds=0, hourly=hourly)


def summarize_weather_next3(data: dict, now: datetime) -> dict:
    """Compact internal guidance; marginal hourly bands are not daily bands."""
    validate_weather_next3(data, now)
    status, forecast = data["status"], data["forecast"]
    times = [_time(value) for value in forecast["valid_time_utc"]]
    first = now.astimezone(EASTERN).date()
    days = []
    for offset in range(15):
        date = first + timedelta(days=offset)
        start = datetime.combine(date, datetime.min.time(), EASTERN) + timedelta(hours=8)
        end = start + timedelta(hours=12)
        instant = [index for index, value in enumerate(times) if max(start, now) <= value < end]
        accumulated = [index for index, value in enumerate(times)
                       if value - timedelta(hours=1) >= max(start, now) and value <= end]
        fields = {}
        for name, field in forecast["fields"].items():
            indices = accumulated if field["step_type"] == "accumulated" else instant
            if not indices:
                fields[name] = None
                continue
            means = [field["mean"][index] for index in indices]
            fields[name] = {
                "unit": field["unit"], "sample_count": len(indices),
                "mean_min": round(min(means), 3), "mean_max": round(max(means), 3),
                "mean_average": round(sum(means) / len(means), 3),
                "hourly_p10_min": round(min(field["p10"][index] for index in indices), 3),
                "hourly_p90_max": round(max(field["p90"][index] for index in indices), 3),
            }
            if field["step_type"] == "accumulated":
                fields[name]["mean_sum_mm"] = round(sum(means), 3)
        days.append({
            "date": date.isoformat(),
            "coverage": {"hours": len(instant), "precipitation_hours": len(accumulated),
                         "expected_hours": 12, "fraction": len(instant) / 12,
                         "complete": len(instant) == 12 and len(accumulated) == 12},
            "fields": fields,
        })
    return {
        "station": "KCDW", "model": forecast["model"], "model_id": forecast["model_id"],
        "latitude": forecast["latitude"], "longitude": forecast["longitude"],
        "source": forecast["source"], "actual_run_utc": status["actual_run_utc"],
        "requested_init_utc": status["requested_init_utc"], "fetched_at": status["fetched_at"],
        "fallback": status["fallback"], "timezone": "America/New_York",
        "planning_window_local": "08:00–20:00; hourly coverage reported per day",
        "available_fields": [*FIELD_SPECS, "wind_direction_10m"],
        "wind_direction": {"unit": "degrees true", "statistic": "direction of ensemble-mean components",
                           "time": forecast["valid_time_utc"],
                           "values": mean_hourly(data, now)["hourly"]["wind_direction_10m"]},
        "missing_fields": ["gust", "ceiling", "visibility", "convection"],
        "semantics": {
            "use": "Experimental planning guidance; not official aviation weather or a VFR determination.",
            "sampling": "The rolling week and configured event windows are read hourly. Missing hours are gaps, never interpolation or benign conditions.",
            "quantiles": "p10/p90 are hourly marginal ensemble quantiles, not bounds on the mean or daily confidence intervals.",
            "precipitation": "The three precipitation fields are distinct official products; one-hour means may be summed only over explicitly covered intervals.",
            "cloud": "Cloud-layer fraction is not cloud-base height or ceiling probability.",
            "uncertainty": "No gust, ceiling, visibility, or convection field is published in this surface statistics set.",
        },
        "days": days,
    }
