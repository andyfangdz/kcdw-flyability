"""Deterministic, traceable preparation of weather evidence for the agent."""
import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .common import parse_time, iso_z
from .scoring import planning_windows


def compact_grid(grid):
    for series in grid.values():
        values = series.get("values", [])
        if not values or any("time" not in value for value in values):
            continue
        result = []
        start = end = None
        previous = None
        for item in values:
            time = parse_time(item["time"])
            if start is not None and time == end and item.get("value") == previous:
                end += timedelta(hours=1)
                continue
            if start is not None:
                result.append({"validTime": f"{iso_z(start)}/PT{int((end - start).total_seconds() / 3600)}H", "value": previous})
            start, end, previous = time, time + timedelta(hours=1), item.get("value")
        if start is not None:
            result.append({"validTime": f"{iso_z(start)}/PT{int((end - start).total_seconds() / 3600)}H", "value": previous})
        series["values"] = result


def decode_nbm(data):
    if data.get("model_version") != "5.0":
        return {"status": "unsupported_version", "model_version": data.get("model_version")}
    count = len(data["valid_times"])
    fields = {
        "TMP": (1, "temperature_degF"), "WDR": (10, "wind_direction_degrees"),
        "WSP": (1, "wind_speed_knots"), "GST": (1, "wind_gust_knots"),
        "WSD": (1, "wind_speed_sd_knots"), "GSD": (1, "wind_gust_sd_knots"),
        "CIG": (100, "ceiling_feet"), "LCB": (100, "lowest_cloud_base_feet"),
        "VIS": (0.1, "visibility_miles"),
        "MVC": (1, "prob_ceiling_le_3000ft_pct"), "IFC": (1, "prob_ceiling_lt_1000ft_pct"),
        "LIC": (1, "prob_ceiling_lt_500ft_pct"), "MVV": (1, "prob_visibility_le_5mi_pct"),
        "IFV": (1, "prob_visibility_lt_3mi_pct"), "LIV": (1, "prob_visibility_lt_1mi_pct"),
    }
    for hours in (1, 3, 6, 12):
        fields[f"P{hours:02}"] = (1, f"precip_probability_{hours}h_pct")
        fields[f"T{hours:02}"] = (1, f"thunder_probability_{hours}h_pct")
        fields[f"Q{hours:02}"] = (0.01, f"precip_amount_{hours}h_inch")
    result = {}
    for line in data["raw_text"].splitlines()[1:]:
        key = line[1:4].strip()
        if key not in fields:
            continue
        factor, name = fields[key]
        cells = line[5:].ljust(count * 3)
        if len(cells.rstrip()) > count * 3:
            raise ValueError(f"NBM {key} has extra columns")
        values = []
        for i in range(count):
            cell = cells[i * 3:i * 3 + 3].strip()
            value = int(cell) if cell else None
            if value is None or value == -99:
                decoded = None
            elif value == -88 and key in ("CIG", "LCB"):
                decoded = "unlimited"
            else:
                decoded = round(value * factor, 3)
            values.append(decoded)
        result[name] = values
    return {"status": "decoded", "model_version": "5.0", "valid_times": data["valid_times"], "fields": result,
            "notes": "null means blank or missing, never zero. Unlimited ceiling is not negative height. NBS visibility 10 means 10 miles or greater. WDR 0 is calm. Probability suffixes retain product duration; they are not two-hour probabilities. Original cards and the verified v5.0 reference remain available."}


def ensemble_summary(data, snapshot):
    hourly = data["hourly"]
    times = [parse_time(t) for t in hourly["time"]]
    now = parse_time(snapshot["collected_at"])
    tz = ZoneInfo(snapshot["airport"]["timezone"])
    fields = ("precipitation", "cloud_cover_low", "wind_speed_10m")
    days = []
    for date in snapshot["report_dates"]:
        start = datetime.fromisoformat(f"{date}T08:00:00").replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{date}T20:00:00").replace(tzinfo=tz)
        if start.date() == now.astimezone(tz).date():
            start = max(start, now)
            if now < end:
                end = max(end, now + timedelta(hours=2))
        indices = [i for i, t in enumerate(times) if t < end and t + timedelta(hours=1) > start]
        summary = {"date": date, "from": iso_z(start), "through": iso_z(end), "sample_count": len(indices), "variables": {}}
        for field in fields:
            if not indices:
                continue
            means = [hourly[field][i] for i in indices]
            spreads = [hourly[f"{field}_spread"][i] for i in indices]
            maximum = max(indices, key=lambda i: hourly[f"{field}_spread"][i])
            summary["variables"][field] = {"unit": data["hourly_units"][field],
                "mean_min": min(means), "mean_max": max(means), "sd_min": min(spreads), "sd_max": max(spreads),
                "variance_max": round(max(spreads) ** 2, 8), "variance_unit": f'({data["hourly_units"][field]})^2',
                "max_sd_at": hourly["time"][maximum]}
        days.append(summary)
    return {"model": data["model"], "days": days,
            "note": "Ranges over actionable hours; variance_max is max squared ensemble standard deviation at a valid time. Hourly interpolation does not supply independent samples or member probabilities. Use raw series to localize a controlling window."}


def prepare(snapshot):
    result = copy.deepcopy(snapshot)
    sources = result.get("sources", {})
    grid = sources.get("nws_grid", {})
    if grid.get("ok"):
        compact_grid(grid["data"])
    derived = {"nbm_decoded": {}, "ensemble_spread": {}}
    for key in ("nbm_nbh", "nbm_nbs"):
        if sources.get(key, {}).get("ok"):
            derived["nbm_decoded"][key] = decode_nbm(sources[key]["data"])
    for key in ("weather_next", "aifs_ens"):
        if sources.get(key, {}).get("ok"):
            derived["ensemble_spread"][key] = ensemble_summary(sources[key]["data"], result)
    result["requested_windows"] = {date: planning_windows(snapshot, date) for date in snapshot["report_dates"]}
    result["derived_evidence"] = derived
    return result
