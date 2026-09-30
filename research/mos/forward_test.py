"""Strict forward test of the KCDW calibration, plus a METAR-defined comparison with NBM (research).

Forward: XGBoost (19 quantiles, the method-comparison settings) trained only on hours before
2025-10-01, then scored on 2025-10-01 onward, against the interleaved-month cross-validation
forecasts for the same hours and against raw guidance. If cross-validation were leaking or
overfitting, the forward year would score clearly worse than the CV numbers.
METAR: NBM targets the METAR observation, so point forecasts are also scored against the
routine METAR wind (sustained) and the hour's highest METAR wind or gust (peak).
Writes var/mos/forward_test.json. Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb

from compare_common import LEVELS, crps, load

SPLIT = pd.Timestamp('2025-10-01')
KEY = ['valid', 'lead_day']


def fit_predict(t, features, train, test, target):
    X, y = t[features].to_numpy('float32'), t[target].to_numpy('float32')
    model = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=LEVELS, tree_method='hist', device=os.environ.get('MOS_DEVICE', 'cpu'),
                             n_estimators=500, learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0)
    model.fit(X[train], y[train])
    return np.maximum(np.sort(model.predict(X[test]), axis=1), 0)


def mae(f, y):
    ok = ~np.isnan(f) & ~np.isnan(y)
    return round(float(np.mean(np.abs(f[ok] - y[ok]))), 2)


def main():
    t, features, _ = load()
    train, test = (t.date < SPLIT).to_numpy(), (t.date >= SPLIT).to_numpy()
    s = t[test].reset_index(drop=True)
    out = {'train_hours': int(train.sum()), 'test_hours': int(test.sum()), 'test_window': f'{s.valid.min():%Y-%m-%d}..{s.valid.max():%Y-%m-%d}'}
    oof = pd.read_parquet('var/mos/oof.parquet').set_index(KEY)
    nbm = pd.read_parquet('var/mos/benchmark_nbm.parquet').drop_duplicates(KEY).set_index(KEY)
    s = s.join(oof[['gust_peak_p50', 'sust_mean_p50', 'metar_peak_p50']].add_prefix('cv_'), on=KEY)
    s = s.join(nbm[['wind_speed_10m', 'wind_gusts_10m']].add_prefix('nbm_'), on=KEY)
    for target in ('gust_peak', 'sust_mean'):
        q = fit_predict(t, features, train, test, target)
        y = s[target].to_numpy()
        s[f'fwd_{target}_p50'] = q[:, 9]
        out[f'{target}_crps'] = {'forward': round(float(crps(y, q).mean()), 3)}
        print(f'{target}: forward CRPS {crps(y, q).mean():.3f}', flush=True)
    both = s.nbm_wind_speed_10m.notna() & s.ifs_ens_wind_gust_10m_mean.notna() & s.cv_gust_peak_p50.notna()
    b = s[both]
    out['identical_hours'] = int(both.sum())
    out['gust_peak_mae'] = {'forward-trained': mae(b.fwd_gust_peak_p50.to_numpy(), b.gust_peak.to_numpy()),
                            'cross-validated': mae(b.cv_gust_peak_p50.to_numpy(), b.gust_peak.to_numpy()),
                            'NBM': mae(b.nbm_wind_gusts_10m.to_numpy(), b.gust_peak.to_numpy()),
                            'ECMWF ENS mean': mae(b.ifs_ens_wind_gust_10m_mean.to_numpy(), b.gust_peak.to_numpy())}
    out['sust_mean_mae'] = {'forward-trained': mae(b.fwd_sust_mean_p50.to_numpy(), b.sust_mean.to_numpy()),
                            'cross-validated': mae(b.cv_sust_mean_p50.to_numpy(), b.sust_mean.to_numpy()),
                            'NBM': mae(b.nbm_wind_speed_10m.to_numpy(), b.sust_mean.to_numpy()),
                            'ECMWF ENS mean': mae(b.ifs_ens_speed_10m_mean.to_numpy(), b.sust_mean.to_numpy())}
    # METAR-defined targets (NBM's home ground). Our sustained model was not trained on the METAR wind, so this is conservative for it.
    m = b[b.metar_sknt.notna()]
    out['metar_wind_mae'] = {'calibrated sustained (not trained on METAR)': mae(m.cv_sust_mean_p50.to_numpy(), m.metar_sknt.to_numpy()),
                             'NBM': mae(m.nbm_wind_speed_10m.to_numpy(), m.metar_sknt.to_numpy()),
                             'ECMWF ENS mean': mae(m.ifs_ens_speed_10m_mean.to_numpy(), m.metar_sknt.to_numpy()), 'hours': int(len(m))}
    p = b[b.metar_peak.notna()]
    out['metar_peak_mae'] = {'calibrated METAR-peak model': mae(p.cv_metar_peak_p50.to_numpy(), p.metar_peak.to_numpy()),
                             'NBM gust': mae(p.nbm_wind_gusts_10m.to_numpy(), p.metar_peak.to_numpy()),
                             'ECMWF ENS mean gust': mae(p.ifs_ens_wind_gust_10m_mean.to_numpy(), p.metar_peak.to_numpy()), 'hours': int(len(p))}
    for k, v in out.items():
        print(k, v, flush=True)
    with open('var/mos/forward_test.json', 'w') as fh:
        json.dump(out, fh, indent=1)


if __name__ == '__main__':
    main()
