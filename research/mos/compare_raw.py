"""How much the calibrated forecasts beat raw guidance (research).

KCDW: production out-of-fold forecasts vs NBM, GFS, GEFS mean, ECMWF ENS mean and ECMWF
HRES on identical hours (NBM archive from 2024-10, so the window is its overlap).
22 stations: the fast site-aware model vs GFS, GEFS mean, ECMWF ENS mean and ECMWF HRES
(no NBM archive away from KCDW). Point forecasts are scored by MAE and bias against the
hourly mean 1-minute wind (sustained) and the 1-hour 1-minute peak (gust); direction by
circular MAE when at least 5 kt is observed; 20 kt gust events by detection and false alarms.
Writes var/mos/compare_raw.json.
"""
import json

import numpy as np
import pandas as pd

KEY = ['valid', 'lead_day']


def mae(f, y):
    return float(np.mean(np.abs(f - y)))


def event_scores(f, y, threshold=20.0):
    hit, miss, false = np.sum((f >= threshold) & (y >= threshold)), np.sum((f < threshold) & (y >= threshold)), np.sum((f >= threshold) & (y < threshold))
    return {'events': int(hit + miss), 'detected_pct': round(100 * hit / max(hit + miss, 1), 1), 'false_alarm_pct': round(100 * false / max(hit + false, 1), 1)}


def table(frame, sources, target, afternoon):
    y = frame[target].to_numpy()
    rows = {}
    for name, col in sources.items():
        f = frame[col].to_numpy()
        rows[name] = {'mae': round(mae(f, y), 2), 'mae_1_4pm': round(mae(f[afternoon], y[afternoon]), 2), 'bias': round(float(np.mean(f - y)), 2)}
        if target == 'gust_peak':
            rows[name].update(event_scores(f, y))
    return rows


def show(title, rows, hours):
    print(f'\n{title} ({hours} identical hours)')
    for name, r in rows.items():
        extra = f"  20 kt: {r['detected_pct']:5.1f}% detected, {r['false_alarm_pct']:5.1f}% false alarms" if 'events' in r else ''
        print(f"  {name:32s} MAE {r['mae']:.2f}  1-4 pm {r['mae_1_4pm']:.2f}  bias {r['bias']:+.2f}{extra}")


def kcdw():
    t = pd.read_parquet('var/mos/table.parquet')
    o = pd.read_parquet('var/mos/oof.parquet')
    n = pd.read_parquet('var/mos/benchmark_nbm.parquet').drop_duplicates(KEY).set_index(KEY)
    d = o.merge(t[KEY + ['sust_mean', 'gust_peak', 'u_obs', 'v_obs', 'gfs_10m_spd', 'gefs_speed_10m_mean', 'gefs_wind_gust_surface_mean',
                         'ifs_ens_speed_10m_mean', 'ifs_ens_wind_gust_10m_mean', 'hres_wind_speed_10m', 'hres_wind_direction_10m']], on=KEY)
    d = d.join(n[['wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m']].add_prefix('nbm_'), on=KEY)
    need = ['nbm_wind_speed_10m', 'nbm_wind_gusts_10m', 'gfs_10m_spd', 'gefs_speed_10m_mean', 'gefs_wind_gust_surface_mean', 'ifs_ens_speed_10m_mean',
            'ifs_ens_wind_gust_10m_mean', 'hres_wind_speed_10m', 'sust_mean', 'gust_peak', 'gust_peak_p50', 'sust_mean_p50']
    s = d.dropna(subset=need).reset_index(drop=True)
    afternoon = s.hour.between(13, 16).to_numpy()
    out = {'hours': len(s), 'window': f'{s.valid.min():%Y-%m-%d}..{s.valid.max():%Y-%m-%d}'}
    out['sustained'] = table(s, {'calibrated (p50)': 'sust_mean_p50', 'NBM': 'nbm_wind_speed_10m', 'GFS': 'gfs_10m_spd', 'GEFS mean': 'gefs_speed_10m_mean',
                                 'ECMWF ENS mean': 'ifs_ens_speed_10m_mean', 'ECMWF HRES': 'hres_wind_speed_10m'}, 'sust_mean', afternoon)
    out['gust'] = table(s, {'calibrated (p50)': 'gust_peak_p50', 'NBM': 'nbm_wind_gusts_10m', 'GEFS mean': 'gefs_wind_gust_surface_mean',
                            'ECMWF ENS mean': 'ifs_ens_wind_gust_10m_mean'}, 'gust_peak', afternoon)  # no HRES gust at KCDW (Open-Meteo)
    windy = (np.hypot(s.u_obs, s.v_obs) >= 5).to_numpy()
    obs_dir = np.degrees(np.arctan2(-s.u_obs, -s.v_obs)) % 360

    def dir_err(col):
        e = np.abs((s[col] - obs_dir + 180) % 360 - 180).to_numpy()
        return round(float(np.nanmean(e[windy])), 1)
    out['direction_mae_deg'] = {'calibrated': dir_err('dir_xgb_uv'), 'NBM': dir_err('nbm_wind_direction_10m'), 'GFS': dir_err('dir_raw_GFS'),
                                'ECMWF ENS mean': dir_err('dir_raw_ECMWF_ENS_mean'), 'ECMWF HRES': dir_err('hres_wind_direction_10m')}
    by_lead = {}
    for lead, g in s.groupby('lead_day'):
        by_lead[int(lead)] = {name: round(mae(g[col], g.gust_peak), 2) for name, col in
                              (('calibrated', 'gust_peak_p50'), ('NBM', 'nbm_wind_gusts_10m'), ('ECMWF ENS mean', 'ifs_ens_wind_gust_10m_mean'))}
    out['gust_mae_by_lead'] = by_lead
    show(f"KCDW sustained, {out['window']}", out['sustained'], len(s))
    show('KCDW gust (1-hour peak)', out['gust'], len(s))
    print('  direction MAE (deg, >= 5 kt observed):', out['direction_mae_deg'])
    print('  gust MAE by lead day:', by_lead)
    return out


def stations():
    o = pd.read_parquet('var/mos-multi/oof_xgb_site_fast.parquet')
    frames = []
    for station, g in o.groupby('station'):
        t = pd.read_parquet(f'var/mos-multi/table/{station}.parquet', columns=KEY + ['gfs_10m_spd', 'gefs_speed_10m_mean', 'gefs_wind_gust_surface_mean',
                                                                                   'ifs_ens_speed_10m_mean', 'ifs_ens_wind_gust_10m_mean', 'ifshres_10m_spd'])
        frames.append(g.merge(t, on=KEY))
    d = pd.concat(frames, ignore_index=True)
    need = ['gfs_10m_spd', 'gefs_speed_10m_mean', 'gefs_wind_gust_surface_mean', 'ifs_ens_speed_10m_mean', 'ifs_ens_wind_gust_10m_mean', 'ifshres_10m_spd']
    s = d.dropna(subset=need).reset_index(drop=True)
    afternoon = pd.DatetimeIndex(s.valid).tz_localize('UTC').tz_convert('America/New_York').hour.isin(range(13, 17))
    out = {'hours': len(s), 'stations': int(s.station.nunique()), 'window': f'{s.valid.min():%Y-%m-%d}..{s.valid.max():%Y-%m-%d}'}
    out['sustained'] = table(s, {'calibrated site model (p50)': 'sust_mean_q50', 'GFS': 'gfs_10m_spd', 'GEFS mean': 'gefs_speed_10m_mean',
                                 'ECMWF ENS mean': 'ifs_ens_speed_10m_mean', 'ECMWF HRES': 'ifshres_10m_spd'}, 'sust_mean', afternoon)
    out['gust'] = table(s, {'calibrated site model (p50)': 'gust_peak_q50', 'GEFS mean': 'gefs_wind_gust_surface_mean',
                            'ECMWF ENS mean': 'ifs_ens_wind_gust_10m_mean'}, 'gust_peak', afternoon)
    per = []
    for station, g in s.groupby('station'):
        per.append({'station': station, 'gust_mae_calibrated': round(mae(g.gust_peak_q50, g.gust_peak), 2),
                    'gust_mae_ecmwf_ens': round(mae(g.ifs_ens_wind_gust_10m_mean, g.gust_peak), 2),
                    'sust_mae_calibrated': round(mae(g.sust_mean_q50, g.sust_mean), 2), 'sust_mae_best_raw': round(min(
                        mae(g[c], g.sust_mean) for c in ('gfs_10m_spd', 'gefs_speed_10m_mean', 'ifs_ens_speed_10m_mean', 'ifshres_10m_spd')), 2)})
    out['per_station'] = per
    show(f"22 stations sustained, {out['window']}", out['sustained'], len(s))
    show('22 stations gust (1-hour peak)', out['gust'], len(s))
    return out


def main():
    out = {'kcdw': kcdw(), 'stations': stations()}
    with open('var/mos/compare_raw.json', 'w') as fh:
        json.dump(out, fh, indent=1)


if __name__ == '__main__':
    main()
