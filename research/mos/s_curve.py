"""S-curve: forecast gust (3 days ahead) vs the share of KCDW 2-4 p.m. afternoons whose observed peak reached 20 kt."""
import json, math, sys
from html import escape
import numpy as np
from sklearn.linear_model import LogisticRegression
sys.path.insert(0, '.')
from kcdw import ecmwf_archive as ea

c = json.load(open('var/events/commercial-checkride/climatology-cache.json'))
lead = c['lead_days']
obs = {r[0]: r for r in c['observed']['rows']}
gfs = {r[0]: r[2] for r in c['models']['gfs']['rows'] if r[2] is not None}
ifs = {k: v['gust_kt'] for k, v in ea.load(ea.cache_path('var', '14:00', lead))['days'].items() if 'error' not in v}
cur = json.load(open('var/events/commercial-checkride/current/snapshot.json'))['event_climatology']['current']['rows']
SERIES = [('ECMWF', ifs, cur['ifs'][1], 'series-1'), ('GFS', gfs, cur['gfs'][1], 'series-2')]
LIMIT = 20
W, H, L, R, T, B = 760, 440, 64, 150, 24, 56
X0, X1 = 4, 34
sx = lambda v: L + (v - X0) / (X1 - X0) * (W - L - R)
sy = lambda p: T + (1 - p) * (H - T - B)
parts, table = [], []
label_floor = [T + 12]
for name, data, now, slot in SERIES:
    keys = [k for k in data if k in obs]
    x = np.array([data[k] for k in keys]); y = np.array([obs[k][2] >= LIMIT for k in keys], float)
    m = LogisticRegression(C=100).fit(x.reshape(-1, 1), y)
    p = lambda v: float(m.predict_proba([[v]])[0, 1])
    xs = np.linspace(X0, X1, 121)
    path = 'M' + ' L'.join(f'{sx(v):.1f},{sy(p(v)):.1f}' for v in xs)
    parts.append(f'<path class="curve" d="{path}" style="stroke:var(--{slot})"/>')
    fifty = next((v for v in xs if p(v) >= 0.5), None)
    for lo in range(X0, X1, 2):
        sel = (x >= lo) & (x < lo + 2)
        n = int(sel.sum())
        if n < 8:
            continue
        f = float(y[sel].mean())
        tip = f'{name} forecast G{lo}–{lo + 1}: {f:.0%} of {n} afternoons reached {LIMIT} kt'
        parts.append(f'<g class="pt"><circle class="hit" cx="{sx(lo + 1):.1f}" cy="{sy(f):.1f}" r="12"/>'
                     f'<circle cx="{sx(lo + 1):.1f}" cy="{sy(f):.1f}" r="5" style="fill:var(--{slot})"/><title>{escape(tip)}</title></g>')
        table.append((name, f'G{lo}–{lo + 1}', n, f'{f:.0%}'))
    parts.append(f'<g class="now"><line x1="{sx(now):.1f}" x2="{sx(now):.1f}" y1="{sy(p(now)):.1f}" y2="{H - B}" style="stroke:var(--{slot})"/>'
                 f'<circle cx="{sx(now):.1f}" cy="{sy(p(now)):.1f}" r="7" class="nowdot" style="stroke:var(--{slot})"/>'
                 f'<title>Thursday: {name} forecasts G{now:.0f} → fitted {p(now):.0%} chance of a {LIMIT} kt peak</title></g>')
    end = sy(p(X1))
    # Keep stacked end labels apart (each block is three lines, about 44 px tall).
    end = max(end, label_floor[0])
    label_floor[0] = end + 52
    parts.append(f'<text class="dl" x="{W - R + 8}" y="{end + 4:.1f}">{name}'
                 f'<tspan x="{W - R + 8}" dy="15" class="sub">50% at G{fifty:.0f}</tspan>'
                 f'<tspan x="{W - R + 8}" dy="15" class="sub">Thu G{now:.0f} → {p(now):.0%}</tspan></text>')
grid = []
for v in range(5, X1 + 1, 5):
    grid.append(f'<line class="g" x1="{sx(v):.1f}" x2="{sx(v):.1f}" y1="{T}" y2="{H - B}"/><text class="t" x="{sx(v):.1f}" y="{H - B + 16}" text-anchor="middle">{v}</text>')
for q in (0, .25, .5, .75, 1):
    grid.append(f'<line class="g" x1="{L}" x2="{W - R}" y1="{sy(q):.1f}" y2="{sy(q):.1f}"/><text class="t" x="{L - 8}" y="{sy(q) + 4:.1f}" text-anchor="end">{q:.0%}</text>')
literal = (f'<path class="literal" d="M{sx(X0):.1f},{sy(0):.1f} L{sx(LIMIT):.1f},{sy(0):.1f} L{sx(LIMIT):.1f},{sy(1):.1f} L{sx(X1):.1f},{sy(1):.1f}"/>'
           f'<text class="note" x="{sx(LIMIT) + 6:.1f}" y="{sy(1) + 14:.1f}">If forecast gusts were taken literally</text>')
rows = ''.join(f'<tr><td>{a}</td><td>{b}</td><td>{n}</td><td>{f}</td></tr>' for a, b, n, f in table)
html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forecast gust vs chance of a 20 kt peak at KCDW</title><style>
.viz-root{{color-scheme:light;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--grid:#e6e5e0;--series-1:#2a78d6;--series-2:#eb6834}}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme="light"])) .viz-root{{color-scheme:dark;--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--grid:#34332f;--series-1:#3987e5;--series-2:#d95926}}}}
body{{margin:0;background:var(--surface-1)}}
.viz-root{{background:var(--surface-1);color:var(--text-primary);font:14px/1.45 system-ui,sans-serif;padding:20px;max-width:800px}}
h1{{font-size:18px;margin:0 0 4px}} p{{margin:0 0 12px;color:var(--text-secondary)}}
svg{{width:100%;height:auto;display:block}} .g{{stroke:var(--grid)}} .t{{fill:var(--text-secondary);font-size:12px}}
.curve{{fill:none;stroke-width:2}} .literal{{fill:none;stroke:var(--text-secondary);stroke-width:1.5;stroke-dasharray:5 4}}
.note,.sub{{fill:var(--text-secondary);font-size:12px}} .dl{{fill:var(--text-primary);font-size:13px;font-weight:600}}
.pt circle:not(.hit){{stroke:var(--surface-1);stroke-width:2}} .hit{{fill:transparent}} .pt:hover circle:not(.hit){{stroke:var(--text-primary)}}
.now line{{stroke-width:1.5;stroke-dasharray:2 3}} .nowdot{{fill:var(--surface-1);stroke-width:2.5}}
.legend{{display:flex;gap:18px;margin:4px 0 8px;color:var(--text-secondary);font-size:13px}} .sw{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}}
.ax{{fill:var(--text-secondary);font-size:13px}} details{{margin-top:10px;color:var(--text-secondary)}} table{{border-collapse:collapse;font-size:13px}} td,th{{padding:2px 10px;text-align:right}}
</style></head><body><div class="viz-root">
<h1>A model's gust forecast is a probability, not a promise</h1>
<p>KCDW, 2–4 p.m., {len(ifs)} ECMWF and {len(gfs)} GFS afternoons ({lead}-day-ahead forecasts, Nov 2024 onward for ECMWF). Dots: share of afternoons whose observed peak reached {LIMIT} kt, per 2-kt forecast bin (bins with 8+ days). Curves: logistic fit. Rings: Thursday's forecast.</p>
<div class="legend"><span><i class="sw" style="background:var(--series-1)"></i>ECMWF (native, gust from 2 of 6 h)</span><span><i class="sw" style="background:var(--series-2)"></i>GFS</span></div>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="S-shaped curves: the chance of a 20 kt peak rises with forecast gust, crossing 50% only in the low 20s">
{''.join(grid)}{literal}{''.join(parts)}
<text class="ax" x="{(L + W - R) / 2:.0f}" y="{H - 14}" text-anchor="middle">Forecast gust, 3 days ahead (kt)</text>
<text class="ax" transform="translate(16 {(T + H - B) / 2:.0f}) rotate(-90)" text-anchor="middle">Afternoons with observed peak ≥ {LIMIT} kt</text></svg>
<details><summary>Table view</summary><table><tr><th>Model</th><th>Forecast</th><th>Afternoons</th><th>Reached {LIMIT} kt</th></tr>{rows}</table></details>
</div></body></html>'''
open('research/mos/s_curve.html', 'w').write(html)
print('written')
