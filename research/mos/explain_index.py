"""Landing page for the published explainers: links plus the leak-free skill and integrity checks, read from the result files (research)."""
import html
import json

import numpy as np
import pandas as pd

from explain_html import GRID, INK, INK2


def pct(new, old):
    return f'{100 * (old - new) / old:.0f}%'


def main():
    bench = json.load(open('var/mos/benchmark.json'))
    audit = json.load(open('var/mos/leakage_audit.json'))
    curve = json.load(open('var/mos/learning_curve.json'))
    multi = json.load(open('var/mos-multi/explain_multi.json'))
    g, s, d = bench['gust_mae_kt'], bench['sust_mae_kt'], bench['dir_mae_deg']
    skill = [('Peak gust (1-minute, 1-hour max)', f"{g['calibrated']:.2f} kt", f"{g['nbm']:.2f} kt", f"{g['raw_ecmwf_ens']:.2f} kt", pct(g['calibrated'], g['nbm'])),
             ('Sustained wind (hourly mean)', f"{s['calibrated']:.2f} kt", f"{s['nbm']:.2f} kt", '–', pct(s['calibrated'], s['nbm'])),
             ('Direction (when ≥ 5 kt)', f"{d['calibrated']:.1f}°", f"{d['nbm']:.1f}°", '–', pct(d['calibrated'], d['nbm']))]
    rows = ''.join(f'<tr><th scope="row">{a}</th><td class="us">{b}</td><td>{c}</td><td>{e}</td><td>{f}</td></tr>' for a, b, c, e, f in skill)
    ms = multi['skill']
    hours = sum(r['hours'] for r in ms)
    cal = sum(r['calibrated_on_ecmwf_ens_hours'] * r['hours'] for r in ms) / hours
    ens = sum(r['ecmwf_ens'] * r['hours'] for r in ms) / hours
    fwd = {r['variant']: r for r in audit['forward']}
    base, fixed = fwd['embargo'], fwd['embargo + Open-Meteo removed']
    om_cost = 100 * (fixed['gust_peak_crps'] - base['gust_peak_crps']) / base['gust_peak_crps']
    emb = 100 * (base['gust_peak_crps'] - fwd['as before (no embargo)']['gust_peak_crps']) / fwd['as before (no embargo)']['gust_peak_crps']
    # The forward test used the lead-day model, so compare with that model's cross-validation (saved before the issue-time switch).
    oof = pd.read_parquet('var/mos/oof_before_issue.parquet').merge(pd.read_parquet('var/mos/table.parquet', columns=['valid', 'lead_day', 'gust_peak']), on=['valid', 'lead_day'])
    oof = oof[(oof.date >= '2025-10-01') & oof.gust_peak.notna() & (oof.lead_day >= 1)]  # the forward test covered lead days 1-7
    cv_fwd = float(np.abs(oof.gust_peak_p50 - oof.gust_peak).mean())
    cv = audit['purged_cv']
    purge = 100 * (cv['purged 7 days'] - cv['plain']) / cv['plain']
    wn2 = fwd['embargo + OM fixed + WN2 2022 masked']
    checks = [('Look-ahead in Open-Meteo archives (ICON, HRES, GEM)', f'Found: "previous day N" used runs up to ~18 h newer than the 00Z runs of every other input, worth {om_cost:.1f}% of gust accuracy. Fixed by removing them; aligned, they added nothing.'),
              ('Training labels after a test forecast was issued', f'8-day embargo before the test year: {emb:+.2f}% (negligible).'),
              ('Leakage across cross-validation month boundaries', f'Purging 7 days around each test month: {purge:+.1f}% (small).'),
              ('WeatherNext 2 hindcast overlapping its own training (2022)', f'Masking 2022 made the forecast {100 * (wn2["gust_peak_crps"] - fwd["embargo + Open-Meteo fixed"]["gust_peak_crps"]) / fwd["embargo + Open-Meteo fixed"]["gust_peak_crps"]:+.1f}% worse, so no leak.'),
              ('Observation-derived inputs', 'None among the inputs; every model input is a forecast.'),
              ('Runs used before they were published', 'Each update uses a model run only if it was in our archives by then, from publication delays measured on 2026-09-30 (HRRR about 2 h, GFS about 6 h, WeatherNext 3 about 7 h, the 00Z ECMWF and WeatherNext 2 runs by about 10 h), with margins; a late run falls back to the previous one in training and production alike, and every training row is checked by an assertion.'),
              ('Same-day forecasts', 'Built only from runs published before the update, and only later hours are forecast and scored.'),
              ('Strict forward year', f'Trained only before Oct 2025, scored Oct 2025–Sep 2026: gust MAE {fixed["gust_peak_mae_p50"]:.2f} kt, no worse than cross-validation on the same hours ({cv_fwd:.2f} kt).')]
    check_rows = ''.join(f'<tr><th scope="row">{html.escape(a)}</th><td>{html.escape(b)}</td></tr>' for a, b in checks)
    day0 = json.load(open('var/mos/day0_test.json'))['forward']
    d_old, d_new = day0['lead day 1 (current practice)']['gust_peak'], day0['day 0, models only']['gust_peak']
    issue = json.load(open('var/mos/issue_vs_old.json'))
    null = []
    for label, path, base, alt in (('NOAA\'s AI model (GraphCast-GFS, then AIGFS)', 'var/mos/aigfs_test.json', 'production inputs', '+ NOAA AI model'),
                                   ('Boundary-layer depth and winds aloft (native HRRR)', 'var/mos/pbl_test.json', 'production inputs', '+ boundary layer + mixing proxies'),
                                   ('Each model\'s recent error at KCDW', 'var/mos/features_test.json', 'production inputs', '+ recent bias')):
        r = json.load(open(path))['forward']
        key = 'crps' if 'crps' in r[base]['gust_peak'] else 'crps_lead1'
        a, b = r[base]['gust_peak'][key], r[alt]['gust_peak'][key]
        null.append(f'<li>{label}: gust CRPS {a:.3f} → {b:.3f} ({100 * (b - a) / a:+.1f}%).</li>')
    blends = json.load(open('var/mos/nn_arch_blends.json'))['gust_peak']
    scaling = json.load(open('var/mos-multi/scaling_nn.json'))
    c1, c4 = curve[0]['gust_peak_crps'], curve[-1]['gust_peak_crps']
    c2, c3 = curve[1]['gust_peak_crps'], curve[2]['gust_peak_crps']
    page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KCDW calibrated wind: research notes</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;color:{INK};background:#fcfcfb;max-width:960px;margin:24px auto;padding:0 20px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:30px 0 6px}} p,li{{color:{INK2}}} p{{max-width:780px}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:18px 0}} .card{{border:1px solid {GRID};border-radius:8px;padding:14px 16px;background:#fff}}
.card a{{font-weight:600;font-size:16px}} table{{border-collapse:collapse;font-size:14px;margin:6px 0}} th,td{{padding:6px 10px;border-bottom:1px solid {GRID};text-align:left;vertical-align:top}}
thead th{{color:{INK2};font-weight:600}} td.us{{font-weight:700;color:{INK}}} tbody th{{font-weight:600}}
@media (max-width:700px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>KCDW calibrated wind: research notes</h1>
<p>A machine-learned calibration of many weather models' wind forecasts into hourly sustained wind, peak gust and direction at Essex County
Airport (KCDW), and at 21 other airports around New York. NBM is used only as a benchmark, never as an input.</p>
<div class="cards">
<div class="card"><a href="mos-explainer/">What the KCDW model learned →</a><p>SHAP attributions, response curves, a beeswarm and the first tree of the production gust model.</p></div>
<div class="card"><a href="mos-multi-explainer/">What the 22-station model learned →</a><p>How one model calibrates each airport differently: per-airport site effects, exposure by wind direction, held-out skill.</p></div>
</div>
<h2>How good is it</h2>
<p>KCDW, 1–4 pm local, {bench["afternoon_hours"]:,} identical hours ({bench["period"][0]} to {bench["period"][1]}), mean absolute error of the median forecast
from the 7 a.m. update, scored on months the model never saw.</p>
<table><thead><tr><th></th><th>Calibrated</th><th>NBM</th><th>ECMWF ENS mean</th><th>Better than NBM</th></tr></thead><tbody>{rows}</tbody></table>
<p>All 22 airports, held-out months: peak-gust error {cal:.2f} kt versus {ens:.2f} kt for the raw ECMWF ensemble mean ({pct(cal, ens)} lower).
NBM's archive here uses runs issued exactly 24N hours before each hour, fresher than most runs the 7 a.m. update has, so the NBM comparison is conservative.</p>
<h2>Integrity checks</h2>
<table><tbody>{check_rows}</tbody></table>
<h2>What helped</h2>
<ul><li>Same-day forecasts: using that day's own runs instead of the previous day's cut gust CRPS from {d_old["crps"]:.3f} to {d_new["crps"]:.3f}
({100 * (d_new["crps"] - d_old["crps"]) / d_old["crps"]:+.1f}%), and {100 * (d_new["crps_1_4pm"] - d_old["crps_1_4pm"]) / d_old["crps_1_4pm"]:+.1f}% for 1–4 pm (forward year).</li>
<li>Each model's newest published run at every update (04, 11, 16, 22Z): {issue["all"]["gust_change_pct"]:+.1f}% gust MAE overall against the previous
production, {issue["04Z"]["gust_change_pct"]:+.1f}% at the midnight update, which used to rely on the previous day's runs.</li></ul>
<h2>What did not help</h2>
<ul>{"".join(null)}
<li>Neural networks: the best (attention across the forecast models) scored {100 * (blends["nn_sources"]["crps"] - blends["xgb"]["crps"]) / blends["xgb"]["crps"]:+.1f}% against
XGBoost; averaged 50/50 with XGBoost it improved gust CRPS by {100 * (blends["xgb"]["crps"] - blends["xgb + sources"]["crps"]) / blends["xgb"]["crps"]:.1f}%, too little to justify a second model in production.</li>
<li>Training on more airports: from 1 to 22 airports the station-hours grew {scaling[-1]["station_hours"] / scaling[0]["station_hours"]:.0f}× but the independent
days only {scaling[0]["independent_days"]:,} → {scaling[-1]["independent_days"]:,}; KCDW gust CRPS moved {scaling[0]["xgb_kcdw"]:.3f} → {scaling[-1]["xgb_kcdw"]:.3f} (XGBoost).</li></ul>
<h2>Still worth knowing</h2>
<ul><li>More history helps, with diminishing returns: training on the last 1, 2, 3 and 4.3 years gives forward-year gust CRPS {c1:.3f}, {c2:.3f}, {c3:.3f} and {c4:.3f}
({100 * (c2 - c1) / c1:+.1f}%, {100 * (c3 - c2) / c2:+.1f}%, {100 * (c4 - c3) / c3:+.1f}% per step).</li>
<li>Model gust fields: once winds and stability are known, the models' own gust diagnostics add almost nothing.</li>
<li>Borrowing from other airports: the 22-station model ties the KCDW-only model at KCDW; its value is calibrating the other airports.</li></ul>
<p>Research code: <code>research/mos</code> in the repository. Supplemental guidance, not an official aviation forecast.</p>
</body></html>'''
    open('research/mos/explain_index.html', 'w').write(page)
    print('wrote research/mos/explain_index.html')


if __name__ == '__main__':
    main()
