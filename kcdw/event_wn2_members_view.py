"""WeatherNext 2 member table for the event page; snapshot-only and fail-closed."""
from html import escape

from .common import parse_time
from .event_wn2_members import validate_members
from .events import TZ


def _range(counts):
    low, high = min(counts), max(counts)
    return str(low) if low == high else f'{low}–{high}'


def _fan(fan, unit=''):
    return f'{fan["p50"]:g}{unit}<small>{fan["p10"]:g}–{fan["p90"]:g}</small>'


def headline(packet):
    n = packet['members']
    if packet.get('version') == 2:
        return 'Independent WN2 member wind and rain check'
    cloudy = _range([s['cloudy']['count'] for s in packet['samples']])
    sector = _range([s['sector']['count'] for s in packet['samples']])
    rain = packet['rain']
    return f'{cloudy} of {n} members keep a low deck; {sector} from the northeast; rain in {rain["count"]} of {rain["n"]}'


def render_wn2_members(snapshot, now):
    packet = snapshot.get('weathernext2_members')
    if not packet:
        return ''
    try:
        validate_members(packet, snapshot)
        if packet['version'] == 2:
            return render_bigquery_members(packet)
        t = packet['thresholds']
        start, end = (parse_time(packet['window'][k]).astimezone(TZ) for k in ('start', 'end'))
        rows = ''.join(
            f'<tr><th scope="row">{parse_time(s["at"]).astimezone(TZ):%a %H:%M}</th>'
            f'<td>{s["cloudy"]["count"]} of {s["cloudy"]["n"]}</td><td>{_fan(s["low_cloud_pct"], "%")}</td>'
            f'<td>{_fan(s["wind_10m_kt"], " kt")}</td><td>{_fan(s["wind_100m_kt"], " kt")}</td>'
            f'<td>{s["sector"]["count"]} of {s["sector"]["n"]}</td><td>{_fan(s["pressure_hpa"], " hPa")}</td></tr>'
            for s in packet['samples'])
        rain = packet['rain']
        init = parse_time(packet['advertised_init'])
        return (f'<section id="wn2-members" class="model-matrix wn2-members" aria-labelledby="wn2-members-title">'
                f'<p class="eyebrow">WeatherNext 2 · {packet["members"]} members · {escape(packet["window"]["kind"])} {start:%H:%M}–{end:%H:%M} {end:%Z}</p>'
                f'<h2 id="wn2-members-title">{escape(headline(packet))}</h2>'
                f'<div class="table-wrap"><table><caption>Member counts and median (10th–90th percentile) at the model’s native six-hour '
                f'valid times bracketing the flight. Rain ≥ {t["rain_mm"]:g} mm inside the flight window: {rain["count"]} of {rain["n"]} members '
                f'(90th percentile {rain["p90_mm"]:g} mm). WeatherNext 2 has no gust field; 100 m wind is context, not a gust forecast.</caption>'
                f'<thead><tr><th scope="col">Valid (Eastern)</th><th scope="col">Low cloud ≥ {t["cloudy_pct"]}%</th><th scope="col">Low cloud</th>'
                f'<th scope="col">10 m wind</th><th scope="col">100 m wind</th>'
                f'<th scope="col">From {t["sector_from_deg"]:03d}–{t["sector_to_deg"]:03d}°</th><th scope="col">Sea-level pressure</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>'
                f'<p class="matrix-note">Open-Meteo rolling ensemble response (CC BY 4.0), latest advertised initialization {init:%b %-d %HZ}; '
                f'not bound to that run. Counts are threshold screens, not probabilities. The hourly charts below still plot the WeatherNext 2 mean.</p></section>')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        return ''


def render_bigquery_members(packet):
    t, rain = packet['thresholds'], packet['rain']
    start, end = (parse_time(rain[k]).astimezone(TZ) for k in ('start', 'end'))
    init = parse_time(packet['init_time'])
    rows = ''.join(
        f'<tr><th scope="row">{parse_time(s["at"]).astimezone(TZ):%a %H:%M}</th>'
        f'<td>{_fan(s["wind_10m_kt"], " kt")}</td><td>{_fan(s["wind_100m_kt"], " kt")}</td>'
        f'<td>{s["sector"]["count"]} of {s["sector"]["n"]}</td><td>{_fan(s["pressure_hpa"], " hPa")}</td></tr>'
        for s in packet['samples'])
    return (f'<section id="wn2-members" class="model-matrix wn2-members" aria-labelledby="wn2-members-title">'
            f'<p class="eyebrow">WeatherNext 2 · 64 members · independent comparison</p>'
            f'<h2 id="wn2-members-title">{headline(packet)}</h2>'
            f'<p>WN3 supplies the primary weather and cloud guidance above. WN2 provides this separate member check.</p>'
            f'<details><summary>View native six-hour member diagnostics</summary>'
            f'<div class="table-wrap"><table><caption>Median and 10th–90th percentiles at native six-hour times. '
            f'Rain ≥ {t["rain_mm"]:g} mm over {start:%a %b %-d %H:%M}–{end:%a %b %-d %H:%M %Z}: '
            f'{rain["count"]} of {rain["n"]} members (90th percentile {rain["p90_mm"]:g} mm). '
            f'This is the full six-hour interval or intervals enclosing the flight, not a flight-window rain estimate.</caption>'
            f'<thead><tr><th scope="col">Valid (Eastern)</th><th scope="col">10 m wind</th><th scope="col">100 m wind</th>'
            f'<th scope="col">From {t["sector_from_deg"]:03d}–{t["sector_to_deg"]:03d}°</th><th scope="col">Sea-level pressure</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            f'<p class="matrix-note"><a href="{escape(packet["source_url"], quote=True)}">Google WeatherNext 2 via BigQuery</a> '
            f'(CC BY 4.0), bound to the {init:%b %-d %HZ} initialization. Counts are threshold screens, not calibrated probabilities. '
            f'Calm winds do not count toward the direction sector. Small negative precipitation predictions are clipped to zero. '
            f'WN2 has no gust field; 100 m wind is context, not a gust forecast. The BigQuery member table has no cloud-cover field.</p></details></section>')
