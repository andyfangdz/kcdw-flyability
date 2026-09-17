"""Dated event definitions (checkrides, trips) that get a dedicated outlook page."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import load_json

TZ = ZoneInfo("America/New_York")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def local_clock(stamp, date=False):
    """Eastern display clock for ISO strings or aware datetimes."""
    moment = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Display timestamps must include a timezone")
    return moment.astimezone(TZ).strftime("%b %-d %H:%M %Z" if date else "%H:%M")


WINDOW = re.compile(r"^(\d{2})-(\d{2})$")
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "events.json"


@dataclass(frozen=True)
class Event:
    slug: str
    title: str
    date: str
    window: str
    nav_label: str
    description: str

    def _hours(self) -> tuple[int, int]:
        match = WINDOW.match(self.window)
        if not match:
            raise ValueError(f"invalid event window: {self.window!r}")
        return int(match.group(1)), int(match.group(2))

    @property
    def start_hour(self) -> int:
        return self._hours()[0]

    @property
    def end_hour(self) -> int:
        return self._hours()[1]

    def label(self) -> str:
        return f"{self.day.strftime('%A, %B %-d')} · {self.start_hour:02d}:00–{self.end_hour:02d}:00 Eastern"

    def as_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "date": self.date, "window": self.window,
                "nav_label": self.nav_label, "description": self.description}

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)

    def days_out(self, now: datetime) -> int:
        return (self.day - now.astimezone(TZ).date()).days

    def path(self) -> str:
        return f"/events/{self.slug}"


def _event(raw: dict) -> Event:
    required = {"slug", "title", "date", "window", "nav_label", "description"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("event entries need exactly slug, title, date, window, nav_label, description")
    if not all(isinstance(raw[key], str) and raw[key].strip() for key in required):
        raise ValueError("event fields must be non-empty strings")
    if not SLUG.match(raw["slug"]):
        raise ValueError(f"invalid event slug: {raw['slug']!r}")
    date.fromisoformat(raw["date"])
    match = WINDOW.match(raw["window"])
    if not match or not 0 <= int(match.group(1)) < int(match.group(2)) <= 24:
        raise ValueError(f"invalid event window: {raw['window']!r}")
    if len(raw["title"]) > 80 or len(raw["nav_label"]) > 40 or len(raw["description"]) > 400:
        raise ValueError("event text fields exceed length limits")
    return Event(**raw)


def load_events(path: Path | str | None = None) -> list[Event]:
    data = load_json(path or DEFAULT_CONFIG)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("events.json must contain an events list")
    events = [_event(item) for item in data["events"]]
    if len({event.slug for event in events}) != len(events):
        raise ValueError("event slugs must be unique")
    return events


def find_event(slug: str, path: Path | str | None = None) -> Event:
    for event in load_events(path):
        if event.slug == slug:
            return event
    raise KeyError(f"unknown event: {slug}")


def upcoming_events(now: datetime, path: Path | str | None = None, horizon_days: int = 45) -> list[Event]:
    """Events from yesterday (still viewable) through the horizon, for navigation."""
    return [event for event in load_events(path) if -1 <= event.days_out(now) <= horizon_days]
