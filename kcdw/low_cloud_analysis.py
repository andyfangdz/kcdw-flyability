"""Compact, independently validated cloud diagnostics for page and narration."""
from html import escape
from .events import local_clock as _local
from .cloud_ceiling import validate_ceiling
from .cloud_layer_signals import validate_layer_signals
from .source_presentation import is_direct, source_description, SOURCE_LICENSES


def low_cloud_evidence(snapshot, now):
    ceiling = validate_ceiling(snapshot.get('cloud_ceiling'), snapshot, now)
    layers = validate_layer_signals(snapshot.get('cloud_layer_signals'), snapshot, now)
    result: dict = {'ceiling': None, 'layers': None}
    if ceiling:
        result['ceiling'] = {k: ceiling[k] for k in ('model', 'model_init', 'fetched_at', 'notes')}
        result['ceiling']['samples'] = [{k: s[k] for k in ('valid_at', 'ceiling_agl_ft', 'low_cloud_pct', 'neighborhood')} for s in ceiling['samples']]
    if layers and any(s['ok'] for family in ('profiles', 'ensembles') for s in layers[family].values()):
        data = result['layers'] = {k: layers[k] for k in ('collected_at', 'sample_times', 'thresholds', 'limitations')}
        data['binding'] = ('Native packet references have verified runs; Open-Meteo fallback packets remain rolling and unverified. Sources are separate, never mixed into one claimed run.' if layers['version'] == 2 else
                           'Rolling explicit-model responses; initialization not response-bound. Not exact native IFS/AIFS runs.')
        if layers['version'] == 2:
            data['source_bindings'] = {}
        data['units'] = 'RH and cloud %, temperatures °C, surface wind kt and degrees, pressure hPa, heights geopotential m MSL.'
        for family in ('profiles', 'ensembles'):
            data[family] = {}
            for key, source in layers[family].items():
                if not source['ok']:
                    data[family][key] = None
                    continue
                d = source['data']
                if layers['version'] == 2:
                    data['source_bindings'][family+'/'+key] = ({'binding': 'response-bound',
                        'init': d['metadata']['initialization_time'], 'provider': d['metadata']['source_provider']}
                        if is_direct(d) else {'binding': 'rolling; unverified', 'provider': 'Open-Meteo fallback'})
                if family == 'ensembles':
                    data[family][key] = {'model': d['model'], 'points': d['points'], 'all_three': d['all_three']}
                else:
                    data[family][key] = {'model': d['model'], 'points': [{
                        'time': p['time'], 'surface': p['surface'],
                        'levels': {k: p['levels'][k] for k in ('925', '850')},
                        'thermal': {'925_850': p['thermal']['925_850']},
                    } for p in d['points']]}
    return result


def narrative_cloud_evidence(snapshot, now):
    """Columnar scalar rows keep the requested diagnostics inside the 60KB budget."""
    data = low_cloud_evidence(snapshot, now)
    layer = data['layers']
    if layer:
        compact = {k: layer[k] for k in ('collected_at', 'sample_times', 'thresholds', 'limitations', 'binding', 'units')}
        if 'source_bindings' in layer:
            compact['source_bindings'] = layer['source_bindings']
        compact['profile_columns'] = ['time_utc', 'surface_rh_pct', 'temperature_2m_c', 'dewpoint_2m_c',
            'wind_from_deg', 'wind_kt', 'rh925_pct', 'rh850_pct', 'height925_msl_m', 'height850_msl_m',
            'temperature925_c', 'temperature850_c', 'inversion925_to_850', 'lapse925_to_850_c_per_km']
        compact['profiles'] = {}
        for key, model in layer['profiles'].items():
            rows = None
            if model:
                rows = []
                for p in model['points']:
                    s, levels, cap = p['surface'], p['levels'], p['thermal']['925_850']
                    rows.append([p['time'], *[s[k] for k in ('relative_humidity_2m', 'temperature_2m',
                        'dew_point_2m', 'wind_direction_10m', 'wind_speed_10m')],
                        *[levels[level][field] for field in ('relative_humidity', 'geopotential_height', 'temperature')
                          for level in ('925', '850')], cap['inversion'], cap['lapse_c_per_km']])
            compact['profiles'][key] = rows
        screens = ['low_cloud', 'rh925', 'joint_low_cloud_rh925', 'joint_surface_rh925']
        compact['member_screen_columns'] = ['time_utc', *screens]
        compact['member_count_format'] = '[passing_count, eligible_denominator]; [null, 0] is unavailable. all_three uses the same member IDs at the named three samples.'
        compact['ensembles'] = {}
        for key, model in layer['ensembles'].items():
            rows = None
            if model:
                rows = [[p['time'], *[[p['screens'][k]['count'], p['screens'][k]['denominator']] for k in screens]] for p in model['points']]
                rows.append(['all_three', *[[model['all_three'][k]['count'], model['all_three'][k]['denominator']] for k in screens]])
            compact['ensembles'][key] = rows
        data['layers'] = compact
    return data


def _n(v, places=0):
    return '—' if v is None else f'{v:,.{places}f}'


def _count(s):
    return 'Unavailable' if s['count'] is None else f"{s['count']}/{s['denominator']}"


def _table(headers, rows, label):
    return (f'<div class="chart-scroll" tabindex="0" role="region" aria-label="{escape(label)}"><table><thead><tr>'
            + ''.join(f'<th scope="col">{escape(h)}</th>' for h in headers) + '</tr></thead><tbody>'
            + ''.join('<tr><th scope="row">'+escape(row[0])+'</th>'+''.join('<td>'+escape(v)+'</td>' for v in row[1:])+'</tr>' for row in rows)
            + '</tbody></table></div>')


def render_low_cloud(snapshot, now):
    data = low_cloud_evidence(snapshot, now)
    c, layer = data['ceiling'], data['layers']
    parts = ['<section id="low-cloud-analysis" aria-labelledby="low-cloud-title"><p class="eyebrow">Maneuvering room</p><h2 id="low-cloud-title">Low-cloud analysis</h2>']
    if not c and not layer:
        return ''.join(parts) + '<p>Current ceiling and cloud-layer diagnostics unavailable.</p></section>'
    if c:
        heights = [s['ceiling_agl_ft'] for s in c['samples'] if s['ceiling_agl_ft'] is not None]
        if heights and all(h < 1000 for h in heights) and len(heights) == len(c['samples']):
            parts.append('<p><strong>GFS keeps a sub-1,000-ft deck at every native sample.</strong> That scenario would obstruct the visual maneuvers; later-day clearing is not supported by this run.</p>')
        elif any(h < 1000 for h in heights):
            parts.append('<p><strong>GFS has sub-1,000-ft ceilings at some native samples.</strong> Timing and daytime recovery remain important.</p>')
        else:
            parts.append('<p>Compare native ceiling height with independent moist-layer depth and the cloudy ensemble subset below.</p>')
        parts.append('<h3>Native GFS ceiling · ft above model terrain</h3><p class="small">Run '+escape(c['model_init'])+' · retrieved '+escape(_local(c['fetched_at'], True))+'</p><div class="brief-cards">')
        for s in c['samples']:
            n = s['neighborhood']
            parts.append(f'<article class="brief-card"><h3>{escape(_local(s["valid_at"]))} Eastern</h3><p class="brief-value"><strong>{_n(s["ceiling_agl_ft"])} ft</strong></p><p>Low cloud {_n(s["low_cloud_pct"])}%</p><p>{n["under_1000_ft"]}/{n["ceiling_valid"]} valid nearby cells below 1,000 ft</p></article>')
        parts.append('</div><p class="small">Native three-hour samples on the event date, including the closing endpoint when available; heights are approximate, not an airport TAF.</p>')
    else:
        parts.append('<h3>Native GFS ceiling</h3><p>Current native ceiling unavailable.</p>')
    if layer:
        parts.append('<h3>Independent cloud-layer signals</h3><p class="small">Collected '+escape(_local(layer['collected_at'], True))+' · '+('source binding listed below.' if 'source_bindings' in layer else 'rolling model profiles, not exact native-run attribution.')+'</p>')
        rows = []
        for key, source in layer['profiles'].items():
            if source is None:
                rows.append([key, 'Unavailable', '—', '—', '—', '—'])
                continue
            for p in source['points']:
                s = p['surface']; levels = p['levels']; thermal = p['thermal']['925_850']
                cap = ('Inversion' if thermal['inversion'] else 'No inversion' if thermal['inversion'] is False else 'Unknown')
                rows.append([source['model']+' · '+_local(p['time']),
                             _n(s['relative_humidity_2m'])+'%',
                             _n(levels['925']['relative_humidity'])+' / '+_n(levels['850']['relative_humidity'])+'%',
                             _n(s['temperature_2m'], 1)+' / '+_n(s['dew_point_2m'], 1)+' °C',
                             _n(s['wind_direction_10m'])+'° / '+_n(s['wind_speed_10m'], 1)+' kt', cap])
        parts.append(_table(['Model · Eastern', 'Surface RH', '925 / 850 RH', 'Surface T / Td', 'Surface wind', '925→850'], rows, 'Cloud-layer profiles'))
        parts.append('<h3>Cloudy ensemble members</h3><p>Low-cloud coverage ≥75%; last column follows the <strong>same members at all three samples</strong>.</p>')
        headers = ['Model']+[_local(t)+' Eastern' for t in layer['sample_times']]+['All three']
        rows = []
        moisture_rows = []
        for key, source in layer['ensembles'].items():
            if source is None:
                rows.append([key]+['Unavailable']*4)
                continue
            rows.append([source['model']]+[_count(p['screens']['low_cloud']) for p in source['points']]+[_count(source['all_three']['low_cloud'])])
            for screen, label in [('rh925', '925 RH ≥90%'), ('joint_surface_rh925', 'Surface + 925 RH ≥90%'), ('joint_low_cloud_rh925', 'Cloud ≥75% + 925 RH ≥90%')]:
                moisture_rows.append([source['model']+' · '+label]+[_count(p['screens'][screen]) for p in source['points']]+[_count(source['all_three'][screen])])
        parts.append(_table(headers, rows, 'Low-cloud ensemble member screens'))
        parts.append('<details><summary>Moist-member cross-checks</summary>'+_table(headers, moisture_rows, 'Moist ensemble member screens')+'</details>')
    else:
        parts.append('<p>Independent profiles and member screens unavailable.</p>')
    parts.append('<details><summary>Low-cloud method &amp; sources</summary>')
    if c:
        parts.append('<p>'+escape(c['notes'])+'</p><p>Grid 41.00°N, 74.25°W. Nearby ranges are spatial variation, not ensemble uncertainty.</p>')
        original = validate_ceiling(snapshot.get('cloud_ceiling'), snapshot, now)
        if original:
            parts.append('<p><a href="'+escape(original['samples'][0]['source_url']+'.idx', quote=True)+'">NOAA native field inventory</a></p>')
    if layer:
        if 'source_bindings' in layer:
            for key, binding in layer['source_bindings'].items():
                parts.append('<p>'+escape(key+' · '+binding['provider']+' · '+binding['binding']+(' · '+binding['init'] if binding.get('init') else ''))+'</p>')
            parts.append('<p>'+escape(SOURCE_LICENSES)+'</p>')
        parts.append('<p>'+escape(layer['limitations'])+'</p><p>'+escape(layer['binding'])+'</p><p><a href="https://open-meteo.com/en/docs">Open-Meteo model profiles</a> · <a href="https://open-meteo.com/en/docs/ensemble-api">Ensemble member source</a> · CC BY 4.0</p>')
    parts.append('<p>Surface drying alone does not establish clearing of an elevated moist layer. GFS ceiling and GFS RH are one model, not independent votes. Member screens are not ceiling or checkride-failure probabilities.</p></details></section>')
    return ''.join(parts)
