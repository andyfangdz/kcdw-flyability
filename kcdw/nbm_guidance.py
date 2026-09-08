from __future__ import annotations

import re
from pathlib import Path
from .common import atomic_write
from datetime import datetime, timedelta

from .common import UTC, iso_z, parse_time

BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
PRODUCTS = {"NBH": (1, 25, 1), "NBS": (6, 72, 3)}
MAX_BULK_BYTES = 40_000_000
MAX_CARD_BYTES = 12_000
MAX_AGE = timedelta(hours=6)


def product_url(product: str, cycle: datetime) -> str:
    if product not in PRODUCTS:
        raise ValueError("unsupported NBM product")
    return f"{BASE_URL}/blend.{cycle:%Y%m%d}/{cycle:%H}/text/blend_{product.lower()}tx.t{cycle:%H}z"


def parse_card(text: str, product: str, cycle: datetime, now: datetime) -> dict:
    """Retain the original fixed-width KCDW card, with an explicit UTC axis."""
    first, last, step = PRODUCTS[product]
    match = re.search(r"(?m)^ *KCDW +NBM +V([\d.]+) +(NBH|NBS) +GUIDANCE +"
                      r"(\d{1,2}/\d{2}/\d{4}) +(\d{4}) UTC[^\n]*\n", text)
    if not match or match[2] != product:
        raise ValueError(f"missing KCDW {product} header")
    issued = datetime.strptime(f"{match[3]} {match[4]}", "%m/%d/%Y %H%M").replace(tzinfo=UTC)
    if issued != cycle or not timedelta(0) <= now - issued <= MAX_AGE:
        raise ValueError("NBM cycle mismatch, future cycle, or stale guidance")
    # A blank line terminates a station card; never retain the next station.
    body = re.split(r"\n[ \t\r]*\n", text[match.start():], maxsplit=1)[0].rstrip()
    if len(body.encode()) > MAX_CARD_BYTES:
        raise ValueError("NBM station card exceeds limit")
    rows = {}
    for line in body.splitlines()[1:]:
        row = re.match(r"^ ([A-Z0-9]{2,3}) +", line)
        if row:
            if row[1] in rows:
                raise ValueError("duplicate NBM row")
            rows[row[1]] = line[5:]
    hours = list(range(first, last + 1, step))
    valid_times = [issued + timedelta(hours=hour) for hour in hours]
    if [int(v) for v in rows.get("UTC", "").split()] != [t.hour for t in valid_times]:
        raise ValueError("invalid NBM UTC axis")
    if product == "NBS" and [int(v) for v in rows.get("FHR", "").split()] != hours:
        raise ValueError("invalid NBS forecast hours")
    for field in ("TMP", "WDR", "WSP", "GST", "CIG", "VIS"):
        values = rows.get(field, "").rstrip()
        if len(values) != 3 * len(hours):
            raise ValueError(f"missing or truncated NBM {field} row")
        for offset in range(0, len(values), 3):
            int(values[offset:offset + 3])
    return {
        "provider": "NOAA/NWS National Blend of Models",
        "station": "KCDW", "product": product, "model_version": match[1],
        "cycle_time": iso_z(issued), "source_url": product_url(product, issued),
        "forecast_hours": hours, "valid_times": [iso_z(t) for t in valid_times],
        "interval_hours": step, "raw_text": body,
        "interpretation_note": "Model guidance, not a TAF or observation. Original fixed-width three-character cells are preserved, including blanks and special codes. Consult the NBM station-card key for the reported version before interpreting units or special values.",
        "documentation_url": f"https://vlab.noaa.gov/web/mdl/nbm-textcard-v{match[1]}",
    }


def collect_nbm(client, now: datetime, product: str, cache_dir: Path | None = None) -> dict:
    """Try recent hourly cycles independently for each product as files arrive."""
    now = now.astimezone(UTC)
    newest = now.replace(minute=0, second=0, microsecond=0)
    error = None
    for age in range(6):
        cycle = newest - timedelta(hours=age)
        try:
            cached = cache_dir / f"{product}.{cycle:%Y%m%d%H}.txt" if cache_dir else None
            if cached and cached.is_file():
                try:
                    return parse_card(cached.read_text(), product, cycle, now)
                except (OSError, ValueError):
                    pass
            text = client.get_text(product_url(product, cycle), maximum=MAX_BULK_BYTES)
            data = parse_card(text, product, cycle, now)
            if cached:
                try:
                    atomic_write(cached, data["raw_text"] + "\n")
                except OSError:
                    pass  # Cache availability must not discard usable guidance.
            return data
        except (RuntimeError, ValueError, OSError) as exc:
            error = exc
    raise RuntimeError(f"no usable recent KCDW {product} bulletin: {error}")


def validate_nbm(data: dict, product: str, now: datetime) -> None:
    expected = parse_card(data["raw_text"], product, parse_time(data["cycle_time"]), now)
    if data != expected:
        raise ValueError("NBM metadata does not match station card")
