"""Would more history help? Train on the last N years before 2025-10-01 and score the same forward year (research).

Same XGBoost settings as forward_test.py. If CRPS is still falling at the longest window,
a longer archive should help; if it has flattened, history is not the bottleneck.
Writes var/mos/learning_curve.json. Runs on CUDA when MOS_DEVICE=cuda.
"""
import json

import pandas as pd

from compare_common import crps, load
from forward_test import SPLIT, fit_predict


def main():
    t, features, _ = load()
    test = (t.date >= SPLIT).to_numpy()
    rows = []
    for years in (1, 2, 3, 4.33):
        train = ((t.date < SPLIT) & (t.date >= SPLIT - pd.DateOffset(months=round(12 * years)))).to_numpy()
        row = {'years': years, 'train_hours': int(train.sum())}
        for target in ('gust_peak', 'sust_mean'):
            q = fit_predict(t, features, train, test, target)
            y = t.loc[test, target].to_numpy()
            row[f'{target}_crps'] = round(float(crps(y, q).mean()), 4)
            afternoon = t.loc[test, 'hour'].between(13, 16).to_numpy()
            row[f'{target}_crps_1_4pm'] = round(float(crps(y[afternoon], q[afternoon]).mean()), 4)
        rows.append(row)
        print(row, flush=True)
    json.dump(rows, open('var/mos/learning_curve.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
