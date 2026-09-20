"""Fold repeated presentation caveats without changing source evidence or SVGs."""
from html import unescape
from html.parser import HTMLParser
import re

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
PROTECTED = {'script', 'style', 'svg', 'math', 'template', 'input', 'button', 'select', 'textarea', 'form', 'video', 'audio', 'iframe', 'object', 'embed', 'canvas', 'img'}
MOVE_CLASSES = {'brief-limits', 'context-limits', 'watch-foot', 'disclaimer'}
MOVE_DETAILS = {'chart-reading', 'planning-diagnostic', 'source-section'}
MOVE_IDS = {'sources-methods', 'wn3-diagnostic'}
BOILERPLATE = (
    'Source: Google Weather Lab.', 'Google attribution:', 'WeatherNext attribution:',
    'Experimental model screening,', 'Atlantic basin guidance is not a KCDW',
    'Hourly p10–p90 bands are marginal model percentiles,',
)
SHORTEN = {
    'Long-range scheduling guidance, not a decision to fly. Use the charts to follow timing and spread across runs.': 'Follow timing and spread across model runs.',
    'The magenta line is the ensemble mean; shading shows the hourly p10–p90 range, not the full ensemble minimum–maximum or event-total percentiles. Gold marks the forecast context window. WN3 also appears in the multimodel charts above. Swipe horizontally on small screens.': 'Magenta: WN3 mean · band: hourly p10–p90 · gold: forecast context window.',
    'Mean line and hourly p10–p90 range. A skewed mean may lie outside the band; percentiles do not establish flight suitability.': '',
    ' One deterministic scenario, not a probability.': '',
    '; pressure alone is not a flight verdict.': '.',
    ' Pressure is not a flight verdict.': '',
    ' Raw pressure, not a flight verdict.': '',
    'Land outlines: Natural Earth 1:110m, public domain; minor islands may be omitted. Plate-carree geographic view, not a navigation map. ': 'Basemap: Natural Earth. ',
    'Faint vertical bars: per-update p10–p90 where supplied, not confidence probabilities. WN3 mean may lie outside its band; no WN3 rain band when unavailable. GFS is deterministic, without a band. Separate model updates are not interpolated onto shared cycles.': 'Bars: p10–p90. GFS: deterministic line. Updates retain each model’s own timestamps.',
    'Missing guidance is not benign weather. ': '',
    'Missing hours remain gaps, not benign weather.': 'Gaps mark missing data.',
    'Missing hours are gaps. ': '',
    'Preceding-hour amounts, not cumulative rain or rain probability. ': '',
    'Preceding-hour amounts, not cumulative rain or rain probability.': '',
    'Statistics are not pooled or converted to flight probabilities. Hourly interpolation adds no timing skill.': '',
    ' (not minimum–maximum)': '',
    ' No favorable inference.': '',
    ' This is not evidence of no tropical impacts.': '',
    ' This is not a formation-risk assessment.': '',
    ' Not a weather clearance.': '',
    'Requested window covered by current outlook valid periods; this is not aviation readiness.': 'Current outlooks cover this window.',
    'Requested window not covered in full: gaps are unknown, not benign weather.': 'Outlook coverage is incomplete.',
    'WN3 cloud fraction is not ceiling height, and WN3 lacks ceiling/visibility/gust fields; other sources remain essential.': '',
    'No cloud, ceiling, visibility or aviation suitability inference follows from these plots.': '',
}


class _Blocks(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=False)
        self.document = document
        self.starts = [0] + [m.end() for m in re.finditer('\n', document)]
        self.stack = []
        self.blocks = []
        self.footers = []
        self.main_ends = []
        self.has_notes = False

    def position(self):
        row, column = self.getpos()
        return self.starts[row-1] + column

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        boundary = tag in PROTECTED or 'contenteditable' in attrs or any(key.startswith('on') for key in attrs)
        inside = any(block['boundary'] for block in self.stack)
        if boundary:
            for block in self.stack:
                block['protected'] = True
        if tag == 'details' and attrs.get('id') == 'notes-sources' and not inside:
            self.has_notes = True
        if tag == 'footer' and not inside:
            self.footers.append(self.position())
        if tag not in VOID:
            self.stack.append({'tag': tag, 'attrs': attrs, 'start': self.position(),
                               'text': [], 'pieces': [], 'boundary': boundary,
                               'protected': boundary or inside})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def record_text(self, data, editable=False):
        for block in self.stack:
            if block['tag'] in ('p', 'details', 'section'):
                block['text'].append(data)
                if editable:
                    block['pieces'].append((self.position(), data))

    def handle_data(self, data):
        self.record_text(data, editable=True)

    def handle_entityref(self, name):
        self.record_text('&' + name + ';')

    def handle_charref(self, name):
        self.record_text('&#' + name + ';')

    def handle_endtag(self, tag):
        if tag == 'main' and not any(block['boundary'] for block in self.stack):
            self.main_ends.append(self.position())
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index]['tag'] == tag:
                block = self.stack[index]
                del self.stack[index:]
                block['end'] = self.document.index('>', self.position()) + 1
                if tag in ('p', 'details', 'section'):
                    self.blocks.append(block)
                break


def compact_notes(document: str) -> str:
    """Edit only recognized prose blocks; preserve script/style bytes for CSP."""
    parser = _Blocks(document)
    parser.feed(document)
    if parser.has_notes:
        return document
    edits = []
    for block in parser.blocks:
        if block['protected']:
            continue
        tag, attrs = block['tag'], block['attrs']
        classes = set((attrs.get('class') or '').split())
        text = unescape(''.join(block['text'])).strip()
        raw = document[block['start']:block['end']]
        replacement = raw
        move = bool(classes & MOVE_CLASSES or attrs.get('id') in MOVE_IDS)
        move |= tag == 'details' and bool(classes & MOVE_DETAILS)
        move |= tag == 'p' and text.startswith(BOILERPLATE)
        if move:
            replacement = ''
        elif tag == 'p':
            if text.startswith('General thunder is distinct from severe risk.'):
                replacement = '<p>Days 1–3: thunder/severe categories. Days 4–8: severe-weather probability within 25 miles.</p>'
            elif text.startswith('CPC: not probability of rain'):
                replacement = '<p>CPC: period-average outlook. WPC: excessive-rainfall risk.</p>'
            else:
                for position, data in reversed(block['pieces']):
                    rewritten = data
                    for old, new in SHORTEN.items():
                        rewritten = rewritten.replace(old, new)
                    relative = position - block['start']
                    replacement = replacement[:relative] + rewritten + replacement[relative + len(data):]
                if replacement != raw and not re.sub('<[^>]*>', '', replacement).strip():
                    replacement = ''
        if replacement != raw:
            edits.append((block['start'], block['end'], replacement, raw))
    selected, last_end = [], -1
    # An enclosing disclosure wins over its nested paragraphs; never duplicate it.
    for item in sorted(edits, key=lambda item: (item[0], -item[1])):
        if item[0] >= last_end:
            selected.append(item)
            last_end = item[1]
    notes, seen = [], set()
    for _, _, _, raw in selected:
        if raw not in seen:
            notes.append(raw)
            seen.add(raw)
    disclosure = ('<details id="notes-sources" class="source-section page-notes"><summary>Notes &amp; sources</summary>'
                  + ''.join(notes) + '</details>')
    # Anchor to parsed elements, using original offsets for every edit.
    candidates = parser.footers + parser.main_ends + [len(document)]
    anchor = next(position for position in candidates
                  if not any(start <= position < end for start, end, _, _ in selected))
    selected.append((anchor, anchor, disclosure, ''))
    for start, end, replacement, _ in sorted(selected, reverse=True):
        document = document[:start] + replacement + document[end:]
    return document
