from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta

from .common import UTC, iso_z, parse_time

WEATHER_NEXT_ENDPOINT = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_HOURS = 192
ENSEMBLE_RAW_UNITS = {
    "time": "unixtime",
    "temperature_2m": "°C",
    "temperature_2m_spread": "K",
    "precipitation": "inch",
    "precipitation_spread": "inch",
    "cloud_cover_low": "%",
    "cloud_cover_low_spread": "%",
    "cloud_cover_mid": "%",
    "cloud_cover_mid_spread": "%",
    "cloud_cover_high": "%",
    "cloud_cover_high_spread": "%",
    "wind_speed_10m": "kn",
    "wind_speed_10m_spread": "kn",
    "wind_direction_10m": "°",
    "pressure_msl": "hPa",
    "pressure_msl_spread": "hPa",
    "weather_code": "wmo code",
}
ENSEMBLE_NORMALIZED_UNITS = ENSEMBLE_RAW_UNITS | {"time": "iso8601 UTC"}
ENSEMBLE_VARIABLES = tuple(name for name in ENSEMBLE_RAW_UNITS if name != "time")
# Fail closed on corrupt/unit-shifted input while keeping bounds deliberately
# wider than credible terrestrial surface weather. Values are in the units
# declared above; precipitation is per returned (interpolated) hour.
ENSEMBLE_VALUE_BOUNDS = {
    "temperature_2m": (-120.0, 80.0),
    "temperature_2m_spread": (0.0, 100.0),
    "precipitation": (0.0, 50.0),
    "precipitation_spread": (0.0, 50.0),
    "cloud_cover_low": (0.0, 100.0),
    "cloud_cover_low_spread": (0.0, 100.0),
    "cloud_cover_mid": (0.0, 100.0),
    "cloud_cover_mid_spread": (0.0, 100.0),
    "cloud_cover_high": (0.0, 100.0),
    "cloud_cover_high_spread": (0.0, 100.0),
    "wind_speed_10m": (0.0, 300.0),
    "wind_speed_10m_spread": (0.0, 300.0),
    "wind_direction_10m": (0.0, 360.0),
    "pressure_msl": (750.0, 1150.0),
    "pressure_msl_spread": (0.0, 150.0),
}
SUPPORTED_WMO_WEATHER_CODES = frozenset(
    (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
     71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99)
)


@dataclass(frozen=True)
class EnsembleModel:
    name: str
    provider: str
    model_id: str
    members: int
    update_frequency_hours: int
    maximum_initialization_age_hours: int


WEATHER_NEXT_2 = EnsembleModel(
    name="WeatherNext 2",
    provider="Google DeepMind and Google Research",
    model_id="google_weathernext2_ensemble_mean",
    members=64,
    # Google publishes four cycles, but Open-Meteo currently processes 00/12 UTC.
    update_frequency_hours=12,
    maximum_initialization_age_hours=24,
)
AIFS_ENS = EnsembleModel(
    name="ECMWF AIFS-ENS",
    provider="ECMWF",
    model_id="ecmwf_aifs025_ensemble_mean",
    members=51,
    update_frequency_hours=6,
    maximum_initialization_age_hours=18,
)
MODELS = {spec.model_id: spec for spec in (WEATHER_NEXT_2, AIFS_ENS)}


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return float(value)


def _unix_time(value, where: str) -> datetime:
    number = _number(value, where)
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{where} is outside the timestamp range") from exc


def _validate_values(variable: str, numbers: list[float], spec: EnsembleModel) -> None:
    if variable == "weather_code":
        if any(not value.is_integer() or int(value) not in SUPPORTED_WMO_WEATHER_CODES for value in numbers):
            raise ValueError(f"{spec.name} weather_code is not a supported integer WMO code")
        return
    try:
        minimum, maximum = ENSEMBLE_VALUE_BOUNDS[variable]
    except KeyError as exc:
        raise ValueError(f"{spec.name} {variable} has no validation bounds") from exc
    if any(not minimum <= value <= maximum for value in numbers):
        raise ValueError(f"{spec.name} {variable} is outside {minimum}..{maximum}")


def _metadata_url(spec: EnsembleModel) -> str:
    return f"https://ensemble-api.open-meteo.com/data/{spec.model_id}/static/meta.json"


def _validate_metadata(metadata: object, spec: EnsembleModel, now: datetime) -> dict:
    if not isinstance(metadata, dict):
        raise ValueError(f"{spec.name} metadata must be an object")
    required = (
        "last_run_initialisation_time",
        "last_run_modification_time",
        "last_run_availability_time",
        "temporal_resolution_seconds",
        "update_interval_seconds",
        "data_end_time",
    )
    if any(name not in metadata for name in required):
        raise ValueError(f"{spec.name} metadata is incomplete")

    initialized = _unix_time(metadata["last_run_initialisation_time"], "initialization time")
    modified = _unix_time(metadata["last_run_modification_time"], "modification time")
    available = _unix_time(metadata["last_run_availability_time"], "availability time")
    data_end = _unix_time(metadata["data_end_time"], "data end time")
    if int(_number(metadata["temporal_resolution_seconds"], "temporal resolution")) != 21_600:
        raise ValueError(f"{spec.name} metadata has an unexpected native timestep")
    expected_update = spec.update_frequency_hours * 3_600
    if int(_number(metadata["update_interval_seconds"], "update interval")) != expected_update:
        raise ValueError(f"{spec.name} metadata has an unexpected update interval")
    if not initialized <= modified <= available <= now + timedelta(minutes=5):
        raise ValueError(f"{spec.name} metadata times are inconsistent")
    if now - initialized > timedelta(hours=spec.maximum_initialization_age_hours):
        raise ValueError(f"{spec.name} latest initialization is stale")
    if data_end <= available:
        raise ValueError(f"{spec.name} metadata has an invalid data horizon")
    return {
        "initialization_time": iso_z(initialized),
        "modification_time": iso_z(modified),
        "availability_time": iso_z(available),
        "data_end_time": iso_z(data_end),
    }


def _validate_raw(raw: object, spec: EnsembleModel, now: datetime) -> tuple[dict, dict, list[datetime]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{spec.name} response must be an object")
    try:
        latitude = _number(raw["latitude"], "grid latitude")
        longitude = _number(raw["longitude"], "grid longitude")
        timezone_name = raw["timezone"]
        utc_offset = raw["utc_offset_seconds"]
        units = raw["hourly_units"]
        hourly = raw["hourly"]
    except KeyError as exc:
        raise ValueError(f"{spec.name} response is incomplete") from exc
    if abs(latitude - 40.8752) > 0.5 or abs(longitude + 74.2814) > 0.5:
        raise ValueError(f"{spec.name} response grid point is not near KCDW")
    if timezone_name != "GMT" or utc_offset != 0:
        raise ValueError(f"{spec.name} response is not in UTC")
    if units != ENSEMBLE_RAW_UNITS or not isinstance(hourly, dict) or set(hourly) != set(ENSEMBLE_RAW_UNITS):
        raise ValueError(f"{spec.name} response units or fields do not match the adapter contract")

    raw_times = hourly.get("time")
    if not isinstance(raw_times, list) or len(raw_times) != ENSEMBLE_HOURS:
        raise ValueError(f"{spec.name} response must contain {ENSEMBLE_HOURS} hourly steps")
    times = [_unix_time(value, f"hourly time {index}") for index, value in enumerate(raw_times)]
    if any(later - earlier != timedelta(hours=1) for earlier, later in zip(times, times[1:])):
        raise ValueError(f"{spec.name} response times are not consecutive hourly steps")
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if abs((times[0] - current_hour).total_seconds()) > 3_600:
        raise ValueError(f"{spec.name} response does not start near the collection hour")

    for variable in ENSEMBLE_VARIABLES:
        values = hourly.get(variable)
        if not isinstance(values, list) or len(values) != ENSEMBLE_HOURS:
            raise ValueError(f"{spec.name} {variable} does not match the hourly axis")
        numbers = [_number(value, f"{spec.name} {variable}[{index}]") for index, value in enumerate(values)]
        _validate_values(variable, numbers, spec)

    return hourly, {"latitude": latitude, "longitude": longitude}, times


def collect_ensemble_mean(client, now: datetime, spec: EnsembleModel) -> dict:
    now = now.astimezone(UTC)
    metadata = _validate_metadata(client.get(_metadata_url(spec)), spec, now)
    params = {
        "latitude": 40.8752,
        "longitude": -74.2814,
        "models": spec.model_id,
        "hourly": ",".join(ENSEMBLE_VARIABLES),
        "forecast_hours": ENSEMBLE_HOURS,
        "timezone": "GMT",
        "timeformat": "unixtime",
        "wind_speed_unit": "kn",
        "precipitation_unit": "inch",
    }
    raw = client.get(f"{WEATHER_NEXT_ENDPOINT}?{urllib.parse.urlencode(params)}")
    hourly, grid_point, times = _validate_raw(raw, spec, now)
    if parse_time(metadata["data_end_time"]) < times[-1]:
        raise ValueError(f"{spec.name} metadata does not cover the returned hourly horizon")
    normalized_hourly = {
        name: [iso_z(value) for value in times] if name == "time" else list(hourly[name])
        for name in ENSEMBLE_RAW_UNITS
    }
    return {
        "label": f"{spec.provider} {spec.name} ensemble guidance via Open-Meteo — not official aviation guidance",
        "model_provider": spec.provider,
        "model": spec.name,
        "model_id": spec.model_id,
        "adapter": "Open-Meteo Ensemble Mean API",
        "endpoint": WEATHER_NEXT_ENDPOINT,
        "metadata_endpoint": _metadata_url(spec),
        "license": "Open-Meteo API data: CC BY 4.0",
        "ensemble_members": spec.members,
        "statistics": ["mean", "spread"],
        "spatial_resolution_degrees": 0.25,
        "native_timestep_hours": 6,
        "returned_timestep_hours": 1,
        "forecast_horizon_days": 15,
        "update_frequency_hours": spec.update_frequency_hours,
        **metadata,
        "run_binding_note": "Initialization metadata describes the latest rolling dataset advertised by the same API host; the point response does not carry an immutable run identifier.",
        "interpolation_note": "Open-Meteo interpolates native six-hour model values to hourly steps; precipitation is distributed across the six corresponding hours while preserving the native total.",
        "aviation_limitations": "This adapter has no aerodrome ceiling, visibility, gust, lightning, or deterministic convective-timing field. Low-cloud fraction is not a ceiling and model output is not an observation.",
        "grid_point": grid_point,
        "hourly_units": dict(ENSEMBLE_NORMALIZED_UNITS),
        "hourly": normalized_hourly,
    }


def collect_weather_next(client, now: datetime) -> dict:
    return collect_ensemble_mean(client, now, WEATHER_NEXT_2)


def collect_aifs_ens(client, now: datetime) -> dict:
    return collect_ensemble_mean(client, now, AIFS_ENS)


def validate_ensemble_guidance(
    data: object,
    expected_model_id: str | None = None,
    collected_at: datetime | None = None,
) -> None:
    if not isinstance(data, dict):
        raise ValueError("ensemble guidance must be an object")
    model_id = data.get("model_id")
    if not isinstance(model_id, str):
        raise ValueError("ensemble guidance has no model identifier")
    spec = MODELS.get(model_id)
    if spec is None or (expected_model_id is not None and model_id != expected_model_id):
        raise ValueError("ensemble guidance has an unexpected model identifier")
    expected_scalar = {
        "model": spec.name,
        "model_provider": spec.provider,
        "ensemble_members": spec.members,
        "statistics": ["mean", "spread"],
        "spatial_resolution_degrees": 0.25,
        "native_timestep_hours": 6,
        "returned_timestep_hours": 1,
        "forecast_horizon_days": 15,
        "update_frequency_hours": spec.update_frequency_hours,
        "hourly_units": ENSEMBLE_NORMALIZED_UNITS,
    }
    if any(data.get(key) != value for key, value in expected_scalar.items()):
        raise ValueError(f"{spec.name} normalized metadata does not match the adapter contract")
    initialized, modified, available, data_end = (
        parse_time(data[key])
        for key in ("initialization_time", "modification_time", "availability_time", "data_end_time")
    )
    if not initialized <= modified <= available < data_end:
        raise ValueError(f"{spec.name} normalized provenance times are inconsistent")
    hourly = data.get("hourly")
    if not isinstance(hourly, dict) or set(hourly) != set(ENSEMBLE_NORMALIZED_UNITS):
        raise ValueError(f"{spec.name} normalized hourly fields are incomplete")
    times = hourly["time"]
    if not isinstance(times, list) or len(times) != ENSEMBLE_HOURS:
        raise ValueError(f"{spec.name} normalized hourly axis is invalid")
    parsed_times = [parse_time(value) for value in times]
    if any(later - earlier != timedelta(hours=1) for earlier, later in zip(parsed_times, parsed_times[1:])):
        raise ValueError(f"{spec.name} normalized hourly times are not consecutive")
    if data_end < parsed_times[-1]:
        raise ValueError(f"{spec.name} normalized metadata does not cover the hourly horizon")
    if collected_at is not None:
        collected_at = collected_at.astimezone(UTC)
        current_hour = collected_at.replace(minute=0, second=0, microsecond=0)
        if available > collected_at + timedelta(minutes=5):
            raise ValueError(f"{spec.name} normalized availability time is in the future")
        if collected_at - initialized > timedelta(hours=spec.maximum_initialization_age_hours):
            raise ValueError(f"{spec.name} normalized initialization is stale")
        if abs((parsed_times[0] - current_hour).total_seconds()) > 3_600:
            raise ValueError(f"{spec.name} normalized hourly data does not start near collection time")
    for variable in ENSEMBLE_VARIABLES:
        values = hourly.get(variable)
        if not isinstance(values, list) or len(values) != ENSEMBLE_HOURS:
            raise ValueError(f"{spec.name} normalized {variable} is invalid")
        numbers = [_number(value, f"{spec.name} normalized {variable}[{index}]") for index, value in enumerate(values)]
        _validate_values(variable, numbers, spec)
