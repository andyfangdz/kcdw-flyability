"""Conformalized quantile regression widths for the p10-p90 ranges, per target and lead day.

For each out-of-fold hour the conformity score is max(p10 - y, y - p90); the
widening c is the ceil((n+1)(1-alpha))/n empirical quantile of those scores, so
[p10 - c, p90 + c] covers the observation with probability about 1 - alpha on
exchangeable data. alpha = 0.2 targets 80%. A split check fits c on half the
months and reports coverage on the other half.
Writes var/mos/conformal.json.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ALPHA = 0.2
TARGETS = ('sust_mean', 'gust_peak', 'metar_peak', 'xw_sust_mean', 'xw_gust_peak')


def width(scores, alpha=ALPHA):
    scores = np.sort(scores[~np.isnan(scores)])
    n = len(scores)
    k = min(n - 1, int(np.ceil((n + 1) * (1 - alpha))) - 1)
    return float(scores[k])


def main():
    oof = pd.read_parquet('var/mos/oof.parquet')
    obs = pd.read_parquet('var/mos/table_issue.parquet', columns=['valid', 'lead_day', 'init', *TARGETS])
    d = oof.merge(obs, on=['valid', 'lead_day', 'init'])
    month = d.valid.dt.year * 12 + d.valid.dt.month
    out = {'alpha': ALPHA, 'targets': {}}
    for target in TARGETS:
        lo, hi, y = d[f'{target}_p10'], d[f'{target}_p90'], d[target]
        score = np.maximum(lo - y, y - hi).to_numpy()
        out['targets'][target] = {}
        for lead in sorted(d.lead_day.unique()):
            m = (d.lead_day == lead).to_numpy() & ~np.isnan(score)
            c = width(score[m])
            half = (month % 2 == 0).to_numpy()
            c_half = width(score[m & half])
            test = m & ~half
            covered = np.mean((y[test] >= lo[test] - c_half) & (y[test] <= hi[test] + c_half))
            raw = np.mean((y[m] >= lo[m]) & (y[m] <= hi[m]))
            out['targets'][target][str(lead)] = {'widen_kt': round(c, 2), 'raw_coverage': round(float(raw), 3),
                                                 'split_check_coverage': round(float(covered), 3), 'n': int(m.sum())}
            print(f'{target:10s} lead {lead}: widen {c:+.2f} kt  coverage {raw:.1%} -> {covered:.1%} (held-out half)')
    Path('var/mos/conformal.json').write_text(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
