"""Cloud-layer profiles derived from the same validated native RH packet.

No network or independent cache access. Small layer records reference the native
proof already in the snapshot; validation rederives every displayed value.
"""
import math
from datetime import datetime
from .common import iso_z
from .source_presentation import is_direct


def profile(snapshot, key, times, now):
    from .event_moisture import validate_moisture
    from .direct_deterministic import align_hourly, native_leads
    from .cloud_layer_signals import PROFILE_FIELDS, FIELD_UNITS, _profiles
    status = validate_moisture(snapshot.get('event_moisture'), now)['models'].get(key, {})
    if not status.get('available') or not is_direct(status['data']):
        raise ValueError('native profile unavailable')
    source = status['data']; meta = source['metadata']; proof = meta['native_proof']
    init = datetime.fromisoformat(meta['initialization_time'].replace('Z', '+00:00'))
    targets = [(t-init).total_seconds()/3600 for t in times]
    leads = native_leads(key, max(0, min(targets)), max(targets))
    bylead = {s['lead']: s for s in proof['samples']}
    def values(name, factor: float=1, offset: float=0):
        native = [bylead.get(h, {}).get('fields', {}).get(name, {}).get('value') for h in leads]
        return [None if v is None else v*factor+offset for v in align_hourly(leads, native, targets)]
    axis = {t: i for i, t in enumerate(source['hourly']['time'])}
    def from_hourly(field):
        return [source['hourly'][field][axis[iso_z(t)]] if iso_z(t) in axis else None for t in times]
    u, v = values('u10'), values('v10')
    raw = {'temperature_2m': values('t2', 1, -273.15), 'dew_point_2m': values('d2', 1, -273.15),
           'relative_humidity_2m': from_hourly('relative_humidity_2m'),
           'surface_pressure': from_hourly('surface_pressure'),
           'wind_speed_10m': [None if a is None or b is None else math.hypot(a, b)*3600/1852 for a, b in zip(u, v)],
           'wind_direction_10m': [None if a is None or b is None or math.hypot(a, b)<.01 else
                                  math.degrees(math.atan2(-a, -b))%360 for a, b in zip(u, v)]}
    for level in (1000, 925, 850):
        for field, name, offset in (('temperature', 't', -273.15), ('relative_humidity', 'r', 0), ('geopotential_height', 'h', 0)):
            raw[f'{field}_{level}hPa'] = values(name+str(level), 1, offset)
    if not any(v is not None for v in raw['relative_humidity_850hPa']):
        raise ValueError('native event profile has no RH coverage')
    data = dict(model_id=source['model_id'], model=source['model'], endpoint=source['endpoint'],
                fetched_at=source['fetched_at'], grid_point={'latitude': 41.0, 'longitude': -74.25},
                units={f: FIELD_UNITS[f] for f in PROFILE_FIELDS},
                metadata={k: meta[k] for k in ('provenance', 'model_init_is_response_bound', 'source_provider',
                                               'initialization_time', 'sampling')},
                points=_profiles({f: {'00': row} for f, row in raw.items()}, times),
                native_reference='event_moisture.'+key)
    return dict(ok=True, data=data, error=None)
