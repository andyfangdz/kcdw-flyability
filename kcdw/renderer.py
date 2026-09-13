from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import UTC, atomic_write, iso_z, load_json, parse_time
from .validation import validate_analysis
from .scoring import BANDS, score_class, score_label
from .changes import compare
from .events import upcoming_events

TZ = ZoneInfo("America/New_York")


def event_nav_links(now: datetime, events_path=None) -> str:
    """Navigation links for dated-event pages; a missing or broken events file hides the links."""
    try:
        events = upcoming_events(now, events_path)
    except (OSError, ValueError):
        return ""
    return "".join(f'<a class="nav-event" href="{esc(event.path())}">{esc(event.nav_label)}</a>' for event in events[:4])


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def window_state(date: str, window: str, now: datetime) -> str:
    start_hour, end_hour = (int(value) for value in window.split("-"))
    local_now = now.astimezone(TZ)
    starts = datetime.fromisoformat(f"{date}T{start_hour:02d}:00:00").replace(tzinfo=TZ)
    ends = datetime.fromisoformat(f"{date}T{end_hour:02d}:00:00").replace(tzinfo=TZ)
    if local_now >= ends:
        return "elapsed"
    if local_now >= starts:
        return "current"
    return "upcoming"


def source_rows(snapshot: dict) -> str:
    labels = {"nbm_nbh":"NBM hourly guidance (24h / NBH)", "nbm_nbs":"NBM short-range guidance (72h / NBS)", "radar_mosaic":"NOAA/NWS MRMS radar loop", "nws_points":"NWS point metadata", "nws_hourly":"NWS hourly forecast", "nws_forecast":"NWS text forecast", "nws_grid":"NWS raw grid", "okx_afd":"NWS OKX AFD", "awc_metars":"AWC METAR history", "awc_tafs":"AWC proxy TAFs", "awc_convective_sigmets":"AWC Convective SIGMETs", "nws_alerts":"NWS active alerts", "spc_outlooks":"SPC convective outlooks", "weather_next3":"Google WeatherNext 3 point statistics", "weather_next":"Google WeatherNext 2 ensemble guidance", "aifs_ens":"ECMWF AIFS-ENS ensemble guidance", "open_meteo":"Open-Meteo model guidance"}
    labels.update({"phi_afd": "NWS Mount Holly (PHI) AFD", "bgm_afd": "NWS Binghamton (BGM) AFD",
                   "aly_afd": "NWS Albany (ALY) AFD", "box_afd": "NWS Boston/Norton (BOX) AFD",
                   "ctp_afd": "NWS State College (CTP) AFD"})
    rows = []
    for key, source in snapshot["sources"].items():
        status = "Available" if source["ok"] else "Unavailable"
        detail = source["fetched_at"] if source["ok"] else source.get("error", "source error")
        if key == "radar_mosaic" and source["ok"]:
            frames = (source.get("data") or {}).get("frames", [])
            if frames and isinstance(frames[-1], dict) and frames[-1].get("valid_at"):
                latest = frames[-1]
                proximity = ""
                if latest.get("local_echo_within_25_nm") is True:
                    proximity = "; displayed echo within 25 NM"
                elif latest.get("local_echo_within_50_nm") is True:
                    proximity = "; displayed echo within 50 NM"
                elif latest.get("local_echo_within_50_nm") is False:
                    proximity = "; no displayed echo within 50 NM"
                nearest = ""
                if latest.get("nearest_displayed_echo_nm") is not None:
                    nearest = f'; nearest displayed echo {latest["nearest_displayed_echo_nm"]} NM {latest.get("nearest_displayed_echo_direction", "")}'.rstrip()
                detail = f'Latest frame {latest["valid_at"]}{proximity}{nearest}; fetched {source["fetched_at"]}'
        elif key.endswith("_afd") and source["ok"]:
            data = source.get("data") or {}
            detail = f'Issued {data.get("issuanceTime", "unknown")}; fetched {source["fetched_at"]}'
        elif key in ("nbm_nbh", "nbm_nbs") and source["ok"]:
            data = source.get("data") or {}
            detail = f'Cycle {data.get("cycle_time", "unknown")}; fetched {source["fetched_at"]}'
        elif key == "weather_next3" and source["ok"]:
            data = (source.get("data") or {}).get("status", {})
            detail = f'Actual run {data.get("actual_run_utc", "unknown")}; upstream fetched {data.get("fetched_at", "unknown")}; mean/p10/p90; fallback {bool(data.get("fallback"))}; no cloud/ceiling/visibility/gust fields'
        elif key in ("weather_next", "aifs_ens") and source["ok"]:
            data = source.get("data") or {}
            if isinstance(data, dict) and data.get("initialization_time"):
                detail = f'Initialized {data["initialization_time"]}; available {data.get("availability_time", "unknown")}; fetched {source["fetched_at"]}'
        rows.append(f'<tr><th scope="row">{esc(labels.get(key,key))}</th><td><span class="status {"ok" if source["ok"] else "fail"}">{status}</span></td><td><time>{esc(detail)}</time></td></tr>')
    return "".join(rows)


def sunset_for(snapshot: dict, date: str) -> str | None:
    source = snapshot["sources"].get("open_meteo", {})
    daily = (source.get("data") or {}).get("daily", {})
    try:
        return dict(zip(daily["time"], daily["sunset"]))[date]
    except (KeyError, TypeError):
        return None


def render(snapshot: dict, analysis: dict, now: datetime | None = None, previous: dict | None = None) -> tuple[str, dict]:
    validate_analysis(analysis, snapshot)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    generated = parse_time(snapshot["collected_at"])
    age = max(0, int((now - generated).total_seconds()))
    stale_after = 5400
    stale = age > stale_after
    day_map = {d["date"]: d for d in analysis["days"]}
    categories = {d["date"]: d.get("outlook", score_label(max((w["score"] for w in d["windows"]), default=45))) for d in analysis["days"]}
    tones = {label: tone for _, tone, label in BANDS}
    cards, week = [], []
    for date in snapshot["report_dates"]:
        day = day_map[date]
        dt = datetime.fromisoformat(date)
        detailed = date in snapshot["report_dates"][:3]
        sunset = sunset_for(snapshot, date)
        caveat = f"Sunset: {esc(sunset.split('T')[-1])} local; the 18–20 score remains meteorological." if sunset else "Sunset unavailable; the requested 18–20 block can extend past sunset and is scored meteorologically only."
        cell_items = []
        for window in sorted(day["windows"], key=lambda w: w["window"]) if detailed else []:
            state = window_state(date, window["window"], now)
            state_label = '<em class="window-state">Now</em>' if state == "current" else ('<em class="window-state">Elapsed</em>' if state == "elapsed" else '<em class="window-state"></em>')
            cell_items.append(f'<li class="score {score_class(window["score"])} {state}" data-window-state="{state}" data-date="{esc(date)}" data-window="{esc(window["window"])}"><span class="window">{esc(window["window"])} {state_label}</span><strong>{esc(score_label(window["score"]))}</strong><small>{esc(window["reason"])}</small></li>')
        cells = f'<ul class="scores" aria-label="Two-hour flyability scores">{"".join(cell_items)}</ul>' if cell_items else f'<p class="outlook {tones[categories[date]]}">{esc(categories[date])}</p>'
        hazards = "".join(f"<li>{esc(h)}</li>" for h in day["hazards"]) or "<li>None singled out</li>"
        weekday = dt.strftime("%A")
        day_label = f"Today · {weekday}" if date == snapshot["local_date"] else weekday
        horizon = "Detailed windows · Eastern" if detailed else "Broad day outlook"
        cards.append(f'''<article class="day-card" id="d-{date}" data-date="{date}" data-weekday="{esc(weekday)}"><header><div><p class="eyebrow">{esc(day_label)}</p><h2>{esc(dt.strftime("%b %-d"))}</h2><p class="day-horizon">{horizon}</p></div><span class="confidence">{esc(day["confidence"])} confidence</span></header><div class="day-body">{cells}<p class="narrative">{esc(day["narrative"])}</p><p class="confidence-note"><strong>Confidence:</strong> {esc(day["confidence_reason"])}</p><section class="day-detail" aria-label="Hazards and daylight"><h3 class="field-label">Hazards &amp; daylight</h3><ul class="hazards">{hazards}</ul><p class="sunset">{caveat}</p></section></div></article>''')
        week.append(f'<a class="week-day {tones[categories[date]]}" href="#d-{date}" aria-label="{esc(weekday)}, {esc(dt.strftime("%b %-d"))}: {esc(categories[date])}"><span class="weekday">{esc(dt.strftime("%a"))}</span><span class="date-number">{dt.day:02d}</span><span class="category">{esc(categories[date])}</span><span class="arrow" aria-hidden="true">↘</span></a>')
    obs = "Current KCDW observation unavailable."
    metars = snapshot["sources"].get("awc_metars", {})
    if metars.get("ok"):
        for item in metars.get("data") or []:
            if item.get("icaoId") == "KCDW" or "KCDW" in item.get("rawOb", ""):
                obs = item.get("rawOb") or json.dumps(item, separators=(",", ":"))[:500]
                break
    badge = '<span id="freshness" class="badge stale">CHECK ASSESSMENT TIME</span>'
    hazards = "".join(f"<li>{esc(x)}</li>" for x in analysis["controlling_hazards"])
    picks = []
    for number, label, key in (("01", "Best day", "best_day"), ("02", "Backup", "backup_day")):
        date = analysis[key]
        picks.append(f'<a class="pick" href="#d-{date}"><span class="pick-number" aria-hidden="true">{number}</span><div><span class="eyebrow">{label}</span><h2>{esc(datetime.fromisoformat(date).strftime("%A, %b %-d"))}</h2><p>{esc(categories[date])} <span class="muted">/ {esc(day_map[date]["confidence"])} confidence</span></p></div><span class="pick-arrow" aria-hidden="true">↗</span></a>')
    change_data = compare(analysis, previous, now)
    change_items = "".join(f'<li><strong>{esc(item["date"])} {esc(item["window"] or "")}: {esc(item["before"])} → {esc(item["after"])}</strong> {esc(item["reason"])}</li>' for item in change_data["items"][:6])
    change_html = f'<section><p class="section-number">02 / Assessment changes</p><h2>What changed?</h2><p class="muted">{esc(change_data["message"])}</p><ul class="changes-list">{change_items}</ul></section>'
    if len(change_data["items"]) > 6:
        change_html += f'<p class="muted">{len(change_data["items"]) - 6} additional changes retained in the run archive.</p>'
    css = (Path(__file__).parent / "assets/fonts.css").read_text() + (Path(__file__).parent / "report.css").read_text()
    freshness_script = (Path(__file__).parent / "freshness.js").read_text(encoding="utf-8")
    from .evidence import model_guidance_policy
    policy = model_guidance_policy(snapshot)
    preferred = policy["preferred_after_48h"] or "No validated long-range model available"
    guidance_html = f'<section class="model-policy" aria-label="Model guidance preference"><p class="eyebrow">Beyond 48 hours / Model guidance</p><h2>{esc(preferred)}</h2><p>AIFS-ENS supplies the independent comparison and fallback; WeatherNext 2 remains secondary. Official NWS forecasts, AWC products and radar retain priority. Missing WN3 cloud, ceiling, visibility and gust fields are not evidence of clear or calm conditions.</p></section>'
    if snapshot["sources"].get("weather_next3", {}).get("ok"):
        guidance_html += '<p class="muted">Source: Google Weather Lab. © 2024-5 Google LLC, whose machine learning models were used to create the experimental data made available under the following <a href="https://storage.googleapis.com/weathernext-public/terms-of-use.pdf">licence terms</a>. This data is intended for experimental modelling only and is not intended, validated, or approved for real world use. Displayed assessments use WeatherNext 3 numerical guidance alongside independent models and official weather products.</p>'
    legend = ''.join(f'<span class="{tone}">{label}</span>' for _, tone, label in BANDS)
    doc = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="description" content="A seven-day weather outlook for local VFR flying at Essex County Airport, KCDW. Compare planning windows, hazards and forecast confidence."><title>KCDW Flyability · 7-Day Outlook</title><style>{css}</style></head><body data-design="field-notes" data-assessed-at="{esc(snapshot['collected_at'])}" data-stale-after="{stale_after}"><a class="skip-link" href="#main">Skip to forecast</a><header class="top"><div class="wrap"><a class="brand" href="/" aria-label="KCDW Flyability home"><span class="brand-symbol" aria-hidden="true"></span><span class="brand-name">KCDW<small>Flyability / Field notes</small></span></a><nav class="top-links" aria-label="Main navigation"><a class="nav-current" href="/" aria-current="page">Current outlook</a>{event_nav_links(now)}<a href="/history">History ↗</a><div class="updated"><span class="update-label">Assessment issued</span><time datetime="{esc(snapshot['collected_at'])}">{esc(generated.astimezone(TZ).strftime('%b %-d · %-I:%M %p %Z'))}</time>{badge}</div></nav></div></header><main class="wrap" id="main"><section class="hero"><div class="hero-intro"><p class="eyebrow">Essex County Airport / Caldwell, NJ</p><h1>Your flying outlook.</h1><p class="hero-summary">{esc(analysis['summary'])}</p><div class="mission"><span>Local VFR pattern work</span><span>2-hour sessions</span><span>All times Eastern</span></div></div><aside class="watch"><p class="eyebrow">The briefing</p><h2>Watch items</h2><ol class="hazards">{hazards}</ol><p class="watch-foot">Planning prospects, not flight clearance.<br>Check current conditions before departure.</p></aside></section>{guidance_html}<section class="picks" aria-label="Recommended planning days">{''.join(picks)}</section><section class="week-overview" aria-label="Seven-day overview"><div class="section-head"><div><p class="section-number">01 / Your flying week</p><h2>This week</h2></div><p class="muted">Select a day for the details ↓</p></div><nav class="week-strip" aria-label="Jump to a forecast day">{''.join(week)}</nav><div class="legend" role="group" aria-label="Score legend">{legend}</div></section><section class="days" aria-label="Daily forecasts">{''.join(cards[:3])}<div class="outlook-intro"><h2>Further out.</h2><p>Days 4–7 / Broad scenarios; timing is less certain.</p></div><div class="broad-grid">{''.join(cards[3:])}</div></section><div class="support-grid">{change_html}<section><p class="section-number">03 / From the field</p><h2>Current KCDW observation</h2><p class="muted">AWC METAR. Observations inform the near-term outlook.</p><p class="obs">{esc(obs)}</p></section></div><details class="source-section"><summary>Source status and freshness</summary><div class="table-wrap"><table><thead><tr><th>Source</th><th>Status</th><th>Issued / fetched</th></tr></thead><tbody>{source_rows(snapshot)}</tbody></table></div></details><details class="source-section"><summary>How to read this outlook</summary><p>Categories describe planning prospects, not calibrated probabilities. The first three days show two-hour weather windows; later days describe broad scenarios. Confidence expresses uncertainty in the forecast, not pilot or aircraft capability.</p><p>NWS and AWC products guide the assessment. KTEB and KEWR TAFs are local proxies; KCDW has no routine TAF. WeatherNext 3 supplies preferred longer-range model guidance when fresh; AIFS-ENS is the independent comparison/fallback and WeatherNext 2 is secondary. Source gaps and model disagreement can reduce confidence.</p><p>Generated from a bounded weather snapshot, validated before publication. <a href="https://open-meteo.com/">Weather data by Open-Meteo.com</a> (CC BY 4.0); model sources Google WeatherNext 2 and ECMWF AIFS-ENS.</p></details><section class="disclaimer"><h2>Plan here.<br>Brief before you fly.</h2><p>This planning aid is not an official weather briefing, dispatch recommendation, or go/no-go decision. Assessments address meteorological comfort and do not account for pilot, aircraft, runway, NOTAM, daylight, traffic, or operational limits. Obtain a current official briefing and apply personal minimums before flight.</p></section><footer class="site-footer"><span>KCDW / Essex County Airport</span><span>Weather changes. Keep checking.</span></footer></main><noscript><p class="wrap">Live freshness requires JavaScript. Check the assessment timestamp before using this report.</p></noscript><script>{freshness_script}</script></body></html>'''
    health = {"generated_at": snapshot["collected_at"], "status": "stale" if stale else "ok", "age_seconds_at_render": age, "stale": stale, "stale_after": 5400}
    return doc, health


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("snapshot"); p.add_argument("analysis"); p.add_argument("--output", default="public/index.html"); p.add_argument("--health", default="public/health.json"); p.add_argument("--previous", help="previous successful analysis JSON"); p.add_argument("--now", help="ISO timestamp for deterministic fixture renders"); args = p.parse_args(argv)
    snapshot, analysis = load_json(args.snapshot), load_json(args.analysis)
    doc, health = render(snapshot, analysis, parse_time(args.now) if args.now else None, load_json(args.previous) if args.previous else None)
    atomic_write(args.output, doc + "\n"); atomic_write(args.health, json.dumps(health, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__": raise SystemExit(main())
