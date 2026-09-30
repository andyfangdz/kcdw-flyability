"""Look-ahead and temporal-leakage audit of the KCDW calibration (research).

1. Open-Meteo previous_dayN is "the value predicted N x 24 h before valid time", i.e. from a
   run up to ~18 h newer than the 00Z run every other input uses (and, for 01Z, the valid
   day's own 00Z run). The fix uses previous_day{N+1} for lead day N: always a run at or
   before the nominal 00Z init (no look-ahead; up to a day staler).
2. Forward test with an 8-day embargo: training ends 8 days before the test year, so no
   training label postdates any test forecast's issue time (lead days reach 7).
3. WeatherNext 2's archive from 2022 overlaps its training data; one variant masks 2022.
4. Purged cross-validation: the month-interleaved folds, but training drops the 7 days on
   either side of every test month.
Variants are scored on identical forward-year hours. Writes var/mos/leakage_audit.json.
"""
import json

import numpy as np
import pandas as pd

from compare_common import crps, load
from forward_test import SPLIT, fit_predict

EMBARGO = pd.Timedelta(days=8)
OM = ('icon', 'hres', 'gem')


def shifted_open_meteo(t):
    """ICON/HRES/GEM columns rebuilt from previous_day{N+1} for lead day N (NaN at lead 7)."""
    out = {}
    for key in OM:
        om = pd.read_parquet(f'var/mos/models/om_{key}.parquet').drop_duplicates(['valid', 'lead_day']).set_index(['valid', 'lead_day'])
        joined = om.reindex(pd.MultiIndex.from_arrays([t.valid, t.lead_day + 1]))
        for var in om.columns:
            out[f'{key}_{var}'] = joined[var].to_numpy('float32')
        rad = np.radians(joined['wind_direction_10m'].to_numpy())
        out[f'{key}_dir_sin'], out[f'{key}_dir_cos'] = np.sin(rad).astype('float32'), np.cos(rad).astype('float32')
    return pd.DataFrame(out, index=t.index)


def main():
    t, features, fold = load()
    om_cols = [c for c in features if c.split('_')[0] in OM]
    fixed = t.copy()
    shifted = shifted_open_meteo(t)
    for c in om_cols:
        fixed[c] = shifted[c] if c in shifted else np.nan
    no_2022 = fixed.copy()
    wn2_cols = [c for c in features if c.startswith('wn2_')]
    no_2022.loc[no_2022.date.dt.year == 2022, wn2_cols] = np.nan
    test = (t.date >= SPLIT).to_numpy()
    embargoed = (t.date < SPLIT - EMBARGO).to_numpy()
    variants = [('as before (no embargo)', t, features, (t.date < SPLIT).to_numpy()),
                ('embargo', t, features, embargoed),
                ('embargo + Open-Meteo fixed', fixed, features, embargoed),
                ('embargo + Open-Meteo removed', t, [c for c in features if c not in om_cols], embargoed),
                ('embargo + OM fixed + WN2 2022 masked', no_2022, features, embargoed)]
    out = {'forward': [], 'purged_cv': {}}
    afternoon = t.loc[test, 'hour'].between(13, 16).to_numpy()
    for name, frame, cols, train in variants:
        row = {'variant': name, 'train_hours': int(train.sum())}
        for target in ('gust_peak', 'sust_mean'):
            q = fit_predict(frame, cols, train, test, target)
            y = frame.loc[test, target].to_numpy()
            c = crps(y, q)
            row[f'{target}_crps'] = round(float(c.mean()), 4)
            row[f'{target}_crps_1_4pm'] = round(float(c[afternoon].mean()), 4)
            row[f'{target}_mae_p50'] = round(float(np.abs(q[:, 9] - y).mean()), 3)
        out['forward'].append(row)
        print(row, flush=True)
    # Purged vs plain month-interleaved cross-validation (gust, Open-Meteo fixed so only the purge differs).
    y = fixed.gust_peak.to_numpy()
    for label, purge in (('plain', pd.Timedelta(0)), ('purged 7 days', pd.Timedelta(days=7))):
        q = np.full((len(fixed), 19), np.nan)
        for k in np.unique(fold):
            te = fold == k
            months = fixed.loc[te, 'date'].dt.to_period('M').unique()
            near = np.zeros(len(fixed), bool)
            for m in months:
                start, end = m.start_time - purge, m.end_time + purge
                near |= ((fixed.date >= start) & (fixed.date <= end)).to_numpy()
            tr = ~te & ~near if purge > pd.Timedelta(0) else ~te
            q[te] = fit_predict(fixed, features, tr, te, 'gust_peak')
        out['purged_cv'][label] = round(float(crps(y, q).mean()), 4)
        print('cross-validation', label, out['purged_cv'][label], flush=True)
    with open('var/mos/leakage_audit.json', 'w') as fh:
        json.dump(out, fh, indent=1)


if __name__ == '__main__':
    main()
