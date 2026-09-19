"""Model scorecard: one dense, snapshot-only table for the expected flight window."""
from datetime import timedelta
from html import escape

from .common import parse_time
from .event_model_matrix import LATER_HOURS, READS, THRESHOLDS, TONE_RANK, classify, validate_matrix
from .events import TZ

CHANGES = {'better': '▲ Better', 'worse': '▼ Worse', 'steady': '● Steady', 'unknown': '— No earlier run'}
TONES = {'good': 'favorable', 'marginal': 'marginal', 'poor': 'unfavorable'}


def _feet(value):
    return f'~{value:,} ft'


def _span(values, suffix, fmt='{:g}'):
    low, high = min(values), max(values)
    return (fmt.format(low) if low == high else fmt.format(low) + '–' + fmt.format(high)) + suffix


def _run(stamp):
    return parse_time(stamp).strftime('%b %-d %HZ')


def headline(rows):
    counts = {tone: sum(row['current']['values']['tone'] == tone for row in rows) for tone in TONES}
    if len(rows) > 1 and max(counts.values()) == len(rows):
        label = READS[rows[0]['current']['values']['read']][0].lower()
        same = len({row['current']['values']['read'] for row in rows}) == 1
        return f'All {len(rows)} models screen {TONES[next(t for t, n in counts.items() if n)]}' + (f': {label}' if same else '')
    parts = [f'{counts[tone]} {TONES[tone]}' for tone in ('good', 'marginal', 'poor') if counts[tone]]
    return f'{len(rows)} models split: ' + ', '.join(parts) if len(rows) > 1 else f'One model available: {parts[0].split(" ", 1)[1]}'


def summary(rows):
    values = [row['current']['values'] for row in rows]
    cloud = [v['low_cloud_pct'] for v in values]
    sentences = [('Cloud is the split' if max(cloud) - min(cloud) >= 40 else 'Low cloud') + ': ' + _span(cloud, '%') +
                 ' cover, estimated bases ' + _span([v['base_ft'] for v in values], ' ft', '{:,}') + '.']
    gusts = [v['gust_kt'] for v in values if v['gust_kt'] is not None]
    sentences.append('Wind ' + _span([round(v['wind_kt']) for v in values], ' kt') +
                     (f', gusts to {max(gusts):.0f} kt' if gusts else '') + '.')
    wet = sum(v['rain_mm'] >= THRESHOLDS['rain_mm'] for v in values)
    sentences.append('No model has meaningful rain.' if not wet else f'{wet} of {len(rows)} with rain in the window.')
    moves = [row['change'] for row in rows]
    if any(move != 'unknown' for move in moves):
        sentences.append('Since each previous run: ' + ', '.join(
            f'{moves.count(move)} {move}' for move in ('better', 'worse', 'steady') if moves.count(move)) + '.')
    later = [READS[classify({'low_cloud_pct': v['later_low_cloud_pct'], 'base_ft': v['later_base_ft'], 'rain_mm': 0})][1] for v in values]
    improving = sum(TONE_RANK[after] > TONE_RANK[READS[v['read']][1]] for after, v in zip(later, values) if v['read'] != 'rain')
    sentences.append(f'In the {LATER_HOURS} hours after the window, cloud screens better in {improving} of {len(rows)}.')
    return ' '.join(sentences)


def ceiling_card(snapshot):
    """Briefing card drawn from a valid scorecard, or None so the caller keeps its unresolved default."""
    try:
        packet = validate_matrix(snapshot.get('model_matrix'), snapshot)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        return None
    values = [row['current']['values'] for row in packet['models'] if row['ok']]
    low = sum(v['read'] in ('low_overcast', 'rain') for v in values)
    clear = sum(v['read'] == 'scattered' for v in values)
    value = f'{low} of {len(values)} models low overcast' if low else f'{clear} of {len(values)} models scattered or clear'
    detail = ('Model scorecard for the flight window: low cloud ' + _span([v['low_cloud_pct'] for v in values], '%') +
              ', estimated bases ' + _span([v['base_ft'] for v in values], ' ft', '{:,}') +
              '. Estimates, not ceilings; check representative TAFs and observations near the date.')
    return {'value': value, 'detail': detail, 'tone': 'watch' if low or clear < len(values) else 'neutral', 'low': low}


def _row(row, collected):
    if not row['ok']:
        return (f'<tr class="matrix-missing"><th scope="row">{escape(row["label"])}</th>'
                f'<td colspan="7">Unavailable: {escape(row.get("error", "no data"))}.</td></tr>')
    current, previous = row['current'], row.get('previous')
    v = current['values']
    age = int((collected - parse_time(current['run'])).total_seconds() // 3600)
    gust = '' if v['gust_kt'] is None else f' G{v["gust_kt"]:.0f}'
    was = ''
    if previous:
        p = previous['values']
        was = (f'<small>{escape(_run(previous["run"]))}: {p["low_cloud_pct"]}% · {_feet(p["base_ft"])} · '
               f'{escape(READS[p["read"]][0].lower())}</small>')
    return (f'<tr data-tone="{escape(v["tone"])}"><th scope="row">{escape(row["label"])}</th>'
            f'<td>{escape(_run(current["run"]))}<small>{age} h old</small></td>'
            f'<td>{v["low_cloud_pct"]}%<small>then {v["later_low_cloud_pct"]}%</small></td>'
            f'<td>{_feet(v["base_ft"])}<small>then {_feet(v["later_base_ft"])}</small></td>'
            f'<td>{v["wind_dir_deg"]:03d}° {v["wind_kt"]:.0f} kt{gust}</td>'
            f'<td>{v["rain_mm"]:g} mm</td>'
            f'<td><span class="matrix-change" data-change="{escape(row["change"])}">{CHANGES[row["change"]]}</span>{was}</td>'
            f'<td><span class="matrix-read">{escape(READS[v["read"]][0])}</span></td></tr>')


def render_matrix(snapshot, now):
    """Return the scorecard section, or an empty string when the packet is absent or invalid."""
    packet = snapshot.get('model_matrix')
    if not packet:
        return ''
    try:
        validate_matrix(packet, snapshot)
        rows = [row for row in packet['models'] if row['ok']]
        collected = parse_time(packet['collected_at'])
        start, end = parse_time(packet['window']['start']).astimezone(TZ), parse_time(packet['window']['end']).astimezone(TZ)
        later = (end + timedelta(hours=LATER_HOURS)).strftime('%H:%M')
        window = f'{start:%H:%M}–{end:%H:%M} {end:%Z}'
        body = ''.join(_row(row, collected) for row in packet['models'])
        grids = '; '.join(f'{escape(row["label"])} {row["current"]["grid"]["latitude"]:.2f}, {row["current"]["grid"]["longitude"]:.2f}'
                          for row in rows if None not in row['current']['grid'].values())
        t = packet['thresholds']
        return (f'<section id="model-matrix" class="model-matrix" aria-labelledby="model-matrix-title">'
                f'<p class="eyebrow">Model scorecard · {escape(packet["window"]["kind"])} {escape(window)}</p>'
                f'<h2 id="model-matrix-title">{escape(headline(rows))}</h2>'
                f'<p class="matrix-summary">{escape(summary(rows))}</p>'
                f'<div class="table-wrap"><table><caption>Latest run of each deterministic model for {escape(window)}; '
                f'“then” is {end:%H:%M}–{escape(later)}. Estimated base is not a ceiling.</caption>'
                '<thead><tr><th scope="col">Model</th><th scope="col">Run</th><th scope="col">Low cloud</th>'
                '<th scope="col">Est. base</th><th scope="col">Wind</th><th scope="col">Rain</th>'
                '<th scope="col">Vs previous run</th><th scope="col">Screen</th></tr></thead>'
                f'<tbody>{body}</tbody></table></div>'
                f'<details><summary>Scorecard method, thresholds &amp; limits</summary><p>Each row is bound to the model run requested from '
                f'Open-Meteo’s single-runs API (CC BY 4.0); supplemental guidance, not an official aviation product. Low cloud is the mean of '
                f'hourly low-cloud cover; wind is the vector-mean direction and mean speed with the highest hourly gust; rain sums the hours ending '
                f'inside the window. Estimated base is the lowest hourly 2 m temperature–dew-point spread × 410 ft/°C: a convective-mixing estimate '
                f'that can miss stratus trapped under an inversion, never a ceiling or a TAF. Screens: rain ≥ {t["rain_mm"]:g} mm; overcast ≥ '
                f'{t["overcast_pct"]}% low cloud, split at an estimated {t["base_ft"]:,} ft base; broken ≥ {t["broken_pct"]}%; a favorable row with '
                f'gusts ≥ {t["gust_kt"]} kt is marked marginal. “Vs previous run” compares the same window in the prior run that covered it. '
                f'Screens are fixed planning thresholds, not probabilities, votes or a go/no-go decision. Grid points: {grids}.</p></details></section>')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        return ''
