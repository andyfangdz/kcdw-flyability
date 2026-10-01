"""Render var/mos/reliability.json as research/mos/reliability.html: reliability of gust-threshold probabilities (research)."""
import html
import json

from explain_html import GRID, INK, INK2

RARE_HOURS = 300  # fewer event hours than this in five years: too rare to judge reliability or skill
# Fixed categorical order from the reference palette; a method keeps its colour in every section.
COLORS = {'quantiles': '#2a78d6', 'classifier': '#eb6834', 'raw ECMWF ensemble': '#1baf7a', 'raw GEFS ensemble': '#eda100',
          'NBM + its past errors': '#e87ba4', 'NBM (yes/no)': '#008300', 'quantiles, recalibrated': '#4a3aa7'}
LABELS = {'quantiles': 'Calibrated (quantile model)', 'classifier': 'Calibrated (threshold classifier)', 'raw ECMWF ensemble': 'Raw ECMWF ensemble',
          'raw GEFS ensemble': 'Raw GEFS ensemble', 'NBM + its past errors': 'NBM + its past errors', 'NBM (yes/no)': 'NBM (yes/no)',
          'quantiles, recalibrated': 'Calibrated (quantiles, recalibrated)'}
POINTS_ONLY = {'NBM (yes/no)'}  # a yes/no forecast has only two probabilities: markers, no line


def panel(thr, res, size=300, pad=40, strip=46):
    """Reliability diagram for one threshold, with a sharpness strip (share of forecasts per bin, quantile model)."""
    plot = size - pad - 12
    X = lambda v: pad + plot * v
    Y = lambda v: 12 + plot * (1 - v)
    h = size + strip + 20
    base = res['quantiles']['base_rate']
    out = [f'<svg viewBox="0 0 {size} {h}" width="100%" role="img" aria-label="Reliability at {thr} kt">']
    for v in (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<line x1="{X(0)}" x2="{X(1)}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{GRID}"/>'
                   f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{Y(0)}" y2="{Y(1)}" stroke="{GRID}"/>'
                   f'<text x="{X(0) - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="tick">{v:.0%}</text>'
                   f'<text x="{X(v):.1f}" y="{Y(0) + 15}" text-anchor="middle" class="tick">{v:.0%}</text>')
    out.append(f'<line x1="{X(0)}" y1="{Y(0)}" x2="{X(1)}" y2="{Y(1)}" stroke="{INK2}" stroke-width="1"/>')
    out.append(f'<line x1="{X(0)}" x2="{X(1)}" y1="{Y(base):.1f}" y2="{Y(base):.1f}" stroke="{INK2}" stroke-dasharray="3 3"/>'
               f'<text x="{X(1) - 2}" y="{Y(base) - 4:.1f}" text-anchor="end" class="tick">base rate {base:.1%}</text>')
    for name, color in COLORS.items():
        if name not in res:
            continue
        pts = [c for c in res[name]['curve'] if c['days'] >= 3]
        if not pts:
            continue
        line = ' '.join(f'{X(c["forecast"]):.1f},{Y(c["observed"]):.1f}' for c in pts)
        if name not in POINTS_ONLY:
            out.append(f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2"/>')
        for c in pts:
            lo, hi = c['ci90']
            tip = html.escape(f'{LABELS[name]}: forecast {c["forecast"]:.1%}, observed {c["observed"]:.1%} '
                              f'(90% interval {lo:.0%}–{hi:.0%}), {c["hours"]:,} hours on {c["days"]:,} days')
            out.append(f'<g><title>{tip}</title><line x1="{X(c["forecast"]):.1f}" x2="{X(c["forecast"]):.1f}" y1="{Y(lo):.1f}" y2="{Y(hi):.1f}" '
                       f'stroke="{color}" stroke-width="1.5" opacity="0.6"/><circle cx="{X(c["forecast"]):.1f}" cy="{Y(c["observed"]):.1f}" r="4" '
                       f'fill="{color}" stroke="#fcfcfb" stroke-width="2"/><circle cx="{X(c["forecast"]):.1f}" cy="{Y(c["observed"]):.1f}" r="11" fill="transparent"/></g>')
    # Sharpness: share of forecast hours per probability bin (quantile model), as a strip of bars.
    curve = res['quantiles']['curve']
    total = sum(c['hours'] for c in curve)
    top = size + 4
    for c in curve:
        share = c['hours'] / total
        x0, x1 = X(c['bin'][0]), X(c['bin'][1])
        bar = max(1.5, (strip - 6) * share ** 0.5)  # square-root scale so rare high-probability bins stay visible
        out.append(f'<g><title>{html.escape(f"{c["bin"][0]:.0%}–{c["bin"][1]:.0%}: {share:.1%} of forecast hours")}</title>'
                   f'<rect x="{x0 + 1:.1f}" y="{top + strip - 6 - bar:.1f}" width="{max(x1 - x0 - 2, 1.5):.1f}" height="{bar:.1f}" rx="2" fill="{COLORS["quantiles"]}" opacity="0.55"/></g>')
    out.append(f'<text x="{X(0.5)}" y="{h - 2}" text-anchor="middle" class="axis">forecast probability (share of hours issued, below)</text></svg>')
    return ''.join(out)


def table(section, title):
    rows = []
    for thr, res in section.items():
        for name in ('quantiles', 'quantiles, recalibrated', 'classifier', 'raw ECMWF ensemble', 'raw GEFS ensemble', 'NBM + its past errors',
                     'NBM (yes/no)', 'climatology'):
            if name not in res:
                continue
            r = res[name]
            if r['events'] < 30:  # a handful of events: no meaningful score
                rows.append(f'<tr><td>≥{thr} kt</td><td>{html.escape(LABELS.get(name, "Climatology (month × hour)"))}</td><td>{r["events"]:,} / {r["hours"]:,}</td>'
                            f'<td colspan="6">too few events to score</td></tr>')
                continue
            bss = '' if r['bss_vs_climatology'] is None else f"{r['bss_vs_climatology']:+.3f}"
            rows.append(f'<tr><td>≥{thr} kt</td><td>{html.escape(LABELS.get(name, "Climatology (month × hour)"))}</td><td>{r["events"]:,} / {r["hours"]:,}</td>'
                        f'<td>{r["base_rate"]:.1%}</td><td>{r["brier"]:.4f}</td><td>{bss}</td>'
                        f'<td>{r["reliability"]:.5f}</td><td>{r["resolution"]:.5f}</td><td>{r["roc_auc"]:.3f}</td></tr>')
    return (f'<h3>{title}</h3><table class="t"><thead><tr><th>Threshold</th><th>Method</th><th>Events / hours</th><th>Base rate</th><th>Brier</th>'
            f'<th>Skill vs climatology</th><th>Reliability (lower is better)</th><th>Resolution (higher is better)</th><th>ROC area</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


TARGETS = {'gust_peak': ('Peak gust', 'the hour\'s highest 1-minute peak gust'),
           'sust_mean': ('Sustained wind', 'the hour\'s mean wind'),
           'xw_sust_mean': ('Runway 04/22 crosswind, sustained', 'the hour\'s mean wind component across runway 04/22 (030/210 true)'),
           'xw_gust_peak': ('Runway 04/22 crosswind, peak gust', 'the hour\'s highest gust component across runway 04/22')}


def section(key, data):
    title, what = TARGETS[key]
    cv = data['cv']
    rare = {thr for thr, r in cv.items() if r['quantiles']['events'] < RARE_HOURS}
    panels = ''.join((f'<figure><figcaption>{title} ≥ {thr} kt · {cv[thr]["quantiles"]["events"]:,} event hours</figcaption>{panel(thr, cv[thr])}</figure>'
                      if thr not in rare else
                      f'<figure><figcaption>{title} ≥ {thr} kt</figcaption><p>Too rare to evaluate: {cv[thr]["quantiles"]["events"]:,} event hours in five years.</p></figure>')
                     for thr in cv)
    methods = [n for n in COLORS if n in next(iter(cv.values()))]
    legend = ''.join(f'<span class="sw" style="background:{COLORS[n]}"></span>{html.escape(LABELS[n])}' for n in methods)
    order = [n for n in COLORS if n in methods]
    def cells(thr, r):
        if thr in rare:
            return f'<td colspan="{len(order) + 2}">too rare to evaluate ({r["quantiles"]["events"]} event hours)</td>'
        c = r['common']
        best = max((v for v in c['bss'].values() if v is not None), default=None)
        out = ''.join(('<td><strong>' if c['bss'][n] == best else '<td>') + (f'{c["bss"][n]:+.2f}' if c['bss'][n] is not None else '–')
                      + ('</strong></td>' if c['bss'][n] == best else '</td>') for n in order)
        return out + f'<td>{c["hours"]:,} ({c["events"]:,} events)</td><td>{data["forward"][thr]["quantiles"]["bss_vs_climatology"]:+.2f}</td>'
    rows = ''.join(f'<tr><td>≥{thr} kt</td><td>{r["quantiles"]["base_rate"]:.1%}</td>{cells(thr, r)}</tr>' for thr, r in cv.items())
    head = ('<th>Threshold</th><th>How often</th>' + ''.join(f'<th>{html.escape(LABELS[n])}</th>' for n in order)
            + '<th>Identical hours</th><th>Calibrated, forward year</th>')
    note = (' The raw ensembles\' crosswind uses their ensemble-mean wind direction, so those baselines are approximate.' if key.startswith('xw_') else '') + \
           ' NBM has no probabilities in our archive, so it appears as a yes/no forecast and dressed with its own past errors.'
    return (f'<h2 id="{key}">{title}</h2><p>The chance that {what} reaches each threshold.{note}</p>'
            f'<p class="legend">{legend}<span class="sw" style="background:{INK2};height:1px"></span>perfect reliability</p>'
            f'<div class="grid">{panels}</div><h3>Skill against climatology on identical hours (cross-validated; best in bold)</h3>'
            f'<p>Hours where every method has a forecast: NBM\'s archive starts November 2024 and ECMWF\'s in 2024, so this is the fair comparison.</p>'
            f'<table class="t"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
            f'<details><summary>Full scores</summary>{table(cv, "Cross-validation, 2021–2026")}{table(data["forward"], "Forward year: trained before October 2025, scored October 2025 – September 2026")}</details>')


def main():
    D = json.load(open('var/mos/reliability.json'))['targets']
    keys = [k for k in TARGETS if k in D]
    nav = ' · '.join(f'<a href="#{k}">{html.escape(TARGETS[k][0])}</a>' for k in keys)
    def skills(k):
        def one(thr, r):
            if r['quantiles']['events'] < RARE_HOURS:
                return f'≥{thr}: too rare'
            b = r['common']['bss']
            raw = max(v for n, v in b.items() if n.startswith('raw') and v is not None)
            return f"≥{thr}: {b['quantiles']:+.2f} (best raw ensemble {raw:+.2f}, NBM {b['NBM + its past errors']:+.2f})"
        return ' · '.join(one(thr, r) for thr, r in D[k]['cv'].items())
    overview = ''.join(f'<tr><td>{html.escape(TARGETS[k][0])}</td><td>{skills(k)}</td></tr>' for k in keys)
    hours = D['gust_peak']['cv']['20']['quantiles']['hours'] if 'gust_peak' in D else 0
    page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KCDW wind probabilities: reliability</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;color:{INK};background:#fcfcfb;max-width:1120px;margin:24px auto;padding:0 20px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:19px;margin:38px 0 4px}} h3{{font-size:15px;margin:20px 0 6px}} p{{color:{INK2};max-width:820px}}
.tick{{font-size:10.5px;fill:{INK2}}} .axis{{font-size:11.5px;fill:{INK2}}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} figure{{margin:0}} figcaption{{font-size:13px;font-weight:600;margin-bottom:2px}}
.legend{{font-size:13px;color:{INK2};margin:6px 0 12px}} .sw{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin:0 6px 0 14px}}
table.t{{border-collapse:collapse;font-size:13px}} .t th,.t td{{padding:4px 9px;border-bottom:1px solid {GRID};text-align:left}} .t th{{color:{INK2};font-weight:600}}
details{{margin-top:12px}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>How reliable are the wind probabilities?</h1>
<p><a href="../">Research notes</a> · <a href="../mos-explainer/">What the model learned</a></p>
<p>For KCDW, the chance that the hour's wind reaches each threshold, from the 7 a.m. update for that day and the seven after: peak gust,
sustained wind, and the crosswind on runway 04/22. A reliable forecast sits on the diagonal: of all the hours given a 30% chance, about 30%
should see it. Every point is out of sample (month-interleaved cross-validation, {hours:,} hours from 2021 to 2026, plus a forward year);
bars are 90% intervals counted in days, since windy hours cluster on the same days. Points from fewer than three days are not drawn.</p>
<p>{nav}</p>
<p><strong>Which probability method?</strong> Three calibrated versions are compared: probabilities read from the quantile model, the same after an
isotonic recalibration fitted only on held-out forecasts, and a dedicated classifier per threshold. Cross-validation favours the recalibrated
version slightly, but in the forward year the recalibration does not hold up for rare thresholds (too few windy events to fit the mapping), and the
plain quantile probabilities score best overall, with the classifiers last.</p>
<h3>Skill against climatology at a glance (cross-validated, identical hours)</h3>
<p>Brier skill score: 0 is no better than the usual frequency for that month and hour, 1 is perfect. Calibrated first, then the best
raw ensemble (ECMWF or GEFS) and NBM dressed with its past errors.</p>
<table class="t"><tbody>{overview}</tbody></table>
{"".join(section(k, D[k]) for k in keys)}
<p>Supplemental guidance, not an official aviation forecast.</p>
</body></html>'''
    open('research/mos/reliability.html', 'w').write(page)
    print('wrote research/mos/reliability.html')


if __name__ == '__main__':
    main()
