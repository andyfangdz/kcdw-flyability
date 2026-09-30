"""Do newer cycles help? Issue-time calibration with each source's newest published run vs 00Z-only (research).

Both tables come from build_issue.py with identical rows (issue times 04/10/16/22Z, every hour after
issue to 7 days) and identical publication-delay rules; they differ only in which cycles a source may
use. Forward year split by issue time with an 8-day embargo (no training label is later than any test
issue), and month-interleaved cross-validation for gust. Scores by issue time and lead day.
Writes var/mos/issue_test.json.
"""
import json

import numpy as np
import pandas as pd

from compare_common import OBS, crps
from forward_test import SPLIT, fit_predict

ID = ['init', 'date', 'hour', 'lead_day', 'valid']


def load(path):
    t = pd.read_parquet(path)
    t = t[(t.date >= '2021-06-01') & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    feats = [c for c in t.columns if c not in ID + OBS and t[c].notna().mean() > 0.01]
    return t, feats


def main():
    a, fa = load('var/mos/table_issue_00z.parquet')
    b, fb = load('var/mos/table_issue.parquet')
    assert (a.init.to_numpy() == b.init.to_numpy()).all() and (a.valid.to_numpy() == b.valid.to_numpy()).all()
    test = (a.init >= SPLIT).to_numpy()
    train = (a.init < SPLIT - pd.Timedelta(days=8)).to_numpy()
    fold = ((a.date.dt.year * 12 + a.date.dt.month) % 5).to_numpy()
    aft = a.hour.between(13, 16).to_numpy()
    out = {'rows': int(len(a)), 'forward': {}, 'cv': {}}
    for name, (t, feats) in {'00Z runs only': (a, fa), 'newest published runs': (b, fb)}.items():
        res = {}
        for target in ('gust_peak', 'sust_mean'):
            q = fit_predict(t, feats, train, test, target)
            y = t.loc[test, target].to_numpy()
            c = crps(y, q)
            slot, lead = t.loc[test, 'init'].dt.hour.to_numpy(), t.loc[test, 'lead_day'].to_numpy()
            res[target] = {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[aft[test]].mean()), 4),
                           'mae': round(float(np.abs(q[:, 9] - y).mean()), 3),
                           'by_issue': {f'{s:02d}Z': round(float(c[slot == s].mean()), 4) for s in sorted(set(slot))},
                           'by_lead_day': {int(l): round(float(c[lead == l].mean()), 4) for l in range(8)}}
        out['forward'][name] = res
        print('forward', name, json.dumps(res), flush=True)
        q = np.full((len(t), 19), np.nan)
        for k in range(5):
            q[fold == k] = fit_predict(t, feats, fold != k, fold == k, 'gust_peak')
        c = crps(t.gust_peak.to_numpy(), q)
        slot = t.init.dt.hour.to_numpy()
        out['cv'][name] = {'gust_crps': round(float(c.mean()), 4), 'gust_crps_1_4pm': round(float(c[aft].mean()), 4),
                           'by_issue': {f'{s:02d}Z': round(float(c[slot == s].mean()), 4) for s in sorted(set(slot))}}
        print('cv', name, json.dumps(out['cv'][name]), flush=True)
    json.dump(out, open('var/mos/issue_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
