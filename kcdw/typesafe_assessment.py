"""TypeSafe weekly planning decisions; weather confidence is a separate ordinal score."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .common import parse_time
from .evidence import prepare
from .intervals import duration
from .scoring import BANDS, planning_windows, score_label
from .typesafe_client import TypeSafeError, digest, encoded, private_json
from .typesafe_review import DATA_RULES

RUBRIC_VERSION = 1
# Representatives preserve the existing category boundaries; these are not event probabilities.
CATEGORY_SCORES = {'nogo': 10, 'unlikely': 35, 'tossup': 55, 'probable': 70, 'strong': 90}
FLYABILITY = {
    'nogo': 'Practical no-go: evidence indicates weather likely prevents ordinary local VFR pattern work during the assessed session, including return.',
    'unlikely': 'Probably not: meaningful ceiling/visibility, convection, rain or wind constraints make a normal VFR session unlikely.',
    'tossup': 'Toss-up: available weather evidence supports conditional or marginal prospects, with competing plausible operational outcomes.',
    'probable': 'Probably flyable: available weather evidence supports a normal VFR session with a limited, manageable weather constraint or timing question.',
    'strong': 'Strong go: available weather evidence supports comfortably usable VFR conditions throughout the assessed session, including return.',
}
CONFIDENCE_LEVELS = [
    'Low weather confidence: material evidence gaps, timing disagreement or operationally significant spread leave different planning outcomes plausible.',
    'Medium weather confidence: the broad scenario is supported, but relevant spread or disagreement could change some windows or constraints.',
    'High weather confidence: current observations or sufficiently complete, fresh, mutually consistent guidance support the controlling outcome across relevant spread; horizon and ceiling/visibility coverage support this conclusion.',
]
WEATHER_RULES = (
    DATA_RULES + ' Assess ordinary local VFR pattern work. Numeric planning scores and weather-confidence indices are ordinal, not probabilities. '
    'Observed local/upstream hazards, current warnings and valid advisory geometry lead near term; then METAR/SPECI, proxy TAF, '
    'NWS discussion and official forecasts. KCDW has no routine TAF. Respect the model_guidance_policy for longer horizons. '
    'Match all hazards to the exact session and return, not a remote time or region. Missing sources reduce confidence, not flyability by themselves. '
    'Do not average away a controlling hazard. Do not mechanically convert rain/thunder probabilities or cloud fraction into flyability. '
    'Mean agreement does not settle material within-model spread; missing spread is unknown. Forecast horizon and operational relevance matter. '
    'Draft prose is a secondary interpretation, not an additional independent source. Radar pixels are not provided to this text-only model; '
    'only supplied metrics and the image-capable draft interpretation are available. Never claim to have inspected radar yourself. '
    'Preserve an explicitly described observed hazard unless supplied evidence supports clearing it. '
    'For a partially elapsed window, assess a launch at collected_at through two hours later. Days 4–7 receive broad daily outlooks only.'
)


def confidence_label(score):
    return 'low' if score < 100 / 3 else 'medium' if score < 200 / 3 else 'high'


def _overlaps(value, start, end):
    try:
        stamp, span = value.split('/', 1)
        first = parse_time(stamp)
        return first < end and first + duration(span) > start
    except (ValueError, TypeError):
        return True  # Unknown timing stays visible rather than silently disappearing.


def _day_slice(value, date, start, end):
    """Retain overlapping intervals and bracketing hourly samples without averaging extrema."""
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                if 'date' in item and item['date'] != date:
                    continue
                if 'validTime' in item and not _overlaps(item['validTime'], start, end):
                    continue
                if 'startTime' in item and 'endTime' in item:
                    try:
                        if parse_time(item['startTime']) >= end or parse_time(item['endTime']) <= start:
                            continue
                    except (ValueError, TypeError):
                        pass
            result.append(_day_slice(item, date, start, end))
        return result
    if not isinstance(value, dict):
        return value
    axis = next((k for k in ('time', 'valid_times', 'valid_time_utc') if isinstance(value.get(k), list)), None)
    if axis:
        times = value[axis]
        try:
            stamps = [parse_time(t) for t in times]
            # The rolling Open-Meteo hourly series uses naive Eastern timestamps.
            stamps = [t.replace(tzinfo=start.tzinfo) if t.tzinfo is None else t for t in stamps]
            if all(isinstance(t, str) and len(t) == 10 for t in times):
                indices = [i for i, t in enumerate(stamps) if start.date() <= t.date() <= end.date()]
            else:
                indices = [i for i, t in enumerate(stamps) if start - timedelta(hours=6) <= t <= end + timedelta(hours=6)]
        except (ValueError, TypeError):
            indices = list(range(len(times)))

        def align(node):
            if isinstance(node, list) and len(node) == len(times):
                return [deepcopy(node[i]) for i in indices]
            if isinstance(node, dict):
                return {k: align(v) for k, v in node.items()}
            return deepcopy(node)
        return align(value)
    return {k: _day_slice(v, date, start, end) for k, v in value.items()}


def _period_context(snapshot, start, end, collected):
    from .synoptic_context import context_evidence
    result = context_evidence(snapshot['synoptic_context'], start, end, collected)
    for product in result['products']:
        if product.get('coverage', '').startswith('no mission overlap;'):
            # Keep the explicit coverage gap and provenance. Forecast text
            # outside this period cannot establish its weather.
            provenance = {k: product[k] for k in ('source', 'id', 'status', 'issued_at', 'valid_start', 'valid_end', 'coverage')
                          if k in product}
            product.clear()
            product.update(provenance)
    return result


def _latest_metars(source):
    if not source.get('ok') or not isinstance(source.get('data'), list):
        return
    latest, unknown = {}, []
    for row in source['data']:
        stamp = row.get('obsTime')
        if not row.get('icaoId') or type(stamp) not in (int, float):
            unknown.append(row)
            continue
        previous = latest.get(row['icaoId'])
        if previous is None or stamp > previous['obsTime']:
            latest[row['icaoId']] = row
    source['data'] = {'latest_by_station': list(latest.values()), 'unknown_time_or_station': unknown,
                      'review_detail_omitted': 'Older METARs are in near-term daily reviews. These observations alone cannot establish trends or future-day weather.'}


def day_evidence(snapshot, date, ranking=None, *, prepared=None):
    evidence = deepcopy(prepared if prepared is not None else prepare(snapshot))
    tz = ZoneInfo(snapshot['airport']['timezone'])
    start = datetime.fromisoformat(date + 'T08:00:00').replace(tzinfo=tz)
    end = datetime.fromisoformat(date + 'T20:00:00').replace(tzinfo=tz)
    collected = parse_time(snapshot['collected_at'])
    if start <= collected < end:
        end = max(end, collected + timedelta(hours=2))
    evidence = _day_slice(evidence, date, start, end)
    hourly = snapshot.get('sources', {}).get('weather_next3_hourly', {})
    if hourly.get('ok'):
        # This supplemental run ends after 48 hours. Recompute day-specific
        # coverage instead of carrying a whole-run label into later dates.
        from .wn3_hourly import summarize
        evidence['sources']['weather_next3_hourly']['data'] = summarize(
            hourly['data'], collected, min(max(start, collected), end), end)
    if 'synoptic_context' in snapshot:
        evidence['synoptic_context'] = _period_context(snapshot, start, end, collected)
        if any(p.get('source') == 'spc' for p in evidence['synoptic_context']['products']):
            spc = evidence.get('sources', {}).get('spc_outlooks')
            if spc and spc.get('ok'):
                spc['data'] = {'validated_evidence_at': 'synoptic_context.products[source=spc]',
                               'note': 'Use the time-checked, target-specific SPC context; duplicate raw products omitted.'}
    evidence['report_dates'] = [date]
    evidence['requested_windows'] = {date: planning_windows(snapshot, date)}
    evidence['assessment_period'] = {'date': date, 'start': min(max(start, collected), end).isoformat(),
                                     'end': end.isoformat(), 'timezone': str(tz), 'fully_elapsed': collected >= end}
    # Decoded NBM fields keep the verified units and interval semantics, avoiding duplicate raw cards.
    for key, decoded in evidence.get('derived_evidence', {}).get('nbm_decoded', {}).items():
        if decoded.get('status') == 'decoded':
            source = evidence['sources'][key]
            source['data'] = {'decoded': decoded, 'product': key,
                              'metadata': {k: v for k, v in (source.get('data') or {}).items()
                                           if k in ('issued_at', 'initialization_time', 'cycle_time', 'interval_hours',
                                                    'model_version', 'station', 'source_url')},
                              'selection_note': 'Only the named decoded fields are present. Other station-card fields are omitted, not zero or benign; the draft author saw the original card.'}
    evidence.get('derived_evidence', {}).pop('nbm_decoded', None)
    selected = {s['source_id']: s for s in (ranking or {}).get('sources', [])}
    for key, source in evidence.get('sources', {}).items():
        if 'afd' not in key or not source.get('ok'):
            continue
        data = source.get('data') or {}
        # Always retain deterministic discussion + aviation context alongside ranked selections.
        from .event_afd import _sections, _discussion_excerpt, _aviation_excerpt
        raw = data.get('excerpt', '')
        sections = _sections(raw)
        discussion, truncated = _discussion_excerpt(sections)
        aviation, aviation_truncated, heading = _aviation_excerpt(sections)
        source['data'] = {k: v for k, v in data.items() if k not in ('excerpt', 'productText')}
        ranked, used = [], 0
        for passage in selected.get(key, {}).get('selected', []):
            if used + len(passage['text']) <= 1600:
                ranked.append({'paragraph_index': passage['paragraph_index'], 'text': passage['text']})
                used += len(passage['text'])
        source['data'].update(discussion_excerpt=discussion or raw[:2600],
                              discussion_truncated=truncated or len(raw) > 2600,
                              aviation_excerpt=aviation,
                              aviation_truncated=aviation_truncated, aviation_heading=heading,
                              ranked_passages=ranked,
                              selection_note='Omitted text does not rule out hazards. Draft interpretation had the full collected excerpt.')
        # Ranked selections often repeat a complete paragraph in these excerpts.
        source['data']['ranked_passages'] = [p for p in ranked if p['text'] not in source['data']['discussion_excerpt']
                                            and p['text'] not in aviation]
    # Raw aviation strings retain all weather groups; omit duplicate decoded fields and database metadata.
    for key, raw_key, fields in (
        ('awc_metars', 'rawOb', ('icaoId', 'rawOb', 'obsTime', 'fltCat', 'lat', 'lon')),
        ('awc_tafs', 'rawTAF', ('icaoId', 'rawTAF', 'issueTime', 'validTimeFrom', 'validTimeTo', 'lat', 'lon')),
    ):
        source = evidence.get('sources', {}).get(key, {})
        if source.get('ok') and isinstance(source.get('data'), list):
            source['data'] = [{k: row[k] for k in fields if k in row} if row.get(raw_key) else row
                              for row in source['data']]
    if start >= collected + timedelta(hours=24):
        _latest_metars(evidence.get('sources', {}).get('awc_metars', {}))
    sigmets = evidence.get('sources', {}).get('awc_convective_sigmets', {})
    if sigmets.get('ok') and isinstance(sigmets.get('data'), list):
        relevant, outside = [], []
        for row in sigmets['data']:
            props = row.get('properties', {})
            try:
                first, last = parse_time(props['validTimeFrom']), parse_time(props['validTimeTo'])
                overlaps = first < end and last > start
            except (KeyError, ValueError, TypeError):
                overlaps = True  # Unknown validity remains visible.
            if overlaps:
                relevant.append(row)
            else:
                outside.append({k: props[k] for k in ('airSigmetType', 'hazard', 'validTimeFrom', 'validTimeTo') if k in props})
        if outside:
            sigmets['data'] = {'overlapping_or_unknown_validity': relevant, 'outside_period': outside,
                               'selection_note': 'Out-of-period advisory details omitted. Current advisories do not provide future hazard coverage; empty overlap is not a forecast of no convection.'}
    # Point metadata is location/URL plumbing; chart prose duplicates numerical sources.
    evidence.get('sources', {}).pop('nws_points', None)
    evidence.pop('weekly_guidance', None)
    evidence['limitations_for_typesafe'] = [
        'Text-only assessment; radar imagery is interpreted by the draft author, not TypeSafe.',
        'All selected intervals retain units, provenance and missing-data semantics. No new observations are inferred.',
    ]
    if len(encoded(evidence).encode()) > 100_000:
        raise TypeSafeError('Daily TypeSafe evidence exceeds byte budget; no source was silently dropped')
    return evidence


def assess_week(client, snapshot, draft, ranking=None):
    from .validation import validate_analysis
    validate_analysis(draft, snapshot)
    prepared = prepare(snapshot)
    days, states = [], {}
    for date in snapshot['report_dates']:
        evidence = day_evidence(snapshot, date, ranking, prepared=prepared)
        day = next(d for d in draft['days'] if d['date'] == date)
        state = {'weather': evidence, 'draft_interpretation': day,
                 'draft_controlling_hazards': draft['controlling_hazards']}
        questions = {
            'outlook': {'type': 'choice', 'instructions': {'question':
                f'Which broad planning category best describes the actionable daytime weather on {date} at KCDW? '
                'For a varied day, reflect whether a useful normal session exists and retain the controlling limitations.',
                'rules': WEATHER_RULES}, 'criteria': FLYABILITY},
            'weather_confidence': {'type': 'score', 'instructions': {'question':
                f'How well does the evidence constrain the weather planning outcome for {date} at KCDW? '
                'Assess forecast reliability from relevant spread, disagreement, missing fields, freshness and horizon. '
                'This is separate from whether the weather is good, and separate from your own classification confidence.',
                'rules': WEATHER_RULES}, 'criteria': CONFIDENCE_LEVELS},
        }
        for window in planning_windows(snapshot, date):
            questions['window_' + window] = {'type': 'choice', 'instructions': {'question':
                f'Which planning category fits a normal two-hour KCDW VFR session in {date} {window} Eastern, including return? '
                'Apply the launch-now rule if this block has partly elapsed.', 'rules': WEATHER_RULES}, 'criteria': FLYABILITY}
        response = client.evaluate('weekly-' + date, state, questions)
        answers = response['answers']
        confidence_score = round(50 * answers['weather_confidence']['score'], 1)
        score = CATEGORY_SCORES[answers['outlook']['choice']]
        days.append({'date': date, 'score': score, 'outlook': score_label(score),
                     'confidence': confidence_label(confidence_score), 'confidence_score': confidence_score,
                     'windows': [{'window': w, 'score': CATEGORY_SCORES[answers['window_' + w]['choice']]}
                                 for w in planning_windows(snapshot, date)]})
        states[date] = evidence
    usable = [d for d in days if d['date'] != snapshot['local_date'] or d['windows']]
    ordered = sorted(usable, key=lambda d: (-max((w['score'] for w in d['windows']), default=d['score']),
                                          -d['confidence_score'], d['date']))
    result = {'version': RUBRIC_VERSION, 'provider': 'typesafe', 'model': client.model,
              'snapshot_sha256': digest(snapshot), 'draft_sha256': digest(draft),
              'best_day': ordered[0]['date'], 'backup_day': ordered[1]['date'], 'days': days,
              'semantics': 'Flyability scores represent categories. Weather confidence is a 0–100 ordinal index; neither is a probability. Model confidence remains in private call artifacts.'}
    private_json(client.artifacts / 'weekly-decisions.json', result)
    return result, states


def overview_evidence(snapshot, ranking=None):
    """Bound broad summary review to original official text and computed daily model summaries.

    Detailed window claims are separately reviewed against day_evidence; this
    packet deliberately does not imply precise timing from daily aggregates.
    """
    prepared = prepare(snapshot)
    result = day_evidence(snapshot, snapshot['report_dates'][0], ranking, prepared=prepared)
    result['report_dates'] = snapshot['report_dates']
    result.pop('assessment_period', None)
    result.pop('requested_windows', None)
    result.pop('derived_evidence', None)
    for key in ('nws_hourly', 'nws_grid', 'open_meteo'):
        source = result['sources'].get(key)
        if source and source.get('ok'):
            source['data'] = {'review_detail_omitted': 'Detailed hourly values are covered by separate daily reviews, not this broad summary check.'}
    if 'synoptic_context' in snapshot:
        tz = ZoneInfo(snapshot['airport']['timezone'])
        start = datetime.fromisoformat(snapshot['report_dates'][0] + 'T08:00:00').replace(tzinfo=tz)
        end = datetime.fromisoformat(snapshot['report_dates'][-1] + 'T20:00:00').replace(tzinfo=tz)
        result['synoptic_context'] = _period_context(snapshot, start, end, parse_time(snapshot['collected_at']))
    _latest_metars(result['sources'].get('awc_metars', {}))
    for key in ('nws_forecast', 'weather_next3'):
        if key in prepared['sources']:
            result['sources'][key] = deepcopy(prepared['sources'][key])
    wn3 = result['sources'].get('weather_next3', {})
    if wn3.get('ok'):
        wn3['data']['days'] = [d for d in wn3['data']['days'] if d['date'] in snapshot['report_dates']]
        wn3['data'].pop('wind_direction', None)
        wn3['data']['review_detail_omitted'] = 'Hourly wind direction is omitted from this broad summary; no directional inference.'
    for key, summary in prepared['derived_evidence']['ensemble_spread'].items():
        result['sources'][key] = {**deepcopy(prepared['sources'][key]), 'data': summary}
    for key, decoded in prepared['derived_evidence']['nbm_decoded'].items():
        if decoded.get('status') == 'decoded':
            # Reuse the daily packet's issue-time provenance and selection note.
            result['sources'][key]['data']['decoded'] = decoded
    result['limitations_for_typesafe'].append(
        'Broad summary review: model daily ranges retain extrema, units and coverage. They cannot establish exact two-hour timing. '
        'Omitted hourly detail is reviewed separately and is not evidence that a hazard is absent.')
    return result


def fixed_schema(schema, decisions):
    schema = deepcopy(schema)
    schema['properties']['best_day'] = {'const': decisions['best_day']}
    schema['properties']['backup_day'] = {'const': decisions['backup_day']}
    alternatives = []
    for day in decisions['days']:
        item = deepcopy(schema['$defs']['day'])
        for key in ('date', 'outlook', 'confidence'):
            item['properties'][key] = {'const': day[key]}
        windows = []
        for window in day['windows']:
            entry = deepcopy(schema['$defs']['window'])
            for key in ('window', 'score'):
                entry['properties'][key] = {'const': window[key]}
            windows.append(entry)
        item['properties']['windows'] = {'type': 'array', 'minItems': len(windows), 'maxItems': len(windows),
                                          'items': {'oneOf': windows} if windows else False}
        alternatives.append(item)
    schema['properties']['days']['items'] = {'oneOf': alternatives}
    return schema


def apply_decisions(analysis, decisions, snapshot):
    """Reject prose written around different decisions before attaching numeric metadata."""
    from .validation import validate_analysis
    validate_analysis(analysis, snapshot)
    if digest(snapshot) != decisions['snapshot_sha256']:
        raise TypeSafeError('TypeSafe decisions belong to another snapshot')
    if any(analysis[key] != decisions[key] for key in ('best_day', 'backup_day')):
        raise TypeSafeError('Writer changed TypeSafe planning dates')
    result = deepcopy(analysis)
    for day in result['days']:
        fixed = next(d for d in decisions['days'] if d['date'] == day['date'])
        if any(day[key] != fixed[key] for key in ('outlook', 'confidence')) or {
                w['window']: w['score'] for w in day['windows']} != {w['window']: w['score'] for w in fixed['windows']}:
            raise TypeSafeError('Writer changed TypeSafe assessment decisions')
        day.update(score=fixed['score'], confidence_score=fixed['confidence_score'])
    result['assessment'] = {key: decisions[key] for key in ('provider', 'model', 'snapshot_sha256')}
    result['assessment']['rubric_version'] = RUBRIC_VERSION
    validate_analysis(result, snapshot)
    return result


def validate_metadata(analysis, snapshot):
    metadata = analysis.get('assessment')
    if 'assessment' not in analysis:
        if any('score' in day or 'confidence_score' in day for day in analysis['days']):
            raise TypeSafeError('Numeric daily assessments require provenance')
        return
    import re
    if (not isinstance(metadata, dict) or set(metadata) != {'provider', 'model', 'snapshot_sha256', 'rubric_version'}
            or metadata['provider'] != 'typesafe' or type(metadata['rubric_version']) is not int
            or metadata['rubric_version'] != RUBRIC_VERSION
            or not isinstance(metadata['model'], str) or not re.fullmatch(r'jev-\d+\.\d+\.\d+', metadata['model'])
            or metadata['snapshot_sha256'] != digest(snapshot)):
        raise TypeSafeError('Invalid TypeSafe assessment provenance')
    for day in analysis['days']:
        score, confidence = day.get('score'), day.get('confidence_score')
        if type(score) is not int or score not in CATEGORY_SCORES.values() or score_label(score) != day.get('outlook'):
            raise TypeSafeError('Daily flyability score disagrees with its category')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 100 or confidence_label(confidence) != day['confidence']:
            raise TypeSafeError('Invalid weather confidence index')
