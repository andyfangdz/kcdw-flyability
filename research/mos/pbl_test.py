"""Do boundary-layer (mixing) inputs help the KCDW calibration? Native HRRR, lead day 1 (research).

Joins extract_hrrr_native.py's KCDW point (00Z run of the day before, 34-48 h) to the production
table and scores lead-day-1 hours only (the only lead HRRR's 48 h run covers):
  production inputs
  + boundary layer   HRRR boundary-layer height, friction velocity, hour's max 10 m wind,
                     0-1 km shear, 925/850 hPa wind, CAPE
  + mixing proxies   also the 925/850 hPa wind weighted by how far the boundary layer reaches
                     toward that level (roughly 800 m and 1500 m): wind the mixing can bring down
Forward year with the 8-day embargo, and month-interleaved cross-validation. Models train on
all leads (the new inputs are missing beyond lead day 1, as they would be in production).
Writes var/mos/pbl_test.json.
"""
import glob
import json

import numpy as np
import pandas as pd

from compare_common import crps, load
from forward_test import SPLIT, fit_predict

KEY = ['valid', 'lead_day']


def hrrr_native():
    h = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob('var/mos-multi/models/hrrrn/*.parquet'))], ignore_index=True)
    h = h[h.station == 'CDW'].drop(columns='station')
    h['valid'] = h.init + pd.to_timedelta(h.lead_h, unit='h')
    h['lead_day'] = 1
    h['mix_925'] = h.wind_925 * np.clip(h.pbl_height / 800, 0, 1)
    h['mix_850'] = h.wind_850 * np.clip(h.pbl_height / 1500, 0, 1)
    return h.drop(columns=['init', 'lead_h']).rename(columns=lambda c: c if c in KEY else f'hrrrn_{c}')


def main():
    t, features, fold = load()
    h = hrrr_native()
    t = t.merge(h, on=KEY, how='left')
    base_new = [c for c in h.columns if c.startswith('hrrrn_') and not c.startswith('hrrrn_mix')]
    mix = ['hrrrn_mix_925', 'hrrrn_mix_850']
    lead1 = (t.lead_day == 1).to_numpy()
    print(f'lead-1 hours with native HRRR: {int((lead1 & t.hrrrn_pbl_height.notna()).sum())} of {int(lead1.sum())}', flush=True)
    variants = {'production inputs': features, '+ boundary layer': features + base_new, '+ boundary layer + mixing proxies': features + base_new + mix}
    test = (t.date >= SPLIT).to_numpy()
    train = (t.date < SPLIT - pd.Timedelta(days=8)).to_numpy()
    aft = t.hour.between(13, 16).to_numpy()
    out = {'forward': {}, 'cv': {}}
    for name, cols in variants.items():
        row = {}
        for target in ('gust_peak', 'sust_mean'):
            q = fit_predict(t, cols, train, test, target)
            y = t.loc[test, target].to_numpy()
            m = lead1[test]
            c = crps(y[m], q[m])
            row[target] = {'crps_lead1': round(float(c.mean()), 4), 'crps_lead1_1_4pm': round(float(c[aft[test][m]].mean()), 4),
                           'mae_lead1': round(float(np.abs(q[m, 9] - y[m]).mean()), 3)}
        out['forward'][name] = row
        print('forward', name, row, flush=True)
    for name, cols in variants.items():
        row = {}
        for target in ('gust_peak', 'sust_mean'):
            q = np.full((len(t), 19), np.nan)
            for k in np.unique(fold):
                q[fold == k] = fit_predict(t, cols, fold != k, fold == k, target)
            y = t[target].to_numpy()
            c = crps(y[lead1], q[lead1])
            row[target] = {'crps_lead1': round(float(c.mean()), 4), 'crps_lead1_1_4pm': round(float(c[aft[lead1]].mean()), 4)}
        out['cv'][name] = row
        print('cv', name, row, flush=True)
    json.dump(out, open('var/mos/pbl_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
