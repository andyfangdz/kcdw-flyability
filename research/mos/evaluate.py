"""Score out-of-fold calibrated forecasts against raw models and the NBM benchmark, on identical hours.

NBM is a benchmark only (never a feature): it is NOAA's own station-calibrated
blend, so beating it is the meaningful bar. Its archive starts 2024-10.
"""
import numpy as np
import pandas as pd

t = pd.read_parquet('var/mos/table.parquet')
oof = pd.read_parquet('var/mos/oof.parquet')
nbm = pd.read_parquet('var/mos/benchmark_nbm.parquet').drop_duplicates(['valid', 'lead_day']).set_index(['valid', 'lead_day'])
d = oof.merge(t[['valid', 'lead_day', 'sust_mean', 'gust_peak', 'metar_peak', 'u_obs', 'v_obs', 'gfs_10m_spd',
                 'gefs_wind_gust_surface_mean', 'ifs_ens_wind_gust_10m_mean', 'ifs_ens_speed_10m_mean']], on=['valid', 'lead_day'])
d = d.join(nbm[['wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m']].rename(columns=lambda c: 'nbm_' + c), on=['valid', 'lead_day'])
mae = lambda a, b: float(np.nanmean(np.abs(a - b)))
both = d.nbm_wind_gusts_10m.notna() & d.ifs_ens_wind_gust_10m_mean.notna()
s = d[both]
print(f'Identical hours with NBM, ECMWF ENS and calibrated forecasts: {len(s)} ({s.valid.min():%Y-%m}..{s.valid.max():%Y-%m})\n')
rows = []
for scope, m in (('all hours 6a-9p', slice(None)), ('1-4 pm', s.hour.isin(range(13, 17)))):
    x = s[m] if not isinstance(m, slice) else s
    rows.append({'scope': scope, 'n': len(x),
                 'sust: raw ECMWF ENS': mae(x.ifs_ens_speed_10m_mean, x.sust_mean), 'sust: NBM': mae(x.nbm_wind_speed_10m, x.sust_mean),
                 'sust: calibrated p50': mae(x.sust_mean_p50, x.sust_mean),
                 'gust: raw ECMWF ENS': mae(x.ifs_ens_wind_gust_10m_mean, x.gust_peak), 'gust: NBM': mae(x.nbm_wind_gusts_10m, x.gust_peak),
                 'gust: calibrated p50': mae(x.gust_peak_p50, x.gust_peak),
                 'METAR peak: NBM gust': mae(x.nbm_wind_gusts_10m, x.metar_peak), 'METAR peak: calibrated p50': mae(x.metar_peak_p50, x.metar_peak)})
print(pd.DataFrame(rows).set_index('scope').T.round(2).to_string())
print('\nBy lead day, 1-minute peak gust MAE (kt), identical hours:')
by = []
for lead, x in s.groupby('lead_day'):
    by.append({'lead day': lead, 'n': len(x), 'raw ECMWF ENS': mae(x.ifs_ens_wind_gust_10m_mean, x.gust_peak),
               'NBM': mae(x.nbm_wind_gusts_10m, x.gust_peak), 'calibrated': mae(x.gust_peak_p50, x.gust_peak),
               'p10-p90 coverage': float(np.mean((x.gust_peak >= x.gust_peak_p10) & (x.gust_peak <= x.gust_peak_p90)))})
print(pd.DataFrame(by).round(2).to_string(index=False))
obs_dir = np.degrees(np.arctan2(-s.u_obs, -s.v_obs)) % 360
err = lambda a: np.abs((a - obs_dir + 180) % 360 - 180)
w = s.sust_mean >= 5
print('\nDirection, hours with observed wind >= 5 kt (mean error, share within 30 deg):')
for name in [c for c in s.columns if c.startswith('dir_')] + ['nbm_wind_direction_10m']:
    e = err(s[name])[w]
    print(f'  {name:45s} {np.nanmean(e):5.1f} deg  {np.nanmean(e <= 30):.0%}')

# Machine-readable summary for the event page.
import json
from pathlib import Path
x = s[s.hour.isin(range(13, 17))]
e = err(s['dir_xgb_16-sector_(probability-weighted)'])[w]
summary = {'period': [f'{s.valid.min():%Y-%m-%d}', f'{s.valid.max():%Y-%m-%d}'], 'hours': int(len(s)), 'afternoon_hours': int(len(x)),
           'gust_mae_kt': {'calibrated': round(mae(x.gust_peak_p50, x.gust_peak), 2), 'nbm': round(mae(x.nbm_wind_gusts_10m, x.gust_peak), 2),
                           'raw_ecmwf_ens': round(mae(x.ifs_ens_wind_gust_10m_mean, x.gust_peak), 2)},
           'sust_mae_kt': {'calibrated': round(mae(x.sust_mean_p50, x.sust_mean), 2), 'nbm': round(mae(x.nbm_wind_speed_10m, x.sust_mean), 2)},
           'dir_mae_deg': {'calibrated': round(float(np.nanmean(e)), 1),
                           'nbm': round(float(np.nanmean(err(s.nbm_wind_direction_10m)[w])), 1)},
           'gust_mae_by_lead_kt': {str(r['lead day']): {'calibrated': round(r['calibrated'], 2), 'nbm': round(r['NBM'], 2)} for r in by}}
Path('var/mos/benchmark.json').write_text(json.dumps(summary, indent=1))
print('\nwrote var/mos/benchmark.json')
