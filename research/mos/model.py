"""Final calibrated KCDW wind models: train on every labelled hour and save to var/mos/artifacts.

Settings and features match train.py's cross-validation, whose out-of-fold
predictions supply the conformal widths (conformal.py) and skill (evaluate.py).
Usage: var/mos-venv/bin/python research/mos/model.py
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from train import EXCEEDANCE, ID, QUANTILE_TARGETS, QUANTILES, TARGETS, exceedance_label, quantile_model

ARTIFACTS = Path('var/mos/artifacts')
PARAMS = dict(n_estimators=500, learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0)
SINCE = '2021-06-01'


def features(table):
    return [c for c in table.columns if c not in ID + TARGETS and table[c].notna().mean() > 0.02]


def main():
    started = time.time()
    t = pd.read_parquet('var/mos/table_issue.parquet')
    feats = features(t)
    train = t[(t.date >= SINCE) & t.gust_peak.notna() & t.sust_mean.notna()]
    X = train[feats].to_numpy('float32')
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for target in QUANTILE_TARGETS:
        y = train[target].to_numpy('float32'); ok = ~np.isnan(y)
        quantile_model(PARAMS).fit(X[ok], y[ok]).save_model(ARTIFACTS / f'{target}.json')
    windy = (train.sust_mean >= 3).to_numpy()
    sector = np.round((np.degrees(np.arctan2(-train.u_obs, -train.v_obs)) % 360) / 22.5).astype(int).to_numpy() % 16
    xgb.XGBClassifier(tree_method='hist', objective='multi:softprob', num_class=16, **dict(PARAMS, n_estimators=150)).fit(
        X[windy], sector[windy]).save_model(ARTIFACTS / 'direction.json')
    labels = {name: exceedance_label(train, column, threshold) for name, (column, threshold) in EXCEEDANCE.items()}
    labels['p_metar_gust'] = (train.metar_gust_reported.fillna(0) > 0).to_numpy('float32')
    for name, y in labels.items():
        known = ~np.isnan(y)  # e.g. crosswind is unknown when the 1-minute direction is missing
        xgb.XGBClassifier(tree_method='hist', eval_metric='logloss', **PARAMS).fit(X[known], y[known].astype(int)).save_model(
            ARTIFACTS / f'{name}.json')
    meta = {'version': 1, 'trained_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'features': feats,
            'quantiles': QUANTILES.tolist(), 'quantile_targets': list(QUANTILE_TARGETS), 'probabilities': sorted(labels),
            'rows': int(len(train)),
            'data_from': f'{train.date.min():%Y-%m-%d}', 'data_through': f'{train.date.max():%Y-%m-%d}'}
    (ARTIFACTS / 'meta.json').write_text(json.dumps(meta, indent=1))
    print(f'trained on {len(train)} hours ({meta["data_from"]}..{meta["data_through"]}), {len(feats)} features, {time.time() - started:.0f} s')


if __name__ == '__main__':
    main()
