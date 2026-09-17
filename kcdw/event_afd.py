"""Bounded supplemental NWS AFD evidence for a dated-event snapshot.

Public API:
    collect_afds(client, snapshot, now) -> envelope
    validate_afds(envelope, snapshot, now) -> sanitized envelope | None
    afd_evidence(snapshot, now) -> {OKX: compact | None, PHI: ..., ALY: ...}

Attach the envelope at snapshot['event_afds']. Envelope schema:
    {version: 1, snapshot_collected_at: str,
     event: {slug: str, date: str, window: str},
     offices: {OKX: source | None, PHI: source | None, ALY: source | None}}
Source schema:
    {office, name, id, productCode, issuingOffice, issuanceTime, productText,
     listing: {id, productCode, issuingOffice, issuanceTime},
     issued_at, fetched_at, product_url,
     discussion_excerpt, aviation_excerpt, discussion_truncated,
     aviation_truncated, aviation_heading}
Compact schema is exactly:
    {office, name, issued_at, fetched_at, product_url, discussion_excerpt,
     aviation_excerpt, discussion_truncated, aviation_truncated, aviation_heading}

All output product timestamps are UTC ISO strings; snapshot_collected_at is
preserved exactly for parent binding. Issuance and fetch clocks stay distinct.
Validation rederives excerpts from the retained original text, without fetching
or renewing any timestamps. Invalid outer binding returns None; independent
invalid office records become None. Missing sections are empty strings, NOT
claims that the event is covered. This module never changes flight readiness.

Collection makes at most six client.get calls: one bounded listing scan (first
100 entries) and one selected detail per office. Client owns timeout, response
byte bounds and retries. URLs are derived solely from pinned hosts/offices and
canonical UUIDs; supplied @id values are never used. Products over 32,000 chars
are rejected rather than clipped, retaining full original source for validation.
No previous snapshot records are carried forward, even on source failure.

Excerpt policy: preserve forecaster wording (whitespace normalized), section and
forecast-period headings, and complete paragraphs where possible. Prefer a
contiguous tail of discussion paragraphs, retaining their KEY MESSAGE / legacy
section context, rather than scoring for a particular weather diagnosis. Prefer
aviation Outlook over near-term TAF text when space is short. Oversized paragraphs
may use their opening complete sentences; omissions are explicit. These are
selected NWS statements, not event-specific forecasts or our own inferences.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .common import UTC, iso_z

OFFICES = {'OKX': 'New York/Upton', 'PHI': 'Philadelphia/Mount Holly', 'ALY': 'Albany'}
ROOT = 'https://api.weather.gov/products'
MAX_TEXT = 32_000
MAX_LIST_ENTRIES = 100
MAX_PRODUCT_AGE = timedelta(hours=18)
MAX_FETCH_AGE = timedelta(hours=12)
CLOCK_SKEW = timedelta(minutes=5)
DISCUSSION_BUDGET = 1900
AVIATION_BUDGET = 700
OMITTED = '[... omitted ...]'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
# Major sections only: KEY MESSAGE and OUTLOOK remain inside their parent.
SECTION = re.compile(
    r'^\.(DISCUSSION|SYNOPSIS|NEAR TERM|SHORT TERM|LONG TERM|AVIATION|MARINE|'
    r'HYDROLOGY|TIDES/COASTAL FLOODING|CLIMATE|FIRE WEATHER|'
    r'WHAT HAS CHANGED|KEY MESSAGES|[A-Z]{3} WATCHES/WARNINGS/ADVISORIES)'
    r'(?=[ /\.])[^\n]*?(?:\n[^\n]*?)?\.\.\.[ \t]*', re.M)
KEY_HEADING = re.compile(r'^\.?KEY MESSAGE\s+\d+\.\.\.(.*)$', re.S)
OUTLOOK = re.compile(r'^\.?OUTLOOK\b', re.I | re.M)
PERIOD_HEADING = re.compile(
    r'^\.?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|'
    r'Today|Tonight|Overnight|This|Next|Early|Late|Through|Weekend)\b[^\n]{0,140}\.\.\.$', re.I)
ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError)
COMPACT_FIELDS = ('office', 'name', 'issued_at', 'fetched_at', 'product_url',
                  'discussion_excerpt', 'aviation_excerpt', 'discussion_truncated',
                  'aviation_truncated', 'aviation_heading')


def _require(condition):
    if not condition:
        raise ValueError('Invalid AFD evidence')


def _aware(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)
    return value.astimezone(UTC)


def _time(value):
    _require(isinstance(value, str) and len(value) <= 40)
    return _aware(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _fresh(value, now, maximum):
    _require(-CLOCK_SKEW <= now - value <= maximum)


def _binding(snapshot):
    _require(isinstance(snapshot, dict))
    collected = snapshot['collected_at']
    _time(collected)
    event = snapshot['event']
    _require(isinstance(event, dict))
    selected = {key: event[key] for key in ('slug', 'date', 'window')}
    _require(all(isinstance(v, str) for v in selected.values()))
    _require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', selected['slug']))
    _require(date.fromisoformat(selected['date']).isoformat() == selected['date'])
    window = re.fullmatch(r'(\d{2})-(\d{2})', selected['window'])
    if window is None:
        raise ValueError('Invalid AFD event window')
    _require(0 <= int(window[1]) < int(window[2]) <= 24)
    return {'version': 1, 'snapshot_collected_at': collected, 'event': selected}


def _metadata(raw, office, now):
    _require(isinstance(raw, dict))
    ident = raw['id']
    _require(isinstance(ident, str) and UUID.fullmatch(ident))
    _require(raw['productCode'] == 'AFD' and raw['issuingOffice'] == 'K' + office)
    issued = _time(raw['issuanceTime'])
    _fresh(issued, now, MAX_PRODUCT_AGE)
    return {'id': ident, 'productCode': 'AFD', 'issuingOffice': 'K' + office,
            'issuanceTime': iso_z(issued)}


def _normalize(value):
    return ' '.join(value.split())


def _sections(text):
    """Stop at && or another major section, not nested dot headings."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    matches = list(SECTION.finditer(text))
    result = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end]
        body = re.split(r'^\s*(?:&&|\$\$)\s*$', body, maxsplit=1, flags=re.M)[0]
        result.append((match[1], _normalize(match[0]), body.strip()))
    return result


def _discussion_blocks(sections):
    """Each paragraph retains the original section/key-message context."""
    selected = [s for s in sections if s[0] == 'DISCUSSION']
    if not selected:
        selected = [s for s in sections if s[0] in ('SYNOPSIS', 'NEAR TERM', 'SHORT TERM', 'LONG TERM')]
    blocks = []
    for _, heading, body in selected:
        context = (heading,)
        for paragraph in re.split(r'\n\s*\n', body):
            paragraph = paragraph.strip()
            if not paragraph or paragraph == OMITTED:
                continue
            key = KEY_HEADING.match(paragraph)
            if key:
                # PHI often gives a multi-line summary on the heading paragraph;
                # ALY often starts the body on the line following a bare heading.
                first_line = paragraph.split('\n', 1)[0]
                if first_line.rstrip().endswith('...'):
                    context = (heading, _normalize(first_line))
                    rest = paragraph[len(first_line):].strip()
                    blocks.append((context, _normalize(rest)))
                else:
                    context = (heading, _normalize(paragraph))
                    blocks.append((context, ''))
                continue
            first_line = paragraph.split('\n', 1)[0].strip()
            if PERIOD_HEADING.fullmatch(first_line):
                # The period scopes following paragraphs even without a blank
                # line. Replace the prior period, retaining section/key context.
                context = tuple(h for h in context if not PERIOD_HEADING.fullmatch(h)) + (first_line,)
                rest = paragraph[len(paragraph.split('\n', 1)[0]):].strip()
                blocks.append((context, _normalize(rest)))
                continue
            blocks.append((context, _normalize(paragraph)))
    return blocks


def _render_blocks(blocks):
    result, previous = [], ()
    for context, paragraph in blocks:
        for i, heading in enumerate(context):
            if previous[:i + 1] != context[:i + 1]:
                result.append(heading)
        if paragraph:
            result.append(paragraph)
        previous = context
    return '\n\n'.join(result)


def _opening_sentences(text, budget):
    """Never emit partial words/sentences as if they were whole quotations."""
    if len(text) <= budget:
        return text
    # Ellipses in NWS day labels are not sentence endings.
    ends = [m.end() for m in re.finditer(r'(?<!\.)[.!?](?!\.)(?=\s|$)', text)]
    ends = [end for end in ends if end <= budget]
    return text[:max(ends)] if ends else ''


def _discussion_excerpt(sections, budget=DISCUSSION_BUDGET):
    blocks = _discussion_blocks(sections)
    full = _render_blocks(blocks)
    if len(full) <= budget:
        return full, False
    available = budget - len(OMITTED) - 2
    chosen = []
    # Contiguous suffix; do not stitch unrelated keyword-matching sentences.
    for block in reversed(blocks):
        candidate = [block, *chosen]
        if len(_render_blocks(candidate)) > available:
            if not chosen:
                context, paragraph = block
                prefix = _render_blocks([(context, '')])
                room = available - len(prefix) - len(OMITTED) - 4
                opening = _opening_sentences(paragraph, room)
                if opening:
                    chosen = [(context, opening + '\n\n' + OMITTED)]
            break
        chosen = candidate
    return OMITTED + ('\n\n' + _render_blocks(chosen) if chosen else ''), True


def discussion_quote(excerpt, budget=1250):
    """Smaller display quote retains the same late-period paragraph policy."""
    _require(isinstance(excerpt, str) and type(budget) is int and 100 <= budget <= DISCUSSION_BUDGET)
    reserve = len(OMITTED) + 2 if OMITTED in excerpt else 0
    quote, _ = _discussion_excerpt(_sections(excerpt), budget - reserve)
    if quote and reserve and OMITTED not in quote:
        quote = OMITTED + '\n\n' + quote
    return quote


def _aviation_excerpt(sections):
    found = next((s for s in sections if s[0] == 'AVIATION'), None)
    if found is None:
        return '', False, ''
    _, heading, body = found
    # Preserve outlook rows and their periods; collapse wrapping within a row.
    paragraphs = [_normalize(p) for p in re.split(r'\n\s*\n', body) if p.strip()]
    full = heading + ('\n\n' + '\n\n'.join(paragraphs) if paragraphs else '')
    if len(full) <= AVIATION_BUDGET:
        return full, False, heading
    _require(len(heading) < AVIATION_BUDGET - 2 * len(OMITTED) - 8)
    outlook = OUTLOOK.search(body)
    selected = body[outlook.start():] if outlook else body
    # Keep an outlook heading separate from its first forecast row even if no
    # blank line intervenes. All original wording remains unchanged.
    selected = re.sub(r'(?im)^(\.?OUTLOOK[^\n]*\.\.\.)[ \t]*\n', r'\1\n\n', selected)
    paragraphs = [_normalize(p) for p in re.split(r'\n\s*\n', selected) if p.strip()]
    result = heading + '\n\n' + OMITTED
    for paragraph in paragraphs:
        # Reserve room for trailing omission notice if any following text is cut.
        room = AVIATION_BUDGET - len(result) - 2 - len(OMITTED) - 2
        if len(paragraph) <= room:
            result += '\n\n' + paragraph
        else:
            opening = _opening_sentences(paragraph, room)
            if opening:
                result += '\n\n' + opening
            result += '\n\n' + OMITTED
            break
    return result, True, heading


def _validated_source(source, office, snapshot_stamp, now):
    _require(isinstance(source, dict))
    metadata = _metadata(source, office, now)
    listing = _metadata(source['listing'], office, now)
    _require(metadata == listing)
    issued, fetched = _time(metadata['issuanceTime']), _time(source['fetched_at'])
    _fresh(fetched, now, MAX_FETCH_AGE)
    _require(issued <= fetched + CLOCK_SKEW)
    _require(_time(snapshot_stamp) <= fetched + CLOCK_SKEW)
    url = f"{ROOT}/{metadata['id']}"
    _require(source['office'] == office and source['name'] == OFFICES[office])
    _require(source['product_url'] == url and source['issued_at'] == iso_z(issued))
    text = source['productText']
    _require(isinstance(text, str) and 0 < len(text) <= MAX_TEXT)
    _require(not any(ord(c) < 32 and c not in '\n\r\t' for c in text))
    header = text[:1500]
    _require(re.search(rf'^AFD{office}[ \t]*\r?$', header, re.M))
    _require(re.search(rf'^FXUS\d{{2}} K{office} \d{{6}}(?:[ \t]+[A-Z]{{3}})?[ \t]*\r?$', header, re.M))
    sections = _sections(text)
    discussion, discussion_cut = _discussion_excerpt(sections)
    aviation, aviation_cut, heading = _aviation_excerpt(sections)
    return {**metadata, 'office': office, 'name': OFFICES[office], 'listing': listing,
            'productText': text, 'product_url': url, 'issued_at': iso_z(issued),
            'fetched_at': source['fetched_at'], 'discussion_excerpt': discussion,
            'aviation_excerpt': aviation, 'discussion_truncated': discussion_cut,
            'aviation_truncated': aviation_cut, 'aviation_heading': heading}


def collect_afds(client, snapshot, now):
    """Fetch at most one detail per office; isolate failures and omit raw errors.

    Invalid caller snapshot/clock raises ValueError (no network calls). Source
    failures, including malformed response payloads, yield office=None.
    """
    now = _aware(now)
    envelope = _binding(snapshot)
    _require(_time(envelope['snapshot_collected_at']) <= now + CLOCK_SKEW)
    envelope['offices'] = dict.fromkeys(OFFICES)
    for office in OFFICES:
        try:
            raw = client.get(f'{ROOT}/types/AFD/locations/{office}')
            graph = raw['@graph']
            _require(isinstance(graph, list))
            candidates = []
            for entry in graph[:MAX_LIST_ENTRIES]:
                try:
                    candidates.append(_metadata(entry, office, now))
                except ERRORS:
                    continue
            _require(candidates)
            selected = max(candidates, key=lambda item: _time(item['issuanceTime']))
            url = f"{ROOT}/{selected['id']}"
            raw = client.get(url)
            detail = _metadata(raw, office, now)
            _require(detail == selected)
            source = {**detail, 'office': office, 'name': OFFICES[office],
                      'listing': selected, 'productText': raw['productText'],
                      'issued_at': detail['issuanceTime'], 'fetched_at': iso_z(now),
                      'product_url': url}
            envelope['offices'][office] = _validated_source(
                source, office, envelope['snapshot_collected_at'], now)
        except Exception:
            # This boundary includes network/client exceptions. Never expose their
            # text or retain stale data from the prior snapshot.
            envelope['offices'][office] = None
    return envelope


def validate_afds(envelope, snapshot, now):
    """Revalidate parent binding and each original source independently, no I/O."""
    try:
        now = _aware(now)
        binding = _binding(snapshot)
        _require(isinstance(envelope, dict) and type(envelope.get('version')) is int)
        _require(all(envelope.get(k) == v for k, v in binding.items()))
        _require(_time(binding['snapshot_collected_at']) <= now + CLOCK_SKEW)
        offices = envelope['offices']
        _require(isinstance(offices, dict))
    except ERRORS:
        return None
    clean = {**binding, 'offices': dict.fromkeys(OFFICES)}
    for office in OFFICES:
        try:
            clean['offices'][office] = _validated_source(
                offices.get(office), office, binding['snapshot_collected_at'], now)
        except ERRORS:
            pass
    return clean


def afd_evidence(snapshot, now):
    """Compact, freshly validated NWS quotations; no asserted event coverage."""
    envelope = snapshot.get('event_afds') if isinstance(snapshot, dict) else None
    validated = validate_afds(envelope, snapshot, now)
    result = dict.fromkeys(OFFICES)
    if validated:
        for office, item in validated['offices'].items():
            if item is not None:
                result[office] = {key: item[key] for key in COMPACT_FIELDS}
    return result
