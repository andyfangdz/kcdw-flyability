"""Can production afford the 23-level quantile model for threshold probabilities? (research)

Forward year (trained before 2025-10 with the 8-day embargo, scored at the 11Z update) for the four
probability targets: the standard multi-quantile model (one tree per quantile level per round, as in
reliability_test.py) against a vector-leaf model (one tree per round holding all 23 levels), which costs
about as much as a single-output model. Brier skill against climatology and reliability error per
threshold, and training time. Writes var/mos/quantile_variants.json. Runs on CUDA when MOS_DEVICE=cuda.
"""
import json
import os
import time

import numpy as np
import pandas as pd
import xgboost as xgb

from forward_test import SPLIT
from model import SINCE, features
from reliability_test import COMMON, LEVELS, SPECS, climatology, exceed, score


def fit(X, y, train, test, vector):
    params = dict(COMMON, multi_strategy='multi_output_tree') if vector else COMMON
    model = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=LEVELS, **params)
    started = time.time()
    model.fit(X[train], y[train])
    seconds = time.time() - started
    return np.sort(model.predict(X[test]), axis=1), seconds


def main():
    t = pd.read_parquet('var/mos/table_issue.parquet')
    t = t[(t.date >= SINCE) & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    X = t[features(t)].to_numpy('float32')
    morning = (t.init.dt.hour == 11).to_numpy()
    out = {}
    for target, spec in SPECS.items():
        y = t[target].to_numpy('float32')
        labelled = ~np.isnan(y)
        train = (t.init < SPLIT - pd.Timedelta(days=8)).to_numpy() & labelled
        test = morning & (t.init >= SPLIT).to_numpy() & labelled
        idx = np.flatnonzero(test)
        days = t.date.to_numpy()[idx]
        res = {}
        for name, vector in (('standard', False), ('vector leaf', True)):
            q, seconds = fit(X, y, train, test, vector)
            res[name] = {'train_seconds': round(seconds)}
            for thr in spec['thresholds']:
                o = (y[idx] >= thr).astype(float)
                s = score(exceed(q, thr), o, days, climatology(t, train, test, thr, target))
                res[name][str(thr)] = {'bss': s['bss_vs_climatology'], 'reliability': s['reliability'], 'events': s['events']}
            print(target, name, json.dumps(res[name]), flush=True)
        out[target] = res
    json.dump(out, open('var/mos/quantile_variants.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
