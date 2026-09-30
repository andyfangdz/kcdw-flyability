"""Speed-ups for the multi-station site-aware XGBoost (research).

On fold 0 of the 22-station table (gust), times training and prediction and scores
out-of-fold CRPS (all stations and KCDW) for each variant against the current setup:
  baseline      one tree per quantile per round, depth 7, 256 bins, eta 0.05 x 600, all inputs
  vector leaf   one multi-output tree per round holding all 19 quantiles
  64 bins       coarser histograms
  lean inputs   without the inputs the ablation found useless
  eta 0.1 x 300 twice the learning rate, half the rounds
Default set: the combined fast setup (vector leaf, 64 bins, eta 0.1) and bigger trees on it;
BENCH_SET=components runs the individual changes above. Writes var/mos-multi/bench_xgb_<set>.json.
"""
import json
import os
import time

import numpy as np
import xgboost as xgb

import multi_train as mt
from compare_common import crps

USELESS = ('hrrr_', 'wn3_', 'aifs_', 'ifs_ens_')


def main():
    t = mt.load()
    train = (t.fold != 0).to_numpy()
    mt.add_history(t, train)
    all_cols = mt.features(t, 'site')
    lean_cols = [c for c in all_cols if 'gust' not in c and not c.startswith(USELESS)]
    codes = t.station.astype('category').cat.codes.to_numpy('float32')
    y = t.gust_peak.to_numpy('float32')
    cdw = (t.station == 'CDW').to_numpy()[~train]
    base = {'objective': 'reg:quantileerror', 'quantile_alpha': mt.LEVELS, 'tree_method': 'hist', 'device': os.environ.get('MOS_DEVICE', 'cpu'),
            'max_cat_to_onehot': 1, 'eta': 0.05, 'max_depth': 7, 'min_child_weight': 100, 'subsample': 0.8, 'colsample_bytree': 0.6,
            'lambda': 5.0, 'seed': 0}
    fast = {'multi_strategy': 'multi_output_tree', 'eta': 0.1}
    variants = [('fast', fast, all_cols, 64, 300), ('fast depth 9', {**fast, 'max_depth': 9}, all_cols, 64, 300),
                ('fast depth 9 x600', {**fast, 'max_depth': 9}, all_cols, 64, 600), ('fast depth 11', {**fast, 'max_depth': 11}, all_cols, 64, 300)]
    if os.environ.get('BENCH_SET') == 'components':
        variants = [('baseline', {}, all_cols, 256, 600), ('vector leaf', {'multi_strategy': 'multi_output_tree'}, all_cols, 256, 600),
                    ('64 bins', {}, all_cols, 64, 600), ('lean inputs', {}, lean_cols, 256, 600), ('eta 0.1 x 300', {'eta': 0.1}, all_cols, 256, 300)]
    results = []
    for name, extra, cols, bins, rounds in variants:
        types = ['q'] * len(cols) + ['c']
        started = time.time()
        X = np.concatenate([mt.matrix(t, cols, train), codes[train, None]], axis=1)
        dtrain = xgb.QuantileDMatrix(X, y[train], feature_types=types, enable_categorical=True, max_bin=bins)
        del X
        prep = time.time() - started
        started = time.time()
        try:
            booster = xgb.train({**base, **extra, 'max_bin': bins}, dtrain, num_boost_round=rounds)
        except xgb.core.XGBoostError as exc:
            print(f'{name}: not supported ({str(exc).splitlines()[0][:150]})', flush=True)
            continue
        fit = time.time() - started
        del dtrain
        started = time.time()
        Xt = np.concatenate([mt.matrix(t, cols, ~train), codes[~train, None]], axis=1)
        q = np.sort(booster.predict(xgb.DMatrix(Xt, feature_types=types, enable_categorical=True)), axis=1)
        pred = time.time() - started
        c = crps(y[~train], np.maximum(q, 0))
        row = {'variant': name, 'inputs': len(cols) + 1, 'prep_s': round(prep), 'train_s': round(fit), 'predict_s': round(pred),
               'crps_all': round(float(c.mean()), 4), 'crps_kcdw': round(float(c[cdw].mean()), 4)}
        results.append(row)
        print(f"{name:14s} prep {row['prep_s']:4d} s  train {row['train_s']:5d} s  predict {row['predict_s']:4d} s  "
              f"CRPS all {row['crps_all']:.4f}  KCDW {row['crps_kcdw']:.4f}", flush=True)
        del booster
    json.dump(results, open(f"var/mos-multi/bench_xgb_{os.environ.get('BENCH_SET', 'fast')}.json", 'w'), indent=1)


if __name__ == '__main__':
    main()
