"""Per-member ensemble guidance for a dated event window (deterministic, no LLM).

Fetches individual members from Open-Meteo's ensemble API for several global
ensembles, validates the contract, and reduces them to hourly percentile fans,
hourly rain-member fractions, and a per-model scenario split for the event
window. Output is supplemental model guidance; it is not an observation, TAF,
or ceiling forecast.
"""
from __future__ import annotations

import math
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta

from .common import UTC, iso_z
from .events import TZ, Event
from .gfs_guidance import collect_gfs

ENDPOINT = "https://ensemble-api.open-meteo.com/v1/ensemble"
LAT, LON = 40.8752, -74.2814
VARIABLES = ("pressure_msl", "precipitation", "wind_speed_10m", "wind_gusts_10m", "cloud_cover_low", "temperature_2m")
UNITS = {"time": "unixtime", "pressure_msl": "hPa", "precipitation": "mm", "wind_speed_10m": "kn",
         "wind_gusts_10m": "kn", "cloud_cover_low": "%", "temperature_2m": "°C"}
BOUNDS = {"pressure_msl": (750.0, 1150.0), "precipitation": (0.0, 500.0), "wind_speed_10m": (0.0, 300.0),
          "wind_gusts_10m": (0.0, 300.0), "cloud_cover_low": (0.0, 100.0), "temperature_2m": (-120.0, 80.0)}
MEMBER_KEY = re.compile(r"^([a-z_0-9]+?)(?:_member(\d{2}))?$")
DAYS_BEFORE, DAYS_AFTER = 2, 1
RAIN_HOUR_MM = 0.2
# Scenario thresholds for a local VFR maneuvers window.
WINDOW_RAIN_MM, WINDOW_WIND_KT, WINDOW_LOW_CLOUD_PCT = 2.0, 15.0, 60.0
PERCENTILES = (10, 25, 50, 75, 90)


@dataclass(frozen=True)
class MemberModel:
    key: str
    name: str
    provider: str
    model_id: str
    metadata_dataset: str
    members: int


MODELS = (
    MemberModel("gefs", "NCEP GEFS", "NOAA NCEP", "gfs05", "ncep_gefs05", 31),
    MemberModel("ecmwf_ens", "ECMWF ENS", "ECMWF", "ecmwf_ifs025", "ecmwf_ifs025_ensemble", 51),
    MemberModel("aifs_ens", "ECMWF AIFS-ENS", "ECMWF", "ecmwf_aifs025", "ecmwf_aifs025_ensemble", 51),
    MemberModel("geps", "CMC GEPS", "Environment Canada", "gem_global", "cmc_gem_geps", 21),
)


def _finite(value, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number or null")
    return float(value)


def _required(value, where: str) -> float:
    number = _finite(value, where)
    if number is None:
        raise ValueError(f"{where} must not be null")
    return number


def _present(values: list[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of empty sample")
    position = (len(ordered) - 1) * pct / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def share_percentages(counts: dict[str, int], total: int) -> dict[str, int]:
    """Integer percentages that sum to exactly 100 (largest-remainder rounding)."""
    if total <= 0:
        return {key: 0 for key in counts}
    exact = {key: 100 * value / total for key, value in counts.items()}
    floors = {key: math.floor(value) for key, value in exact.items()}
    remainder = 100 - sum(floors.values())
    for key in sorted(exact, key=lambda k: (exact[k] - floors[k], counts[k]), reverse=True)[:remainder]:
        floors[key] += 1
    return floors


def event_range(event: Event, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Legacy centered range, or collection-day Eastern midnight onward.

    Bound far-future requests to sixteen days before the event. This is a
    display bound, not a claim that any provider covers that entire horizon.
    Persisted ranges are checked against collected_at, never render time.
    """
    first_day = event.day - timedelta(days=DAYS_BEFORE)
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("collection time must be timezone-aware")
        first_day = max(now.astimezone(TZ).date(), event.day - timedelta(days=16))
    start = datetime.combine(first_day, datetime.min.time(), TZ)
    end = datetime.combine(event.day + timedelta(days=DAYS_AFTER + 1), datetime.min.time(), TZ)
    if start >= end:
        raise ValueError("event display range has ended")
    return start, end


def _validate(raw: object, spec: MemberModel, expected_hours: int, expected_start: datetime | None = None, *, allow_partial: bool = False) -> tuple[list[datetime], dict[str, dict[str, list[float | None]]]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{spec.name} response must be an object")
    try:
        latitude, longitude = _required(raw["latitude"], "latitude"), _required(raw["longitude"], "longitude")
        units, hourly = raw["hourly_units"], raw["hourly"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{spec.name} response is incomplete") from exc
    if abs(latitude - LAT) > 0.5 or abs(longitude - LON) > 0.5:
        raise ValueError(f"{spec.name} grid point is not near KCDW")
    if raw.get("timezone") != "GMT" or raw.get("utc_offset_seconds") != 0:
        raise ValueError(f"{spec.name} response is not UTC")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise ValueError(f"{spec.name} hourly payload malformed")
    times_raw = hourly.get("time")
    if not isinstance(times_raw, list) or len(times_raw) != expected_hours:
        raise ValueError(f"{spec.name} returned {len(times_raw) if isinstance(times_raw, list) else 'no'} hours, expected {expected_hours}")
    times = [datetime.fromtimestamp(_required(value, "time"), UTC) for value in times_raw]
    if any(later - earlier != timedelta(hours=1) for earlier, later in zip(times, times[1:])):
        raise ValueError(f"{spec.name} time axis is not consecutive hourly")
    if units.get("time") != "unixtime" or (expected_start is not None and times[0] != expected_start):
        raise ValueError("unexpected time unit or start timestamp")
    members: dict[str, dict[str, list[float | None]]] = {variable: {} for variable in VARIABLES}
    for key, values in hourly.items():
        if key == "time":
            continue
        match = MEMBER_KEY.match(key)
        if not match or match.group(1) not in VARIABLES:
            raise ValueError(f"{spec.name} returned unexpected field {key}")
        variable, member = match.group(1), match.group(2) or "00"
        if units.get(key) != UNITS[variable]:
            raise ValueError(f"{spec.name} {key} unit {units.get(key)!r} != {UNITS[variable]!r}")
        if not isinstance(values, list) or len(values) != expected_hours:
            raise ValueError(f"{spec.name} {key} does not match the time axis")
        numbers = [_finite(value, f"{spec.name} {key}[{index}]") for index, value in enumerate(values)]
        low, high = BOUNDS[variable]
        if any(number is not None and not low <= number <= high for number in numbers):
            raise ValueError(f"{spec.name} {key} outside {low}..{high}")
        if member in members[variable] or int(member) >= spec.members:
            raise ValueError("duplicate or out-of-range member ID")
        if not allow_partial and any(v is not None for v in numbers) and any(v is None for v in numbers):
            raise ValueError(f"{variable} partially missing member series")
        members[variable][member] = numbers
    for variable in ("pressure_msl", "precipitation", "wind_speed_10m"):
        if len(members[variable]) != spec.members:
            raise ValueError(f"{spec.name} returned {len(members[variable])} {variable} members, expected {spec.members}")
    expected_ids = {f"{i:02d}" for i in range(spec.members)}
    for variable, series in members.items():
        if series and set(series) != expected_ids:
            raise ValueError(f"{variable} member identities incomplete")
    return times, members


def _hourly_stats(times: list[datetime], members: dict[str, dict[str, list[float | None]]]) -> dict:
    out: dict = {"time": [iso_z(value) for value in times]}
    for variable in VARIABLES:
        series = list(members[variable].values())
        fans = {f"p{pct}": [] for pct in PERCENTILES}
        for index in range(len(times)):
            sample = _present([row[index] for row in series])
            for pct in PERCENTILES:
                fans[f"p{pct}"].append(_round(_percentile(sample, pct)) if sample else None)
        out[variable] = fans | {"sample_counts": [sum(row[i] is not None for row in series) for i in range(len(times))], "members_with_data": sum(1 for row in series if any(value is not None for value in row))}
    rain = []
    for index in range(len(times)):
        sample = _present([row[index] for row in members["precipitation"].values()])
        rain.append(round(100 * sum(value >= RAIN_HOUR_MM for value in sample) / len(sample)) if sample else None)
    out["rain_member_percent"] = rain
    out["rain_sample_counts"] = [sum(row[i] is not None for row in members["precipitation"].values()) for i in range(len(times))]
    return out


def _window_scenarios(event: Event, times: list[datetime], members: dict[str, dict[str, list[float | None]]]) -> dict:
    start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
    end = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.end_hour)
    indices = [index for index, value in enumerate(times) if start < value <= end]
    if not indices:
        raise ValueError("event window is outside the returned hours")
    has_low_cloud = any(any(row[index] is not None for index in indices) for row in members["cloud_cover_low"].values())
    counts = {"dry_light_wind": 0, "cloud_unknown": 0, "dry_low_cloud": 0, "dry_windy": 0, "rain": 0}
    complete = 0
    rain_totals, wind_max, low_cloud_mean, pressure_min = [], [], [], []
    for member in members["precipitation"]:
        precipitation = _present([members["precipitation"][member][index] for index in indices])
        wind = _present([members["wind_speed_10m"][member][index] for index in indices]) if member in members["wind_speed_10m"] else []
        if len(precipitation) != len(indices) or len(wind) != len(indices):
            continue
        low_cloud = _present([members["cloud_cover_low"][member][index] for index in indices]) if has_low_cloud and member in members["cloud_cover_low"] else []
        complete += 1
        total, peak = sum(precipitation), max(wind)
        rain_totals.append(total)
        wind_max.append(peak)
        cloud = sum(low_cloud) / len(low_cloud) if len(low_cloud) == len(indices) else None
        if cloud is not None:
            low_cloud_mean.append(cloud)
        pressure = _present([members["pressure_msl"][member][index] for index in indices]) if member in members["pressure_msl"] else []
        if len(pressure) == len(indices):
            pressure_min.append(min(pressure))
        if total >= WINDOW_RAIN_MM:
            counts["rain"] += 1
        elif peak >= WINDOW_WIND_KT:
            counts["dry_windy"] += 1
        elif cloud is not None and cloud >= WINDOW_LOW_CLOUD_PCT:
            counts["dry_low_cloud"] += 1
        elif cloud is None:
            counts["cloud_unknown"] += 1
        else:
            counts["dry_light_wind"] += 1
    if complete == 0:
        raise ValueError("no member covers the event window")

    def summary(values: list[float]) -> dict | None:
        if not values:
            return None
        return {"p10": _round(_percentile(values, 10)), "median": _round(_percentile(values, 50)),
                "p90": _round(_percentile(values, 90)), "min": _round(min(values)), "max": _round(max(values)), "count": len(values)}

    return {"members_complete": complete, "window_hours": len(indices), "cloud_members_complete": len(low_cloud_mean), "counts": counts,
            "percent": share_percentages(counts, complete),
            "has_low_cloud_field": has_low_cloud,
            "rain_total_mm": summary(rain_totals), "wind_max_kt": summary(wind_max),
            "low_cloud_mean_percent": summary(low_cloud_mean), "pressure_min_hpa": summary(pressure_min),
            "thresholds": {"rain_mm": WINDOW_RAIN_MM, "wind_kt": WINDOW_WIND_KT, "low_cloud_percent": WINDOW_LOW_CLOUD_PCT}}


def _metadata(client, spec: MemberModel, now: datetime, end: datetime) -> dict:
    try:
        meta = client.get(f"https://ensemble-api.open-meteo.com/data/{spec.metadata_dataset}/static/meta.json")
        initialized = datetime.fromtimestamp(_required(meta["last_run_initialisation_time"], "init"), UTC)
        available = datetime.fromtimestamp(_required(meta["last_run_availability_time"], "avail"), UTC)
        data_end = datetime.fromtimestamp(_required(meta["data_end_time"], "end"), UTC)
        if not initialized <= available <= now + timedelta(minutes=5) or data_end <= available:
            raise ValueError("metadata times inconsistent")
        return {"ok": True, "fresh": now - initialized <= timedelta(hours=24),
                "covers_display": data_end >= end - timedelta(hours=1),
                "binding_note": "Latest advertised dataset metadata, not an immutable run binding. Short 06/18Z IFS cycles may not cover the extended rolling response; earlier long cycles can supply that horizon.",
                "initialization_time": iso_z(initialized), "availability_time": iso_z(available),
                "data_end_time": iso_z(data_end), "native_timestep_hours": int(_required(meta["temporal_resolution_seconds"], "res")) // 3600}
    except Exception as exc:  # metadata is provenance, not evidence; record its absence.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def collect_model(client, spec: MemberModel, event: Event | None, now: datetime,
                  display_range: tuple[datetime, datetime] | None = None) -> dict:
    if display_range is not None:
        start, end = display_range
    elif event is not None:
        start, end = event_range(event)
    else:
        raise ValueError("non-event collection requires an explicit display range")
    fallback = False
    if getattr(client, 'direct_native', False) is True and spec.key in ('gefs', 'ecmwf_ens', 'aifs_ens'):
        try:
            from .direct_ensemble import collect_chart
            native = collect_chart(client, spec, event, start.astimezone(UTC), end.astimezone(UTC), now)
            if native is not None:
                return native
            fallback = True
        except Exception:
            fallback = True
    params = {"latitude": LAT, "longitude": LON, "models": spec.model_id, "hourly": ",".join(VARIABLES),
              "start_hour": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"), "end_hour": (end.astimezone(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
              "timezone": "GMT", "timeformat": "unixtime", "wind_speed_unit": "kn", "precipitation_unit": "mm"}
    raw = client.get(f"{ENDPOINT}?{urllib.parse.urlencode(params)}")
    expected_hours = int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600)
    times, members = _validate(raw, spec, expected_hours, start.astimezone(UTC), allow_partial=event is None)
    result = {"key": spec.key, "model": spec.name, "provider": spec.provider, "model_id": spec.model_id,
            "members": spec.members, "label": f"{spec.provider} {spec.name} ensemble members via Open-Meteo — supplemental model guidance, not official aviation guidance",
            "fetched_at": iso_z(now), "metadata": _metadata(client, spec, now, end), "endpoint": ENDPOINT, "grid_point": {"latitude": raw["latitude"], "longitude": raw["longitude"]}, "hourly_units": dict(UNITS) | {"time": "iso8601 UTC"},
            "hourly": _hourly_stats(times, members), "window": _window_scenarios(event, times, members) if event is not None else None}

    if fallback:
        from .direct_ensemble import FALLBACK
        result["metadata"]["direct_fallback_reason"] = FALLBACK
    return result


def collect_weathernext_comparator(client, event: Event, now: datetime,
                                 display_range: tuple[datetime, datetime] | None = None) -> dict:
    from .ensemble_guidance import WEATHER_NEXT_2, _validate_metadata, _metadata_url
    spec = WEATHER_NEXT_2
    metadata = _validate_metadata(client.get(_metadata_url(spec)), spec, now)
    start, end = display_range or event_range(event)
    variables = ("pressure_msl", "wind_speed_10m", "cloud_cover_low", "precipitation")
    fields = tuple(f for v in variables for f in (v, v + "_spread"))
    params = {"latitude": LAT, "longitude": LON, "models": spec.model_id, "hourly": ",".join(fields),
              "start_hour": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"),
              "end_hour": (end.astimezone(UTC)-timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
              "timezone": "GMT", "timeformat": "unixtime", "wind_speed_unit": "kn", "precipitation_unit": "mm"}
    raw = client.get(ENDPOINT + "?" + urllib.parse.urlencode(params))
    if abs(_required(raw["latitude"], "latitude")-LAT) > .5 or abs(_required(raw["longitude"], "longitude")-LON) > .5:
        raise ValueError("WeatherNext grid mismatch")
    if raw.get("timezone") != "GMT" or raw.get("utc_offset_seconds") != 0:
        raise ValueError("WeatherNext timezone mismatch")
    h, units = raw["hourly"], raw["hourly_units"]
    n = int((end.astimezone(UTC)-start.astimezone(UTC)).total_seconds()/3600)
    expected = [int((start.astimezone(UTC)+timedelta(hours=i)).timestamp()) for i in range(n)]
    if set(h) != {"time", *fields} or h["time"] != expected or units != {"time": "unixtime", **{f: UNITS[f.removesuffix("_spread")] for f in fields}}:
        raise ValueError("WeatherNext fields, units or exact time axis mismatch")
    for f in fields:
        values = h[f]
        low, high = (0, 150 if f.startswith("pressure") else BOUNDS[f.removesuffix("_spread")][1]) if f.endswith("_spread") else BOUNDS[f]
        if len(values) != n or any(not low <= _required(v, f) <= high for v in values):
            raise ValueError("WeatherNext missing, non-finite or out-of-bounds data")
    from .common import parse_time
    if parse_time(metadata["data_end_time"]) < end - timedelta(hours=1):
        raise ValueError("WeatherNext metadata does not cover event display")
    return {"model_id": spec.model_id, "statistics": ["mean", "standard_deviation"], "metadata": metadata,
            "hourly": {**h, "time": [iso_z(datetime.fromtimestamp(t,UTC)) for t in expected]}, "grid_point": {"latitude":raw["latitude"], "longitude":raw["longitude"]}}


def validate_snapshot(snapshot: dict) -> None:
    """Recheck persisted normalized arrays and counts before render/publication."""
    from .common import parse_time
    event = Event(**snapshot["event"])
    collected = parse_time(snapshot["collected_at"])
    range_clock = collected
    if snapshot.get('direct_native_version') == 1:
        range_clock = parse_time(snapshot['collection_started_at'])
        if not timedelta(0) <= collected-range_clock <= timedelta(minutes=20):
            raise ValueError('native collection clock mismatch')
    start, end = event_range(event)
    legacy = {"start": iso_z(start), "end": iso_z(end)}
    if snapshot["range"] != legacy:
        start, end = event_range(event, range_clock)
    n = int((end.astimezone(UTC)-start.astimezone(UTC)).total_seconds()/3600)
    axis = [iso_z(start.astimezone(UTC)+timedelta(hours=i)) for i in range(n)]
    if snapshot["range"] != {"start": iso_z(start), "end": iso_z(end)}:
        raise ValueError("snapshot display range mismatch")
    for spec in MODELS:
        source = snapshot["models"][spec.key]
        if not source["ok"]:
            continue
        d = source["data"]
        direct = d.get("metadata", {}).get("direct_native") is True
        if direct:
            from .direct_ensemble import validate_normalized, COUNTS
            validate_normalized(d, spec, collected)
        if d["model_id"] != spec.model_id or d["members"] != (COUNTS[spec.key] if direct else spec.members) or d["hourly"]["time"] != axis:
            raise ValueError("snapshot model identity/time mismatch")
        for coord,target in (("latitude",LAT),("longitude",LON)):
            if abs(_required(d["grid_point"][coord],coord)-target) > .5:
                raise ValueError("snapshot grid mismatch")
        h = d["hourly"]
        for variable in VARIABLES:
            # Archives written before multimodel amount charts lack rain fans.
            if variable == "precipitation" and variable not in h:
                continue
            f = h[variable]
            if any(len(f[f"p{p}"]) != n for p in PERCENTILES):
                raise ValueError("snapshot percentile length mismatch")
            counts = f["sample_counts"]
            if len(counts) != n or any(type(c) is not int or not 0 <= c <= spec.members for c in counts):
                raise ValueError("snapshot sample count mismatch")
            for i in range(n):
                vals = [f[f"p{p}"][i] for p in PERCENTILES]
                if not counts[i]:
                    if any(v is not None for v in vals): raise ValueError("values without samples")
                else:
                    lo,hi = BOUNDS[variable]
                    if any(not lo <= _required(v,variable) <= hi for v in vals) or vals != sorted(vals):
                        raise ValueError("snapshot percentile contract mismatch")
            if any(len(f[f"p{p}"]) != n for p in PERCENTILES):
                raise ValueError("snapshot percentile length mismatch")
        if "precipitation" in h and h["precipitation"]["sample_counts"] != h["rain_sample_counts"]:
            raise ValueError("rain fan sample count mismatch")
        if len(h["rain_member_percent"]) != n or len(h["rain_sample_counts"]) != n:
            raise ValueError("rain time count mismatch")
        for value,count in zip(h["rain_member_percent"], h["rain_sample_counts"]):
            if type(count) is not int or not 0 <= count <= spec.members or (count and not 0 <= _required(value,"rain fraction") <= 100) or (not count and value is not None):
                raise ValueError("rain fraction/count mismatch")
        w = d["window"]
        if w["window_hours"] != event.end_hour-event.start_hour or not 0 < w["members_complete"] <= spec.members:
            raise ValueError("window coverage mismatch")
        for key in ("rain_total_mm","wind_max_kt","low_cloud_mean_percent","pressure_min_hpa"):
            v = w[key]
            if v is not None:
                values = [_required(v[k],key) for k in ("min","p10","median","p90","max")]
                if values != sorted(values) or type(v["count"]) is not int or not 0 < v["count"] <= spec.members:
                    raise ValueError("window distribution mismatch")
    comparator = snapshot.get("weathernext2",{})
    if comparator.get("ok"):
        d = comparator["data"]
        if d["model_id"] != "google_weathernext2_ensemble_mean" or d["hourly"]["time"] != axis:
            raise ValueError("comparator identity/time mismatch")
        for f,values in d["hourly"].items():
            if f == "time": continue
            lo,hi = (0,150 if f.startswith("pressure") else BOUNDS[f.removesuffix("_spread")][1]) if f.endswith("_spread") else BOUNDS[f]
            if len(values) != n or any(not lo <= _required(v,f) <= hi for v in values):
                raise ValueError("comparator values mismatch")


def collect_weather_next3(now):
    # Lazy imports keep independently available ensembles working during adapter outages.
    from .weathernext3 import collect_weather_next3 as collect
    return collect(now)


def validate_weather_next3(envelope, now):
    from .weathernext3 import validate_weather_next3 as validate
    return validate(envelope, now)


def weathernext3_diagnostic(snapshot: dict, now: datetime) -> dict:
    """Public-safe, non-retrievable event diagnostic; never return raw values.

    Marginal hourly percentiles cannot be summed into an event distribution.
    Only fixed qualitative planning categories leave this boundary.
    """
    from .common import parse_time
    source = snapshot.get("weathernext3", {})
    unavailable = {"available": False, "reason": "not collected or upstream unavailable"}
    if not source.get("ok"):
        return unavailable
    try:
        envelope = source["data"]
        validate_weather_next3(envelope, now)
        if not envelope["status"]["available"]:
            return unavailable
        forecast = envelope["forecast"]
        event = Event(**snapshot["event"])
        start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
        expected = [start + timedelta(hours=i) for i in range(1, event.end_hour-event.start_hour+1)]
        times = [parse_time(t) for t in forecast["valid_time_utc"]]
        wind_expected = [start + timedelta(hours=i) for i in range(event.end_hour-event.start_hour)]
        if any(times.count(t) != 1 for t in expected + wind_expected):
            return {"available": False, "reason": "incomplete event window"}
        indices = [times.index(t) for t in expected]
        wind_indices = [times.index(t) for t in wind_expected]
        rain, wind = forecast["fields"]["precipitation_1h"], forecast["fields"]["wind_speed_10m"]
        def reached(value, threshold):
            return "planning trigger reached" if value >= threshold else "below planning trigger"
        def signal(field, threshold, sample_indices, scale: float = 1):
            if any(field["p10"][i]*scale >= threshold for i in sample_indices):
                return "lower and upper percentiles reach trigger"
            if any(field["p90"][i]*scale >= threshold for i in sample_indices):
                return "upper percentile reaches trigger"
            return "percentile range below trigger"
        rain_signal, wind_signal = signal(rain, RAIN_HOUR_MM, indices), signal(wind, WINDOW_WIND_KT, wind_indices, 3600/1852)
        broad = (any(rain["p90"][i]-rain["p10"][i] >= RAIN_HOUR_MM for i in indices) or
                 any((wind["p90"][i]-wind["p10"][i])*3600/1852 >= 5 for i in wind_indices))
        rain_mean = reached(sum(rain["mean"][i] for i in indices), WINDOW_RAIN_MM)
        wind_mean = reached(max(wind["mean"][i] for i in wind_indices)*3600/1852, WINDOW_WIND_KT)
        caution = (rain_mean == "planning trigger reached" or wind_mean == "planning trigger reached" or
                   rain_signal != "percentile range below trigger" or wind_signal != "percentile range below trigger")
        return {"available": True, "precipitation_mean": rain_mean, "wind_mean": wind_mean,
                "precipitation_signal": rain_signal, "wind_signal": wind_signal,
                "uncertainty": "broad" if broad else "tight relative to planning thresholds",
                "caution": "Keep a weather contingency for maneuvers and runway work; precipitation or wind triggers need follow-up." if caution else
                           "No precipitation/wind trigger identified; do not infer checkride suitability. Verify maneuvers ceiling and runway conditions closer in.",
                "run": iso_z(parse_time(forecast["response_init_utc"])),
                "requested_run": iso_z(parse_time(envelope["status"].get("requested_init_utc", forecast["requested_init_utc"]))),
                "fetched": iso_z(parse_time(envelope["status"].get("fetched_at", snapshot["collected_at"]))),
                "fallback": "explicit last-good" if envelope.get("explicit_last_good") else
                            ("earlier run than requested" if envelope["status"].get("fallback") or forecast["response_init_utc"] != forecast["requested_init_utc"] else "none")}
    except (ValueError, KeyError, TypeError, IndexError, ImportError):
        # Never publish exception text: upstream validation errors may contain raw data.
        return {"available": False, "reason": "validation failed"}


def collect_event(client, event: Event, now: datetime | None = None, *, allow_empty: bool = False) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start, end = event_range(event, now)
    models = {}
    def collect_one(spec):
        try:
            return spec.key, {"ok": True, "error": None, "data": collect_model(client, spec, event, now, (start, end))}
        except Exception as exc:
            return spec.key, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "data": None}
    if getattr(client, 'direct_native', False) is True:
        from concurrent.futures import ThreadPoolExecutor
        client._direct_ensemble_cache = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            models.update(pool.map(collect_one, MODELS))
    else:
        models.update(map(collect_one, MODELS))
    if not allow_empty and not any(model["ok"] for model in models.values()):
        raise RuntimeError("no ensemble model was usable for the event")
    try:
        comparator = {"ok": True, "data": collect_weathernext_comparator(client, event, now, (start, end))}
    except Exception as exc:
        comparator = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        envelope = collect_weather_next3(now)
        validate_weather_next3(envelope, now)
        wn3 = {"ok": bool(envelope["status"]["available"]), "data": envelope, "error": None}
        if not wn3["ok"]:
            wn3["error"] = "WeatherNext 3 upstream unavailable"
    except Exception:
        wn3 = {"ok": False, "data": None, "error": "WeatherNext 3 collection or validation failed"}
    from .synoptic_context import collect_context
    # Keep the full Eastern chart axis, but request only GFS's accepted UTC
    # forecast interval (same policy as the rolling weekly comparison). Earlier
    # elapsed hours remain gaps rather than suppressing the whole model.
    gfs_start = max(start, now.replace(hour=0, minute=0, second=0, microsecond=0))
    snapshot = {"gfs": collect_gfs(client, gfs_start, end, now), "synoptic_context": collect_context(client, now), "weathernext3": wn3, "weathernext2": comparator, "version": 1, "collected_at": iso_z(now), "event": event.as_dict(), "days_out": event.days_out(now),
            "range": {"start": iso_z(start), "end": iso_z(end)}, "airport": {"icao": "KCDW", "latitude": LAT, "longitude": LON, "timezone": "America/New_York"},
            "license": "Open-Meteo API data: CC BY 4.0", "models": models}
    if getattr(client, 'direct_native', False) is True:
        snapshot.update(direct_native_version=1, collection_started_at=iso_z(now), collected_at=iso_z(datetime.now(UTC)))
    return snapshot
