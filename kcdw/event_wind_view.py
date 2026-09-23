"""Compact wind tables and independently citable, snapshot-only evidence."""
from html import escape
from .events import local_clock as _local

SOURCES={'wind_surface':('Flight-window winds · ensembles and NWS','event_wind'),
         'wind_deterministic':('Flight-window gusts · NBM blend and deterministic models','event_gusts'),
         'wind_native':('Native winds aloft','native_wind'),
         'wind_trends':('Saved wind/gust forecast evolution','wind_trends')}


def wind_sources(snapshot,now):
    result={}
    for alias,(_,key) in SOURCES.items():
        if key not in snapshot:continue
        try:
            if key=='event_wind':
                from .event_wind import wind_evidence
                value=wind_evidence(snapshot,now)
                available=value and (value.get('nws') or any(value.get('ensembles',{}).values()))
            elif key=='event_gusts':
                from .event_gusts import gust_evidence
                value=gust_evidence(snapshot,now)
                available=value and any(value.get('models',{}).values())
            elif key=='native_wind':
                from .native_wind import native_wind_evidence
                value=native_wind_evidence(snapshot,now)
                available=value and any(value.get('models',{}).values())
            else:
                from .event_wind_trends import wind_trend_evidence
                value=wind_trend_evidence(snapshot,now)
                available=value and any(value.get('models',{}).values())
            result[alias]=value if available else None
        except (ImportError,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError):
            result[alias]=None
    return result


def _n(value):
    return '—' if value is None else f'{value:.1f}'.removesuffix('.0')


def _pair(value):
    return 'Unavailable' if not value or not value.get('n') else _n(value.get('p50'))+' / '+_n(value.get('p90'))


def _count(value,key):
    return 'Unavailable' if not value or not value.get('n') or value.get(key) is None else f'{value[key]}/{value["n"]}'


def _table(headers,rows,label):
    return ('<div class="wind-scroll" tabindex="0" role="region" aria-label="'+escape(label)+'"><table class="wind-table"><thead><tr>'+
            ''.join('<th scope="col">'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+
            ''.join('<tr><th scope="row">'+escape(str(row[0]))+'</th>'+''.join('<td>'+escape(str(v))+'</td>' for v in row[1:])+'</tr>' for row in rows)+
            '</tbody></table></div>')


def _profile(point):
    return 'Unavailable' if not point else _n(point.get('from_true_deg'))+'° / '+_n(point.get('speed_kt'))


def _run(stamp):
    from .common import parse_time
    return parse_time(stamp).strftime('%b %-d %HZ')


def _nws_cross(samples):
    import math
    from .event_wind import HEADINGS
    if any(p.get('gust_kt') is None or p.get('from_deg') is None for p in samples):return 'Unavailable'
    return ' / '.join(_n(round(max(p['gust_kt']*abs(math.sin(math.radians(p['from_deg']-h))) for p in samples),1)) for h in HEADINGS.values())


def _deterministic(packet,nws):
    """NWS grid beside NBM and single-run models: where the gust split comes from."""
    if not packet:return '<h3>Deterministic &amp; blend gusts</h3><p>Deterministic and NBM gust guidance unavailable for this flight window.</p>'
    rows=[]
    if nws:
        samples=nws['samples']
        rows.append(['NWS grid','Updated '+_local(nws['issued_at'],True),
                     ' · '.join(_n(p.get('gust_kt')) for p in samples),
                     _n(max((p['gust_kt'] for p in samples if p.get('gust_kt') is not None),default=None)),
                     _n(sum(p['wind_kt'] for p in samples)/len(samples)) if all(p.get('wind_kt') is not None for p in samples) else '—',
                     _nws_cross(samples)])
    for key,model in packet['models'].items():
        if not model:continue
        c=model['gust_crosswind_max_kt']
        rows.append([model['label'],_run(model['run']) if model.get('run') else 'Page GFS source',
                     ' · '.join(_n(p['gust_kt']) for p in model['samples']),_n(model['peak_gust_kt']),_n(model['mean_wind_kt']),
                     'Unavailable' if c['04'] is None else _n(c['04'])+' / '+_n(c['10'])])
    from .event_gusts import MODELS
    labels={m[0]:m[1] for m in MODELS}
    missing=[labels.get(k,k) for k,v in packet['models'].items() if v is None]
    hours=' · '.join(_local(p['at']) for p in next(m for m in packet['models'].values() if m)['samples'])
    return ('<h3>Deterministic &amp; blend gusts</h3><p class="small">NWS grids start from NBM, so an NWS–ensemble gust split usually traces to NBM. Each other row is one model run requested by initialization time; none is a probability.</p>'+
            _table(['Source','Run',f'Gust by hour ({hours})','Peak gust','Mean sustained','Peak gust crosswind RWY 4 / 10'],rows,'Deterministic and blend gusts')+
            ('<p class="small">No covering run yet: '+escape(', '.join(missing))+'.</p>' if missing else ''))


def render_wind(snapshot,now):
    sources=wind_sources(snapshot,now)
    if not sources:return ''
    parts=['<section id="wind-analysis"><h2>Wind &amp; runways</h2>']
    from .event_timing import timing_evidence
    timing=timing_evidence(snapshot)
    if timing and timing.get('flight_end'):
        parts.append('<p>Expected flight around '+escape(timing['flight_start'])+'–'+escape(timing['flight_end_local'])+'. All wind speeds below are knots; directions are from true north.</p>')
    surface=sources.get('wind_surface')
    if surface:
        nws=surface.get('nws')
        parts.append('<h3>NWS flight-window guidance</h3>')
        if nws:
            parts.append('<p class="small">Grid updated '+escape(_local(nws['issued_at'],True))+' · retrieved '+escape(_local(nws['fetched_at'],True))+' · not a TAF.</p>')
            rows=[[_local(p['at']),_n(p.get('from_deg'))+'°',_n(p.get('wind_kt')),_n(p.get('gust_kt'))] for p in nws['samples']]
            if nws.get('interval_rows'):
                rows=[[_local(a)+'–'+_local(b),_n(direction)+'°',_n(wind),_n(gust)]
                      for a,b,wind,gust,direction in nws['interval_rows']]
                p=nws['samples'][-1]
                rows.append([_local(p['at'])+' · return sample',_n(p.get('from_deg'))+'°',_n(p.get('wind_kt')),_n(p.get('gust_kt'))])
            parts.append(_table(['Valid interval · Eastern','From · true','Sustained · kt','Gust · kt'],rows,'NWS flight-window winds'))
        else:parts.append('<p>Current NWS grid wind guidance unavailable for this flight window.</p>')
        rows=[];counts=[];provenance=[]
        for key,model in surface['ensembles'].items():
            if not model:
                rows.append([key] + ['Unavailable'] * 4)
                counts.append([key] + ['Unavailable'] * 5)
                continue
            gust=model['gust_max'];label=model['label']
            directions=', '.join(f'{k} {v}' for k,v in model.get('direction_counts',{}).items()) or 'Unavailable'
            r4=model['crosswind']['04'];r10=model['crosswind']['10']
            rows.append([label, _pair(model['sustained']), _pair(gust), _pair(r4), _pair(r10)])
            counts.append([label, _count(gust,'ge20'), _count(gust,'ge25'), _count(r4,'ge15'), _count(r10,'ge15'), directions])
            provenance.append(label+' · fetched '+_local(model['fetched_at'],True)+' · advertised init '+str(model.get('advertised_init') or 'unknown')+
                              ' · coverage ends '+str(model.get('data_end') or 'unknown')+
                              (' · likely source '+model['likely_init']+' (inferred/unverified)' if model.get('likely_init') else ' · supplying run unverified'))
        parts.append(_deterministic(sources.get('wind_deterministic'),nws))
        parts.append('<h3>Ensemble flight-window wind</h3><p class="small">Median / p90 of member flight maxima, in kt. Crosswind projects each member’s gust along its mean direction; compare with your own limits.</p>')
        parts.append(_table(['Model','Sustained','Gust','RWY 4 crosswind','RWY 10 crosswind'], rows, 'Ensemble wind and runway crosswind'))
        parts.append('<details><summary>Gust thresholds &amp; direction counts</summary>' +
                     _table(['Model','Gust ≥20 kt','Gust ≥25 kt','RWY 4 ≥15 kt','RWY 10 ≥15 kt','Direction at return'], counts, 'Wind threshold member counts') + '</details>')
        parts.append('<details><summary>Wind sampling &amp; source clocks</summary><p>True runway headings: 4 = 030°, 10 = 083°; runway availability and NOTAMs are not checked. Sustained winds include departure and return; gusts use intervals ending within the flight, and hourly points can be interpolated. A gust maximum is the largest available sample, not a guaranteed peak. Member counts are not calibrated probabilities. Actual peak-gust direction is unknown; missing gust fields are unknown, not calm.</p><ul>'+''.join('<li>'+escape(p)+'</li>' for p in provenance)+'</ul><p><a href="https://api.weather.gov/gridpoints/OKX/23,48">NWS grid</a> · <a href="https://open-meteo.com/en/docs/ensemble-api">Open-Meteo ensemble members · CC BY 4.0</a> · <a href="https://www.airnav.com/airport/KCDW">Published runway headings</a></p></details>')
    else:parts.append('<p>Current ensemble runway-wind and NWS diagnostics unavailable.</p>')
    native=sources.get('wind_native')
    parts.append('<details><summary>Native winds aloft &amp; gust intervals</summary>')
    if native:
        rows=[];clocks=[]
        for key,model in native['models'].items():
            if not model:
                rows.append([key,'Unavailable','—','—','—','—']);continue
            clocks.append(model['label']+' · init '+model['init']+' · fetched '+_local(model['fetched_at'],True))
            for p in model['samples']:
                g=p.get('gust')
                gust='Unavailable' if not g else (_n(g['kt'])+' · '+('instant' if g['semantics'].startswith('instant') else _local(g['start'])+'–'+_local(g['end'])+' maximum'))
                rows.append([model['label'],_local(p['at']),_profile(p.get('surface')),_profile(p.get('925')),_profile(p.get('850')),gust])
        parts.append('<p class="small">Direction° / speed kt. Bracketing samples do not resolve exact flight hours; pressure levels are not AGL heights.</p>')
        parts.append(_table(['Model','Valid Eastern','10 m','925 hPa','850 hPa','Native gust · interval'],rows,'Native wind profiles'))
        parts.append('<details><summary>Native model clocks &amp; limitations</summary><ul>'+''.join('<li>'+escape(c)+'</li>' for c in clocks)+'</ul><p>Native GFS instantaneous gust and IFS interval-maximum gust are different statistics. AIFS gusts may be unavailable. A surface/aloft speed difference alone does not establish turbulence or LLWS. Forecast grid points are not airport observations. Model cycles can differ.</p><p><a href="https://www.ecmwf.int/en/forecasts/datasets/open-data">ECMWF open data</a> · <a href="https://noaa-gfs-bdp-pds.s3.amazonaws.com/">NOAA GFS native fields</a></p></details>')
    else:parts.append('<p>Current native wind profiles unavailable.</p>')
    parts.append('</details>')
    trend=sources.get('wind_trends')
    if trend:
        rows=[]
        for key,model in trend.get('models',{}).items():
            if not model:continue
            states=model['states']
            if states and isinstance(states[0],list):states=[dict(zip(trend['columns'],s)) for s in states]
            for s in states:
                rows.append([model.get('label',key),_local(s['collected_at'],True),_n(s.get('wind_center'))+' / '+_n(s.get('wind_p90')),_n(s.get('gust_center'))+' / '+_n(s.get('gust_p90'))])
        parts.append('<details><summary>Recent wind/gust forecast evolution</summary><p>Same valid sample: '+escape(_local(trend['sample_at'],True))+'. Latest distinct values within a bounded 36-hour archive scan; repeated values are not new independent confirmations. These are snapshot changes, not verified rolling model cycles. WN3 center is mean; ensemble centers are medians; GFS has no percentile band.</p>')
        parts.append(_table(['Model','Collection Eastern','Sustained · center / p90','Gust · center / p90'],rows,'Saved wind forecast evolution')+'</details>')
    parts.append('</section>')
    return ''.join(parts)
