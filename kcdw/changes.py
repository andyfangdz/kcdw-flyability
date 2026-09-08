"""Compare only matching, still-actionable forecast windows."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .scoring import score_label


def compare(analysis, previous, now):
    if not previous:
        return {"previous_assessment_at": None, "items": [], "message": "First archived assessment; future updates will show changes here."}
    old_days = {day["date"]: day for day in previous.get("days", [])}
    local = now.astimezone(ZoneInfo("America/New_York"))
    items = []
    for day in analysis["days"]:
        old = old_days.get(day["date"])
        if not old:
            continue
        old_windows = {window["window"]: window for window in old.get("windows", [])}
        if day.get("outlook") and old.get("outlook") and day["outlook"] != old["outlook"] and not day["windows"]:
            items.append({"date": day["date"], "window": None, "before": old["outlook"], "after": day["outlook"], "reason": day["narrative"]})
        for window in sorted(day["windows"], key=lambda w: w["window"]):
            end = datetime.fromisoformat(f'{day["date"]}T{window["window"].split("-")[1]}:00:00').replace(tzinfo=local.tzinfo)
            before = old_windows.get(window["window"])
            if end <= local or not before or score_label(before["score"]) == score_label(window["score"]):
                continue
            items.append({"date": day["date"], "window": window["window"],
                          "before": score_label(before["score"]), "after": score_label(window["score"]),
                          "reason": window["reason"]})
        if day["date"] >= local.date().isoformat() and old.get("confidence") != day["confidence"]:
            items.append({"date": day["date"], "window": None, "before": f'{old.get("confidence")} confidence',
                          "after": f'{day["confidence"]} confidence', "reason": day["confidence_reason"]})
    for key in ("best_day", "backup_day"):
        before = previous.get(key)
        if before and before >= local.date().isoformat() and before != analysis[key]:
            items.insert(0, {"date": analysis[key], "window": None, "before": f'{key.replace("_", " ")}: {before}',
                             "after": analysis[key], "reason": next(d["narrative"] for d in analysis["days"] if d["date"] == analysis[key])})
    return {"previous_assessment_at": previous.get("source_collected_at"), "items": items,
            "message": "No category, confidence, or best/backup changes in comparable actionable windows." if not items else "Compared with the previous assessment. Reasons below describe the new assessment; they do not prove what caused a change."}
