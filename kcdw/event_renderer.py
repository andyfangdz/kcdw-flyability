"""Render dated-event reports with snapshot-bound prose and inline SVG charts."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import struct
from datetime import datetime, timedelta
from pathlib import Path

from .presentation import compact_notes

from .common import UTC, parse_time
from .events import TZ, Event
from .event_timing import timing_header
from .event_afd_view import render_afds
from .event_ensemble import MODELS
from .event_narrative import UNAVAILABLE, render_event_narrative
from .low_cloud_analysis import render_low_cloud
from .trend_renderer import render_trends
from .run_history import render_run_history

STALE_AFTER = 8 * 3600
CHART_HISTORY = False
PAGE_BUDGET = 790_000  # the worker's checkHtml cap is 800,000 characters
MODEL_COLORS = {"gfs": "#172b3a", "gefs": "#b54a2b", "ecmwf_ens": "#1f5f8b", "aifs_ens": "#26764e", "geps": "#6b4f9e", "wn3": "#c02679", "wn2": "#9b6021"}
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 240, 48, 14, 14, 34


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def encode_chart_values(values):
    """Losslessly trim long empty margins; legacy arrays remain supported."""
    values = [int(v) if isinstance(v, float) and v.is_integer() else v for v in values]
    plain = json.dumps(values, separators=(',', ':'), allow_nan=False)
    if len(values) > 2000:
        return plain
    first = next((i for i, v in enumerate(values) if v is not None), len(values))
    last = next((i + 1 for i in range(len(values) - 1, first - 1, -1) if values[i] is not None), first)
    packed = json.dumps(dict(v=1, n=len(values), s=first, d=values[first:last]),
                        separators=(',', ':'), allow_nan=False)
    raw = b''.join(struct.pack('<d', float('nan') if v is None else v) for v in values[first:last])
    binary = json.dumps(dict(v=2, n=len(values), s=first, b=base64.b64encode(raw).decode('ascii')),
                        separators=(',', ':'))
    return min((plain, packed, binary), key=lambda text: len(esc(text)))


def _straight_points(points):
    """Remove collinear interior points without changing the path's direction."""
    result = []
    for point in points:
        while len(result) >= 2:
            a, b = result[-2:]
            ab = (b[0] - a[0], b[1] - a[1])
            bc = (point[0] - b[0], point[1] - b[1])
            if abs(ab[0] * bc[1] - ab[1] * bc[0]) > 1e-7 or ab[0] * bc[0] + ab[1] * bc[1] < 0:
                break
            result.pop()
        result.append(point)
    return result


class Chart:
    def __init__(self, times: list[datetime], y_min: float, y_max: float, window: tuple[int, int] | None):
        self.times, self.n = times, len(times)
        span = max(y_max - y_min, 1e-9)
        self.y_min, self.y_max = y_min - span * 0.06, y_max + span * 0.06
        self.window = window
        self.parts: list[str] = []

    def x(self, index: int) -> float:
        elapsed = (self.times[index] - self.times[0]).total_seconds()
        duration = max((self.times[-1] - self.times[0]).total_seconds(), 1)
        return PAD_L + (W - PAD_L - PAD_R) * elapsed / duration

    def y(self, value: float) -> float:
        return PAD_T + (H - PAD_T - PAD_B) * (1 - (value - self.y_min) / (self.y_max - self.y_min))

    def _segments(self, values: list) -> list[list[tuple[float, float]]]:
        segments, current = [], []
        for index, value in enumerate(values):
            if value is None:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append((self.x(index), self.y(value)))
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def _points(points):
        return " ".join(",".join(f"{v:.1f}".removesuffix(".0") for v in point)
                        for point in _straight_points(points))

    def band(self, low: list, high: list, fill: str, opacity: float) -> None:
        segments, current = [], []
        for index, (l, h) in enumerate(zip(low, high)):
            if l is None or h is None:
                segments.append(current)
                current = []
            else:
                current.append((index, l, h))
        segments.append(current)
        for points in segments:
            if len(points) == 1:
                i, low_value, high_value = points[0]
                self.parts.append(f'<line x1="{self.x(i):.1f}" x2="{self.x(i):.1f}" y1="{self.y(low_value):.1f}" y2="{self.y(high_value):.1f}" stroke="{fill}" stroke-opacity="{opacity}" stroke-width="6"/>')
            if len(points) < 2:
                continue
            forward = self._points((self.x(i), self.y(h)) for i, _, h in points)
            backward = self._points((self.x(i), self.y(l)) for i, l, _ in reversed(points))
            self.parts.append(f'<polygon points="{forward} {backward}" fill="{fill}" fill-opacity="{opacity}" stroke="none"/>')

    def line(self, values: list, color: str, width: float = 1.8, dashed: bool = False, compact: bool = True) -> None:
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        for segment in self._segments(values):
            if len(segment) == 1:
                x, y = segment[0]
                self.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}"/>')
                continue
            points = self._points(segment) if compact else " ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
            self.parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="{width}"{dash} vector-effect="non-scaling-stroke" stroke-linejoin="round"/>')

    def reference(self, value: float, label: str) -> None:
        if not self.y_min <= value <= self.y_max:
            return
        y = self.y(value)
        self.parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#182d2c" stroke-width=".8" stroke-dasharray="2 4"/>'
                          f'<text x="{W - PAD_R - 4}" y="{y - 4:.1f}" text-anchor="end" class="ref">{esc(label)}</text>')

    def render(self, title: str, unit: str, note: str, legend: list[tuple[str, str, bool]], y_ticks: list[float]) -> str:
        axes = {color: [] for color in ("#d4dad2", "#c9d0c7", "#a9b3a8")}
        labels, dates = [], []
        for value in y_ticks:
            if self.y_min <= value <= self.y_max:
                y = self.y(value)
                axes["#d4dad2"].append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
                labels.append(f'<span style="top:{y / H * 100:.3f}%">{value:g}</span>')
        for index, moment in enumerate(self.times):
            local = moment.astimezone(TZ)
            if local.hour == 0 or index == 0:
                x = self.x(index)
                axes["#c9d0c7"].append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{PAD_T}" y2="{H - PAD_B}"/>')
                left = (x - PAD_L) / (W - PAD_L - PAD_R) * 100
                dates.append(f'<span style="left:{left:.5f}%" title="{local:%Y-%m-%d}">{esc(local.strftime("%b %-d"))}</span>')
            elif local.hour == 12:
                x = self.x(index)
                axes["#a9b3a8"].append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{H - PAD_B - 5}" y2="{H - PAD_B}"/>')
        grid = "".join(f'<g stroke="{color}" stroke-width="1">{"".join(lines)}</g>' for color, lines in axes.items())
        start, end = self.window or (0, 0)
        shade = (f'<rect x="{self.x(start):.1f}" y="{PAD_T}" width="{self.x(end) - self.x(start):.1f}" height="{H - PAD_T - PAD_B}" fill="#e8b84a" fill-opacity=".28"/>' if self.window is not None else '')
        swatches = "".join(f'<span class="legend-item"><span class="swatch{" dashed" if dashed else ""}" style="--c:{color}"></span>{esc(label)}</span>' for label, color, dashed in legend)
        axis_start, axis_end = self.times[0].astimezone(UTC), self.times[-1].astimezone(UTC)
        center = self.times[start] + (self.times[end] - self.times[start]) / 2
        hours = (axis_end-axis_start).total_seconds()/3600
        return (f'<figure class="chart"><figcaption><h3>{esc(title)}</h3><span class="unit">{esc(unit)}</span></figcaption>'
                f'<div class="forecast-frame"><div class="fixed-y-axis" aria-label="{esc(unit)} axis">{"".join(labels)}</div>'
                f'<div class="chart-scroll" data-sync-group="forecast" data-axis-start="{axis_start:%Y-%m-%dT%H:%M:%SZ}" data-axis-end="{axis_end:%Y-%m-%dT%H:%M:%SZ}" data-event-center="{center.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}" tabindex="0" role="region" aria-label="{esc(title)} chart; pan horizontally; synchronized forecast dates">'
                f'<div class="forecast-plane" style="width:{max(720, hours*12):.1f}px"><svg viewBox="{PAD_L} 0 {W-PAD_L-PAD_R} {H}" role="img" aria-label="{esc(title)}" preserveAspectRatio="none">{shade}{grid}{"".join(self.parts)}</svg>'
                f'<div class="forecast-x-axis">{"".join(dates)}</div></div></div><output class="chart-tooltip" hidden></output></div>'
                f'<div class="legend-row">{swatches}</div><p class="chart-note">{esc(note)}</p></figure>')


def _ok_models(snapshot: dict) -> list[dict]:
    order = [spec.key for spec in MODELS]
    return [snapshot["models"][key]["data"] for key in order if key in snapshot["models"] and snapshot["models"][key]["ok"]]


def _extent(*series: list) -> tuple[float, float]:
    values = [value for values in series for value in values if value is not None]
    return (min(values), max(values)) if values else (0.0, 1.0)


def _ticks(low: float, high: float) -> list[float]:
    import math
    span = max(high-low, .01)
    target = span / 5
    base = 10 ** math.floor(math.log10(target))
    step = next(multiplier * base for multiplier in (1, 2, 5, 10) if multiplier * base >= target)
    start = math.floor(low / step) * step
    return [start + step * i for i in range(int((high - start) / step) + 2)]


def gfs_status(snapshot: dict, now: datetime) -> dict:
    from .gfs_guidance import validate_gfs
    return validate_gfs(snapshot.get("gfs"), now)


def _gfs_window(hourly: dict, start: datetime, end: datetime) -> str:
    """No reduction over missing hours; rain endpoints differ from wind samples."""
    axis = {parse_time(t): i for i, t in enumerate(hourly["time"])}
    n = int((end - start).total_seconds() / 3600)
    def value(field, rain=False):
        expected = [start + timedelta(hours=i + int(rain)) for i in range(n)]
        values = [hourly[field][axis[t]] if t in axis else None for t in expected]
        if not values or any(v is None for v in values):
            return "unavailable (window gaps)"
        complete = [float(v) for v in values if v is not None]
        result = sum(complete) if rain else max(complete)
        return f'{result:.1f} {"mm" if rain else "kt"}'
    return ('<p class="gfs-window"><strong>GFS operational · forecast context:</strong> '
            + value("precipitation", True) + ' rain · peak sampled sustained wind '
            + value("wind_speed_10m") + ' · peak sampled gust '
            + value("wind_gusts_10m") + '. One deterministic scenario, not a probability.</p>')


def _aligned(hourly, values, times):
    positions = {parse_time(t): i for i, t in enumerate(hourly.get("time", []))}
    return [values[positions[t]] if t in positions and positions[t] < len(values) else None for t in times]


def _validated_history(snapshot, now):
    if not snapshot.get("forecast_history"):
        return None
    from .forecast_history import validate_forecast_history
    history = validate_forecast_history(snapshot["forecast_history"], snapshot["event"], now)
    if history and (history['collected_at'] != snapshot.get('collected_at') or
                    history['cutoff'] != snapshot.get('range', {}).get('start')):
        return None
    return history


def _history_series(snapshot, history, field, times, current):
    """Saved values are display-only, never substitutes for present guidance."""
    if not history:
        return []
    cutoff = parse_time(snapshot["range"]["start"])
    result = []
    for key, source in (history or {}).get("sources", {}).items():
        hourly = source["hourly"]
        raw = hourly.get(field)
        if not raw:
            continue
        present = current.get(key, [None] * len(times))
        arrays = [_aligned(hourly, raw.get(stat, []), times) for stat in ("center", "low", "high")]
        for i, t in enumerate(times):
            if t >= cutoff or present[i] is not None:
                for values in arrays:
                    values[i] = None
        if not any(v is not None for v in arrays[0]):
            continue
        statistic = {"median": "Median (p50) / p10–p90", "mean": "Mean ±1 SD" if key == "wn2" else "Mean / p10–p90",
                     "deterministic": "Deterministic / no uncertainty band"}[source["statistic"]]
        if source["statistic"] == "deterministic":
            arrays[1:] = [[], []]
        result.append((key, *arrays, statistic))
    return result


def _saved_group(chart, row, label, color, unit, indices=None, moisture=False):
    key, center, low, high, statistic = row
    # Hover lookup already treats out-of-array hours as missing. Preserve all
    # interior/leading gaps, but avoid repeating the empty future tail per curve.
    last = next((i+1 for i in range(len(center)-1, -1, -1) if center[i] is not None), 0)
    encoded = esc(encode_chart_values([round(v, 4) if v is not None else None for v in center[:last]]))
    model = "rh-" + key if moisture else key
    rh_attribute = f' data-rh-model="{key}"' if moisture else ""
    chart.parts.append(f'<g data-model="{model}"{rh_attribute} data-history="saved-forecast" opacity=".48" data-label="{esc(label)} / saved forecast / {esc(statistic)}" data-unit="{esc(unit)}" data-values="{encoded}">')
    sample = lambda values: [values[i] for i in indices] if indices is not None else values
    if low and high:
        chart.parts.append('<g class="' + ('rh-band' if moisture else 'comparison-band') + '">')
        chart.band(sample(low), sample(high), color, .10)
        chart.parts.append('</g>')
    chart.parts.append('<g stroke-dasharray="2 4">')
    chart.line(sample(center), color, 1.5, compact=True)
    chart.parts.append('</g></g>')


def _comparison_charts(snapshot: dict, models: list[dict], times: list[datetime], window: tuple[int, int], now: datetime, history=None) -> str:
    """One timestamp-aligned chart per variable; no pooling of statistics."""
    from .event_ensemble import weathernext3_diagnostic
    wn3_ok = weathernext3_diagnostic(snapshot, now)["available"]
    forecast = snapshot["weathernext3"]["data"]["forecast"] if wn3_ok else {}
    wn2 = snapshot.get("weathernext2", {})
    wn2_hourly = wn2["data"]["hourly"] if wn2.get("ok") else {}
    gfs = gfs_status(snapshot, now)
    gfs_data = snapshot["gfs"]["data"] if gfs["available"] else {}
    gfs_hourly = gfs_data.get("hourly", {})
    gfs_axis = {parse_time(t): i for i, t in enumerate(gfs_hourly.get("time", []))}
    names = {m["key"]: m["model"] for m in models}
    if gfs["available"]:
        names["gfs"] = "GFS operational (deterministic)"
    if wn2_hourly:
        names["wn2"] = "WeatherNext 2"
    if wn3_ok:
        names["wn3"] = "WeatherNext 3"
    for key, source in (history or {}).get("sources", {}).items():
        if key in MODEL_COLORS:
            names.setdefault(key, source["label"])
    today = parse_time(snapshot["collected_at"]).astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    controls = ''.join(f'<label><input id="compare-{key}" type="checkbox" checked><span class="swatch" style="--c:{MODEL_COLORS[key]}"></span>{esc(name)}</label>' for key, name in names.items())
    from .chart_retention import render_retention
    retained_note = render_retention(snapshot)
    body = (f'<section id="multimodel-comparison" class="multimodel-comparison" data-forecast-today="{today:%Y-%m-%dT%H:%M:%SZ}"><h2>Multimodel comparison</h2>{retained_note}'
            '<p class="comparison-intro">Gold = forecast context window · Eastern time. Pan any forecast chart to move all forecast charts together; y-axes stay fixed. Charts run from today through the checkride and its following day.</p>'
            '<div class="forecast-controls" role="group" aria-label="Forecast time view"><button type="button" data-forecast-view="today">Today</button><button type="button" data-forecast-view="checkride">Center checkride</button><button type="button" data-forecast-view="full">Full date range</button></div>'
            '<fieldset class="comparison-controls"><legend>Show models / uncertainty</legend>' + controls +
            '<label><input id="compare-bands" type="checkbox" checked>Show ensemble ranges / SD</label></fieldset>'
            f'<p class="chart-note">Lines: ensemble medians · WN3 mean (magenta){" · WN2 mean (dashed)" if wn2.get("ok") else ""} · GFS operational (bold dark line, no band). Ranges shown by default; deselect models to compare spread.</p><details class="chart-reading"><summary>Reading the ranges &amp; chart controls</summary><p>GFS operational: one deterministic forecast, not the GEFS control or ensemble median; no uncertainty band. GFS values after 120 hours are interpolated from native 3-hourly output, not added timing skill. Conventional ensembles: Median (p50), thin solid line; p10–p90 bands. WN3: mean, bold magenta line; p10–p90 bands. {"WN2: dashed mean; Mean ±1 SD (standard deviation), not percentiles or a probability interval. SD bands may extend beyond physical bounds." if wn2.get("ok") else ""} Percentile ranges are p10–p90, not full ensemble minima/maxima. Hourly interpolation does not create hourly forecast skill. Models are not pooled; axes stay fixed when hidden. Hover a point for its exact UTC timestamp and value. Use Tab and Space to toggle models and ranges.</p></details>')
    if history:
        sampled = history.get('provenance', {}).get('truncated')
        body += ('<p class="chart-note"' + (' data-history-coverage="sampled"' if sampled else '') + '>Earlier saved forecasts—not observations: faded dotted lines keep their original statistics; gaps mean unavailable saved guidance.'
                 + (' Coverage is sampled within archive limits.' if sampled else '') + '</p>')
    if gfs["available"]:
        body += _gfs_window(gfs_hourly, times[window[0]], times[window[1]])
        datasets = gfs_data.get("metadata", {}).get("datasets", {})
        advertised = '; '.join(f'{key}: {item["latest_advertised_init"]}' for key, item in datasets.items()) or 'unavailable'
        from .source_presentation import is_direct, source_description
        if is_direct(gfs_data):
            body += ('<details class="chart-reading"><summary>GFS source and cycle provenance</summary><p>'
                     + esc(source_description(gfs_data)) + ' Fetched: ' + esc(gfs_data['fetched_at'])
                     + '. No HRRR/seamless blend or GEFS control substitution. One scenario, not a flight-completion probability.</p></details>')
        else:
            body += ('<details class="chart-reading"><summary>GFS source and cycle provenance</summary><p>NOAA NCEP operational GFS via Open-Meteo, explicitly selected as gfs_global; no HRRR/seamless blend. Fetched: '
                     + esc(gfs_data["fetched_at"]) + '. Latest advertised initialization: ' + esc(advertised)
                     + '. The rolling point response is not immutably bound to these cycles. One scenario, not an impact or flight-completion probability.</p></details>')
    else:
        body += '<p class="gfs-window">GFS operational unavailable: ' + esc(gfs.get("error", "not collected")) + '. Other model guidance remains independent.</p>'
    wn3_axis = {parse_time(t): i for i, t in enumerate(forecast.get("valid_time_utc", []))}
    if wn3_ok and any(t not in wn3_axis for t in times):
        body += '<p>WN3 covers the event window but not the full display; missing surrounding hours remain gaps, never interpolated or bridged.</p>'
    for variable, short, title, unit, wn3_field, factor in (
        ("precipitation", "rain", "Hourly precipitation", "mm / preceding hour", "precipitation_1h", 1),
        ("wind_speed_10m", "wind", "Sustained wind at 10 m", "kt", "wind_speed_10m", 3600 / 1852),
        ("cloud_cover_low", "cloud", "Low-cloud fraction — not ceiling height", "%", "low_cloud_cover", 1),
        ("pressure_msl", "pressure", "Sea-level pressure", "hPa", "sea_level_pressure", .01),
        ("temperature_2m", "temperature", "Temperature", "°C", "temperature_2m", 1),
        ("wind_gusts_10m", "gust", "Wind gusts", "kt", None, 1),
    ):
        series, missing = [], []
        for m in models:
            fan = m["hourly"].get(variable)
            if not fan or not fan.get("members_with_data"):
                missing.append(m["model"])
                continue
            series.append((m["key"], *(_aligned(m["hourly"], fan[stat], times) for stat in ("p50", "p10", "p90")), "Median (p50) / p10–p90"))
        if wn2_hourly:
            mean, spread = wn2_hourly.get(variable), wn2_hourly.get(variable + "_spread")
            if mean is not None and spread is not None:
                mean, spread = _aligned(wn2_hourly, mean, times), _aligned(wn2_hourly, spread, times)
                series.append(("wn2", mean, [a-b if a is not None and b is not None else None for a,b in zip(mean, spread)], [a+b if a is not None and b is not None else None for a,b in zip(mean, spread)], "Mean ±1 SD"))
            else:
                missing.append("WeatherNext 2")
        if wn3_ok and wn3_field:
            field = forecast["fields"].get(wn3_field)
            if field:
                mean, low, high = ([field[stat][wn3_axis[t]] * factor if t in wn3_axis else None for t in times] for stat in ("mean", "p10", "p90"))
                series.append(("wn3", mean, low, high, "Mean / p10–p90"))
        if gfs["available"]:
            values = gfs_hourly.get(variable, [])
            aligned = [values[gfs_axis[t]] if t in gfs_axis and values else None for t in times]
            if any(v is not None for v in aligned):
                series.append(("gfs", aligned, [], [], "Deterministic / no uncertainty band"))
            else:
                missing.append("GFS operational")
        note = "Missing guidance is not benign weather. "
        if short == "rain":
            note += "Preceding-hour amounts, not cumulative rain or rain probability. "
        if short == "cloud":
            note += "Low-cloud fraction is not ceiling height. "
        if short == "gust":
            note += "WN3: no gust field available. "
        if missing:
            note += "Field unavailable: " + ", ".join(missing) + "."
        saved = [row for row in _history_series(snapshot, history, variable, times, {row[0]: row[1] for row in series}) if row[0] in MODEL_COLORS]
        if not series and not saved:
            body += f'<div data-comparison-field="{short}"><h3>{esc(title)}</h3><p>{esc(note)} No usable series.</p></div>'
            continue
        lo, hi = _extent(*(values for _, mean, low, high, _ in series + saved for values in (mean, low, high)))
        chart = Chart(times, lo, max(hi, lo + .1), window)
        legend = []
        for key, mean, low, high, statistic in series:
            color = MODEL_COLORS[key]
            values = encode_chart_values([round(v, 4) if v is not None else None for v in mean])
            chart.parts.append(f'<g data-model="{key}" data-label="{esc(names[key] + " / " + statistic)}" data-unit="{esc(unit)}" data-values="{esc(values)}"><title>{esc(names[key] + " / " + statistic)}</title>')
            if key != "gfs":
                chart.parts.append('<g class="comparison-band">')
                chart.band(low, high, color, .14)
                chart.parts.append('</g>')
            chart.line(mean, color, 3.2 if key in ("wn3", "gfs") else 1.8, dashed=key == "wn2")
            chart.parts.append('</g>')
            legend.append((names[key] + " / " + statistic, color, key == "wn2"))
        for row in saved:
            _saved_group(chart, row, names[row[0]], MODEL_COLORS[row[0]], unit)
        body += f'<div data-comparison-field="{short}">' + chart.render(title, unit, note, legend, _ticks(lo, max(hi, lo + .1))) + '</div>'
    return body + _deterministic_charts(snapshot, times, window, now, gfs_hourly if gfs["available"] else {}) + '</section>'


def _deterministic_charts(snapshot, times, window, now, gfs_hourly) -> str:
    """NWS grid, NBM and single-run models on their own charts; no ensemble bands."""
    from .event_gusts import DASHED, MODELS as RUNS, NWS_COLOR, domain, nws_series, validate_gusts
    try:
        packet = validate_gusts(snapshot.get("event_gusts"), snapshot)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        return ""
    lo, _ = domain(snapshot)
    colors = {key: color for key, *_, color in RUNS}
    series = []
    try:
        nws = nws_series(snapshot, now, times)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        nws = None
    if nws:
        series.append(("det-nws", "NWS grid (from NBM)", NWS_COLOR, nws["wind"], nws["gust"]))
    for row in packet["models"]:
        if row["ok"]:
            index = {lo + timedelta(hours=i): i for i in range(len(row["gust"]))}
            align = lambda values: [values[index[t]] if t in index else None for t in times]
            series.append(("det-" + row["key"], f'{row["label"]} · {parse_time(row["run"]):%b %-d %HZ}', colors[row["key"]], align(row["wind"]), align(row["gust"])))
    if gfs_hourly:
        series.append(("gfs", "GFS operational", MODEL_COLORS["gfs"], _aligned(gfs_hourly, gfs_hourly.get("wind_speed_10m", []), times), _aligned(gfs_hourly, gfs_hourly.get("wind_gusts_10m", []), times)))
    dashed = {"det-" + key for key in DASHED}
    controls = "".join(f'<label><input id="compare-{key}" type="checkbox" checked><span class="swatch{" dashed" if key in dashed else ""}" style="--c:{color}"></span>{esc(label)}</label>'
                       for key, label, color, *_ in series if key != "gfs")
    body = ('<h3 id="deterministic-wind">Gusts and wind · NWS grid, NBM blend and single-run models</h3>'
            '<p class="comparison-intro">One line per deterministic run (NBM is the statistical blend NWS grids start from); no uncertainty bands. The GFS checkbox above also controls GFS here.</p>'
            '<fieldset class="comparison-controls"><legend>Show deterministic sources</legend>' + controls + '</fieldset>')
    missing = [row["label"] for row in packet["models"] if not row["ok"]]
    for position, title, note in ((4, "Wind gusts · deterministic and blend", "Each model's own gust diagnostic at hourly samples; NWS grid values hold for each grid interval. "),
                                  (3, "Sustained wind · deterministic and blend", "Hourly 10 m sustained wind. ")):
        lo_v, hi_v = _extent(*(s[position] for s in series))
        chart = Chart(times, lo_v, max(hi_v, lo_v + .1), window)
        for key, label, color, *values in series:
            data = values[position - 3]
            if not any(v is not None for v in data):
                continue
            chart.parts.append(f'<g data-model="{key}" data-label="{esc(label)}" data-unit="kt" data-values="{esc(encode_chart_values([round(v, 2) if v is not None else None for v in data]))}"><title>{esc(label)}</title>')
            chart.line(data, color, 3.0 if key in ("det-nws", "gfs") else 1.8, dashed=key in dashed)
            chart.parts.append('</g>')
        legend = [(label, color, key in dashed) for key, label, color, *values in series if any(v is not None for v in values[position - 3])]
        text = note + "Not probabilities or votes; missing is unknown, not calm." + (" No covering run: " + ", ".join(missing) + "." if missing else "")
        body += f'<div data-comparison-field="det-{"gust" if position == 4 else "wind"}">' + chart.render(title, "kt", text, legend, _ticks(lo_v, max(hi_v, lo_v + .1))) + '</div>'
    return body


def _wn3_numbers(snapshot: dict, times: list[datetime], window: tuple[int, int], now: datetime, history=None) -> str:
    """WN3 fields absent from the shared charts, plus event-window numbers."""
    from .event_ensemble import weathernext3_diagnostic
    if not weathernext3_diagnostic(snapshot, now)["available"]:
        return ""
    forecast = snapshot["weathernext3"]["data"]["forecast"]
    axis = [parse_time(t) for t in forecast["valid_time_utc"]]
    event = Event(**snapshot["event"])
    start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
    end = start + timedelta(hours=event.end_hour - event.start_hour)
    rain_indices = [i for i, t in enumerate(axis) if start < t <= end]
    instant_indices = [i for i, t in enumerate(axis) if start <= t < end]
    fields = forecast["fields"]
    rain = sum(fields["precipitation_1h"]["mean"][i] for i in rain_indices)
    wind = max(fields["wind_speed_10m"]["mean"][i] for i in instant_indices) * 3600 / 1852
    temperature = [fields["temperature_2m"]["mean"][i] for i in instant_indices]
    pressure = min(fields["sea_level_pressure"]["mean"][i] for i in instant_indices) / 100
    cloud = [fields["low_cloud_cover"]["mean"][i] for i in instant_indices]
    numbers = ('<details><summary>Forecast-context numerical summary</summary><div class="planning-signals">'
            f'<article><h3>Mean event rainfall</h3><p>{rain:.2f} mm</p><small>Sum of hourly ensemble means over the forecast context window.</small></article>'
            f'<article><h3>Peak hourly mean wind</h3><p>{wind:.1f} kt</p><small>Maximum of sampled hourly means, not the mean of member maxima.</small></article>'
            f'<article><h3>Mean temperature range</h3><p>{min(temperature):.1f}–{max(temperature):.1f} °C</p></article>'
            f'<article><h3>Peak hourly mean low cloud</h3><p>{max(cloud):.0f}%</p><small>Fractional coverage, not ceiling height.</small></article>'
            f'<article><h3>Lowest hourly mean pressure</h3><p>{pressure:.1f} hPa</p></article></div>'
            '<p>Hourly p10–p90 bands are marginal model percentiles, not event-total percentiles or flyability probabilities. The central line is the mean, not the median; a skewed mean can lie outside the band. Gold shading marks the forecast context window, not a confirmed flight duration. Ceiling, visibility, gust and convection fields are not available in this WN3 surface set.</p>')
    body = ('<section id="wn3-numbers" class="wn3-focus"><h3>Additional WeatherNext 3 fields</h3>'
            '<p>Mean and hourly p10–p90. Rain, wind, low cloud, pressure and temperature appear in the shared charts.</p>')
    positions = {t: i for i, t in enumerate(axis)}
    if any(t not in positions for t in times):
        body += '<p>Only available forecast hours are drawn; missing surrounding hours remain gaps.</p>'
    for field, short, label, unit, factor in (
        ('dewpoint_temperature_2m', 'dewpoint', 'Dew point', '°C', 1),
        ('total_cloud_cover', 'total-cloud', 'Total cloud fraction', '%', 1),
    ):
        mean, low, high = ([fields[field][stat][positions[t]] * factor if t in positions else None for t in times] for stat in ('mean', 'p10', 'p90'))
        saved = [row for row in _history_series(snapshot, history, field, times, {"wn3": mean}) if row[0] == "wn3"]
        lo, hi = _extent(mean, low, high, *(values for row in saved for values in row[1:4]))
        chart = Chart(times, lo, max(hi, lo + .1), window)
        for row in saved:
            _saved_group(chart, row, "WeatherNext 3", MODEL_COLORS['wn3'], unit)
        values = encode_chart_values([round(v, 4) if v is not None else None for v in mean])
        chart.parts.append(f'<g data-model="wn3" data-label="WeatherNext 3 / mean" data-unit="{esc(unit)}" data-values="{esc(values)}">')
        chart.parts.append('<g class="comparison-band">')
        chart.band(low, high, MODEL_COLORS['wn3'], .22)
        chart.parts.append('</g>')
        chart.line(mean, MODEL_COLORS['wn3'], 3.2)
        chart.parts.append('</g>')
        body += f'<div data-wn3-field="{short}">' + chart.render('WN3 / ' + label, unit,
            'Mean line and hourly p10–p90 range. A skewed mean may lie outside the band; percentiles do not establish flight suitability.',
            [('WeatherNext 3 / mean and p10–p90', MODEL_COLORS['wn3'], False)], _ticks(lo, max(hi, lo + .1))) + '</div>'
    return body + numbers + '</details></section>'


def _planning_panel(snapshot: dict, now: datetime) -> tuple[str, dict, str]:
    from .event_ensemble import weathernext3_diagnostic
    diagnostic = weathernext3_diagnostic(snapshot, now)
    event = Event(**snapshot["event"])
    start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
    long_range = start - now > timedelta(hours=48)
    fallback = ("ECMWF AIFS-ENS" if snapshot["models"].get("aifs_ens", {}).get("ok") else
                "Google WeatherNext 2 (legacy)" if snapshot.get("weathernext2", {}).get("ok") else
                "available conventional ensembles (AI guidance unavailable)")
    preferred = ("Google WeatherNext 3" if diagnostic["available"] else fallback) if long_range else "official near-term aviation guidance"
    body = f'<p class="eyebrow">Event-specific diagnostic / beyond-48-hour preference</p><h2>Checkride planning caution</h2><p><strong>Preferred source: {esc(preferred)}</strong></p>'
    if diagnostic["available"]:
        body += (f'<p class="planning-caution">{esc(diagnostic["caution"])}</p><div class="planning-signals">'
                 f'<article><h3>Precipitation</h3><p>Model mean: {esc(diagnostic["precipitation_mean"])}.</p><p>Percentile signal: {esc(diagnostic["precipitation_signal"])}.</p></article>'
                 f'<article><h3>Sustained wind</h3><p>Model mean: {esc(diagnostic["wind_mean"])}.</p><p>Percentile signal: {esc(diagnostic["wind_signal"])}.</p></article>'
                 f'<article><h3>Low-cloud fraction</h3><p>Model mean: {esc(diagnostic["low_cloud_mean"])}.</p><p>Percentile signal: {esc(diagnostic["low_cloud_signal"])}.</p></article></div>'
                 f'<p>Uncertainty: {esc(diagnostic["uncertainty"])}. Tight spread is not confidence in usable aviation conditions.</p>'
                 f'<p class="small">Google WeatherNext 3 actual response run: {esc(diagnostic["run"])}; requested run: {esc(diagnostic["requested_run"])}; fetched: {esc(diagnostic["fetched"])}; fallback: {esc(diagnostic["fallback"])}.</p>')
    else:
        body += f'<p class="planning-caution">WeatherNext 3 unavailable: {esc(diagnostic["reason"])}. Fallback: {esc(fallback)}. Missing guidance is not benign weather.</p>'
    aifs = snapshot["models"].get("aifs_ens", {})
    if aifs.get("ok"):
        w = aifs["data"]["window"]
        rain = w["rain_total_mm"]["median"] >= 2
        wind = w["wind_max_kt"]["median"] >= 15
        body += '<p>Independent ECMWF AIFS-ENS comparison: median member event rain ' + ('reaches' if rain else 'is below') + ' the rain trigger; median member peak wind ' + ('reaches' if wind else 'is below') + ' the wind trigger. Different statistics, not independent votes or a calibrated consensus.</p>'
    else:
        body += '<p>Independent ECMWF AIFS-ENS comparator unavailable; comparison uncertainty remains.</p>'
    body += ('<details><summary>Transparent planning thresholds and limitations</summary><p>These are conservative screening triggers, not aircraft limits or regulatory minima. Model-mean precipitation uses the sum of hourly means: trigger ≥2 mm in the event window. Model-mean wind and low cloud use the maximum sampled hourly mean: triggers ≥15 kt and ≥60%. Percentile signals inspect each hourly p10/p90 against ≥0.2 mm preceding-hour precipitation, ≥15 kt wind, or ≥60% low cloud; no hourly percentiles are summed. Broad uncertainty means at least one hourly p90–p10 gap ≥0.2 mm rain, ≥5 kt wind, or ≥30 percentage points low cloud; otherwise tight relative to these thresholds.</p><p>Rain uses preceding-hour endpoints strictly after the window start through its end. Instantaneous wind and cloud use samples from the opening hour up to but not including the closing hour. Low-cloud fraction is not ceiling height. This diagnostic is not an event probability. It provides no conclusion on ceiling/visibility, convection, gusts, crosswind, runway state or safe completion of a commercial checkride. Retain scheduling flexibility and obtain an official briefing and current observations/TAFs closer to the event.</p></details>'
             '<p class="small">Google attribution: Google Weather Lab. © 2024-5 Google LLC, whose machine learning models were used to create the experimental data made available under the following licence terms. This data is intended for experimental modelling only and is not intended, validated, or approved for real world use. This independent planning diagnostic is not endorsed by Google; forecast data are provided as is, without warranties, and are not an official aviation briefing. <a href="https://storage.googleapis.com/weathernext-public/terms-of-use.pdf">WeatherNext data license and terms</a>. Numerical plots show model means and hourly marginal percentiles, not calibrated flyability probabilities.</p>')
    return '<details id="wn3-diagnostic" class="planning-diagnostic"><summary>Screening diagnostic · thresholds, provenance &amp; limitations</summary>' + body + '</details>', diagnostic, preferred


def render(snapshot: dict, now: datetime | None = None, events_path: Path | str | None = None) -> tuple[str, dict]:
    from .event_ensemble import validate_snapshot
    validate_snapshot(snapshot)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    event = Event(**snapshot["event"])
    collected = parse_time(snapshot["collected_at"])
    models = _ok_models(snapshot)
    if not models:
        raise ValueError("event snapshot has no usable model")
    times = [parse_time(t) for t in models[0]["hourly"]["time"]]
    if any(m["hourly"]["time"] != models[0]["hourly"]["time"] for m in models):
        raise ValueError("models do not share one time axis")
    day = datetime.combine(event.day, datetime.min.time(), TZ)
    start, end = day + timedelta(hours=event.start_hour), day + timedelta(hours=event.end_hour)
    from .forecast_domain import forecast_times
    times = forecast_times(snapshot, models, times, now, start)
    # Saved forecasts for hours already past doubled the chart width and the comparison markup without informing the
    # event; charts now run from the collection day forward. Run-to-run change lives in the scorecard and trend sections.
    history = _validated_history(snapshot, now) if CHART_HISTORY else None
    cutoff = parse_time(snapshot["range"]["start"])
    historical = [parse_time(stamp) for source in (history or {}).get("sources", {}).values()
                  for i, stamp in enumerate(source["hourly"]["time"])
                  if parse_time(stamp) < cutoff and any(isinstance(raw, dict) and i < len(raw.get("center", [])) and raw["center"][i] is not None
                                                      for raw in source["hourly"].values())]
    if historical and min(historical) < times[0]:
        earliest = min(historical)
        while times[0] > earliest:
            times.insert(0, times[0] - timedelta(hours=1))
    window = (times.index(start), times.index(end))
    comparison = _comparison_charts(snapshot, models, times, window, now, history)
    from .event_moisture_view import render_moisture
    moisture_html = render_moisture(snapshot, times, window, now, history)
    trends = render_trends(snapshot.get("ensemble_trends"), snapshot["event"], now)
    recovered = render_run_history(snapshot.get("ensemble_run_history"), snapshot["event"], now)
    if recovered:
        fetch_history = trends.replace("ensemble-trends", "ensemble-fetch-history")
        legacy_history = '<details><summary>Earlier page-fetch history</summary>' + fetch_history + '</details>'
        trends = '<div id="ensemble-trends">' + recovered + legacy_history + '</div>'
    else:
        legacy_history = ''
    rows, provenance = [], []
    for m in models:
        cells = []
        for key in ("rain_total_mm", "wind_max_kt", "low_cloud_mean_percent", "pressure_min_hpa"):
            v = m["window"][key]
            cells.append("<td>Unavailable</td>" if v is None else f'<td>{v["median"]:g} <small>({v["p10"]:g}–{v["p90"]:g}); n={v["count"]}</small></td>')
        rows.append(f'<tr><th scope="row">{esc(m["model"])}</th>{"".join(cells)}</tr>')
        meta = m["metadata"]
        from .source_presentation import is_direct, source_description
        if is_direct(m):
            provenance.append(f'<li><strong>{esc(m["model"])}</strong> · {esc(source_description(m))} '
                              f'Fetched {esc(m["fetched_at"])}; grid {esc(m["grid_point"])}; '
                              f'{m["members"]} member identities. Missing native fields remain unknown.</li>')
        else:
            provenance.append(f'<li><strong>{esc(m["model"])} · {esc(m["model_id"])}</strong><br>Grid {esc(m["grid_point"])}; {m["members"]} member identities; {len(m["hourly"]["time"])} hourly timestamps.<br>Latest advertised init: {esc(meta.get("initialization_time", "unavailable"))}; availability: {esc(meta.get("availability_time", "unavailable"))}; data end: {esc(meta.get("data_end_time", "unavailable"))}; native metadata cadence: {esc(meta.get("native_timestep_hours", "unknown"))} h.<br>Metadata freshness: {"within 24 h" if meta.get("fresh") else "stale or unknown"}; advertised horizon covers display: {esc(meta.get("covers_display", False))}. Rolling point values cannot be bound to this exact cycle. {"Open-Meteo fallback: direct native source unavailable." if meta.get("direct_fallback_reason") else ""}</li>')
    failed = "".join(f'<li><strong>{esc(next((s.name for s in MODELS if s.key == k), k))}</strong> unavailable: {esc(v["error"])}</li>' for k,v in snapshot["models"].items() if not v["ok"])
    css = (Path(__file__).parent / "assets/fonts.css").read_text() + (Path(__file__).parent / "report.css").read_text() + (Path(__file__).parent / "context.css").read_text() + (Path(__file__).parent / "event.css").read_text()
    days = event.days_out(now)
    age = max(0, int((now - collected).total_seconds()))
    status = "stale" if age > STALE_AFTER else "provenance_unverified"
    health = {"generated_at": snapshot["collected_at"], "status": status, "age_seconds_at_render": age, "stale": age > STALE_AFTER, "stale_after": STALE_AFTER, "event": event.slug, "source_status": "provenance_unverified"}
    planning_panel, diagnostic, preferred = _planning_panel(snapshot, now)
    health.update({"gfs": gfs_status(snapshot, now), "weathernext3": diagnostic, "preferred_source": preferred})
    wn3_numbers = _wn3_numbers(snapshot, times, window, now, history)
    comparator = snapshot.get("weathernext2", {})
    wn = '<p>WeatherNext 2 mean/spread comparator unavailable: ' + esc(comparator.get("error", "not collected")) + '.</p>'
    if comparator.get("ok"):
        d = comparator["data"]
        wn = '<p>Secondary / legacy comparator: Google WeatherNext 2. See the screening diagnostic for the currently preferred source. Mean and standard deviation only; no individual members or probability conversion. Native 6-hour fields interpolated hourly.</p>'
        wn += '<p>Latest advertised metadata: ' + esc(d["metadata"]) + '. Rolling point response is not immutably bound to that run.</p>'
    wn = '' if comparator.get('retired') else '<h3>WeatherNext 2 / archived mean and spread comparator</h3>' + wn
    from .event_briefing import operational_briefing
    briefing = operational_briefing(snapshot, now)
    cards = ''.join(
        f'<article class="brief-card" data-tone="{esc(card["tone"])}"><h3>{esc(card["label"])}</h3>'
        f'<p class="brief-value">{esc(card["value"])}</p><p>{esc(card["detail"])}</p></article>'
        for card in briefing["cards"])
    freshness = "Stale snapshot · refresh before use" if health["stale"] else "Snapshot fetched"
    from .synoptic_context import render_context
    context_html = render_context(snapshot.get("synoptic_context", {}), start, end, now)
    try:
        narrative_html = render_event_narrative(snapshot, now)
    except Exception:
        narrative_html = UNAVAILABLE
    initializations_html = render_initializations(snapshot, now)
    from .synoptic_pattern import render_pattern
    pattern_html = render_pattern(snapshot, now)
    pattern_link = '<a href="#weather-pattern">Weather pattern</a>' if pattern_html else ''
    from .week_ahead import render_week
    week_html = render_week(snapshot, now)
    pattern_link += '<a href="#week-ahead">Week ahead</a>' if week_html else ''
    from .event_climatology import render_climatology
    climatology_html = render_climatology(snapshot, now)
    pattern_link += '<a href="#climatology">How it compares</a>' if climatology_html else ''
    from .event_wind_view import render_wind
    wind_html = render_wind(snapshot, now)
    wind_link = '<a href="#wind-analysis">Wind &amp; runways</a>' if wind_html else ''
    afd_html = render_afds(snapshot, now)
    from .event_model_matrix_view import render_matrix
    matrix_html = render_matrix(snapshot, now)
    from .event_wn2_members_view import render_wn2_members
    wn2_html = render_wn2_members(snapshot, now)
    navigation = (Path(__file__).parent / 'assets/forecast-navigation.js').read_text()
    from .coastal_maps import render as render_coastal_maps
    coastal_html = render_coastal_maps(snapshot.get('coastal_maps'), snapshot['event'])
    coastal_link = '<a href="#coastal-low">Coastal low</a>' if coastal_html else ''
    if coastal_html:
        navigation += '\n' + (Path(__file__).parent / 'assets/coastal-maps.js').read_text()
        css += (Path(__file__).parent / 'assets/coastal-maps.css').read_text()
    script_hash = base64.b64encode(hashlib.sha256(navigation.encode()).digest()).decode()
    source_introduction = ('Direct native sources are preferred; fallback packets are labeled separately. '
                          'NOAA/NCEP native data are public domain; ECMWF native data and Open-Meteo fallback data are CC BY 4.0. '
                          'Native interval rainfall distributed to display hours does not establish hourly timing. '
                          'Source, grid, and interpolation changes can contribute to differences from older snapshots.'
                          if snapshot.get('direct_native_version') else
                          'Open-Meteo rolling API data, CC BY 4.0; supplemental guidance, not an official aviation briefing. '
                          'Latest dataset metadata is not a cycle identifier for each returned value. '
                          'IFS 06/18Z short-cycle metadata can end before this display while the rolling extended response uses earlier long cycles. '
                          'Exact run attribution is unverified.')
    current_narrative = narrative_html != UNAVAILABLE and briefing['tone'] != 'stale'
    if current_narrative:
        brief_html = narrative_html
    else:
        brief_html = (f'<p class="eyebrow">Flight brief</p><h2 id="briefing-title">{esc(briefing["headline"])}</h2>'
                      f'<p class="brief-summary">{esc(briefing["summary"])}</p>'
                      f'<div class="next-check"><strong>{esc(briefing["next_check"]["title"])}</strong>'
                      f'<p>{esc(briefing["next_check"]["detail"])}</p></div>')
        if briefing['tone'] != 'stale':
            brief_html += '<p class="small">Written assessment unavailable; model evidence is below.</p>'
    brief_html += (f'<details class="brief-context"><summary>Full-day model context · {event.start_hour:02d}:00–{event.end_hour:02d}:00 Eastern</summary>'
                   f'<p class="small">These statistics cover the broader forecast context, not just the expected flight.</p>'
                   f'<div class="brief-cards">{cards}</div><p class="brief-source">{esc(briefing["source"])}</p></details>')
    if afd_html:
        brief_html += '<details class="report-detail"><summary>NWS forecaster excerpts</summary>' + afd_html + '</details>'
    cloud_html = ('<div class="evidence-group">' + render_low_cloud(snapshot, now)
                  + '<details class="report-detail"><summary>Humidity profiles &amp; charts</summary>' + moisture_html + '</details></div>')
    from .wn3_hourly import render_event as render_hourly_wn3
    model_detail = (render_hourly_wn3(snapshot, now)
                    + '<details class="report-detail"><summary>Model-run trends</summary>' + trends + '</details>'
                    + ('<details class="report-detail"><summary>Additional WN3 fields &amp; event totals</summary>' + wn3_numbers + '</details>' if wn3_numbers else '')
                    + ('<details class="report-detail"><summary>Independent WN2 member check</summary>' + wn2_html + '</details>' if wn2_html else ''))
    doc = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="script-src 'sha256-{script_hash}'; object-src 'none'; base-uri 'none'"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KCDW · {esc(event.title)} · {esc(event.day.strftime("%b %-d"))}</title><style>{css}</style></head>
<body data-page="event"><a class="skip-link" href="#main">Skip to briefing</a>
<header class="top"><div class="wrap"><a class="brand" href="/">KCDW / Field notes</a><nav class="top-links" aria-label="Main navigation"><a href="/">Current outlook</a><a href="{esc(event.path())}" aria-current="page">{esc(event.nav_label)}</a><a href="{esc(event.path())}/history">History ↗</a></nav></div></header>
<main id="main" class="wrap">
<header class="event-header"><div><p class="eyebrow">KCDW / Dated event briefing</p><h1>{esc(event.title)}</h1>{timing_header(snapshot, event)}</div><div class="event-meta"><p>{days} days out · {len(models)} of {len(MODELS)} systems</p><p class="freshness"><strong>{freshness}</strong><br><time datetime="{esc(snapshot['collected_at'])}">{esc(collected.astimezone(TZ).strftime('%b %-d, %H:%M %Z'))}</time> · {age // 3600}h {(age % 3600) // 60}m old at render</p></div></header>
<nav class="section-nav" aria-label="Briefing sections"><a href="#briefing">Flight brief</a>{pattern_link}<a href="#low-cloud-analysis">Cloud &amp; ceiling</a>{wind_link}<a href="#model-guidance">Model comparison</a>{coastal_link}<a href="#regional-guidance">Regional context</a><a href="#notes-sources">Sources</a></nav>
<section id="briefing" class="operational-briefing" data-tone="{esc(briefing['tone'])}" aria-label="Flight brief">{brief_html}</section>
{pattern_html}{week_html}{climatology_html}{cloud_html}{wind_html}
<div id="model-guidance" class="evidence-group">{matrix_html}{comparison}{model_detail}</div>
{coastal_html}
<details id="regional-guidance" class="report-detail"><summary>Regional outlooks &amp; tropical context</summary>{context_html}</details>
<section class="supporting-detail" aria-labelledby="detail-title"><h2 id="detail-title">Reference</h2>
<details id="window-distributions"><summary>Forecast context / per-model distributions</summary><p>Median (10th–90th percentile), with complete-member counts. Rain sums preceding-hour intervals ending after the opening time through the closing time. Conventional wind/cloud/pressure use those same sampled endpoints, not continuous extrema. Missing low cloud is unavailable, never favorable.</p><div class="chart-scroll" tabindex="0" role="region" aria-label="Per-model event distributions"><table><thead><tr><th scope="col">Model</th><th scope="col">Rain total · mm</th><th scope="col">Peak sustained · kt</th><th scope="col">Mean low cloud · %</th><th scope="col">Lowest pressure · hPa</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
{planning_panel}
<details id="sources-methods"><summary>Sources &amp; methods · model runs, gaps and licenses</summary><p>{esc(event.description)}</p>{initializations_html}<h3>Source provenance and gaps</h3><p>{esc(source_introduction)}</p><ul class="runs">{''.join(provenance)}{failed}</ul>{wn}<p>Point pressure cannot establish the absence of a hurricane, nearby storm, or convection. Low-cloud fraction cannot establish a usable maneuvers ceiling. Neither missing gusts nor low mean wind establishes runway/crosswind suitability. WeatherNext licensing and actual response-run metadata are retained in the screening diagnostic.</p></details></section>
<section class="official-guidance" aria-labelledby="official-title"><h2 id="official-title">Before departure</h2><p>Check current observations, TAFs, radar, advisories and NOTAMs against your flight and aircraft limits.</p><p><a href="/">Current 7-day outlook</a> · <a href="https://aviationweather.gov/">AWC observations, TAFs &amp; advisories</a> · <a href="https://www.weather.gov/okx/">NWS forecasts</a> · <a href="https://www.nhc.noaa.gov/">NHC tropical outlooks</a></p></section>
<footer class="site-footer"><span>Planning aid, not a go/no-go decision or official briefing.</span><a href="{esc(event.path())}/history">Guidance history ↗</a></footer></main><script data-forecast-script>{navigation}</script></body></html>'''
    return fit_page_budget(doc, legacy_history), health


def fit_page_budget(doc: str, optional_block: str, budget: int = PAGE_BUDGET) -> str:
    """Compact the page; if it still exceeds the publisher's HTML cap, drop the superseded, collapsed fetch history.

    The Cloudflare worker rejects HTML over 800,000 characters, which silently leaves the published page stale.
    """
    page = compact_notes(doc)
    if len(page) <= budget or not optional_block or doc.count(optional_block) != 1:
        return page
    return compact_notes(doc.replace(optional_block, '<p class="muted">Earlier page-fetch history omitted from this page to stay within '
                                     'the publication size limit; the recovered run history above is unaffected.</p>'))


def render_initializations(snapshot, now):
    if snapshot.get('initialization_provenance_version') != 1:
        return ''
    from .event_initialization import render_initializations as render_times
    return render_times(snapshot, now)


def summary(snapshot: dict) -> str:
    from .events import _event
    from .event_timing import timing_evidence
    event = _event(snapshot['event'])
    timing = timing_evidence(snapshot)
    context = f"Forecast context {event.start_hour:02d}:00–{event.end_hour:02d}:00 Eastern; ceiling/convection unresolved."
    if timing:
        end = (f"expected end around {timing['flight_end_local']} ({timing['flight_duration_minutes'] / 60:g} hours)"
               if timing['flight_end'] else "flight end unknown")
        schedule = f"{timing['appointment_start_local']} confirmed appointment; flight expected around {timing['flight_start_local']}; {end}. "
    else:
        schedule = "Appointment timing unconfirmed. "
    return f"Per-model weather distributions; no calibrated flyability probability. {len(_ok_models(snapshot))} member systems; {snapshot['days_out']} days out. {schedule}{context}"
