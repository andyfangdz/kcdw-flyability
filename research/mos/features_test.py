"""Do recent-error and recent-regime features help the KCDW calibration? (research)

For a row forecasting local date D at lead day N, the forecast is issued from 00Z runs of
D-N, so only days up to D-N-1 are fully observed. Features use those days only:
  recent bias  per model (GEFS, WN2, GFS, ECMWF ENS) and target (sustained, gust): the mean
               error of that model's lead-1 forecast over the 3 and 7 days before issue
  regime       observed mean gust spread (peak minus sustained) over the 3 days before issue,
               and the previous day's observed mean sustained wind and peak gust
Scored against the production inputs on identical hours: the leak-safe forward year (8-day
embargo) and the month-interleaved cross-validation. Writes var/mos/features_test.json.
"""
import json

import numpy as np
import pandas as pd

from compare_common import crps, load
from forward_test import SPLIT, fit_predict

MODELS = {'gefs': ('gefs_speed_10m_mean', 'gefs_wind_gust_surface_mean'), 'wn2': ('wn2_speed_10m_mean', None),
          'gfs': ('gfs_10m_spd', None), 'ifs_ens': ('ifs_ens_speed_10m_mean', 'ifs_ens_wind_gust_10m_mean')}


def daily_errors(t):
    """Per local date: mean error of each model's lead-1 forecast, and observed daily means."""
    one = t[t.lead_day == 1]
    out = {}
    for name, (sust_col, gust_col) in MODELS.items():
        out[f'{name}_sust_err'] = (one[sust_col] - one.sust_mean).groupby(one.date).mean()
        if gust_col:
            out[f'{name}_gust_err'] = (one[gust_col] - one.gust_peak).groupby(one.date).mean()
    obs = t.drop_duplicates('valid')
    out['obs_spread'] = (obs.gust_peak - obs.sust_mean).groupby(obs.date).mean()
    out['obs_sust'] = obs.sust_mean.groupby(obs.date).mean()
    out['obs_gust'] = obs.gust_peak.groupby(obs.date).mean()
    daily = pd.DataFrame(out)
    return daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq='D'))


def recent_features(t, daily):
    """Features known at issue: windows ending the day before the issue day (D - N - 1)."""
    feats = {}
    for col in [c for c in daily.columns if c.endswith('_err')]:
        for k in (3, 7):
            feats[f'recent_{col}_{k}d'] = daily[col].rolling(k, min_periods=max(1, k // 2)).mean()
    feats['recent_obs_spread_3d'] = daily.obs_spread.rolling(3, min_periods=2).mean()
    feats['recent_obs_sust_1d'] = daily.obs_sust
    feats['recent_obs_gust_1d'] = daily.obs_gust
    frame = pd.DataFrame(feats)
    # Value for issue day I uses days up to I - 1: shift by one day, then look up at I = D - N.
    frame = frame.shift(1)
    issue = t.date - pd.to_timedelta(t.lead_day, unit='D')
    return frame.reindex(issue).set_axis(t.index)


def main():
    t, features, fold = load()
    full = pd.read_parquet('var/mos/table.parquet')  # all labelled rows, for the error history
    full = full[full.sust_mean.notna() & full.gust_peak.notna()]
    new = recent_features(t, daily_errors(full))
    t = pd.concat([t, new], axis=1)
    bias_cols = [c for c in new.columns if c.endswith(('_3d', '_7d')) and '_err_' in c]
    regime_cols = [c for c in new.columns if 'obs' in c]
    variants = {'production inputs': features, '+ recent bias': features + bias_cols, '+ recent regime': features + regime_cols,
                '+ both': features + bias_cols + regime_cols}
    print({k: len(v) for k, v in variants.items()}, 'coverage of new features:', round(float(new.notna().all(axis=1).mean()), 3), flush=True)
    test = (t.date >= SPLIT).to_numpy()
    train = (t.date < SPLIT - pd.Timedelta(days=8)).to_numpy()
    aft = t.hour.between(13, 16).to_numpy()
    out = {'forward': {}, 'cv': {}}
    for name, cols in variants.items():
        row = {}
        for target in ('gust_peak', 'sust_mean'):
            q = fit_predict(t, cols, train, test, target)
            y = t.loc[test, target].to_numpy()
            c = crps(y, q)
            row[target] = {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[aft[test]].mean()), 4), 'mae': round(float(np.abs(q[:, 9] - y).mean()), 3)}
        out['forward'][name] = row
        print('forward', name, row, flush=True)
    for name, cols in variants.items():
        q = np.full((len(t), 19), np.nan)
        for k in np.unique(fold):
            q[fold == k] = fit_predict(t, cols, fold != k, fold == k, 'gust_peak')
        c = crps(t.gust_peak.to_numpy(), q)
        out['cv'][name] = {'gust_crps': round(float(c.mean()), 4), 'gust_crps_1_4pm': round(float(c[aft].mean()), 4)}
        print('cv', name, out['cv'][name], flush=True)
    json.dump(out, open('var/mos/features_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
