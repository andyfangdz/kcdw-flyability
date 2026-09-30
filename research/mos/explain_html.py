"""Render var/mos/explain.json as research/mos/explain.html: what the calibrated gust model learned (research)."""
import html
import json

BLUE, ORANGE, RED, INK, INK2, GRID = '#2a78d6', '#eb6834', '#e34948', '#0b0b0b', '#52514e', '#e4e3df'
NAMES = {
    'gefs_dt_80m': 'GEFS surface minus 80 m temperature (instability)',
    'gfs_dt_80m': 'GFS surface minus 80 m temperature (instability)',
    'wn2_speed_10m_p50': 'WeatherNext 2 10 m wind, member median',
    'wn2_speed_10m_mean': 'WeatherNext 2 10 m wind, member mean',
    'wn2_speed_10m_p90': 'WeatherNext 2 10 m wind, member 90th pct',
    'wn2_speed_10m_p10': 'WeatherNext 2 10 m wind, member 10th pct',
    'gefs_speed_10m_p50': 'GEFS 10 m wind, member median',
    'gefs_speed_10m_mean': 'GEFS 10 m wind, member mean',
    'gefs_speed_10m_p90': 'GEFS 10 m wind, member 90th pct',
    'gefs_downward_short_wave_radiation_flux_surface_p90': 'GEFS sunshine (solar flux), 90th pct',
    'gefs_downward_short_wave_radiation_flux_surface_mean': 'GEFS sunshine (solar flux), mean',
    'gfs_downward_short_wave_radiation_flux_surface': 'GFS sunshine (solar flux)',
    'doy_sin': 'Season (high = spring, low = autumn)',
    'hour_cos': 'Hour of day (high = near midnight, low = midday)',
    'lead_h': 'Hours from the update to the valid hour',
    'slot': 'Update time (UTC hour)',
    'hres_wind_speed_10m': 'ECMWF HRES 10 m wind',
    'gefs_downward_short_wave_radiation_flux_surface_p50': 'GEFS sunshine (solar flux), median',
    'gfs_10m_spd': 'GFS 10 m wind',
    'wn2_10m_spd': 'WeatherNext 2 10 m wind (derived)',
    'ifs_ens_wind_gust_10m_mean': 'ECMWF ensemble 10 m gust, mean',
    'wn2_wind_u_10m_mean': 'WeatherNext 2 west-east wind',
    'ifs_ens_dt_925': 'ECMWF ensemble surface minus 925 hPa temperature',
    'aifs_dt_925': 'AIFS surface minus 925 hPa temperature (instability)',
}
UNITS = {'dt': '°C', 'speed': 'kt', 'spd': 'kt', 'short_wave': 'W/m²', 'gust': 'kt'}


MODEL_NAMES = {'gfs': 'GFS', 'gefs': 'GEFS', 'ifs_ens': 'ECMWF ensemble', 'aifs_ens': 'AIFS ensemble', 'aifs': 'AIFS', 'hrrr': 'HRRR',
               'wn2': 'WeatherNext 2', 'wn3': 'WeatherNext 3'}


def nice(f):
    if f.endswith('_age_h'):
        model = next((v for k, v in MODEL_NAMES.items() if f.startswith(k + '_')), f)
        return f'{model} run age (hours to the valid hour)'
    return NAMES.get(f, f)


def unit(f):
    return next((u for k, u in UNITS.items() if k in f), '')


def hbars(rows, label, value, width=760, bar_h=22, left=330, fmt='{:.2f} kt'):
    vmax = max(r[value] for r in rows)
    h = len(rows) * (bar_h + 6) + 10
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    for i, r in enumerate(rows):
        y = 5 + i * (bar_h + 6)
        w = max(2, (width - left - 80) * r[value] / vmax)
        tip = html.escape(f'{r[label]}: {fmt.format(r[value])}')
        out.append(f'<g><title>{tip}</title><text x="{left - 10}" y="{y + bar_h * 0.7}" text-anchor="end" class="lab">{html.escape(r[label])}</text>'
                   f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" fill="{BLUE}"/>'
                   f'<text x="{left + w + 6}" y="{y + bar_h * 0.7}" class="val">{fmt.format(r[value])}</text>'
                   f'<rect x="0" y="{y - 3}" width="{width}" height="{bar_h + 6}" fill="transparent"/></g>')
    out.append('</svg>')
    return ''.join(out)


def line_chart(series, xlab, ylab, width=360, height=220, band=None, zero=True, xfmt='{:.1f}'):
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 38
    xs = [p[0] for s in series for p in s['pts']]
    ys = [p[1] for s in series for p in s['pts']] + ([v for p in band for v in p[1:]] if band else []) + ([0] if zero else [])
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    y0, y1 = y0 - 0.08 * (y1 - y0 + 1e-9), y1 + 0.08 * (y1 - y0 + 1e-9)
    X = lambda v: pad_l + (width - pad_l - pad_r) * (v - x0) / (x1 - x0 + 1e-9)
    Y = lambda v: pad_t + (height - pad_t - pad_b) * (1 - (v - y0) / (y1 - y0))
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for k in range(5):
        v = y0 + (y1 - y0) * k / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{GRID}"/>'
                   f'<text x="{pad_l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="tick">{v:+.{2 if y1 - y0 < 1 else 1}f}</text>' if zero else
                   f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{GRID}"/>'
                   f'<text x="{pad_l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="tick">{v:.0f}</text>')
    for k in range(5):
        v = x0 + (x1 - x0) * k / 4
        out.append(f'<text x="{X(v):.1f}" y="{height - pad_b + 16}" text-anchor="middle" class="tick">{xfmt.format(v)}</text>')
    if zero:
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="{INK2}" stroke-width="1"/>')
    if band:
        up = ' '.join(f'{X(p[0]):.1f},{Y(p[2]):.1f}' for p in band)
        dn = ' '.join(f'{X(p[0]):.1f},{Y(p[1]):.1f}' for p in reversed(band))
        out.append(f'<polygon points="{up} {dn}" fill="{BLUE}" opacity="0.15"/>')
    for s in series:
        pts = ' '.join(f'{X(x):.1f},{Y(y):.1f}' for x, y in s['pts'])
        out.append(f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" stroke-width="2"/>')
        for x, y in s['pts']:
            out.append(f'<g><title>{html.escape(s.get("name", ""))} {xfmt.format(x)}: {y:+.2f}</title>'
                       f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="4" fill="{s["color"]}" stroke="#fcfcfb" stroke-width="2"/>'
                       f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="10" fill="transparent"/></g>')
        if s.get('label'):
            x, y = s['pts'][-1]
            out.append(f'<text x="{X(x) - 4:.1f}" y="{Y(y) - 9:.1f}" text-anchor="end" class="val">{html.escape(s["label"])}</text>')
    out.append(f'<text x="{(pad_l + width - pad_r) / 2}" y="{height - 4}" text-anchor="middle" class="axis">{html.escape(xlab)}</text>'
               f'<text x="12" y="{(pad_t + height - pad_b) / 2}" text-anchor="middle" class="axis" transform="rotate(-90 12 {(pad_t + height - pad_b) / 2})">{html.escape(ylab)}</text></svg>')
    return ''.join(out)


def tree_svg(nodes, depth=3, categories=None):
    by = {n['ID']: n for n in nodes}
    width, level_h, box_w, box_h = 1100, 120, 132, 76
    out = [f'<svg viewBox="0 0 {width} {level_h * (depth + 1) + 10}" width="100%" role="img">']

    def draw(nid, d, lo, hi, parent=None, edge=''):
        n = by[nid]
        cx, cy = (lo + hi) / 2, 10 + d * level_h
        if parent:
            out.append(f'<line x1="{parent[0]}" y1="{parent[1] + box_h}" x2="{cx}" y2="{cy}" stroke="{INK2}" stroke-width="1.5"/>'
                       f'<text x="{(parent[0] + cx) / 2 + (6 if cx > parent[0] else -6)}" y="{(parent[1] + box_h + cy) / 2}" '
                       f'text-anchor="{"start" if cx > parent[0] else "end"}" class="tick">{edge}</text>')
        leaf = n['Feature'] == 'Leaf' or d == depth
        name = nice(n['Feature']) if not leaf else ''
        short = (name[:54] + '…') if len(name) > 55 else name
        tip = html.escape(f"{n['Feature']} {'< ' + format(n['Split'], '.2f') if n.get('Split') is not None else 'categorical'}, {n['Cover']:.0f} hours" if n['Feature'] != 'Leaf' else f"leaf, {n['Cover']:.0f} hours")
        fill = '#eef4fc' if not leaf else '#f0efec'
        out.append(f'<g><title>{tip}</title><rect x="{cx - box_w / 2}" y="{cy}" width="{box_w}" height="{box_h}" rx="6" fill="{fill}" stroke="{GRID}"/>')
        if n['Feature'] == 'Leaf':
            out.append(f'<text x="{cx}" y="{cy + 36}" text-anchor="middle" class="tick">leaf · {n["Cover"]:.0f} h</text></g>')
            return
        if d == depth:
            out.append(f'<text x="{cx}" y="{cy + 28}" text-anchor="middle" class="tick">… deeper splits</text>'
                       f'<text x="{cx}" y="{cy + 46}" text-anchor="middle" class="tick">{n["Cover"]:.0f} hours</text></g>')
            return
        words, lines = short.split(), ['']
        for w in words:
            if len(lines[-1]) + len(w) > 19 and lines[-1]:
                lines.append('')
            lines[-1] = (lines[-1] + ' ' + w).strip()
        for i, ln in enumerate(lines[:3]):
            out.append(f'<text x="{cx}" y="{cy + 15 + i * 13}" text-anchor="middle" class="tree">{html.escape(ln)}</text>')
        if n.get('Split') is None and n.get('Category'):  # categorical split: the listed categories go left
            listed = [categories[int(c)] if categories else str(c) for c in n['Category']]
            rule = 'in ' + ', '.join(listed[:4]) + (f' +{len(listed) - 4}' if len(listed) > 4 else '')
        else:
            rule = f'&lt; {n["Split"]:.2f} {unit(n["Feature"])}'
        out.append(f'<text x="{cx}" y="{cy + 67}" text-anchor="middle" class="treev">{rule} · {n["Cover"]:.0f} h</text></g>')
        mid = (lo + hi) / 2
        draw(n['Yes'], d + 1, lo, mid, (cx, cy), 'yes')
        draw(n['No'], d + 1, mid, hi, (cx, cy), 'no')

    draw('0-0', 0, 0, width)
    out.append('</svg>')
    return ''.join(out)


def mix(a, b, t):
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(x + (y - x) * t):02x}' for x, y in zip(a, b))


def colour(rank):
    return mix(BLUE, '#bdbcb6', rank * 2) if rank < 0.5 else mix('#bdbcb6', RED, rank * 2 - 1)


def beeswarm(rows, width=1080, row_h=46, left=330):
    lo = min(min(r['shap']) for r in rows); hi = max(max(r['shap']) for r in rows)
    lo, hi = min(lo, -0.5), max(hi, 0.5)
    top, bottom = 10, 44
    height = top + row_h * len(rows) + bottom
    X = lambda v: left + (width - left - 20) * (v - lo) / (hi - lo)
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    step = 0.5 if hi - lo < 8 else 1
    v = step * int(lo / step)
    while v <= hi:
        out.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{top}" y2="{height - bottom}" stroke="{GRID}"/>'
                   f'<text x="{X(v):.1f}" y="{height - bottom + 16}" text-anchor="middle" class="tick">{v:+.1f}</text>')
        v += step
    out.append(f'<line x1="{X(0):.1f}" x2="{X(0):.1f}" y1="{top}" y2="{height - bottom}" stroke="{INK2}"/>')
    for k, r in enumerate(rows):
        cy = top + row_h * (k + 0.5)
        out.append(f'<g><title>{html.escape(r["feature"])}: {len(r["shap"])} hours</title><text x="{left - 12}" y="{cy + 4}" text-anchor="end" class="lab">'
                   f'{html.escape(nice(r["feature"]))}</text><rect x="0" y="{cy - row_h / 2}" width="{left}" height="{row_h}" fill="transparent"/></g>')
        counts = {}
        pts = sorted(zip(r['shap'], r['rank']), key=lambda p: (p[1] is None, p[1] or 0))  # missing first, so filled dots draw on top
        for sv, rk in pts:
            b = round(X(sv) / 3.2)
            n = counts.get(b, 0); counts[b] = n + 1
            dy = ((n + 1) // 2) * 2.4 * (1 if n % 2 else -1)
            if abs(dy) > row_h / 2 - 3:  # bin full: scatter the overflow across the row rather than piling it on the edge
                dy = (((n * 7919) % 97) / 96 - 0.5) * (row_h - 6)
            if rk is None:
                out.append(f'<circle cx="{X(sv):.1f}" cy="{cy + dy:.1f}" r="1.7" fill="none" stroke="#a3a29c" stroke-width="0.6"/>')
            else:
                out.append(f'<circle cx="{X(sv):.1f}" cy="{cy + dy:.1f}" r="1.9" fill="{colour(rk)}"/>')
    out.append(f'<text x="{(left + width) / 2}" y="{height - 6}" text-anchor="middle" class="axis">push on the gust forecast (kt): left = lower gust, right = higher</text></svg>')
    return ''.join(out)


def main():
    D = json.load(open('var/mos/explain.json'))
    swarm = beeswarm(D['beeswarm'])
    fam = hbars(D['families'], 'family', 'mean_abs_kt')
    top = hbars([{'f': nice(r['feature']), 'v': r['mean_abs_kt']} for r in D['top_features'][:12]], 'f', 'v')
    deps = []
    for name, pts in D['dependence'].items():
        if name == 'gefs_speed_10m_mean':
            continue
        chart = line_chart([{'pts': [(p['x'], p['shap']) for p in pts], 'color': BLUE, 'name': nice(name)}],
                           f'{nice(name)} ({unit(name)})', 'effect on gust (kt)', band=[(p['x'], p['p10'], p['p90']) for p in pts],
                           xfmt='{:.0f}' if 'short_wave' in name else '{:.1f}')
        deps.append(f'<figure><figcaption>{html.escape(nice(name))}</figcaption>{chart}</figure>')
    hours = line_chart([{'pts': [(r['hour'], r['observed_kt']) for r in D['by_hour']], 'color': ORANGE, 'name': 'observed'},
                        {'pts': [(r['hour'], r['prediction_kt']) for r in D['by_hour']], 'color': BLUE, 'name': 'model median', 'label': ''}],
                       'hour (local)', 'peak gust (kt)', width=560, height=240, zero=False, xfmt='{:.0f}')
    tree = tree_svg(D['tree0'])
    table = ''.join(f'<tr><td>{html.escape(r["feature"])}</td><td>{html.escape(r["family"])}</td><td>{r["mean_abs_kt"]:.3f}</td></tr>' for r in D['top_features'])

    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>What the KCDW gust model learned</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;color:{INK};background:#fcfcfb;max-width:1120px;margin:24px auto;padding:0 20px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:34px 0 4px}} p{{color:{INK2};max-width:820px;margin:4px 0 10px}}
.lab{{font-size:13px;fill:{INK}}} .val{{font-size:12px;fill:{INK2}}} .tick{{font-size:11px;fill:{INK2}}} .axis{{font-size:12px;fill:{INK2}}}
.tree{{font-size:11.5px;fill:{INK};font-weight:600}} .treev{{font-size:11px;fill:{INK2}}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} figure{{margin:0}} figcaption{{font-size:13px;font-weight:600;margin-bottom:2px}}
.sw2{{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:middle;margin:0 3px 0 8px}} .legend{{font-size:13px;color:{INK2}}} .sw{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin:0 6px 0 12px}}
details{{margin-top:30px}} table{{border-collapse:collapse;font-size:13px}} td{{padding:2px 10px;border-bottom:1px solid {GRID}}}
</style></head><body>
<h1>What the KCDW gust model learned</h1>
<p><a href="../mos-multi-explainer/">The 22-station model's explainer</a></p>
<p>Median (p50) model of the true 1-hour peak gust: {D["trees"]} trees, {D["features"]} inputs, {D["rows"]:,} training rows. Each row is one
production update (04, 11, 16 or 22Z) and one later hour up to 7 days ahead, built from every model's newest run published by that update,
so each model's run age is an input too. Open-Meteo's ICON, HRES and GEM are excluded: the leakage audit found their archive used newer runs
than a live forecast could.
Attributions are exact TreeSHAP on {D["sample"]:,} random hours. Every prediction starts at the average, {D["base_kt"]} kt, and each input pushes it up or down.</p>
<h2>Which models it listens to</h2>
<p>Average absolute push on the gust forecast, summed over each model's inputs (a model's run age counts with it). GEFS, WeatherNext 2 and GFS cover the whole 2021–2026 record, so the trees lean on them;
ECMWF ensemble (2024 onward) and the short-archive models get less credit partly because they are missing in older hours.</p>
{fam}
<h2>Every hour, every input (beeswarm)</h2>
<p>One dot per forecast hour ({len(D["beeswarm"][0]["shap"]):,} random hours), for the 15 inputs with the most influence. Position: how far that input pushed the gust forecast.
Colour: the input's own value, as a percentile. <span class="sw2" style="background:{BLUE}"></span>low <span class="sw2" style="background:#bdbcb6"></span>middle
<span class="sw2" style="background:{RED}"></span>high <span class="sw2" style="background:none;border:1px solid #a3a29c"></span>model not available that hour.</p>
{swarm}
<h2>The inputs that matter most</h2>
<p>Low-level instability from GEFS is the single largest input, ahead of any wind speed. GFS's own 10 m wind comes next: at the noon,
evening and midnight updates it comes from a run only 6–12 hours old, fresher than the ensembles, and the model leans on that freshness.
WeatherNext 2's ensemble median and mean follow.</p>
{top}
<h2>How each input moves the forecast</h2>
<p>Line: average effect on the gust forecast at that input value. Band: 10th–90th percentile of the effect across hours (it depends on the other inputs).</p>
<div class="grid">{"".join(deps)}</div>
<h2>The day's shape</h2>
<p class="legend">Mean over the sample, by local hour<span class="sw" style="background:{ORANGE}"></span>observed<span class="sw" style="background:{BLUE}"></span>model median</p>
<div style="max-width:600px">{hours}</div>
<p>The median sits ~0.4 kt under the observed mean at every hour, as it should: gusts are right-skewed, so the median is below the mean.</p>
<h2>The first tree</h2>
<p>Tree 1 of {D["trees"]}, top three levels (hover for exact thresholds). Wind speeds are in knots, sunshine in W/m². Each later tree fixes what the earlier ones got wrong, in 0.05-sized steps.</p>
{tree}
<details><summary>Table: top 25 inputs</summary><table><tr><td>input</td><td>model</td><td>mean |SHAP| kt</td></tr>{table}</table></details>
</body></html>'''
    open('research/mos/explain.html', 'w').write(page)
    print('wrote research/mos/explain.html')


if __name__ == '__main__':
    main()
