"""Official WeatherNext 3 statistics-Zarr adapter for KCDW.

The upstream Zarr chunks are complete Zstd-compressed global planes. They are
not seekable, so collection downloads whole immutable objects over the GCS
gRPC API and caches them by generation and checksum. Only event-relevant valid
hours are selected; missing surrounding hours remain explicit gaps.
"""
from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .weathernext3_zarr import GrpcStore, KCDW, WeatherNext3Zarr


EASTERN = ZoneInfo("America/New_York")
STATISTICS = ("mean", "p10", "p90")
SOURCE = "WeatherNext 3 official statistics Zarr via GCS gRPC"


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
    _require(status.get("authentication") == "gcs_authenticated_read_succeeded")
    _require(status.get("transport") == "GCS gRPC whole-object reads")
    _require(status.get("station") == "KCDW" and status.get("model_id") == 12 and status.get("model") == "WeatherNext 3")
    _require(_number(status.get("latitude"), KCDW[0], KCDW[0]) and _number(status.get("longitude"), KCDW[1], KCDW[1]))
    _require(forecast.get("model_id") == 12 and forecast.get("model") == "WeatherNext 3" and forecast.get("source") == SOURCE)
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
    transfer = forecast.get("transfer")
    _require(type(transfer) is dict and set(transfer) == {
        "objects", "network_objects", "cache_objects", "object_bytes", "network_bytes"})
    _require(all(type(transfer[key]) is int and transfer[key] >= 0 for key in transfer))
    _require(transfer["objects"] == len(FIELD_SPECS) * len(STATISTICS) * len(parsed))
    _require(transfer["network_objects"] + transfer["cache_objects"] == transfer["objects"])


def validate_weather_next3(data: dict, now: datetime) -> None:
    """Fail closed on malformed, stale, unavailable, or partial field data."""
    try:
        _validate(data, now)
    except Exception:
        raise ValueError("weathernext3_validation_error") from None


def event_valid_times(event: Any) -> list[datetime]:
    """Instant samples from opening through closing (closing covers rain)."""
    start = datetime.combine(event.day, datetime.min.time(), EASTERN) + timedelta(hours=event.start_hour)
    return [start + timedelta(hours=hour) for hour in range(event.end_hour - event.start_hour + 1)]


def relevant_valid_times(now: datetime, init: datetime) -> list[datetime]:
    """Select configured upcoming event hours that fall inside this run."""
    from .events import upcoming_events
    selected: set[datetime] = set()
    for event in upcoming_events(now, horizon_days=16):
        selected.update(value.astimezone(timezone.utc) for value in event_valid_times(event))
    lower, upper = init + timedelta(hours=1), init + timedelta(hours=360)
    selected = {value for value in selected if max(lower, now.astimezone(timezone.utc)) <= value <= upper}
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
                          *, store: GrpcStore | None = None,
                          source: WeatherNext3Zarr | None = None) -> dict:
    """Read every aviation-relevant surface field for selected valid hours."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("weathernext3_request_error")
    now = now.astimezone(timezone.utc)
    try:
        workers = max(1, min(8, int(os.environ.get("WN3_ZARR_WORKERS", "4"))))
        if source is None:
            store = store or GrpcStore(cache_dir=Path("var/wn3-zarr"))
            for candidate in WeatherNext3Zarr.candidates(store, now):
                candidate_times = _normalize_times(
                    valid_times or relevant_valid_times(now, candidate.init), candidate.init)
                candidate_jobs = [
                    (name, spec, statistic, valid)
                    for valid in candidate_times
                    for name, spec in FIELD_SPECS.items()
                    for statistic in STATISTICS
                ]
                try:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        list(pool.map(
                            lambda job: candidate.point_info(
                                f"{job[1].array}_{job[2]}", _iso(job[3])),
                            candidate_jobs,
                        ))
                except Exception as error:
                    if store.is_not_found(error):
                        continue
                    raise
                source = candidate
                times = candidate_times
                jobs = candidate_jobs
                break
            else:
                raise ValueError("no complete recent WeatherNext 3 Zarr run is available")
        else:
            times = _normalize_times(valid_times or relevant_valid_times(now, source.init), source.init)
            jobs = [(name, spec, statistic, valid)
                    for valid in times
                    for name, spec in FIELD_SPECS.items()
                    for statistic in STATISTICS]
        requested = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)

        def read(job):
            name, spec, statistic, valid = job
            result = source.point(f"{spec.array}_{statistic}", _iso(valid))
            if result["unit"] != spec.source_unit:
                raise ValueError("WeatherNext 3 source unit mismatch")
            value = spec.convert(result["value"])
            if not _number(value, spec.low, spec.high):
                raise ValueError("WeatherNext 3 value outside physical bounds")
            return name, statistic, valid, value, result

        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(read, jobs))
        fields = {
            name: {"unit": spec.unit, "source_unit": spec.source_unit,
                   "source_array": spec.array, "step_type": spec.step_type,
                   **{statistic: [] for statistic in STATISTICS}}
            for name, spec in FIELD_SPECS.items()
        }
        by_key = {(name, statistic, valid): value for name, statistic, valid, value, _ in rows}
        for name in fields:
            for statistic in STATISTICS:
                fields[name][statistic] = [by_key[name, statistic, valid] for valid in times]
        points = {(result["grid_point"]["latitude"], result["grid_point"]["longitude"])
                  for *_, result in rows}
        if len(points) != 1:
            raise ValueError("WeatherNext 3 grid point changed within one collection")
        latitude, longitude = next(iter(points))
        transfers = [result["transfer"] for *_, result in rows]
        cache_objects = sum(item["cache_hit"] is True for item in transfers)
        transfer = {
            "objects": len(transfers),
            "network_objects": len(transfers) - cache_objects,
            "cache_objects": cache_objects,
            "object_bytes": sum(item["object_bytes"] for item in transfers),
            "network_bytes": sum(item["bytes_read"] for item in transfers if not item["cache_hit"]),
        }
        actual = source.init
        envelope = {
            "explicit_last_good": False,
            "status": {
                "state": "degraded" if actual != requested else "ready",
                "available": True, "freshness": "fresh", "error_code": None,
                "authentication": "gcs_authenticated_read_succeeded",
                "transport": "GCS gRPC whole-object reads", "station": "KCDW",
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
                "fields": fields, "transfer": transfer,
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
        "planning_window_local": "08:00–20:00; sparse event-relevant samples only",
        "available_fields": list(FIELD_SPECS),
        "missing_fields": ["gust", "ceiling", "visibility", "convection"],
        "semantics": {
            "use": "Experimental planning guidance; not official aviation weather or a VFR determination.",
            "sampling": "Only configured event-relevant hours are read. Missing hours are gaps, never interpolation or benign conditions.",
            "quantiles": "p10/p90 are hourly marginal ensemble quantiles, not bounds on the mean or daily confidence intervals.",
            "precipitation": "The three precipitation fields are distinct official products; one-hour means may be summed only over explicitly covered intervals.",
            "cloud": "Cloud-layer fraction is not cloud-base height or ceiling probability.",
            "uncertainty": "No gust, ceiling, visibility, or convection field is published in this surface statistics set.",
        },
        "days": days,
    }
