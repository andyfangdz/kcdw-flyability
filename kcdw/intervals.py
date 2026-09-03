from __future__ import annotations

import re
from datetime import datetime, timedelta

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        raise ValueError(f"unsupported ISO duration: {value!r}")
    days, hours, minutes, seconds = (int(v or 0) for v in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def expand_valid_time(value: str) -> list[datetime]:
    """Expand an NWS validTime interval to hour boundaries (start inclusive)."""
    start_text, duration_text = value.split("/", 1)
    start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    span = duration(duration_text)
    if span <= timedelta(0) or span > timedelta(days=14):
        raise ValueError("interval duration out of bounds")
    count = max(1, int(span.total_seconds() // 3600))
    return [start + timedelta(hours=i) for i in range(count)]


def expand_grid_values(values: list[dict], *, limit: int = 400) -> list[dict]:
    expanded: list[dict] = []
    for item in values:
        for instant in expand_valid_time(item["validTime"]):
            expanded.append({"time": instant.isoformat(), "value": item.get("value")})
            if len(expanded) >= limit:
                return expanded
    return expanded
