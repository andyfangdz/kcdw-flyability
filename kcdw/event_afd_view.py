"""Small source excerpts; the live narrative provides the operational synthesis."""
from html import escape
import re
from .events import local_clock

OFFICES = {'OKX': ('New York/Upton', 'Local'),
           'PHI': ('Mount Holly', 'South'), 'ALY': ('Albany', 'North')}


def validated_afds(snapshot, now):
    if 'event_afds' not in snapshot:
        return {}
    from .event_afd import afd_evidence
    return afd_evidence(snapshot, now)


def _clip(text, limit):
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    # Stop at a sentence when possible; mark every shortened quote.
    ends = [m.end() for m in re.finditer(r'(?<!\.)[.!?](?!\.)(?=\s)', text[:limit])]
    end = ends[-1] if ends else text.rfind(' ', 0, limit)
    return text[:end if end > 0 else limit] + ' …'


def _local(stamp):
    return local_clock(stamp, date=True)


def render_afds(snapshot, now):
    if 'event_afds' not in snapshot:
        return ''
    try:
        packets = validated_afds(snapshot, now)
    except (ValueError, TypeError, KeyError):
        packets = {}
    parts = ['<section id="forecaster-discussion" aria-labelledby="afd-title">'
             '<p class="eyebrow">NWS forecaster reasoning</p><h2 id="afd-title">Regional AFD readings</h2>'
             '<p class="small">Selected discussion and aviation excerpts. Forecast periods below are not automatically the checkride date; the weather story above explains their relevance.</p>'
             '<div class="afd-grid">']
    for office, (name, role) in OFFICES.items():
        packet = packets.get(office)
        parts.append(f'<article class="afd-card"><h3>{office} · {escape(name)}</h3><p class="small">{role} perspective</p>')
        if not packet:
            parts.append('<p>Current AFD unavailable.</p></article>')
            continue
        parts.append(f'<p class="small">Issued <time datetime="{escape(packet["issued_at"],quote=True)}">{escape(_local(packet["issued_at"]))}</time></p>')
        from .event_afd import discussion_quote
        quote = discussion_quote(packet['discussion_excerpt']) or _clip(packet['discussion_excerpt'], 1250)
        parts.append('<details><summary>Read selected excerpts</summary><blockquote>' + escape(quote) + '</blockquote>')
        if packet['aviation_excerpt']:
            parts.append('<p class="small"><strong>' + escape(packet['aviation_heading']) + '</strong> · Aviation outlook in the full AFD below.</p>')
        parts.append(f'<p class="small">Retrieved {escape(_local(packet["fetched_at"]))} · <a href="{escape(packet["product_url"],quote=True)}" rel="noopener noreferrer">Full issued AFD ↗</a></p></details></article>')
    return ''.join(parts) + '</div></section>'
