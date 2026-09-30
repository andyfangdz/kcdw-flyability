"""Does NOAA's AI global model (GraphCast-GFS, then AIGFS) improve the KCDW calibration? (research)

Joins extract_aigfs.py's KCDW point (00Z runs, 6-hourly steps to 102 h, interpolated to the hour)
to the production table on lead days 0-3, adding its 10 m wind, 925/850 hPa wind and temperature,
2 m temperature, sea-level pressure and the era flag (GraphCast-GFS vs AIGFS). Scored on lead days
0-3 hours where it exists, forward year (8-day embargo) and month-interleaved cross-validation.
Writes var/mos/aigfs_test.json.
"""
import glob
import json

import numpy as np
import pandas as pd

import build_features as bf
from compare_common import crps, load
from forward_test import SPLIT, fit_predict


def aigfs_features(t):
    frame = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob('var/mos-multi/models/aigfs/*.parquet'))], ignore_index=True)
    frame = frame[frame.station == 'CDW'].drop(columns='station')
    model = {}
    for var in frame.columns.drop(['init', 'lead_h']):
        wide = frame.pivot_table(index='init', columns='lead_h', values=var, aggfunc='first').reindex(columns=range(bf.MAX_LEAD))
        wide = wide.interpolate(axis=1, limit_area='inside')
        model[var] = (wide.index, wide.to_numpy(dtype='float32'))
    cols = bf.gather(model, pd.DatetimeIndex(t.init), t.lead_h.to_numpy().astype(int))
    bf.wind(cols, '10m', 'wind_u_10m', 'wind_v_10m')
    bf.wind(cols, '925', 'wind_u_925hpa', 'wind_v_925hpa')
    bf.wind(cols, '850', 'wind_u_850hpa', 'wind_v_850hpa')
    cols['dt_925'] = cols['temperature_2m'] - cols['temperature_925hpa']
    return pd.DataFrame({f'aigfs_{k}': v for k, v in cols.items()}, index=t.index)


def main():
    t, features, fold = load()
    ai = aigfs_features(t)
    t = pd.concat([t, ai], axis=1)
    has = t.aigfs_10m_spd.notna().to_numpy() & (t.lead_day <= 3).to_numpy()
    print(f'hours with the AI model (lead 0-3): {int(has.sum())}; from {t.date[has].min():%Y-%m-%d}', flush=True)
    variants = {'production inputs': features, '+ NOAA AI model': features + list(ai.columns)}
    test = (t.date >= SPLIT).to_numpy()
    train = (t.date < SPLIT - pd.Timedelta(days=8)).to_numpy()
    aft = t.hour.between(13, 16).to_numpy()
    out = {'forward': {}, 'cv': {}}
    for mode in ('forward', 'cv'):
        for name, cols in variants.items():
            res = {}
            for target in ('gust_peak', 'sust_mean'):
                q = np.full((len(t), 19), np.nan)
                if mode == 'forward':
                    te = test & has
                    q[te] = fit_predict(t, cols, train, te, target)
                else:
                    te = has
                    for k in np.unique(fold):
                        m = te & (fold == k)
                        q[m] = fit_predict(t, cols, fold != k, m, target)
                y = t[target].to_numpy()
                c = crps(y[te], q[te])
                res[target] = {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[aft[te]].mean()), 4),
                               'mae': round(float(np.abs(q[te, 9] - y[te]).mean()), 3), 'hours': int(te.sum()),
                               'by_lead': {int(l): round(float(c[(t.lead_day.to_numpy()[te] == l)].mean()), 4) for l in range(4)}}
            out[mode][name] = res
            print(mode, name, res, flush=True)
    json.dump(out, open('var/mos/aigfs_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
