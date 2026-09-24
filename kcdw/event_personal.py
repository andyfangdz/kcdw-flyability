"""Pilot-set limits and reference flights for one event, snapshot-bound like timing.

Optional events.json metadata: {"personal": {slug: {slug, gust_limit_kt, references}}}.
Each snapshot archives its own validated copy; invalid config is absent, never guessed.
"""
from __future__ import annotations

import re
from datetime import date

from .common import load_json
from .events import DEFAULT_CONFIG

MAX_REFERENCES = 5


def validate_personal(value, event):
    if not isinstance(value, dict) or set(value) != {'slug', 'gust_limit_kt', 'references'} or value['slug'] != event.slug:
        raise ValueError('invalid personal settings')
    limit = value['gust_limit_kt']
    if type(limit) is not int or not 5 <= limit <= 60:
        raise ValueError('invalid gust limit')
    refs = value['references']
    if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
        raise ValueError('invalid references')
    clean = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {'label', 'date', 'sustained_kt', 'gust_kt', 'note'}:
            raise ValueError('invalid reference')
        if not isinstance(ref['label'], str) or not 0 < len(ref['label']) <= 60 or not isinstance(ref['note'], str) or len(ref['note']) > 160:
            raise ValueError('invalid reference text')
        if not isinstance(ref['date'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ref['date']):
            raise ValueError('invalid reference date')
        date.fromisoformat(ref['date'])
        if any(type(ref[k]) not in (int, float) or not 0 <= ref[k] <= 150 for k in ('sustained_kt', 'gust_kt')) or ref['gust_kt'] < ref['sustained_kt']:
            raise ValueError('invalid reference wind')
        clean.append(dict(ref))
    return {'slug': value['slug'], 'gust_limit_kt': limit, 'references': clean}


def load_event_personal(event, path=None):
    try:
        data = load_json(path or DEFAULT_CONFIG)
        return validate_personal(data.get('personal', {}).get(event.slug), event)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def personal(snapshot):
    """Validated archived settings, or None."""
    from .events import Event
    try:
        return validate_personal(snapshot.get('event_personal'), Event(**snapshot['event']))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
