"""Calibrated hourly KCDW wind forecast for one local date from the newest available 00Z runs (research).

Trains the final models on every labelled row (same features and settings as
train.py), then predicts each local hour 06-21 of DATE using, for every model,
the most recent 00Z run already extracted (lead_day = DATE - init date).
Outputs p10/p50/p90 for 1-minute mean sustained wind, 1-hour peak gust and
METAR peak, the probability-weighted direction, and threshold probabilities.
Usage: var/mos-venv/bin/python research/mos/predict.py 2026-10-01
"""
import sys

import numpy as np
import pandas as pd
import xgboost as xgb

from train import ID, QUANTILES, TARGETS, quantile_model

PARAMS = dict(n_estimators=500, learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0)


def main(date):
    t = pd.read_parquet('var/mos/table.parquet')
    features = [c for c in t.columns if c not in ID + TARGETS and t[c].notna().mean() > 0.02]
    train = t[(t.date >= '2021-06-01') & t.gust_peak.notna() & t.sust_mean.notna()]
    day = t[t.date == pd.Timestamp(date)]
    # For each hour, the shortest lead whose 00Z run already has model data.
    have = day[features].notna().sum(axis=1)
    day = day[have >= 10].sort_values(['hour', 'lead_day']).groupby('hour').head(1)
    X, Xd = train[features].to_numpy('float32'), day[features].to_numpy('float32')
    out = day[['hour', 'lead_day', 'init']].copy()
    for target in ('sust_mean', 'gust_peak', 'metar_peak'):
        y = train[target].to_numpy('float32'); ok = ~np.isnan(y)
        q = np.sort(quantile_model(PARAMS).fit(X[ok], y[ok]).predict(Xd), axis=1)
        for i, a in enumerate(QUANTILES):
            out[f'{target}_p{int(a * 100)}'] = q[:, i].round(1)
    windy = (train.sust_mean >= 3).to_numpy()
    obs_dir = np.degrees(np.arctan2(-train.u_obs, -train.v_obs)).to_numpy() % 360
    sector = np.round(obs_dir / 22.5).astype(int) % 16
    probs = xgb.XGBClassifier(tree_method='hist', objective='multi:softprob', num_class=16, **dict(PARAMS, n_estimators=250)).fit(
        X[windy], sector[windy]).predict_proba(Xd)
    angles = np.radians(np.arange(16) * 22.5)
    vec = probs @ np.c_[np.sin(angles), np.cos(angles)]
    out['dir'] = (np.degrees(np.arctan2(vec[:, 0], vec[:, 1])) % 360).round()
    out['dir_top_sector_prob'] = probs.max(axis=1).round(2)
    for name, y in (('p_gust_ge20', (train.gust_peak >= 20)), ('p_spread_ge10', (train.spread >= 10)),
                    ('p_metar_gust', train.metar_gust_reported.fillna(0) > 0)):
        out[name] = xgb.XGBClassifier(tree_method='hist', eval_metric='logloss', **PARAMS).fit(X, y.astype(int)).predict_proba(Xd)[:, 1].round(2)
    raw = {c: day[c].round(1) for c in ('ifs_ens_speed_10m_mean', 'ifs_ens_wind_gust_10m_mean', 'gefs_wind_gust_surface_mean', 'gfs_10m_spd', 'hrrr_wind_gust_surface') if c in day}
    for k, v in raw.items():
        out['raw_' + k] = v.values
    pd.set_option('display.width', 250)
    print(out.to_string(index=False))


if __name__ == '__main__':
    main(sys.argv[1])
