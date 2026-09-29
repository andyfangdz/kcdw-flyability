"""Hourly observation targets at KCDW for hour H (H:00-H:59 UTC).

From IEM 1-minute ASOS (true values):
  sust_mean   mean of the minute 2-minute-average winds (kt)
  gust_peak   highest 5-second gust in the hour (kt)
  spread      gust_peak - sust_mean
  u_obs/v_obs speed-weighted vector mean wind (for direction)
From METARs (what is officially reported), dynamical.org ASOS parquet:
  metar_sknt, metar_drct  routine observation at about H:53
  metar_gust              reported gust anywhere in the hour (NaN when none)
  metar_gust_reported     1 if any METAR in the hour carried a gust
  metar_peak              max over the hour's METARs of max(sknt, gust)
Hours with fewer than 45 valid minutes get NaN 1-minute targets.
"""
import glob
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path('var/mos/targets.parquet')


def one_minute():
    frames = [pd.read_csv(p, usecols=['valid(UTC)', 'sknt', 'drct', 'gust_sknt'], na_values=['M', '']) for p in sorted(glob.glob('var/mos/obs/*.csv'))]
    m = pd.concat(frames, ignore_index=True).rename(columns={'valid(UTC)': 'valid'})
    m['valid'] = pd.to_datetime(m['valid'])
    m = m.dropna(subset=['sknt'])
    m = m[(m.sknt >= 0) & (m.sknt < 100) & (m.gust_sknt.isna() | ((m.gust_sknt >= m.sknt - 1) & (m.gust_sknt < 120)))]
    rad = np.radians(m.drct.fillna(0))
    m['u'], m['v'] = -m.sknt * np.sin(rad), -m.sknt * np.cos(rad)
    m['hour'] = m.valid.dt.floor('h')
    g = m.groupby('hour').agg(n=('sknt', 'size'), sust_mean=('sknt', 'mean'), sust_max=('sknt', 'max'),
                              gust_peak=('gust_sknt', 'max'), u_obs=('u', 'mean'), v_obs=('v', 'mean'))
    g = g[g.n >= 45].drop(columns='n')
    g['gust_peak'] = np.maximum(g.gust_peak.fillna(g.sust_max), g.sust_max)
    g['spread'] = g.gust_peak - g.sust_mean
    return g


def metar():
    t = pd.read_parquet('var/mos/metar.parquet')
    t['valid'] = pd.to_datetime(t.valid_utc)
    t['hour'] = t.valid.dt.floor('h')
    t['peak'] = np.fmax(t.sknt, t.gust)
    routine = t[t.valid.dt.minute.between(50, 56)].groupby('hour').agg(metar_sknt=('sknt', 'last'), metar_drct=('drct', 'last'))
    hourly = t.groupby('hour').agg(metar_gust=('gust', 'max'), metar_peak=('peak', 'max'))
    hourly['metar_gust_reported'] = hourly.metar_gust.notna().astype('int8')
    return routine.join(hourly, how='outer')


def main():
    table = one_minute().join(metar(), how='outer').sort_index()
    table.index.name = 'valid'
    table.reset_index().to_parquet(OUT, index=False)
    print(f'{len(table)} hours {table.index.min()} .. {table.index.max()}')
    print(table.describe().T[['count', 'mean', '50%', 'max']].round(2).to_string())


if __name__ == '__main__':
    main()
