"""Render var/mos-multi/explain_multi.json as research/mos/explain_multi.html: what the 22-station gust model learned (research)."""
import html
import json

import explain_html as eh
from explain_html import BLUE, GRID, INK, INK2, ORANGE, RED, beeswarm, hbars, line_chart, tree_svg

AQUA = '#1baf7a'
MODEL = {'gfs': 'GFS', 'gefs': 'GEFS', 'ifs': 'ECMWF ENS', 'ifshres': 'ECMWF HRES', 'aifs': 'AIFS', 'hrrr': 'HRRR', 'wn2': 'WeatherNext 2',
         'wn3': 'WeatherNext 3', 'ukmo': 'UKMO'}
SITE = {'station': 'Station identity', 'stn_sea_km': 'Distance to open water (km)', 'stn_elev_minus_area': 'Elevation vs surrounding 25 km (m)',
        'stn_near_tree_all': 'Tree cover within 1 km', 'stn_far_tree_all': 'Tree cover 1–3 km', 'stn_near_built_all': 'Built-up within 1 km',
        'stn_far_water_all': 'Water 1–3 km', 'stn_lat': 'Latitude', 'stn_lon': 'Longitude', 'stn_elev': 'Elevation (m)', 'stn_code': 'Station code',
        'hist_gust': "Station's typical gust for this wind direction", 'hist_sust': "Station's typical wind for this direction",
        'hist_spread': "Station's typical gust spread for this direction", 'consensus_dir_sin': 'Forecast wind direction (east–west)',
        'consensus_dir_cos': 'Forecast wind direction (north–south)'}
for ring, label in (('near', '0.2–1 km'), ('far', '1–3 km')):
    for cover in ('tree', 'built', 'open', 'water'):
        SITE[f'upwind_{ring}_{cover}'] = f'Upwind {cover} cover, {label}'
SITE['upwind_relief_mean_m'] = 'Upwind terrain height, mean (m)'
SITE['upwind_relief_max_m'] = 'Upwind terrain height, max (m)'


def pretty(name):
    if name in SITE or name in eh.NAMES:
        return SITE.get(name) or eh.NAMES[name]
    head, _, rest = name.partition('_')
    if head == 'ifs' and rest.startswith('ens_'):
        head, rest = 'ifs', rest[4:]
    words = rest.replace('downward_short_wave_radiation_flux_surface', 'sunshine').replace('wind_gust_surface', 'gust').replace('wind_gust_10m', 'gust')
    words = words.replace('speed_10m', '10 m wind').replace('10m_spd', '10 m wind').replace('100m_spd', '100 m wind').replace('dt_80m', 'surface minus 80 m temp')
    words = words.replace('dt_925', 'surface minus 925 hPa temp').replace('_', ' ')
    return f'{MODEL.get(head, head)} {words}'.strip()


def mix(a, b, t):
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(x + (y - x) * t):02x}' for x, y in zip(a, b))


def diverging(v, span):
    t = max(-1.0, min(1.0, v / span))
    return mix('#f0efec', RED, t) if t >= 0 else mix('#f0efec', BLUE, -t)


def site_bars(rows, width=760, bar_h=18, left=90):
    span = max(abs(r['site_push_kt']) for r in rows)
    mid = left + (width - left - 60) / 2
    scale = (width - left - 60) / 2 / span
    h = len(rows) * (bar_h + 5) + 34
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">',
           f'<line x1="{mid}" x2="{mid}" y1="0" y2="{h - 26}" stroke="{INK2}"/>']
    for i, r in enumerate(rows):
        y = 4 + i * (bar_h + 5)
        v = r['site_push_kt']
        x0, w = (mid, v * scale) if v >= 0 else (mid + v * scale, -v * scale)
        colour = RED if v >= 0 else BLUE
        out.append(f'<g><title>{r["station"]}: site inputs push the gust {v:+.2f} kt on average</title>'
                   f'<text x="{left - 10}" y="{y + bar_h * 0.75}" text-anchor="end" class="lab">{"K" + r["station"]}</text>'
                   f'<rect x="{x0:.1f}" y="{y}" width="{max(w, 1.5):.1f}" height="{bar_h}" rx="3" fill="{colour}"/>'
                   f'<text x="{(x0 + w + 6) if v >= 0 else (x0 - 6):.1f}" y="{y + bar_h * 0.75}" text-anchor="{"start" if v >= 0 else "end"}" class="val">{v:+.1f}</text>'
                   f'<rect x="0" y="{y - 2}" width="{width}" height="{bar_h + 4}" fill="transparent"/></g>')
    out.append(f'<text x="{mid - 8}" y="{h - 8}" text-anchor="end" class="axis">← site lowers the gust</text>'
               f'<text x="{mid + 8}" y="{h - 8}" class="axis">site raises the gust →</text></svg>')
    return ''.join(out)


def sector_heatmap(rows, sectors=('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')):
    span = max(abs(v) for r in rows for v in r['by_sector'].values())
    head = ''.join(f'<th scope="col">{s}</th>' for s in sectors)
    body = []
    for r in rows:
        cells = []
        for s in sectors:
            v = r['by_sector'].get(s)
            if v is None:
                cells.append('<td class="na" title="fewer than 40 hours">·</td>')
            else:
                ink = '#ffffff' if abs(v) / span > 0.6 else INK
                cells.append(f'<td style="background:{diverging(v, span)};color:{ink}" title="K{r["station"]}, wind from {s}: {v:+.2f} kt">{v:+.1f}</td>')
        body.append(f'<tr><th scope="row">K{r["station"]}</th>{"".join(cells)}</tr>')
    return f'<table class="heat"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def skill_dots(rows, width=760, row_h=22, left=90):
    vals = [v for r in rows for v in (r['calibrated_on_ecmwf_ens_hours'], r['ecmwf_ens'], r['gefs'])]
    lo, hi = min(vals) - 0.2, max(vals) + 0.2
    X = lambda v: left + (width - left - 30) * (v - lo) / (hi - lo)
    h = len(rows) * row_h + 40
    out = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    v = int(lo)
    while v <= hi:
        out.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="0" y2="{h - 34}" stroke="{GRID}"/>'
                   f'<text x="{X(v):.1f}" y="{h - 20}" text-anchor="middle" class="tick">{v} kt</text>')
        v += 1
    for i, r in enumerate(rows):
        cy = 10 + i * row_h
        pts = [('calibrated', r['calibrated_on_ecmwf_ens_hours'], BLUE), ('ECMWF ENS mean', r['ecmwf_ens'], ORANGE), ('GEFS mean', r['gefs'], AQUA)]
        xs = [X(p[1]) for p in pts]
        out.append(f'<text x="{left - 10}" y="{cy + 4}" text-anchor="end" class="lab">K{r["station"]}</text>'
                   f'<line x1="{min(xs):.1f}" x2="{max(xs):.1f}" y1="{cy}" y2="{cy}" stroke="{GRID}" stroke-width="2"/>')
        for name, val, colour in pts:
            out.append(f'<g><title>K{r["station"]} {name}: {val:.2f} kt MAE</title><circle cx="{X(val):.1f}" cy="{cy}" r="5" fill="{colour}" stroke="#fcfcfb" stroke-width="2"/>'
                       f'<circle cx="{X(val):.1f}" cy="{cy}" r="10" fill="transparent"/></g>')
    out.append(f'<text x="{(left + width) / 2}" y="{h - 4}" text-anchor="middle" class="axis">1-hour peak gust, mean absolute error (kt), held-out months</text></svg>')
    return ''.join(out)


def main():
    D = json.load(open('var/mos-multi/explain_multi.json'))
    for f in {r['feature'] for r in D['top_features']} | set(D['dependence']) | {r['feature'] for r in D['beeswarm']} | {n['Feature'] for n in D['tree0']}:
        eh.NAMES.setdefault(f, pretty(f))
    fam = hbars(D['families'], 'family', 'mean_abs_kt')
    top = hbars([{'f': pretty(r['feature']), 'v': r['mean_abs_kt']} for r in D['top_features'][:15]], 'f', 'v')
    swarm = beeswarm(D['beeswarm'])
    push = sorted(D['per_station'], key=lambda r: r['site_push_kt'])
    bars = site_bars(push)
    heat = sector_heatmap(push)
    skill = sorted(D['skill'], key=lambda r: r['calibrated_on_ecmwf_ens_hours'])
    dots = skill_dots(skill)
    cal = sum(r['calibrated_on_ecmwf_ens_hours'] * r['hours'] for r in skill) / sum(r['hours'] for r in skill)
    ens = sum(r['ecmwf_ens'] * r['hours'] for r in skill) / sum(r['hours'] for r in skill)
    deps = []
    for name, pts in D['dependence'].items():
        chart = line_chart([{'pts': [(p['x'], p['shap']) for p in pts], 'color': BLUE, 'name': pretty(name)}], pretty(name), 'effect on gust (kt)',
                           band=[(p['x'], p['p10'], p['p90']) for p in pts], xfmt='{:.0f}' if 'short_wave' in name or 'sea_km' in name else '{:.1f}')
        deps.append(f'<figure><figcaption>{html.escape(pretty(name))}</figcaption>{chart}</figure>')
    tree = tree_svg(D['tree0'], categories=['K' + s for s in D['stations']])
    table = ''.join(f'<tr><td>{html.escape(r["feature"])}</td><td>{html.escape(r["family"])}</td><td>{r["mean_abs_kt"]:.3f}</td></tr>' for r in D['top_features'])
    cdw = next(r for r in D['per_station'] if r['station'] == 'CDW')
    by = {r['station']: r['by_sector'] for r in D['per_station']}
    ewr_nw, lga_nw, jfk_se = by['EWR']['NW'], by['LGA']['NW'], by['JFK']['SE']
    page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>What the 22-station gust model learned</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;color:{INK};background:#fcfcfb;max-width:1120px;margin:24px auto;padding:0 20px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:34px 0 4px}} p{{color:{INK2};max-width:820px;margin:4px 0 10px}}
.lab{{font-size:13px;fill:{INK}}} .val{{font-size:12px;fill:{INK2}}} .tick{{font-size:11px;fill:{INK2}}} .axis{{font-size:12px;fill:{INK2}}}
.tree{{font-size:11.5px;fill:{INK};font-weight:600}} .treev{{font-size:11px;fill:{INK2}}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} figure{{margin:0}} figcaption{{font-size:13px;font-weight:600;margin-bottom:2px}}
.legend{{font-size:13px;color:{INK2}}} .sw2{{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:middle;margin:0 3px 0 8px}}
table.heat{{border-collapse:separate;border-spacing:2px;font-size:12px}} .heat th{{font-weight:600;color:{INK2};padding:2px 6px;text-align:right}}
.heat td{{width:54px;text-align:center;padding:4px 0;border-radius:3px}} .heat td.na{{color:{INK2}}}
details{{margin-top:30px}} table.t{{border-collapse:collapse;font-size:13px}} .t td{{padding:2px 10px;border-bottom:1px solid {GRID}}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>What the 22-station gust model learned</h1>
<p><a href="../mos-explainer/">The KCDW-only model's explainer</a></p>
<p>One model calibrates the true 1-hour peak gust at 22 airports around New York. It is told which airport it is forecasting and what surrounds it
(tree, building, open and water cover upwind, terrain, distance to open water) plus each station's typical gust by wind direction.
This page explains its median (p50) with the fast production settings (depth 9, {D["rounds"]} rounds chosen by early stopping), trained on
{D["train_hours"]:,} station-hours and checked on {D["test_hours"]:,} held-out hours from other months. Open-Meteo inputs are excluded.
Attributions are exact TreeSHAP on {D["sample"]:,} held-out hours, balanced across stations. Every forecast starts at {D["base_kt"]} kt.</p>

<h2>Held-out accuracy at every airport</h2>
<p class="legend">Gust error on months the model never saw, on hours where ECMWF ENS exists<span class="sw2" style="background:{BLUE}"></span>calibrated
<span class="sw2" style="background:{ORANGE}"></span>ECMWF ENS mean <span class="sw2" style="background:{AQUA}"></span>GEFS mean</p>
<p>Across all airports: {cal:.2f} kt versus {ens:.2f} kt for the raw ECMWF ensemble mean ({100 * (ens - cal) / ens:.0f}% lower).</p>
{dots}

<h2>How much each airport's site moves the forecast</h2>
<p>The summed push from the site inputs (identity, exposure, history) on the gust forecast, averaged over each airport's held-out hours.
The same weather forecast becomes a lower gust at sheltered, tree-lined or inland fields and a higher one at open, waterfront fields.
KCDW's site takes about {abs(cdw["site_push_kt"]):.1f} kt off.</p>
{bars}

<h2>Exposure by wind direction</h2>
<p>The same site push, split by the forecast wind direction (the direction the wind comes from). Red: the site raises the gust; blue: it lowers it.
The airport matters more than the direction: almost every row keeps one sign all the way round. Within a row, direction mostly tracks the weather
that comes with it rather than the view: the open fields get their biggest boost in northwest winds (KEWR {ewr_nw:+.1f}, KLGA {lga_nw:+.1f} kt), the dry
post-frontal flow that mixes gusts down, and KJFK its smallest in southeast winds off the ocean ({jfk_se:+.1f} kt), which usually come with a stable
marine layer. KCDW is sheltered from every direction, most with north and southwest winds.</p>
{heat}

<h2>Which inputs it listens to</h2>
<p>Average absolute push on the gust forecast by input family. The site families together are the model's way of calibrating each airport differently.
Station identity carries most of that: each exposure input on its own moves the gust by only about 0.1–0.2 kt (see the curves below). With 22 airports,
an airport's surroundings never change, so the model cannot tell "tree-lined" apart from "this airport" and mostly learns each airport's offset directly;
the exposure inputs would matter more for an airport it has never seen.</p>
{fam}
<h2>Every hour, every input (beeswarm)</h2>
<p>One dot per held-out station-hour ({len(D["beeswarm"][0]["shap"]):,} hours), for the 15 most influential inputs. Position: how far that input pushed the gust.
Colour: the input's own value as a percentile <span class="sw2" style="background:{BLUE}"></span>low <span class="sw2" style="background:#bdbcb6"></span>middle
<span class="sw2" style="background:{RED}"></span>high <span class="sw2" style="background:none;border:1px solid #a3a29c"></span>not available that hour (for station identity, colour is the station's alphabetical order and carries no meaning).</p>
{swarm}
<h2>The inputs that matter most</h2>
{top}
<h2>How each input moves the forecast</h2>
<p>Line: average effect on the gust forecast at that input value; band: 10th–90th percentile across hours. Winds are in knots.</p>
<div class="grid">{"".join(deps)}</div>
<h2>The first tree</h2>
<p>Tree 1 of {D["rounds"]}, top three levels (hover for exact thresholds). A station split sends the listed airports one way.</p>
{tree}
<details><summary>Table: top 25 inputs</summary><table class="t"><tr><td>input</td><td>family</td><td>mean |SHAP| kt</td></tr>{table}</table></details>
</body></html>'''
    open('research/mos/explain_multi.html', 'w').write(page)
    print('wrote research/mos/explain_multi.html')


if __name__ == '__main__':
    main()
