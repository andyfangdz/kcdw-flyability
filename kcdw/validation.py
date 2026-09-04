from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .common import parse_time
from .ensemble_guidance import validate_ensemble_guidance

WINDOWS = ("08-10", "10-12", "12-14", "14-16", "16-18", "18-20")
CONFIDENCE = {"high", "medium", "low"}


class ValidationError(ValueError):
    pass


def validate_snapshot_readiness(snapshot: dict) -> None:
    sources = snapshot.get("sources", {})
    radar = sources.get("radar_mosaic", {})
    if isinstance(radar, dict) and radar.get("ok"):
        data = radar.get("data")
        frames = data.get("frames") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("stale") is not False
            or data.get("frame_order") != "oldest_to_newest"
            or data.get("image_size") != [900, 500]
            or not isinstance(frames, list)
            or not 2 <= len(frames) <= 6
        ):
            raise ValidationError("radar source marked available without a fresh 2..6-frame loop")
        try:
            frame_times = [parse_time(frame["valid_at"]) for frame in frames if isinstance(frame, dict)]
            attachments = [frame["attachment"] for frame in frames if isinstance(frame, dict)]
            collected = parse_time(snapshot["collected_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid radar frame metadata") from exc
        if (
            len(frame_times) != len(frames)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in attachments)
            or attachments != list(range(1, len(frames) + 1))
            or frame_times != sorted(frame_times)
        ):
            raise ValidationError("radar frames must be complete and ordered oldest to newest")
        if frame_times[-1] > collected + timedelta(minutes=5) or collected - frame_times[-1] > timedelta(minutes=30):
            raise ValidationError("radar latest frame is not fresh at collection time")
    for source_key, model_id, label in (
        ("weather_next", "google_weathernext2_ensemble_mean", "WeatherNext 2"),
        ("aifs_ens", "ecmwf_aifs025_ensemble_mean", "AIFS-ENS"),
    ):
        source = sources.get(source_key, {})
        if isinstance(source, dict) and source.get("ok"):
            try:
                if source.get("fetched_at") != snapshot.get("collected_at"):
                    raise ValueError("ensemble fetch time does not match collection time")
                validate_ensemble_guidance(source.get("data"), model_id, parse_time(snapshot["collected_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(f"{label} source marked available with invalid guidance") from exc
    forecast_ok = any(sources.get(k, {}).get("ok") for k in ("nws_hourly", "nws_forecast", "nws_grid", "open_meteo"))
    context_ok = any(sources.get(k, {}).get("ok") for k in ("okx_afd", "awc_metars", "awc_tafs", "nws_alerts"))
    total = sum(bool(source.get("ok")) for source in sources.values() if isinstance(source, dict))
    if not forecast_ok or not context_ok or total < 3:
        raise ValidationError("insufficient source coverage to replace the report")


def _exact(obj: dict, required: set[str], where: str) -> None:
    if not isinstance(obj, dict) or set(obj) != required:
        raise ValidationError(f"{where}: expected exactly {sorted(required)}")


def _text(value, where: str, maximum: int, *, minimum: int = 1) -> None:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValidationError(f"{where}: text length must be {minimum}..{maximum}")


def validate_analysis(value: dict, snapshot: dict) -> dict:
    _exact(value, {"generated_at", "source_collected_at", "best_day", "backup_day", "summary", "controlling_hazards", "days"}, "analysis")
    if value["source_collected_at"] != snapshot["collected_at"]:
        raise ValidationError("source_collected_at does not match snapshot")
    generated = parse_time(value["generated_at"])
    collected = parse_time(snapshot["collected_at"])
    if generated < collected - timedelta(minutes=2) or generated > collected + timedelta(hours=2):
        raise ValidationError("generated_at inconsistent with snapshot")
    dates = snapshot["report_dates"]
    if value["best_day"] not in dates or value["backup_day"] not in dates or value["best_day"] == value["backup_day"]:
        raise ValidationError("best/backup day invalid")
    try:
        local_tz = ZoneInfo(snapshot["airport"]["timezone"])
        first_day_cutoff = datetime.fromisoformat(f"{dates[0]}T20:00:00").replace(tzinfo=local_tz)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValidationError("invalid report timezone or dates") from exc
    if generated.astimezone(local_tz) >= first_day_cutoff and snapshot["local_date"] in (value["best_day"], value["backup_day"]):
        raise ValidationError("fully elapsed today cannot be best or backup day")
    _text(value["summary"], "summary", 500)
    if not isinstance(value["controlling_hazards"], list) or not 1 <= len(value["controlling_hazards"]) <= 6:
        raise ValidationError("controlling_hazards must contain 1..6 items")
    for hazard in value["controlling_hazards"]:
        _text(hazard, "hazard", 120)
    if not isinstance(value["days"], list) or len(value["days"]) != 7:
        raise ValidationError("exactly seven days required")
    seen_dates = set()
    for day in value["days"]:
        _exact(day, {"date", "confidence", "narrative", "hazards", "windows"}, "day")
        if day["date"] not in dates or day["date"] in seen_dates:
            raise ValidationError("unexpected or duplicate day")
        seen_dates.add(day["date"])
        if day["confidence"] not in CONFIDENCE:
            raise ValidationError("invalid confidence")
        _text(day["narrative"], "narrative", 360)
        if not isinstance(day["hazards"], list) or len(day["hazards"]) > 5:
            raise ValidationError("invalid hazards")
        for hazard in day["hazards"]:
            _text(hazard, "day hazard", 100)
        if not isinstance(day["windows"], list) or len(day["windows"]) != 6:
            raise ValidationError("exactly six windows required")
        seen_windows = set()
        for window in day["windows"]:
            _exact(window, {"window", "score", "label", "reason"}, "window")
            name, score = window["window"], window["score"]
            if name not in WINDOWS or name in seen_windows:
                raise ValidationError("unexpected or duplicate window")
            seen_windows.add(name)
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 95 or score % 5:
                raise ValidationError("score must be 0..95 in 5-point increments")
            _text(window["label"], "window label", 32)
            _text(window["reason"], "window reason", 300)
        if seen_windows != set(WINDOWS):
            raise ValidationError("missing windows")
    if seen_dates != set(dates):
        raise ValidationError("missing dates")
    return value
