"""Score every compared method on identical hours; blends are quantile averages (Vincentization)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from compare_common import LEVELS, OUT, TARGETS, crps, exceedance, load

t, _, _ = load()
names = [p.stem for p in sorted(Path(OUT).glob('*.parquet'))]
preds = {n: pd.read_parquet(f'{OUT}/{n}.parquet') for n in names}
cols = lambda target: [f'{target}_q{int(round(a * 100)):02d}' for a in LEVELS]
results = {}
for target in TARGETS:
    y = t[target].to_numpy()
    q = {n: p[cols(target)].to_numpy() for n, p in preds.items()}
    for a, b in (('xgb', 'bqn'), ('xgb', 'drn')):
        if a in q and b in q:
            q[f'{a}+{b}'] = np.sort((q[a] + q[b]) / 2, axis=1)
    if all(k in q for k in ('xgb', 'bqn', 'drn')):
        q['xgb+bqn+drn'] = np.sort((q['xgb'] + q['bqn'] + q['drn']) / 3, axis=1)
    ok = np.all([~np.isnan(v).any(axis=1) for v in q.values()], axis=0)
    afternoon = t.hour.isin(range(13, 17)).to_numpy()
    rows = []
    for name, v in q.items():
        entry = {'method': name}
        for scope, m in (('all', ok), ('1-4pm', ok & afternoon)):
            entry[f'CRPS {scope}'] = crps(y[m], v[m]).mean()
            entry[f'MAE p50 {scope}'] = np.abs(v[m, 9] - y[m]).mean()
        entry['cover 10-90'] = np.mean((y[ok] >= v[ok, 1]) & (y[ok] <= v[ok, 17]))
        if target == 'gust_peak':
            p = exceedance(v[ok], 20)
            obs = (y[ok] >= 20).astype(float)
            clim = obs.mean()
            entry['Brier skill >=20'] = 1 - np.mean((p - obs) ** 2) / np.mean((clim - obs) ** 2)
        rows.append(entry)
    table = pd.DataFrame(rows).set_index('method').sort_values('CRPS all')
    print(f'\n{target}: {ok.sum()} identical hours')
    print(table.round(3).to_string())
    results[target] = table.round(4).reset_index().to_dict(orient='records')
Path(f'{OUT}/scores.json').write_text(json.dumps(results, indent=1))
