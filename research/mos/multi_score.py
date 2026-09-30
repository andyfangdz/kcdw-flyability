"""Score the multi-station variants: KCDW head-to-head, per-station gains from site information, per-station conformal widths."""
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('var/mos-multi')
LEVELS = np.round(np.arange(0.05, 0.96, 0.05), 2)


def crps(y, q):
    d = y[:, None] - q
    return 2 * np.maximum(LEVELS * d, (LEVELS - 1) * d).mean(axis=1)


def load(name):
    path = ROOT / f'oof_{name}.parquet'
    return pd.read_parquet(path) if path.exists() else None


def main():
    runs = {n: load(n) for n in ('bqn_local', 'bqn_pooled', 'bqn_site', 'xgb_local', 'xgb_pooled', 'xgb_site', 'xgb_site_fast')}
    runs = {k: v for k, v in runs.items() if v is not None}
    prod = pd.read_parquet('var/mos/oof.parquet')
    summary = {}
    for target in ('gust_peak', 'sust_mean'):
        key = ['valid', 'lead_day']
        cdw = {k: v[v.station == 'CDW'].set_index(key) for k, v in runs.items()}
        common = None
        for v in cdw.values():
            common = v.index if common is None else common.intersection(v.index)
        common = common.intersection(prod.set_index(key).index)
        y = next(iter(cdw.values())).loc[common, target].to_numpy()
        after = pd.Index(common.get_level_values('valid')).tz_localize('UTC').tz_convert('America/New_York').hour.isin(range(13, 17))
        rows = []
        for name, v in cdw.items():
            v = v.loc[common]
            if True:
                q = v[[f'{target}_q{int(round(a * 100)):02d}' for a in LEVELS]].to_numpy()
                med, lo, hi = q[:, 9], q[:, 1], q[:, 17]
                c = crps(y, q)
                rows.append({'model': name, 'CRPS': c.mean(), 'CRPS 1-4pm': c[after].mean(), 'MAE p50': np.abs(med - y).mean(),
                             'MAE p50 1-4pm': np.abs(med - y)[after].mean(), 'cover 10-90': np.mean((y >= lo) & (y <= hi))})
            else:
                med, lo, hi = v[f'{target}_q50'].to_numpy(), v[f'{target}_q10'].to_numpy(), v[f'{target}_q90'].to_numpy()
                rows.append({'model': name, 'MAE p50': np.abs(med - y).mean(), 'MAE p50 1-4pm': np.abs(med - y)[after].mean(),
                             'cover 10-90': np.mean((y >= lo) & (y <= hi))})
        p = prod.set_index(key).loc[common]
        rows.append({'model': 'production (KCDW-only xgb)', 'MAE p50': np.abs(p[f'{target}_p50'].to_numpy() - y).mean(),
                     'MAE p50 1-4pm': np.abs(p[f'{target}_p50'].to_numpy() - y)[after].mean(),
                     'cover 10-90': np.mean((y >= p[f'{target}_p10'].to_numpy()) & (y <= p[f'{target}_p90'].to_numpy()))})
        table = pd.DataFrame(rows).set_index('model')
        print(f'\nKCDW {target}: {len(common)} identical hours\n' + table.round(3).to_string())
        summary[f'kcdw_{target}'] = table.round(4).reset_index().to_dict(orient='records')
        for method in ('xgb', 'bqn'):
            if f'{method}_pooled' not in runs or f'{method}_site' not in runs:
                continue
            per = []
            for station in sorted(runs[f'{method}_site'].station.unique()):
                a = runs[f'{method}_pooled'][runs[f'{method}_pooled'].station == station].set_index(key)
                b = runs[f'{method}_site'][runs[f'{method}_site'].station == station].set_index(key)
                idx = a.index.intersection(b.index)
                yy = b.loc[idx, target].to_numpy()
                cols = [f'{target}_q{int(round(x * 100)):02d}' for x in LEVELS]
                ca, cb = crps(yy, a.loc[idx, cols].to_numpy()).mean(), crps(yy, b.loc[idx, cols].to_numpy()).mean()
                per.append({'station': station, 'hours': len(idx), 'CRPS pooled': ca, 'CRPS site': cb, 'gain %': 100 * (ca - cb) / ca})
            per = pd.DataFrame(per).set_index('station')
            print(f'\nPer-station {target} CRPS, pooled vs site-aware {method}:\n' + per.round(3).to_string())
            summary[f'per_station_{method}_{target}'] = per.round(4).reset_index().to_dict(orient='records')
    if 'bqn_site' in runs:
        widths = {}
        s = runs['bqn_site']
        for target in ('gust_peak', 'sust_mean', ):
            for (station, lead), g in s.groupby(['station', 'lead_day']):
                y = g[target].to_numpy(); lo = g[f'{target}_q10'].to_numpy(); hi = g[f'{target}_q90'].to_numpy()
                score = np.sort(np.maximum(lo - y, y - hi))
                k = min(len(score) - 1, int(np.ceil((len(score) + 1) * 0.8)) - 1)
                widths.setdefault(target, {}).setdefault(station, {})[str(lead)] = round(float(score[k]), 2)
        (ROOT / 'conformal_by_station.json').write_text(json.dumps(widths, indent=1))
        print('\nper-station conformal widths written')
    (ROOT / 'scores.json').write_text(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
