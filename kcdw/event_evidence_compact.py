"""Lossless column layouts for narrative moisture, preserving space for winds."""
from copy import deepcopy


def _fan(value):
    if isinstance(value,dict) and set(value)=={'p10','p50','p90','members'}:
        return [value[k] for k in ('p10','p50','p90','members')]
    return deepcopy(value)


def compact_moisture(packet):
    out=deepcopy(packet)
    out['sample_columns']=['at','surface_RH_percent','levels']
    out['level_columns']=['pressure_hPa','RH_percent','height_m_MSL']
    out['statistic_layout']='Four-element statistic arrays are [p10,p50,p90,eligible_members]; deterministic values remain scalars; null means unavailable.'
    for model in out.get('models',[]):
        if 'samples' in model:
            model['sample_rows']=[[p['at'],_fan(p['surface_RH_percent']),
                 [[l['pressure_hPa'],_fan(l['RH_percent']),_fan(l['height_m_MSL'])] for l in p['levels']]] for p in model.pop('samples')]
    return out


def compact_changes(packet):
    """Lossless tagged column rows; preserve nulls, provenance and unknown fields."""
    layouts={
        'guidance':['fetched_at','grid','low_cloud','pressure','rain_window','run_binding','run_time','wind'],
        'moisture':['fetched_at','grid','run_binding','samples'],
        'pair':['alias','previous','current'], 'grid':['latitude','longitude'],
        'periods':['morning','noon','afternoon'], 'rh_levels':['surface','925hPa'],
        'rh':['p10','p50','p90','members'], 'ensemble':['p10','p50','p90'],
        'ensemble_unit':['p10','p50','p90','unit'],
        'mean_unit':['mean','p10','p90','unit'], 'deterministic_unit':['value','p10','p90','unit']}
    used=set()
    def pack(value):
        if isinstance(value,dict):
            for name,columns in layouts.items():
                if set(value)==set(columns):
                    used.add(name)
                    return ['@'+name]+[pack(value[k]) for k in columns]
            return {k:pack(v) for k,v in value.items()}
        if isinstance(value,list):return [pack(v) for v in value]
        return deepcopy(value)
    out=pack(packet)
    out['column_layouts']={k:layouts[k] for k in layouts if k in used}
    out['row_encoding']='An array beginning @name is a dictionary row with columns in column_layouts[name]. Null remains unavailable.'
    return out


def compact_official_prose(packet):
    """Last-resort prose reduction; retain every product's facts and coverage."""
    out=deepcopy(packet)
    for product in out.get('products',[]):
        for key in ('text','title','scope'):
            product.pop(key,None)
    out['detail_omitted']='Regional prose/title/scope omitted for size; full products remain on the site. CPC categories are period-average terciles, not flight-hour probabilities. WPC regional context is not a KCDW risk category.'
    return out
