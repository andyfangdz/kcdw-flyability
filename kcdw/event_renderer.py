"""Render a dedicated outlook page for one dated event from per-member ensemble guidance.

Charts are inline SVG built deterministically from the validated event snapshot.
No model prose is involved. Shared charts distinguish medians, means and
standard-deviation comparators alongside purpose-specific planning diagnostics.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from pathlib import Path

from .common import UTC, parse_time
from .events import TZ, Event
from .event_ensemble import MODELS

STALE_AFTER = 8 * 3600
MODEL_COLORS = {"gefs": "#b54a2b", "ecmwf_ens": "#1f5f8b", "aifs_ens": "#26764e", "geps": "#6b4f9e", "wn3": "#c02679", "wn2": "#9b6021"}
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 240, 48, 14, 14, 34


def esc(value) -> str:
    return html.escape(str(value), quote=True)


class Chart:
    def __init__(self, times: list[datetime], y_min: float, y_max: float, window: tuple[int, int]):
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
            if len(points) < 2:
                continue
            forward = " ".join(f"{self.x(i):.1f},{self.y(h):.1f}" for i, _, h in points)
            backward = " ".join(f"{self.x(i):.1f},{self.y(l):.1f}" for i, l, _ in reversed(points))
            self.parts.append(f'<polygon points="{forward} {backward}" fill="{fill}" fill-opacity="{opacity}" stroke="none"/>')

    def line(self, values: list, color: str, width: float = 1.8, dashed: bool = False) -> None:
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        for segment in self._segments(values):
            if len(segment) < 2:
                continue
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
            self.parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="{width}"{dash} stroke-linejoin="round"/>')

    def reference(self, value: float, label: str) -> None:
        if not self.y_min <= value <= self.y_max:
            return
        y = self.y(value)
        self.parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#182d2c" stroke-width=".8" stroke-dasharray="2 4"/>'
                          f'<text x="{W - PAD_R - 4}" y="{y - 4:.1f}" text-anchor="end" class="ref">{esc(label)}</text>')

    def render(self, title: str, unit: str, note: str, legend: list[tuple[str, str, bool]], y_ticks: list[float]) -> str:
        axes = []
        for value in y_ticks:
            if self.y_min <= value <= self.y_max:
                y = self.y(value)
                axes.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#d4dad2" stroke-width="1"/>'
                            f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{value:g}</text>')
        for index, moment in enumerate(self.times):
            local = moment.astimezone(TZ)
            if local.hour == 0:
                x = self.x(index)
                axes.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" stroke="#c9d0c7" stroke-width="1"/>')
                axes.append(f'<text x="{x + 5:.1f}" y="{H - 10}" class="tick">{esc(local.strftime("%a %b %-d"))}</text>')
            elif local.hour == 12:
                x = self.x(index)
                axes.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{H - PAD_B - 5}" y2="{H - PAD_B}" stroke="#a9b3a8" stroke-width="1"/>')
        start, end = self.window
        shade = f'<rect x="{self.x(start):.1f}" y="{PAD_T}" width="{self.x(end) - self.x(start):.1f}" height="{H - PAD_T - PAD_B}" fill="#e8b84a" fill-opacity=".28"/>'
        swatches = "".join(f'<span class="legend-item"><span class="swatch{" dashed" if dashed else ""}" style="--c:{color}"></span>{esc(label)}</span>' for label, color, dashed in legend)
        return (f'<figure class="chart"><figcaption><h3>{esc(title)}</h3><span class="unit">{esc(unit)}</span></figcaption>'
                f'<div class="chart-scroll" tabindex="0" role="region" aria-label="{esc(title)} chart; scroll horizontally on small screens"><svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}" preserveAspectRatio="none">{shade}{"".join(axes)}{"".join(self.parts)}</svg></div>'
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


def _comparison_charts(snapshot: dict, models: list[dict], times: list[datetime], window: tuple[int, int], now: datetime) -> str:
    """One timestamp-aligned chart per variable; no pooling of statistics."""
    from .event_ensemble import weathernext3_diagnostic
    wn3_ok = weathernext3_diagnostic(snapshot, now)["available"]
    forecast = snapshot["weathernext3"]["data"]["forecast"] if wn3_ok else {}
    wn2 = snapshot.get("weathernext2", {})
    wn2_hourly = wn2["data"]["hourly"] if wn2.get("ok") else {}
    names = {m["key"]: m["model"] for m in models}
    if wn2_hourly:
        names["wn2"] = "WeatherNext 2"
    if wn3_ok:
        names["wn3"] = "WeatherNext 3"
    controls = ''.join(f'<label><input id="compare-{key}" type="checkbox" checked><span class="swatch" style="--c:{MODEL_COLORS[key]}"></span>{esc(name)}</label>' for key, name in names.items())
    body = ('<section id="multimodel-comparison" class="multimodel-comparison"><h2>Multimodel comparison</h2>'
            '<p class="comparison-intro">Gold = provisional window · Eastern time · shared axes, separate models. Scroll charts horizontally on small screens.</p>'
            '<fieldset class="comparison-controls"><legend>Show models / uncertainty</legend>' + controls +
            '<label><input id="compare-bands" type="checkbox" checked>Show ensemble ranges / SD</label></fieldset>'
            '<p class="chart-note">Lines: ensemble medians · WN3 mean (magenta) · WN2 mean (dashed). Ranges shown by default; deselect models to compare spread.</p><details class="chart-reading"><summary>Reading the ranges &amp; chart controls</summary><p>Conventional ensembles: Median (p50), thin solid line; p10–p90 bands. WN3: mean, bold magenta line; p10–p90 bands. WN2: dashed mean; Mean ±1 SD (standard deviation), not percentiles or a probability interval. SD bands may extend beyond physical bounds. Percentile ranges are p10–p90, not full ensemble minima/maxima. Hourly interpolation does not create hourly forecast skill. Models are not pooled; axes stay fixed when hidden. Hover a point for its exact UTC timestamp and value. Use Tab and Space to toggle models and ranges.</p></details>')
    wn3_axis = {parse_time(t): i for i, t in enumerate(forecast.get("valid_time_utc", []))}
    if wn3_ok and any(t not in wn3_axis for t in times):
        body += '<p>WN3 covers the event window but not the full display; missing surrounding hours remain gaps, never interpolated or bridged.</p>'
    for variable, short, title, unit, wn3_field, factor in (
        ("precipitation", "rain", "Hourly precipitation", "mm / preceding hour", "precipitation_1h", 1),
        ("wind_speed_10m", "wind", "Sustained wind at 10 m", "kt", "wind_speed_10m", 3600 / 1852),
        ("cloud_cover_low", "cloud", "Low-cloud fraction — not ceiling height", "%", None, 1),
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
            series.append((m["key"], fan["p50"], fan["p10"], fan["p90"], "Median (p50) / p10–p90"))
        if wn2_hourly:
            mean, spread = wn2_hourly.get(variable), wn2_hourly.get(variable + "_spread")
            if mean is not None and spread is not None:
                series.append(("wn2", mean, [a-b for a,b in zip(mean, spread)], [a+b for a,b in zip(mean, spread)], "Mean ±1 SD"))
            else:
                missing.append("WeatherNext 2")
        if wn3_ok and wn3_field:
            field = forecast["fields"].get(wn3_field)
            if field:
                mean, low, high = ([field[stat][wn3_axis[t]] * factor if t in wn3_axis else None for t in times] for stat in ("mean", "p10", "p90"))
                series.append(("wn3", mean, low, high, "Mean / p10–p90"))
        note = "Missing guidance is not benign weather. "
        if short == "rain":
            note += "Preceding-hour amounts, not cumulative rain or rain probability. "
        if short == "cloud":
            note += "WN3: no cloud field available; low-cloud fraction is not ceiling height. "
        if short == "gust":
            note += "WN3: no gust field available. "
        if missing:
            note += "Field unavailable: " + ", ".join(missing) + "."
        if not series:
            body += f'<div data-comparison-field="{short}"><h3>{esc(title)}</h3><p>{esc(note)} No usable series.</p></div>'
            continue
        lo, hi = _extent(*(values for _, mean, low, high, _ in series for values in (mean, low, high)))
        chart = Chart(times, lo, max(hi, lo + .1), window)
        legend = []
        for key, mean, low, high, statistic in series:
            color = MODEL_COLORS[key]
            chart.parts.append(f'<g data-model="{key}"><title>{esc(names[key] + " / " + statistic)}</title><g class="comparison-band">')
            chart.band(low, high, color, .14)
            chart.parts.append('</g>')
            chart.line(mean, color, 3.2 if key == "wn3" else 1.8, dashed=key == "wn2")
            for i, value in enumerate(mean):
                if value is None:
                    continue
                timestamp = times[i].astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
                tooltip = f'{names[key]} / {statistic}: {value:.2f} {unit}; {timestamp}'
                chart.parts.append(f'<circle cx="{chart.x(i):.1f}" cy="{chart.y(value):.1f}" r="2.5" fill="{color}" fill-opacity=".15"><title>{esc(tooltip)}</title></circle>')
            chart.parts.append('</g>')
            legend.append((names[key] + " / " + statistic, color, key == "wn2"))
        body += f'<div data-comparison-field="{short}">' + chart.render(title, unit, note, legend, _ticks(lo, max(hi, lo + .1))) + '</div>'
    return body + '</section>'


def _wn3_numbers(snapshot: dict, times: list[datetime], window: tuple[int, int], now: datetime) -> str:
    """Dedicated WN3 mean/range charts, with optional event-window numbers."""
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
    numbers = ('<details><summary>Event-window numerical summary</summary><div class="planning-signals">'
            f'<article><h3>Mean event rainfall</h3><p>{rain:.2f} mm</p><small>Sum of hourly ensemble means over the provisional window.</small></article>'
            f'<article><h3>Peak hourly mean wind</h3><p>{wind:.1f} kt</p><small>Maximum of sampled hourly means, not the mean of member maxima.</small></article>'
            f'<article><h3>Mean temperature range</h3><p>{min(temperature):.1f}–{max(temperature):.1f} °C</p></article>'
            f'<article><h3>Lowest hourly mean pressure</h3><p>{pressure:.1f} hPa</p></article></div>'
            '<p>Hourly p10–p90 bands are marginal model percentiles, not event-total percentiles or flyability probabilities. The central line is the mean, not the median; a skewed mean can lie outside the band. Gold shading marks the provisional checkride window. No cloud, ceiling, visibility or gust field is available in this WN3 feed.</p>')
    body = ('<section id="wn3-numbers" class="wn3-focus"><p class="eyebrow">WeatherNext 3 / Ensemble charts</p>'
            '<h2>WN3 mean and ensemble range</h2><p>The magenta line is the ensemble mean; shading shows the hourly p10–p90 range, not the full ensemble minimum–maximum or event-total percentiles. Gold marks the provisional checkride window. WN3 also appears in the multimodel charts above. Swipe horizontally on small screens.</p>')
    positions = {t: i for i, t in enumerate(axis)}
    if any(t not in positions for t in times):
        body += '<p>Only available forecast hours are drawn; missing surrounding hours remain gaps.</p>'
    for field, short, label, unit, factor in (
        ('sea_level_pressure', 'pressure', 'Sea-level pressure', 'hPa', .01),
        ('wind_speed_10m', 'wind', 'Sustained wind', 'kt', 3600 / 1852),
        ('precipitation_1h', 'rain', 'Hourly precipitation', 'mm / preceding hour', 1),
        ('temperature_2m', 'temperature', 'Temperature', '°C', 1),
    ):
        mean, low, high = ([fields[field][stat][positions[t]] * factor if t in positions else None for t in times] for stat in ('mean', 'p10', 'p90'))
        lo, hi = _extent(mean, low, high)
        chart = Chart(times, lo, max(hi, lo + .1), window)
        chart.band(low, high, MODEL_COLORS['wn3'], .22)
        chart.line(mean, MODEL_COLORS['wn3'], 3.2)
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
                 f'<article><h3>Sustained wind</h3><p>Model mean: {esc(diagnostic["wind_mean"])}.</p><p>Percentile signal: {esc(diagnostic["wind_signal"])}.</p></article></div>'
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
    body += ('<details><summary>Transparent planning thresholds and limitations</summary><p>These are conservative screening triggers, not aircraft limits or regulatory minima. Model-mean precipitation uses the sum of hourly means: trigger ≥2 mm in the event window. Model-mean wind uses the maximum sampled hourly mean: trigger ≥15 kt. Percentile signals inspect each hourly p10/p90 against ≥0.2 mm preceding-hour precipitation or ≥15 kt wind; no hourly percentiles are summed. Broad uncertainty means at least one hourly p90–p10 gap ≥0.2 mm rain or ≥5 kt wind; otherwise tight relative to these thresholds.</p><p>Rain uses preceding-hour endpoints strictly after the window start through its end. Instantaneous wind uses samples from the opening hour up to but not including the closing hour. This diagnostic is not an event probability. It provides no conclusion on ceiling/visibility, convection, gusts, crosswind, runway state or safe completion of a commercial checkride. Retain scheduling flexibility and obtain an official briefing and current observations/TAFs closer to the event.</p></details>'
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
    window = (times.index(start), times.index(end))
    comparison = _comparison_charts(snapshot, models, times, window, now)
    rows, provenance = [], []
    for m in models:
        cells = []
        for key in ("rain_total_mm", "wind_max_kt", "low_cloud_mean_percent", "pressure_min_hpa"):
            v = m["window"][key]
            cells.append("<td>Unavailable</td>" if v is None else f'<td>{v["median"]:g} <small>({v["p10"]:g}–{v["p90"]:g}); n={v["count"]}</small></td>')
        rows.append(f'<tr><th scope="row">{esc(m["model"])}</th>{"".join(cells)}</tr>')
        meta = m["metadata"]
        provenance.append(f'<li><strong>{esc(m["model"])} · {esc(m["model_id"])}</strong><br>Grid {esc(m["grid_point"])}; {m["members"]} member identities; {len(times)} hourly timestamps.<br>Latest advertised init: {esc(meta.get("initialization_time", "unavailable"))}; availability: {esc(meta.get("availability_time", "unavailable"))}; data end: {esc(meta.get("data_end_time", "unavailable"))}; native metadata cadence: {esc(meta.get("native_timestep_hours", "unknown"))} h.<br>Metadata freshness: {"within 24 h" if meta.get("fresh") else "stale or unknown"}; advertised horizon covers display: {esc(meta.get("covers_display", False))}. Rolling point values cannot be bound to this exact cycle.</li>')
    failed = "".join(f'<li><strong>{esc(next((s.name for s in MODELS if s.key == k), k))}</strong> unavailable: {esc(v["error"])}</li>' for k,v in snapshot["models"].items() if not v["ok"])
    css = (Path(__file__).parent / "assets/fonts.css").read_text() + (Path(__file__).parent / "report.css").read_text() + (Path(__file__).parent / "event.css").read_text()
    days = event.days_out(now)
    age = max(0, int((now - collected).total_seconds()))
    status = "stale" if age > STALE_AFTER else "provenance_unverified"
    health = {"generated_at": snapshot["collected_at"], "status": status, "age_seconds_at_render": age, "stale": age > STALE_AFTER, "stale_after": STALE_AFTER, "event": event.slug, "source_status": "provenance_unverified"}
    planning_panel, diagnostic, preferred = _planning_panel(snapshot, now)
    health.update({"weathernext3": diagnostic, "preferred_source": preferred})
    wn3_numbers = _wn3_numbers(snapshot, times, window, now)
    comparator = snapshot.get("weathernext2", {})
    wn = '<p>WeatherNext 2 mean/spread comparator unavailable: ' + esc(comparator.get("error", "not collected")) + '.</p>'
    if comparator.get("ok"):
        d = comparator["data"]
        wn = '<p>Secondary / legacy comparator: Google WeatherNext 2. See the screening diagnostic for the currently preferred source. Mean and standard deviation only; no individual members or probability conversion. Native 6-hour fields interpolated hourly.</p>'
        wn += '<p>Latest advertised metadata: ' + esc(d["metadata"]) + '. Rolling point response is not immutably bound to that run.</p>'
    from .event_briefing import operational_briefing
    briefing = operational_briefing(snapshot, now)
    cards = ''.join(
        f'<article class="brief-card" data-tone="{esc(card["tone"])}"><h3>{esc(card["label"])}</h3>'
        f'<p class="brief-value">{esc(card["value"])}</p><p>{esc(card["detail"])}</p></article>'
        for card in briefing["cards"])
    freshness = "Stale snapshot · refresh before use" if health["stale"] else "Snapshot fetched"
    wn3_link = '<a href="#wn3-numbers">WN3 detail</a>' if wn3_numbers else ''
    doc = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KCDW · {esc(event.title)} · {esc(event.day.strftime("%b %-d"))}</title><style>{css}</style></head>
<body data-page="event"><a class="skip-link" href="#main">Skip to briefing</a>
<header class="top"><div class="wrap"><a class="brand" href="/">KCDW / Field notes</a><nav class="top-links" aria-label="Main navigation"><a href="/">Current outlook</a><a href="{esc(event.path())}" aria-current="page">{esc(event.nav_label)}</a><a href="{esc(event.path())}/history">History ↗</a></nav></div></header>
<main id="main" class="wrap">
<header class="event-header"><div><p class="eyebrow">KCDW / Dated event briefing</p><h1>{esc(event.title)}</h1><p class="event-when">{esc(event.label())}</p><p class="event-window">Provisional {start:%H:%M}–{end:%H:%M} Eastern · actual time not confirmed</p></div><div class="event-meta"><p>{days} days out · {len(models)} of {len(MODELS)} systems</p><p class="freshness"><strong>{freshness}</strong><br><time datetime="{esc(snapshot['collected_at'])}">{esc(collected.astimezone(TZ).strftime('%b %-d, %H:%M %Z'))}</time> · {age // 3600}h {(age % 3600) // 60}m old at render</p><p>Conventional cycle binding unverified</p></div></header>
<section id="briefing" class="operational-briefing" data-tone="{esc(briefing['tone'])}" aria-labelledby="briefing-title"><p class="eyebrow">At a glance</p><h2 id="briefing-title">{esc(briefing['headline'])}</h2><p class="brief-summary">{esc(briefing['summary'])}</p><div class="brief-cards">{cards}</div><div class="next-check"><strong>{esc(briefing['next_check']['title'])}</strong><p>{esc(briefing['next_check']['detail'])}</p></div><p class="brief-source">{esc(briefing['source'])}</p><p class="brief-limits">No calibrated flyability probability. Ceiling, visibility, convection and runway/crosswind suitability require an official aviation briefing.</p></section>
<nav class="section-nav" aria-label="Briefing sections"><a href="#briefing">Brief</a><a href="#multimodel-comparison">Compare models</a>{wn3_link}<a href="#sources-methods">Sources &amp; methods</a></nav>
{comparison}{wn3_numbers}
<section class="supporting-detail" aria-labelledby="detail-title"><h2 id="detail-title">Supporting detail</h2>
<details id="window-distributions"><summary>Provisional window / per-model distributions</summary><p>Median (10th–90th percentile), with complete-member counts. Rain sums preceding-hour intervals ending after the opening time through the closing time. Conventional wind/cloud/pressure use those same sampled endpoints, not continuous extrema. Missing low cloud is unavailable, never favorable.</p><div class="chart-scroll" tabindex="0" role="region" aria-label="Per-model event distributions"><table><thead><tr><th scope="col">Model</th><th scope="col">Rain total · mm</th><th scope="col">Peak sustained · kt</th><th scope="col">Mean low cloud · %</th><th scope="col">Lowest pressure · hPa</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
{planning_panel}
<details id="sources-methods"><summary>Sources &amp; methods · model runs, gaps and licenses</summary><p>{esc(event.description)}</p><h3>Source provenance and gaps</h3><p>Open-Meteo rolling API data, CC BY 4.0; supplemental guidance, not an official aviation briefing. Latest dataset metadata is not a cycle identifier for each returned value. IFS 06/18Z short-cycle metadata can end before this display while the rolling extended response uses earlier long cycles. Exact run attribution is unverified.</p><ul class="runs">{''.join(provenance)}{failed}</ul><h3>WeatherNext 2 / mean and spread comparator</h3>{wn}<p>Point pressure cannot establish the absence of a hurricane, nearby storm, or convection. Low-cloud fraction cannot establish a usable maneuvers ceiling. Neither missing gusts nor low mean wind establishes runway/crosswind suitability. WeatherNext licensing and actual response-run metadata are retained in the screening diagnostic.</p></details></section>
<section class="official-guidance" aria-labelledby="official-title"><h2 id="official-title">Near-term guidance</h2><p>Within 48 hours, prioritize official aviation guidance. Confirm issue times, valid periods and airport coverage; these links are not fetched by this page.</p><p><a href="/">Current 7-day outlook</a> · <a href="https://aviationweather.gov/">AWC observations, TAFs &amp; advisories</a> · <a href="https://www.weather.gov/okx/">NWS forecasts</a> · <a href="https://www.nhc.noaa.gov/">NHC tropical outlooks</a></p></section>
<footer class="site-footer"><span>Planning aid, not a go/no-go decision or official briefing.</span><a href="{esc(event.path())}/history">Guidance history ↗</a></footer></main></body></html>'''
    return doc, health


def summary(snapshot: dict) -> str:
    return f"Per-model weather distributions; no calibrated flyability probability. {len(_ok_models(snapshot))} member systems; {snapshot['days_out']} days out. Provisional 08–17 Eastern window; ceiling/convection unresolved."
