"""One mapping for assessment scores, categories, and display labels."""
BANDS = ((80, "strong", "Strong go"), (65, "probable", "Probably flyable"),
         (45, "tossup", "Toss-up"), (25, "unlikely", "Probably not"), (0, "nogo", "Practical no-go"))


def score_class(score):
    return next(css for minimum, css, label in BANDS if score >= minimum)


def score_label(score):
    return next(label for minimum, css, label in BANDS if score >= minimum)


def planning_windows(snapshot, date):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .common import parse_time
    windows = ("08-10", "10-12", "12-14", "14-16", "16-18", "18-20")
    if date not in snapshot["report_dates"][:3]:
        return []
    now = parse_time(snapshot["collected_at"])
    tz = ZoneInfo(snapshot["airport"]["timezone"])
    return [w for w in windows if datetime.fromisoformat(f"{date}T{w.split('-')[1]}:00:00").replace(tzinfo=tz) > now]
