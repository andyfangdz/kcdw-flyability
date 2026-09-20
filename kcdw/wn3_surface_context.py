"""Derived surface RH and 100 m wind context; neither upper-air RH nor gusts."""
import math
from html import escape

from .common import parse_time
from .events import local_clock
from .weathernext3_bigquery import BigQueryStore, validate_provenance
from .wn3_cloud_analysis import cloud_evidence

ARRAYS = ['wind_speed_100m_'+s for s in ('mean','p10','p90')]
KNOTS = 3600/1852
RH_METHOD = ('Approximate surface RH over liquid water from WN3 ensemble-mean 2 m temperature and dew point, '
             'using 100 × exp(17.67 Td/(Td+243.5) − 17.67 T/(T+243.5)), °C; capped at 100%. '
             'This is RH of the mean inputs, not ensemble-mean RH or an RH percentile. No WN3 925/850 hPa RH is supplied by BigQuery.')
WIND_METHOD = ('100 m mean and p10–p90 are elevated wind-speed statistics, not gust statistics. '
               'They provide context for a possible mix-down scenario only if momentum reaches the surface. '
               'Surface stability, mixing depth, terrain and convection are unresolved here; 100 m speed is neither a predicted gust nor a gust upper bound.')


def surface_rh(temperature, dewpoint):
    if any(type(v) not in (int,float) or not math.isfinite(v) or not -45 <= v <= 60 for v in (temperature,dewpoint)):
        return None
    return round(min(100.,100*math.exp(17.67*dewpoint/(dewpoint+243.5)-17.67*temperature/(temperature+243.5))),1)


def collect_wind(snapshot, now, *, store=None):
    clouds=cloud_evidence(snapshot,now)
    if not clouds:
        return None
    store=store or BigQueryStore()
    result=store.fetch(clouds['init_time'],arrays=ARRAYS)
    indexed={parse_time(r['valid_time']):r for r in result['rows']}
    hours=[]
    for sample in clouds['hours']:
        raw=indexed[parse_time(sample['at'])]
        if parse_time(raw['init_time']) != parse_time(clouds['init_time']) or raw['latitude'] != clouds['grid_point']['latitude'] or raw['longitude'] != clouds['grid_point']['longitude']:
            raise ValueError('WN3 100 m wind run/grid mismatch')
        hours.append({'at':sample['at'],**{s:raw['wind_speed_100m_'+s] for s in ('mean','p10','p90')}})
    packet=dict(version=1,init_time=clouds['init_time'],snapshot_collected_at=snapshot['collected_at'],
                grid_point=clouds['grid_point'],unit='m/s',hours=hours,query=result['provenance'])
    if validate_wind(packet,snapshot,now) is None:
        raise ValueError('invalid WN3 100 m wind')
    return packet


def validate_wind(packet,snapshot,now):
    try:
        clouds=cloud_evidence(snapshot,now)
        if not clouds or packet['version']!=1 or packet['unit']!='m/s' or packet['init_time']!=clouds['init_time'] or packet['grid_point']!=clouds['grid_point'] or packet['snapshot_collected_at']!=snapshot['collected_at']:
            return None
        validate_provenance(packet['query'])
        if [r['at'] for r in packet['hours']] != [r['at'] for r in clouds['hours']]:
            return None
        for row in packet['hours']:
            if any(type(row[s]) not in (int,float) or not math.isfinite(row[s]) or not 0 <= row[s] <= 160 for s in ('mean','p10','p90')) or row['p10']>row['p90']:
                return None
        return packet
    except (ValueError,KeyError,TypeError,AttributeError,IndexError):
        return None


def surface_evidence(snapshot,now):
    clouds=cloud_evidence(snapshot,now)
    if not clouds:
        return None
    f=snapshot['weathernext3']['data']['forecast']
    axis={t:i for i,t in enumerate(f['valid_time_utc'])}
    wind=validate_wind(snapshot.get('wn3_100m_wind'),snapshot,now)
    rows=[]
    for j,sample in enumerate(clouds['hours']):
        i=axis[sample['at']]
        rh=surface_rh(f['fields']['temperature_2m']['mean'][i],f['fields']['dewpoint_temperature_2m']['mean'][i])
        elevated=[round(wind['hours'][j][s]*KNOTS,1) for s in ('mean','p10','p90')] if wind else [None]*3
        rows.append([sample['at'],rh,round(f['fields']['wind_speed_10m']['mean'][i]*KNOTS,1),*elevated])
    return dict(init_time=clouds['init_time'],columns=['time_utc','derived_surface_rh_pct','10m_mean_kt','100m_mean_kt','100m_p10_kt','100m_p90_kt'],
                rows=rows,rh_method=RH_METHOD,wind_method=WIND_METHOD)


def render_surface(data):
    if not data:
        return ''
    number=lambda v:'Unavailable' if v is None else f'{v:g}'
    rows=''.join(f'<tr><th scope="row">{escape(local_clock(r[0]))}</th><td>{number(r[1])}</td><td>{number(r[2])}</td>'
                 f'<td>{number(r[3])}</td><td>{number(r[4])}–{number(r[5])}</td></tr>' for r in data['rows'])
    return ('<div id="wn3-surface-rh-wind"><h3>WN3 surface moisture and elevated-wind context</h3>'
            '<div class="chart-scroll" tabindex="0" role="region" aria-label="WN3 surface RH and 100 m wind"><table>'
            '<caption>Same hours and WN3 initialization as the cloud analysis. Wind in knots; RH in percent.</caption>'
            '<thead><tr><th scope="col">Eastern</th><th scope="col">Derived 2 m RH</th><th scope="col">10 m mean wind</th>'
            '<th scope="col">100 m mean wind</th><th scope="col">100 m p10–p90</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div><p>{escape(RH_METHOD)}</p><p><strong>Gust context:</strong> {escape(WIND_METHOD)}</p>'
            '<p class="small"><a href="https://www.weather.gov/media/epz/wxcalc/rhTdFromWetBulb.pdf">NWS humidity formula</a> · '
            '<a href="https://www.weather.gov/ict/windtools">NWS wind-mixing guidance</a></p></div>')
