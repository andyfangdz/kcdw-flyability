from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import UTC, atomic_write, iso_z, load_json, parse_time
from .validation import validate_analysis

TZ = ZoneInfo("America/New_York")


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def score_class(score: int) -> str:
    if score >= 75: return "good"
    if score >= 50: return "mixed"
    if score >= 25: return "poor"
    return "bad"


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
    labels = {"nws_points":"NWS point metadata", "nws_hourly":"NWS hourly forecast", "nws_forecast":"NWS text forecast", "nws_grid":"NWS raw grid", "okx_afd":"NWS OKX AFD", "awc_metars":"AWC METAR history", "awc_tafs":"AWC proxy TAFs", "awc_convective_sigmets":"AWC Convective SIGMETs", "nws_alerts":"NWS active alerts", "spc_outlooks":"SPC convective outlooks", "open_meteo":"Open-Meteo model guidance"}
    rows = []
    for key, source in snapshot["sources"].items():
        status = "Available" if source["ok"] else "Unavailable"
        detail = source["fetched_at"] if source["ok"] else source.get("error", "source error")
        rows.append(f'<tr><th scope="row">{esc(labels.get(key,key))}</th><td><span class="status {"ok" if source["ok"] else "fail"}">{status}</span></td><td><time>{esc(detail)}</time></td></tr>')
    return "".join(rows)


def sunset_for(snapshot: dict, date: str) -> str | None:
    source = snapshot["sources"].get("open_meteo", {})
    daily = (source.get("data") or {}).get("daily", {})
    try:
        return dict(zip(daily["time"], daily["sunset"]))[date]
    except (KeyError, TypeError):
        return None


def render(snapshot: dict, analysis: dict, now: datetime | None = None) -> tuple[str, dict]:
    validate_analysis(analysis, snapshot)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    generated = parse_time(analysis["generated_at"])
    age = max(0, int((now - generated).total_seconds()))
    stale_after = 5400
    stale = age > stale_after
    day_map = {d["date"]: d for d in analysis["days"]}
    cards = []
    for date in snapshot["report_dates"]:
        day = day_map[date]
        dt = datetime.fromisoformat(date)
        sunset = sunset_for(snapshot, date)
        caveat = f"Sunset: {esc(sunset.split('T')[-1])} local; the 18–20 score remains meteorological." if sunset else "Sunset unavailable; the requested 18–20 block can extend past sunset and is scored meteorologically only."
        cell_items = []
        for window in day["windows"]:
            state = window_state(date, window["window"], now)
            state_label = '<em class="window-state">Now</em>' if state == "current" else ('<em class="window-state">Elapsed</em>' if state == "elapsed" else "")
            cell_items.append(f'<li class="score {score_class(window["score"])} {state}" data-window-state="{state}"><span class="window">{esc(window["window"])} {state_label}</span><strong>{window["score"]}%</strong><span>{esc(window["label"])}</span><small>{esc(window["reason"])}</small></li>')
        cells = "".join(cell_items)
        hazards = "".join(f"<li>{esc(h)}</li>" for h in day["hazards"]) or "<li>None singled out</li>"
        weekday = dt.strftime("%A")
        day_label = f"Today · {weekday}" if date == snapshot["local_date"] else weekday
        cards.append(f'''<article class="day-card" id="d-{date}"><header><div><p class="eyebrow">{esc(day_label)}</p><h2>{esc(dt.strftime("%b %-d"))}</h2></div><span class="confidence">{esc(day["confidence"].upper())} confidence</span></header><ul class="scores" aria-label="Two-hour flyability scores">{cells}</ul><p>{esc(day["narrative"])}</p><ul class="hazards">{hazards}</ul><p class="sunset">{caveat}</p></article>''')
    obs = "Current KCDW observation unavailable."
    metars = snapshot["sources"].get("awc_metars", {})
    if metars.get("ok"):
        for item in metars.get("data") or []:
            if item.get("icaoId") == "KCDW" or "KCDW" in item.get("rawOb", ""):
                obs = item.get("rawOb") or json.dumps(item, separators=(",", ":"))[:500]
                break
    badge = '<span class="badge stale">STALE · over 90 min</span>' if stale else '<span class="badge fresh">CURRENT</span>'
    hazards = "".join(f"<li>{esc(x)}</li>" for x in analysis["controlling_hazards"])
    best, backup = day_map[analysis["best_day"]], day_map[analysis["backup_day"]]
    css = '''*{box-sizing:border-box}body{margin:0;background:#071018;color:#e8f2f5;font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}a{color:inherit}.wrap{width:min(1180px,calc(100% - 24px));margin:auto}.top{position:sticky;top:0;z-index:2;background:#071018ee;border-bottom:1px solid #263944;backdrop-filter:blur(10px)}.top .wrap{display:flex;align-items:center;justify-content:space-between;padding:10px 0;gap:12px}.brand{font-weight:800;letter-spacing:.08em}.brand small{display:block;color:#8ca9b5;font-weight:500;letter-spacing:0}.badge,.confidence,.status{display:inline-block;padding:.2rem .55rem;border-radius:99px;font-size:.72rem;font-weight:800;letter-spacing:.05em}.fresh,.ok{background:#123e35;color:#91f1c7}.stale,.fail{background:#54232a;color:#ffb5bd}main{padding:22px 0 50px}.hero{display:grid;grid-template-columns:1.5fr 1fr;gap:14px}.panel,.day-card{background:#0e1b24;border:1px solid #263944;border-radius:16px;padding:18px}.eyebrow{color:#70bde5;text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;font-weight:800;margin:0}h1{font-size:clamp(1.8rem,6vw,3.6rem);line-height:1.05;margin:.2rem 0 1rem}h2{margin:0}.pick h2{margin:.2rem 0}.muted,.sunset{color:#9eb3bc}.picks{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pick{border-left:4px solid #61dda8;padding-left:12px}.pick.backup{border-color:#e8bd68}.legend{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}.legend span{padding:.3rem .65rem;border-radius:8px}.days{display:grid;gap:14px}.day-card>header{display:flex;justify-content:space-between;align-items:start}.scores{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;list-style:none;padding:0}.score{min-width:0;border-radius:10px;padding:10px;display:flex;flex-direction:column}.score strong{font-size:1.55rem}.score small{color:#d2e0e5;margin-top:6px}.window{font-weight:800;display:flex;justify-content:space-between;gap:6px}.window-state{font-style:normal;font-size:.65rem;letter-spacing:.05em;text-transform:uppercase}.score.elapsed{opacity:.46;filter:saturate(.55)}.score.current{outline:2px solid #70bde5;outline-offset:1px}.good{background:#124638}.mixed{background:#4d401d}.poor{background:#512f24}.bad{background:#50202b}.hazards{padding-left:20px;color:#f3cf8a}.confidence{background:#183448;color:#a8ddf5}details{margin-top:18px}.obs{font:13px/1.5 ui-monospace,SFMono-Regular,monospace;background:#061017;padding:12px;border-radius:8px;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px;border-bottom:1px solid #263944}.disclaimer{border-color:#76414a;background:#29181d}@media(max-width:760px){.hero,.picks{grid-template-columns:1fr}.scores{grid-template-columns:repeat(2,1fr)}.score strong{font-size:1.35rem}.top .updated{font-size:.75rem}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}@media print{body{background:#fff;color:#111}.top{position:static;background:#fff}.panel,.day-card{background:#fff;border-color:#aaa;break-inside:avoid}.score{border:1px solid #777;background:#fff!important}.badge,.confidence,.status{border:1px solid #777;background:#fff!important;color:#111!important}details{display:block}}'''
    doc = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>KCDW Flyability · 7-Day Outlook</title><style>{css}</style></head><body><header class="top"><div class="wrap"><div class="brand">KCDW <small>Flyability outlook</small></div><div class="updated">Updated <time datetime="{esc(analysis['generated_at'])}">{esc(generated.astimezone(TZ).strftime('%b %-d, %-I:%M %p %Z'))}</time> {badge}</div></div></header><main class="wrap"><section class="hero"><div class="panel"><p class="eyebrow">Today + six-day pattern-work outlook</p><h1>Weather windows,<br>graded for KCDW.</h1><p>{esc(analysis['summary'])}</p><div class="picks"><div class="pick"><span class="eyebrow">Best day</span><h2>{esc(datetime.fromisoformat(analysis['best_day']).strftime('%A, %b %-d'))}</h2><p>{esc(best['narrative'])}</p></div><div class="pick backup"><span class="eyebrow">Backup</span><h2>{esc(datetime.fromisoformat(analysis['backup_day']).strftime('%A, %b %-d'))}</h2><p>{esc(backup['narrative'])}</p></div></div></div><aside class="panel"><p class="eyebrow">Controlling hazards</p><ul class="hazards">{hazards}</ul><p class="muted">Percentages estimate comfortable flyability—not safety probability.</p></aside></section><div class="legend" role="group" aria-label="Score legend"><span class="good">75–95 · Favorable</span><span class="mixed">50–70 · Marginal</span><span class="poor">25–45 · Poor</span><span class="bad">0–20 · Unfavorable</span></div><section class="days" aria-label="Daily forecasts">{''.join(cards)}</section><section class="panel"><h2>Current KCDW observation</h2><p class="muted">AWC METAR; observations influence only near-term first-day windows.</p><p class="obs">{esc(obs)}</p><details><summary>Source status and freshness</summary><table><thead><tr><th>Source</th><th>Status</th><th>Fetched / error</th></tr></thead><tbody>{source_rows(snapshot)}</tbody></table></details><details><summary>Methodology</summary><p>Codex interprets a deterministic, bounded snapshot using a stable rubric. Python strictly validates seven dates, six two-hour windows, bounded text, and 5-point scores, then escapes all prose and atomically publishes this single file. NWS/AWC are authoritative sources; Open-Meteo is supplemental model guidance. KTEB and KEWR TAFs are local proxies because KCDW has no routine TAF.</p></details></section><section class="panel disclaimer"><h2>Safety disclaimer</h2><p>This planning aid is not an official weather briefing, dispatch recommendation, or go/no-go decision. Scores address meteorological comfort only and do not account for pilot, aircraft, runway, NOTAM, daylight, traffic, or operational limits. Obtain a current official briefing and apply personal minimums before flight.</p></section></main></body></html>'''
    health = {"generated_at": analysis["generated_at"], "status": "stale" if stale else "ok", "age_seconds_at_render": age, "stale": stale, "stale_after": 5400}
    return doc, health


def main(argv=None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("snapshot"); p.add_argument("analysis"); p.add_argument("--output", default="public/index.html"); p.add_argument("--health", default="public/health.json"); p.add_argument("--now", help="ISO timestamp for deterministic fixture renders"); args = p.parse_args(argv)
    snapshot, analysis = load_json(args.snapshot), load_json(args.analysis)
    doc, health = render(snapshot, analysis, parse_time(args.now) if args.now else None)
    atomic_write(args.output, doc + "\n"); atomic_write(args.health, json.dumps(health, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__": raise SystemExit(main())
