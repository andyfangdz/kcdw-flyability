"""Keep TypeSafe requests inside the service's model context by shortening long prose, never data.

The service rejects requests past its context (``max_tokens_exceeded``); the last
same-day success was ~99 KB and failures began near 108 KB. When a request is
over TARGET_BYTES this shortens, longest first: other offices' AFD discussion
excerpts, then synoptic product texts (CPC/WPC/NHC prose), then the local OKX
discussion. Each shortened text ends with SHORTENED and is flagged truncated.
Aviation sections, ranked passages, numbers and tables are never touched.
Callers' objects are never modified: an oversize state is deep-copied first,
because evidence dicts are shared (the event narrative hashes its evidence).
"""
from __future__ import annotations

import copy

TARGET_BYTES = 95_000
SHORTENED = ' [... shortened for model context ...]'
FLOORS = {'afd': 1200, 'product': 700}


def _size(state, questions):
    from .typesafe_client import encoded
    from .typesafe_packing import compact_state
    return len(encoded({'state': compact_state(state), 'questions': questions}).encode())


def _candidates(node, path=()):
    """(priority, length, holder, field, kind) for every shortenable prose field."""
    if isinstance(node, dict):
        text = node.get('discussion_excerpt')
        if isinstance(text, str):
            local = any('okx' in str(p).lower() for p in path)
            yield (0 if local else 2, len(text.removesuffix(SHORTENED)), node, 'discussion_excerpt', 'afd')
        for key, value in node.items():
            if key == 'products' and isinstance(value, list):
                for product in value:
                    if isinstance(product, dict) and isinstance(product.get('text'), str):
                        yield (1, len(product['text'].removesuffix(SHORTENED)), product, 'text', 'product')
            yield from _candidates(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _candidates(value, path + (i,))


def fit(state, questions, target=None):
    """Return ``state`` unchanged if it fits, else a shortened deep copy (or the best achievable)."""
    target = TARGET_BYTES if target is None else target
    if _size(state, questions) <= target:
        return state
    state = copy.deepcopy(state)
    while _size(state, questions) > target:
        options = [c for c in _candidates(state) if c[1] > FLOORS[c[4]]]
        if not options:
            return state  # every prose field is at its floor; the service decides from here
        _, length, holder, field, kind = max(options, key=lambda c: (c[0], c[1]))
        text = holder[field].removesuffix(SHORTENED)[:max(FLOORS[kind], length // 2)]
        cut = max(text.rfind('. '), text.rfind('\n'))
        holder[field] = (text[:cut + 1] if cut > FLOORS[kind] // 2 else text) + SHORTENED
        if kind == 'afd':
            holder['discussion_truncated'] = True
        else:
            holder['truncated'] = True
    return state
