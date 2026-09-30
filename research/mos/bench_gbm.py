"""Speed benchmark for the multi-station site-aware model: XGBoost (19 quantiles in one model, GPU) vs LightGBM (one CPU model per quantile).

Times 100 boosting rounds on fold 0 of the 22-station table, for the current tree size
and a bigger one, so full-run costs can be extrapolated. Research only.
"""
import os
import time

import numpy as np

import multi_train as mt

ROUNDS = 100


def main():
    import lightgbm as lgb
    import xgboost as xgb
    t = mt.load()
    train = (t.fold != 0).to_numpy()
    mt.add_history(t, train)
    cols = mt.features(t, 'site')
    codes = t.station.astype('category').cat.codes.to_numpy('float32')
    X = np.concatenate([mt.matrix(t, cols, train), codes[train, None]], axis=1)
    y = t.gust_peak.to_numpy('float32')[train]
    print(f'{X.shape[0]} training rows, {X.shape[1]} inputs', flush=True)
    types = ['q'] * len(cols) + ['c']
    started = time.time()
    dtrain = xgb.QuantileDMatrix(X, y, feature_types=types, enable_categorical=True)
    print(f'xgb matrix {time.time() - started:.0f} s', flush=True)
    for name, extra in (('xgb depth 7 (current)', {'max_depth': 7}), ('xgb depth 10', {'max_depth': 10}),
                        ('xgb 255 leaves lossguide', {'grow_policy': 'lossguide', 'max_leaves': 255, 'max_depth': 0})):
        params = {'objective': 'reg:quantileerror', 'quantile_alpha': mt.LEVELS, 'tree_method': 'hist', 'device': os.environ.get('MOS_DEVICE', 'cpu'),
                  'max_cat_to_onehot': 1, 'eta': 0.05, 'min_child_weight': 100, 'subsample': 0.8, 'colsample_bytree': 0.6, 'lambda': 5.0, **extra}
        started = time.time()
        xgb.train(params, dtrain, num_boost_round=ROUNDS)
        print(f'{name}: {time.time() - started:.1f} s per {ROUNDS} rounds, all 19 quantiles', flush=True)
    del dtrain
    started = time.time()
    dset = lgb.Dataset(X, y, categorical_feature=[X.shape[1] - 1], free_raw_data=False, params={'max_bin': 255, 'verbose': -1}).construct()
    print(f'lgb dataset {time.time() - started:.0f} s (built once, reused for every quantile)', flush=True)
    for name, leaves in (('lgb 127 leaves (~depth 7)', 127), ('lgb 1023 leaves (bigger)', 1023)):
        params = {'objective': 'quantile', 'alpha': 0.5, 'num_leaves': leaves, 'learning_rate': 0.05, 'feature_fraction': 0.6,
                  'bagging_fraction': 0.8, 'bagging_freq': 1, 'min_data_in_leaf': 100, 'lambda_l2': 5.0, 'num_threads': os.cpu_count(), 'verbose': -1}
        started = time.time()
        lgb.train(params, dset, num_boost_round=ROUNDS)
        one = time.time() - started
        print(f'{name}: {one:.1f} s per {ROUNDS} rounds for ONE quantile -> {19 * one:.0f} s for 19', flush=True)


if __name__ == '__main__':
    main()
