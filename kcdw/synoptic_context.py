"""Supplementary tropical/extended context, separate from aviation readiness."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .common import iso_z


def _providers(include_spc=False):
    from .tropical_guidance import collect_wn3_cyclones, render_wn3_cyclones
    from .nhc_guidance import collect_nhc, render_nhc
    from .extended_guidance import collect_extended, render_extended
    providers = [
        ('wn3_cyclones', 'WN3 tropical guidance', collect_wn3_cyclones, render_wn3_cyclones),
        ('nhc', 'NWS / National Hurricane Center', collect_nhc, render_nhc),
        ('extended', 'CPC / Weather Prediction Center', collect_extended, render_extended),
    ]
    if include_spc:
        from .spc_guidance import collect_spc, render_spc
        providers = [('spc', 'NWS / Storm Prediction Center', collect_spc, render_spc), providers[2], providers[1], providers[0]]
    return providers


def collect_context(client, now: datetime, include_spc=False) -> dict:
    result = {}
    for key, _, collect, _ in _providers(include_spc=include_spc):
        try:
            source = collect(client, now)
            if not isinstance(source, dict) or type(source.get('ok')) is not bool:
                raise ValueError('invalid source envelope')
            result[key] = source
        except Exception:
            result[key] = {'ok': False, 'fetched_at': iso_z(now), 'data': None,
                           'error': 'Supplementary source collection failed'}
    return result


def report_window(snapshot: dict, now: datetime) -> tuple[datetime, datetime]:
    tz = ZoneInfo(snapshot['airport']['timezone'])
    start = datetime.fromisoformat(snapshot['report_dates'][0] + 'T08:00:00').replace(tzinfo=tz)
    end = datetime.fromisoformat(snapshot['report_dates'][-1] + 'T20:00:00').replace(tzinfo=tz)
    # Archive windows stay anchored to the assessment, not shifted to render date.
    return max(start, now) if now < end else start, end


def _fragments(context, start, end, now):
    context = context if isinstance(context, dict) else {}
    for key, label, _, render in _providers(include_spc='spc' in context):
        try:
            markup = render(context.get(key, {}), start, end, now)
            if not isinstance(markup, str):
                raise ValueError('invalid rendered context')
        except Exception:
            markup = f'<h3>{escape(label)}</h3><p>Unavailable — no conclusion about local weather.</p>'
        yield key, label, markup


def render_context(context: dict, start: datetime, end: datetime, now: datetime) -> str:
    cards = ''.join(f'<article class="context-source" data-context-source="{key}">{markup}</article>'
                    for key, _, markup in _fragments(context, start, end, now))
    tz = ZoneInfo('America/New_York')
    label = 'WPC / NHC / SPC · Regional guidance' if isinstance(context, dict) and 'spc' in context else 'Tropical systems / Extended outlook'
    window = f'{start.astimezone(tz):%b %d, %H:%M} – {end.astimezone(tz):%b %d, %H:%M %Z}'
    return ('<section id="synoptic-context" class="synoptic-context" aria-labelledby="context-title">'
            f'<header><p class="eyebrow">{label}</p>'
            '<h2 id="context-title">The wider weather picture</h2>'
            f'<p class="muted">Planning window: {escape(window)}. Each product has its own forecast horizon.</p></header>'
            f'<div class="context-grid">{cards}</div>'
            '<p class="context-limits">NHC is the official tropical forecast; WN3 tracks are experimental model guidance. '
            'CPC probabilities describe above/below-normal period averages, not the chance of rain on one day. '
            'A product that does not cover the planning window cannot establish an all-clear.</p></section>')


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('svg', 'script', 'style'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('svg', 'script', 'style') and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def _official_product_evidence(key, source, start, end, now):
    """Allocate a bounded record to every product before narrative excerpts."""
    data = source.get('data') or {}
    units = []
    if key == 'extended' and isinstance(data, dict) and ('cpc' in data or 'wpc' in data):
        from .extended_guidance import _state, _coverage
        for provider in ('cpc', 'wpc'):
            for p in data.get(provider, {}).get('products', [])[:16]:
                status = _state(p, provider, now)
                fields = ('id', 'title', 'issued_at', 'issue_precision', 'valid_start', 'valid_end',
                          'source_url', 'scope', 'regional_categories', 'category', 'probability_label')
                unit = {field: p[field] for field in fields if field in p}
                unit.update(source=provider, status=status,
                            coverage=_coverage(p, start, end) if status == 'current' else 'unusable',
                            text=str(p.get('excerpt', ''))[:700], truncated=len(str(p.get('excerpt', ''))) > 700)
                units.append(unit)
        return units
    if key == 'nhc' and isinstance(data, dict) and ('outlook' in data or 'storms' in data):
        from .nhc_guidance import _fresh, _dt, _coverage, INVENTORY_AGE
        inventory = data.get('inventory') or {}
        cache_fresh = _fresh(source.get('fetched_at'), now, INVENTORY_AGE)
        inv_fresh = cache_fresh and _fresh(inventory.get('feed_refreshed_at'), now, INVENTORY_AGE)
        units.append({'source': 'nhc', 'id': 'nhc_inventory', 'status': 'current' if inv_fresh else 'unverified',
                      'atlantic_count': inventory.get('atlantic_count') if inv_fresh else None,
                      'feed_refreshed_at': inventory.get('feed_refreshed_at'),
                      'text': 'Active-storm inventory, not formation risk; unverified inventory cannot establish absence.'})
        products = [('nhc_formation', 'Atlantic 7-day formation outlook', data.get('outlook'), None)]
        products.extend((s.get('id'), s.get('name'), s.get('advisory'), s.get('error')) for s in data.get('storms', [])[:8])
        for identity, title, p, error in products:
            unit: dict = {'source': 'nhc', 'id': identity, 'title': title, 'status': 'unavailable'}
            if not p:
                units.append(unit)
                continue
            unit.update({field: p[field] for field in ('issued_at', 'valid_start', 'valid_end', 'source_url') if field in p})
            try:
                issued, first, last = (_dt(p[field]) for field in ('issued_at', 'valid_start', 'valid_end'))
                valid = first == issued and first < last <= first + timedelta(days=7)
                if 'track' in p:
                    previous = first
                    valid = valid and isinstance(p['track'], list) and 1 <= len(p['track']) <= 12
                    for point in p['track'][:12]:
                        at = _dt(point['valid_at'])
                        valid = valid and previous < at <= last
                        previous = at
                    valid = valid and previous == last
                fresh = valid and not error and cache_fresh and _fresh(issued, now)
                if 'next_advisory_at' in p:
                    fresh = fresh and now <= _dt(p['next_advisory_at']) + timedelta(hours=2)
                unit['status'] = 'current' if fresh else 'expired_or_invalid'
                unit['coverage'] = _coverage(start, end, first, last) if fresh else 'unusable'
                if fresh:
                    if 'track' in p:
                        unit['track'] = [{f: point.get(f) for f in ('valid_at', 'latitude', 'longitude', 'wind_kt', 'gust_kt', 'status')} for point in p['track'][:12]]
                    if 'areas' in p:
                        unit['areas'] = [{f: str(area.get(f, ''))[:300] for f in ('narrative', 'chance_48h', 'chance_7day')} for area in p['areas'][:8]]
                    unit['no_formation_expected'] = bool(p.get('no_formation_expected'))
                    unit['text'] = str(p.get('narrative', ''))[:500]
                    unit['truncated'] = len(str(p.get('narrative', ''))) > 500
            except (ValueError, TypeError, KeyError, AttributeError):
                unit['status'] = 'invalid'
            units.append(unit)
        return units
    return None


def context_evidence(context: dict, start: datetime, end: datetime, now: datetime) -> dict:
    """Reuse render-time validity checks and exclude large raw track arrays."""
    summaries = []
    for key, label, markup in _fragments(context, start, end, now):
        try:
            products = _official_product_evidence(key, (context or {}).get(key, {}), start, end, now)
        except (ValueError, TypeError, KeyError, AttributeError):
            products = [{'source': key, 'status': 'invalid', 'text': 'Product summaries unavailable; no favorable inference.'}]
        if products is not None:
            summaries.extend(products)
            continue
        parser = _Text()
        parser.feed(markup)
        text = ' '.join(' '.join(parser.parts).split())
        summaries.append({'source': key, 'label': label, 'text': text[:6000], 'truncated': len(text) > 6000})
    return {'window_start': iso_z(start), 'window_end': iso_z(end), 'evaluated_at': iso_z(now),
            'products': summaries,
            'use': 'Supplementary regional context, not aviation-readiness evidence. Respect each product issue/valid time; out-of-range and missing products are unknown. WN3 member proximity is not landfall or airport-impact probability; CPC category probabilities are not rain chances.'}
