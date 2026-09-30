"""Training table: one row per (local date, local hour 06-21, lead day 0-7) at KCDW.

Dynamical.org models use the 00Z run issued ``lead_day`` calendar days before the
local date; their 1/3/6-hourly leads are linearly interpolated to the hour. Lead day 0
is the same day's 00Z runs, complete by the 10Z update (research/mos/day0_test.py: ~6%
better than lead day 1 on identical hours, with no temporal leakage).
Missing models stay NaN (XGBoost handles them natively). Open-Meteo's ICON, HRES
and GEM were removed: its previous_dayN archive is "predicted N x 24 h before valid
time", up to ~18 h newer than the 00Z run the other inputs use (a look-ahead), and
once aligned to that run they added nothing (research/mos/leakage_audit.py).
Targets from build_targets.py are joined on the valid UTC hour.
"""
import glob
from pathlib import Path

import numpy as np
import pandas as pd

TZ = 'America/New_York'
HOURS = range(6, 22)
LEADS = range(0, 8)
MAX_LEAD = 199
OUT = Path('var/mos/table.parquet')


def dense(dataset):
    """{variable: (inits, array[n_init, MAX_LEAD])} with leads interpolated to every hour."""
    paths = sorted(glob.glob(f'var/mos/models/{dataset}/*.parquet'))
    if not paths:
        return None
    frame = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    frame = frame[frame.lead_h < MAX_LEAD]
    out = {}
    for var in frame.columns.drop(['init', 'lead_h']):
        wide = frame.pivot_table(index='init', columns='lead_h', values=var, aggfunc='first')
        wide = wide.reindex(columns=range(MAX_LEAD)).interpolate(axis=1, limit_area='inside')
        out[var] = (wide.index, wide.to_numpy(dtype='float32'))
    return out


def gather(model, inits, leads):
    cols = {}
    for var, (index, array) in model.items():
        pos = index.get_indexer(inits)
        ok = (pos >= 0) & (leads >= 0) & (leads < MAX_LEAD)
        values = np.full(len(inits), np.nan, dtype='float32')
        values[ok] = array[pos[ok], leads[ok]]
        cols[var] = values
    return cols


def wind(cols, prefix, u, v):
    if u in cols and v in cols:
        speed = np.hypot(cols[u], cols[v]) * (3600 / 1852)
        cols[f'{prefix}_spd'] = speed.astype('float32')
        cols[f'{prefix}_dir_sin'] = (-cols[u] / np.maximum(np.hypot(cols[u], cols[v]), 1e-3)).astype('float32')
        cols[f'{prefix}_dir_cos'] = (-cols[v] / np.maximum(np.hypot(cols[u], cols[v]), 1e-3)).astype('float32')


def main():
    days = pd.date_range('2020-10-02', pd.Timestamp.now(tz=TZ).normalize().tz_localize(None) + pd.Timedelta(days=8), freq='D')
    rows = pd.MultiIndex.from_product([days, HOURS, LEADS], names=['date', 'hour', 'lead_day']).to_frame(index=False)
    local = (rows.date + pd.to_timedelta(rows.hour, unit='h')).dt.tz_localize(TZ, nonexistent='shift_forward', ambiguous=True)
    rows['valid'] = local.dt.tz_convert('UTC').dt.tz_localize(None)
    rows['init'] = rows.date - pd.to_timedelta(rows.lead_day, unit='D')
    rows['lead_h'] = ((rows.valid - rows.init) / pd.Timedelta(hours=1)).astype(int)
    feats = {'lead_h': rows.lead_h.to_numpy('float32'),
             'hour_sin': np.sin(2 * np.pi * rows.hour / 24).astype('float32'), 'hour_cos': np.cos(2 * np.pi * rows.hour / 24).astype('float32'),
             'doy_sin': np.sin(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32'),
             'doy_cos': np.cos(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32')}
    inits, leads = pd.DatetimeIndex(rows.init), rows.lead_h.to_numpy()
    for dataset in ('gfs', 'gefs', 'ifs_ens', 'aifs', 'aifs_ens', 'hrrr', 'wn2', 'wn3'):
        model = dense(dataset)
        if model is None:
            print(f'{dataset}: no data yet')
            continue
        cols = gather(model, inits, leads)
        wind(cols, '10m', 'wind_u_10m', 'wind_v_10m')
        wind(cols, '10m', 'wind_u_10m_mean', 'wind_v_10m_mean')
        wind(cols, '100m', 'wind_u_100m', 'wind_v_100m')
        wind(cols, '100m', 'wind_u_100m_mean', 'wind_v_100m_mean')
        wind(cols, '80m', 'wind_u_80m', 'wind_v_80m')
        for t2, upper, name in (('temperature_2m', 'temperature_80m', 'dt_80m'), ('temperature_2m_mean', 'temperature_80m_mean', 'dt_80m'),
                                ('temperature_2m', 'temperature_925hpa', 'dt_925'), ('temperature_2m_mean', 'temperature_925hpa_mean', 'dt_925'),
                                ('temperature_925hpa', 'temperature_850hpa', 'dt_925_850'), ('temperature_925hpa_mean', 'temperature_850hpa_mean', 'dt_925_850')):
            if t2 in cols and upper in cols:
                cols[name] = (cols[t2] - cols[upper]).astype('float32')
        for key, value in cols.items():
            feats[f'{dataset}_{key}'] = value
        print(f'{dataset}: {len(cols)} features, coverage {np.mean(~np.isnan(cols[next(iter(cols))])):.0%}', flush=True)
    table = pd.concat([rows[['date', 'hour', 'lead_day', 'valid', 'init']], pd.DataFrame(feats)], axis=1)
    targets = pd.read_parquet('var/mos/targets.parquet').set_index('valid')
    table = table.join(targets, on='valid')
    table.to_parquet(OUT, index=False)
    print(f'{len(table)} rows, {table.shape[1]} columns; with 1-minute target {table.gust_peak.notna().sum()}')


if __name__ == '__main__':
    main()
