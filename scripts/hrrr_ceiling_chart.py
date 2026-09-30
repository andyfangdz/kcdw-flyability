"""Render HRRR cloud-ceiling panels within 100 nm of KCDW (charts virtualenv).

Input (stdin JSON): {"data": ".../grid.npz", "out": ".../map.png", "title": str,
"subtitle": str, "frames": [{"lead": int, "label": str}, ...]}.
Stdout: {"file", "sha256", "width", "height"} for the written PNG.
Natural Earth 10 m layers download into var/charts/cartopy-data on first use.
"""
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAT, LON = 40.8752, -74.2814
NM_KM = 1.852
BINS = [0, 1000, 3000, 7000, 1e9]
COLORS = ['#104281', '#256abf', '#6da7ec', '#e4eefb']
LABELS = ['Below 1,000 ft', '1,000–3,000 ft', '3,000–7,000 ft', '7,000 ft or higher']
INK, MUTED, SURFACE, LAND, WATER = '#1f1f1d', '#5f5e5a', '#ffffff', '#f4f3f0', '#dfe6ee'
AIRPORTS = {'KCDW': (40.8752, -74.2814), 'KTEB': (40.8501, -74.0608),
            'KEWR': (40.6925, -74.1687), 'KJFK': (40.6413, -73.7781),
            'KHPN': (41.0670, -73.7076), 'KSWF': (41.5041, -74.1048), 'KPOU': (41.6266, -73.8842),
            'KABE': (40.6521, -75.4408), 'KTTN': (40.2767, -74.8135),
            'KISP': (40.7952, -73.1002), 'KBDR': (41.1635, -73.1262), 'KHVN': (41.2637, -72.8868),
            'KAVP': (41.3385, -75.7234), 'KACY': (39.4576, -74.5772), 'KMPO': (41.1374, -75.3789),
            'KDXR': (41.3715, -73.4822), 'KBLM': (40.1869, -74.1249), 'KFWN': (41.2003, -74.6231)}


def circle(km):
    t = [i * 2 * math.pi / 240 for i in range(241)]
    return ([LON + km / (111.32 * math.cos(math.radians(LAT))) * math.cos(a) for a in t],
            [LAT + km / 110.57 * math.sin(a) for a in t])


def main():
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch
    import cartopy
    import cartopy.crs as ccrs
    import cartopy.feature as cf

    request = json.loads(sys.stdin.read(65536))
    assert set(request) == {'data', 'out', 'title', 'subtitle', 'frames'}
    data, out = Path(request['data']).resolve(), Path(request['out']).resolve()
    events = (REPO / 'var/events').resolve()
    assert events in data.parents and events in out.parents and out.suffix == '.png'
    frames = request['frames']
    assert 0 < len(frames) <= 10
    cartopy.config['data_dir'] = str(REPO / 'var/charts/cartopy-data')
    grid = np.load(data)
    lat, lon = grid['lat'], grid['lon']
    cmap, norm = ListedColormap(COLORS), BoundaryNorm(BINS, len(COLORS))
    radius = 100 * NM_KM
    dlat, dlon = radius / 110.57 + .05, radius / (111.32 * math.cos(math.radians(LAT))) + .05
    extent = [LON - dlon, LON + dlon, LAT - dlat, LAT + dlat]
    proj = ccrs.LambertConformal(central_longitude=LON, central_latitude=LAT)
    layers = [(cf.NaturalEarthFeature('physical', 'land', '10m'), dict(facecolor=LAND, edgecolor='none', zorder=0)),
              (cf.NaturalEarthFeature('physical', 'lakes', '10m'), dict(facecolor=WATER, edgecolor='none', zorder=0)),
              (cf.NaturalEarthFeature('cultural', 'roads', '10m'), dict(facecolor='none', edgecolor='#8f8d87', linewidth=.35, zorder=2)),
              (cf.NaturalEarthFeature('physical', 'coastline', '10m'), dict(facecolor='none', edgecolor='#5d5c58', linewidth=.6, zorder=3)),
              (cf.NaturalEarthFeature('cultural', 'admin_1_states_provinces_lines', '10m'),
               dict(facecolor='none', edgecolor='#5d5c58', linewidth=.55, linestyle=(0, (4, 3)), zorder=3))]
    # Read each layer once, clipped to the map, rather than once per panel.
    from shapely.geometry import box
    clip = box(extent[0] - .3, extent[2] - .3, extent[1] + .3, extent[3] + .3)
    shapes = [([g.intersection(clip) for g in feature.geometries() if g.intersects(clip)], style) for feature, style in layers]

    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'text.color': INK})
    cols = 3
    rows = math.ceil((len(frames) + 1) / cols)
    fig = plt.figure(figsize=(15, 5.1 * rows + .6), facecolor=SURFACE)
    for i, frame in enumerate(frames):
        ax = fig.add_subplot(rows, cols, i + 1, projection=proj)
        ax.set_extent(extent, crs=ccrs.PlateCarree())
        ax.set_facecolor(WATER)
        for geometries, style in shapes:
            ax.add_geometries(geometries, ccrs.PlateCarree(), **style)
        agl = grid[f"agl_{frame['lead']}"]
        ax.pcolormesh(lon, lat, np.ma.masked_invalid(agl), cmap=cmap, norm=norm, shading='nearest',
                      transform=ccrs.PlateCarree(), alpha=.88, zorder=1,
                      edgecolors='none', linewidth=0, antialiased=False, rasterized=True)
        for km, style in ((10, dict(color=INK, linewidth=1.1, linestyle=(0, (3, 2)))),
                          (50 * NM_KM, dict(color=MUTED, linewidth=.8, linestyle=(0, (1, 2)))),
                          (radius, dict(color=MUTED, linewidth=.8, linestyle=(0, (1, 2))))):
            xs, ys = circle(km)
            ax.plot(xs, ys, transform=ccrs.PlateCarree(), zorder=4, **style)
        for name, (la, lo) in AIRPORTS.items():
            main = name == 'KCDW'
            ax.plot(lo, la, marker='o', markersize=7 if main else 3.5, markerfacecolor=INK if main else SURFACE,
                    markeredgecolor=INK, markeredgewidth=1, transform=ccrs.PlateCarree(), zorder=5)
            ax.text(lo - .05 if main else lo + .03, la + .05 if main else la + .03, name, ha='right' if main else 'left',
                    transform=ccrs.PlateCarree(), fontsize=9 if main else 6.5,
                    fontweight='bold' if main else 'normal', color=INK, zorder=6, clip_on=True,
                    bbox=dict(boxstyle='round,pad=0.12', facecolor=SURFACE, edgecolor='none', alpha=.7))
        ax.set_title(frame['label'], loc='left', fontsize=12, fontweight='bold', color=INK, pad=6)
        for spine in ax.spines.values():
            spine.set_edgecolor('#c9c7c1')
    legend = fig.add_subplot(rows, cols, len(frames) + 1)
    legend.axis('off')
    handles = [Patch(facecolor=c, edgecolor='#9a9893' if c == COLORS[-1] else c, label=l) for c, l in zip(COLORS, LABELS)]
    handles.append(Patch(facecolor=LAND, edgecolor='#9a9893', label='No ceiling'))
    legend.legend(handles=handles, title='HRRR cloud ceiling, ft above model ground', loc='upper left', frameon=False,
                  fontsize=10, title_fontsize=10.5, alignment='left', handlelength=1.6, handleheight=1.2)
    legend.text(0, .45, 'Rings: 10 km (dashed), 50 nm and 100 nm (dotted)\naround KCDW.\n\n' + request['subtitle'],
                transform=legend.transAxes, fontsize=9.5, color=MUTED, va='top', linespacing=1.45, wrap=True)
    fig.suptitle(request['title'], x=.02, ha='left', fontsize=15, fontweight='bold', color=INK)
    fig.subplots_adjust(left=.02, right=.98, top=1 - .7 / (5.1 * rows + .6), bottom=.02, wspace=.06, hspace=.14)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    png = out.read_bytes()
    width, height = struct.unpack('>II', png[16:24])
    print(json.dumps({'file': out.name, 'sha256': hashlib.sha256(png).hexdigest(), 'width': width, 'height': height}))


if __name__ == '__main__':
    main()
