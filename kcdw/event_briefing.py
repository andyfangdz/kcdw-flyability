"""Compact, deterministic mission briefing; no calibrated flight probabilities."""
from datetime import datetime, timedelta

from .common import UTC, parse_time
from .events import Event, TZ
from .event_ensemble import weathernext3_diagnostic, WINDOW_RAIN_MM, WINDOW_WIND_KT


def operational_briefing(snapshot: dict, now: datetime) -> dict:
    event = Event(**snapshot['event'])
    start = datetime.combine(event.day, datetime.min.time(), TZ) + timedelta(hours=event.start_hour)
    end = start + timedelta(hours=event.end_hour - event.start_hour)
    lead = start - now
    stale = now - parse_time(snapshot['collected_at']) > timedelta(hours=8)
    diagnostic = weathernext3_diagnostic(snapshot, now)
    models = [snapshot['models'][key]['data'] for key in ('aifs_ens', 'ecmwf_ens', 'gefs', 'geps')
              if snapshot['models'].get(key, {}).get('ok')]
    source = 'No usable primary model'
    rain_value, wind_value = 'Unavailable', 'Unavailable'
    rain_detail, wind_detail = 'No usable rain estimate.', 'No usable wind estimate.'
    rain_watch = wind_watch = False
    if diagnostic['available']:
        forecast = snapshot['weathernext3']['data']['forecast']
        times = [parse_time(t) for t in forecast['valid_time_utc']]
        rain_indices = [i for i, t in enumerate(times) if start < t <= end]
        wind_indices = [i for i, t in enumerate(times) if start <= t < end]
        rain, wind = forecast['fields']['precipitation_1h'], forecast['fields']['wind_speed_10m']
        rain_total = sum(rain['mean'][i] for i in rain_indices)
        wind_peak = max(wind['mean'][i] for i in wind_indices) * 3600 / 1852
        rain_tail = max(rain['p90'][i] for i in rain_indices)
        wind_tail = max(wind['p90'][i] for i in wind_indices) * 3600 / 1852
        rain_value = f'{rain_total:.2f} mm mean total'
        wind_value = f'{wind_peak:.1f} kt peak hourly mean'
        rain_detail = f'WN3 window sum of means. Highest hourly p90: {rain_tail:.2f} mm; not a total-rain percentile.'
        wind_detail = f'WN3 highest hourly p90: {wind_tail:.1f} kt. Gusts and direction unavailable; no crosswind assessment.'
        rain_watch = (diagnostic['precipitation_mean'] == 'planning trigger reached' or
                      diagnostic['precipitation_signal'] != 'percentile range below trigger')
        wind_watch = (diagnostic['wind_mean'] == 'planning trigger reached' or
                      diagnostic['wind_signal'] != 'percentile range below trigger')
        source = 'WeatherNext 3 · run ' + parse_time(diagnostic['run']).strftime('%b %-d %HZ')
    elif models:
        model = models[0]
        window = model['window']
        rain, wind = window.get('rain_total_mm'), window.get('wind_max_kt')
        source = model['model'] + ' fallback · exact cycle unverified'
        if rain:
            rain_value = f"{rain['median']:.2f} mm median total"
            rain_detail = f"{model['model']} member-total p10–p90: {rain['p10']:.2f}–{rain['p90']:.2f} mm."
            rain_watch = rain['p90'] >= WINDOW_RAIN_MM
        if wind:
            wind_value = f"{wind['median']:.1f} kt median member peak"
            wind_detail = f"{model['model']} member-peak p10–p90: {wind['p10']:.1f}–{wind['p90']:.1f} kt; not gusts or crosswind."
            wind_watch = wind['p90'] >= WINDOW_WIND_KT

    model_rain = [m['window']['rain_total_mm']['median'] for m in models if m['window'].get('rain_total_mm')]
    model_wind = [m['window']['wind_max_kt']['median'] for m in models if m['window'].get('wind_max_kt')]
    rain_split = bool(model_rain) and min(model_rain) < WINDOW_RAIN_MM <= max(model_rain)
    wind_split = bool(model_wind) and min(model_wind) < WINDOW_WIND_KT <= max(model_wind)
    disagreement = rain_split or wind_split
    medians_flag = ((bool(model_rain) and max(model_rain) >= WINDOW_RAIN_MM) or
                    (bool(model_wind) and max(model_wind) >= WINDOW_WIND_KT))
    agreement = ('Split on ' + ('rain & wind' if rain_split and wind_split else 'rain' if rain_split else 'wind')
                 if disagreement else 'Model medians flag rain/wind' if medians_flag else
                 'Medians below rain/wind screens' if model_rain and model_wind else 'Comparison incomplete')
    agreement_detail = 'Conventional models only; no pooled probability.'
    if model_rain:
        agreement_detail += f' Rain-total medians: {min(model_rain):.2f}–{max(model_rain):.2f} mm.'
    if model_wind:
        agreement_detail += f' Peak-wind medians: {min(model_wind):.1f}–{max(model_wind):.1f} kt.'
    cards = [
        {'label': 'Rain during the window', 'value': rain_value, 'detail': rain_detail, 'tone': 'watch' if rain_watch else 'neutral'},
        {'label': 'Runway wind', 'value': wind_value, 'detail': wind_detail, 'tone': 'watch' if wind_watch else 'neutral'},
        {'label': 'Maneuvers ceiling', 'value': 'Not resolved', 'detail': 'Low-cloud fraction is not ceiling or visibility. Check representative TAFs and observations near the date.', 'tone': 'watch'},
        {'label': 'Model disagreement', 'value': agreement, 'detail': agreement_detail, 'tone': 'watch' if disagreement or medians_flag else 'neutral'},
    ]
    watch = rain_watch or wind_watch or disagreement or medians_flag
    if lead > timedelta(days=7):
        next_check = {'title': 'At 7 days · ' + (start - timedelta(days=7)).strftime('%b %-d'),
                      'detail': 'Compare successive model runs and NWS forecast/AFD. Retain a weather contingency; do not use the hourly curves to lock a time.'}
    elif lead > timedelta(hours=48):
        next_check = {'title': 'Next model cycle; decisive review at 48 hours',
                      'detail': 'Track rain/front timing and NWS forecast changes. At 48 hours, check ceiling, visibility, wind direction and gusts against your aircraft and examiner limits.'}
    else:
        next_check = {'title': 'Before committing to departure',
                      'detail': 'Use fresh METARs, representative terminal TAFs, NWS, radar and advisories. Confirm maneuvers ceiling, crosswind/gust limits and a safe return window.'}
    concerns = [name for name, present in (('rain', rain_watch), ('wind', wind_watch), ('model disagreement', disagreement)) if present]
    if medians_flag and not concerns:
        concerns.append('other models flag rain/wind')
    headline = ('Keep a backup: ' + ', '.join(concerns) if watch else
                'Rain/wind look modest; ceiling remains unresolved')
    summary = 'Long-range scheduling guidance, not a decision to fly. Use the charts to follow timing and spread across runs.'
    if not diagnostic['available']:
        summary = 'WN3 unavailable; using the independent model fallback. ' + summary
    if not diagnostic['available'] and not models:
        headline, watch = 'Guidance unavailable — check sources', True
    if lead <= timedelta(hours=48):
        headline = 'Use official aviation guidance for the decision'
        summary = 'This event page contains global models; current aviation products are not collected here. Numbers below are supplemental.'
        source = 'Official aviation guidance first · model display: ' + source
    if stale:
        headline, summary = 'Refresh needed — snapshot is outdated', 'Do not treat the archived model values as current guidance.'
        source = 'Outdated snapshot · refresh required'
        next_check = {'title': 'Refresh now', 'detail': 'Obtain a current update before interpreting model agreement or choosing an operating window.'}
        for card in cards:
            if card['label'] != 'Maneuvers ceiling':
                card.update(value='Outdated', detail='Refresh required before using this estimate.', tone='stale')
    if now >= end:
        headline, summary = 'Historical checkride outlook', 'The planning window has ended. Retained forecasts are not current operating guidance.'
        next_check = {'title': 'Archive only', 'detail': 'Use the current outlook for a new flight; do not reuse this dated assessment.'}
    return {'headline': headline, 'summary': summary, 'tone': 'stale' if stale or now >= end else 'watch' if watch else 'neutral',
            'cards': cards, 'next_check': next_check, 'source': source}
