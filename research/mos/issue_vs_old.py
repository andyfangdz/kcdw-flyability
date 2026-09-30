"""Issue-time production vs the previous lead-day production, each as it would really have run (research).

Out-of-fold median forecasts on identical valid hours. At the 04Z update (local midnight) the old
production could only use the previous day's 00Z runs, so it is compared at lead day + 1; at 11, 16
and 22Z it had the same day's 00Z runs. Needs var/mos/oof_before_issue.parquet (the old out-of-fold
file) and the current var/mos/oof.parquet. Writes var/mos/issue_vs_old.json.
"""
import json

import numpy as np
import pandas as pd


def main():
    t = pd.read_parquet('var/mos/table_issue.parquet', columns=['valid', 'lead_day', 'init', 'gust_peak', 'sust_mean'])
    old = pd.read_parquet('var/mos/oof_before_issue.parquet')[['valid', 'lead_day', 'gust_peak_p50', 'sust_mean_p50']]
    new = pd.read_parquet('var/mos/oof.parquet')
    out, tot = {}, {'g_old': 0.0, 'g_new': 0.0, 's_old': 0.0, 's_new': 0.0, 'n': 0}
    for slot, shift in ((4, 1), (11, 0), (16, 0), (22, 0)):
        n = new[new.init.dt.hour == slot].merge(t, on=['valid', 'lead_day', 'init'])
        m = n.assign(old_lead=n.lead_day + shift).merge(old.rename(columns={'lead_day': 'old_lead'}), on=['valid', 'old_lead'], suffixes=('', '_old'))
        m = m[m.gust_peak.notna()]
        g = [float(np.abs(m[c] - m.gust_peak).mean()) for c in ('gust_peak_p50_old', 'gust_peak_p50')]
        s = [float(np.abs(m[c] - m.sust_mean).mean()) for c in ('sust_mean_p50_old', 'sust_mean_p50')]
        out[f'{slot:02d}Z'] = {'hours': int(len(m)), 'gust_mae_old': round(g[0], 3), 'gust_mae_new': round(g[1], 3),
                               'sust_mae_old': round(s[0], 3), 'sust_mae_new': round(s[1], 3), 'gust_change_pct': round(100 * (g[1] - g[0]) / g[0], 1)}
        for k, v in (('g_old', g[0]), ('g_new', g[1]), ('s_old', s[0]), ('s_new', s[1])):
            tot[k] += v * len(m)
        tot['n'] += len(m)
    out['all'] = {'gust_change_pct': round(100 * (tot['g_new'] - tot['g_old']) / tot['g_old'], 1),
                  'sust_change_pct': round(100 * (tot['s_new'] - tot['s_old']) / tot['s_old'], 1), 'hours': tot['n']}
    json.dump(out, open('var/mos/issue_vs_old.json', 'w'), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
