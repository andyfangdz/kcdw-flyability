# WeatherNext wind and pressure maps rendered by Earth Engine

`scripts/wn3_earth_engine_chart.py` asks Earth Engine to render the map raster:
10 m wind shading, wind barbs, smoothed sea-level pressure contours, Natural
Earth boundaries, and projection to EPSG:5070. Python prepares standard barb
line and flag geometry from verified U/V samples; Earth Engine paints those
geometries into the exported image. Local Matplotlib adds pressure labels,
H/L labels, the KCDW marker, timestamps, and the color legend.

The continental and Northeast views show the same initialization and valid
hour. These are fixed research charts, separate from scheduled reports.

## Render

Install `requirements-earth-engine-charts.txt` in a separate environment and
use the project's existing Google credentials. The credential identity needs
Earth Engine and WeatherNext access. Natural Earth 50m shapefiles are reused
from `var/charts/cartopy-data/shapefiles/natural_earth/`; these are the same
boundaries used by the existing Zarr chart. `--cartopy-dir` overrides that path.

```sh
uv venv var/earth-engine-charts-venv
uv pip install --python var/earth-engine-charts-venv/bin/python \
  -r requirements-earth-engine-charts.txt
MPLCONFIGDIR=var/charts/.matplotlib scripts/with-runtime.sh \
  var/earth-engine-charts-venv/bin/python scripts/wn3_earth_engine_chart.py \
  --run 2026-09-20T12:00:00Z \
  --valid 2026-09-24T15:00:00Z \
  --view both \
  --output-dir var/charts/earth-engine-20260920T12Z-f099-retina
```

The initialization and valid time are explicit; unavailable source images fail.
Each view saves the original `earth-engine-map.png`, a finished
`wn3-earth-engine-wind-pressure.png`, an annotation grid, `barb-geometries.json`,
and `provenance.json`.
Matching cached files are checksum-checked and reused; the CLI still checks
the source image metadata online. Use a new output directory for a new run,
valid time, or changed rendering settings. Renderer version 3 rejects older
caches, including the previous sparse-barb rendering.

The default Earth Engine map width is 4,000 pixels (2× the original). Final
charts use 360 DPI and are 5,760 pixels wide, also 2× the original export.
`--width 2000` produces the same layout at standard resolution; allowed widths
are 800–4,000. The map is rendered at the requested resolution by Earth Engine,
and local labels are exported at matching pixel density. Barb and boundary
strokes and contour weight scale with resolution. Text has no white boxes or
halos; pressure numbers sit just beside their contour lines. Large maps use
aligned Earth Engine row strips (at most 8 million
pixels per request) to stay within the request-size limit. Python joins their
RGB pixels without resampling; `provenance.json` records each request's extent
in rows, elapsed time, and byte count.

Barbs are sampled from a fixed 500-column annotation grid, every 12 cells for
the continental view and every 15 cells for the Northeast. This gives roughly
four times the previous density. Glyphs are also larger (1% of map width).
Changing output resolution preserves their locations, winds, and apparent
size rather than changing the underlying weather sampling.

## Reading and verification

- Wind shading: ensemble-mean scalar speed, m/s converted to knots, 0–60 kt.
- Contours: ensemble-mean sea-level pressure, Pa converted to hPa, every 2 hPa.
  Earth Engine applies a normalized Gaussian filter with sigma 2 native pixels
  (0.2 degrees) and radius 6 pixels, followed by bilinear display interpolation.
- Barbs: ensemble-mean U/V components, rotated into the map projection.
  Their magnitude can be smaller than the mean scalar speed. The staff points
  toward the wind's origin. A half feather denotes 5 kt, a full feather 10 kt,
  and a filled flag 50 kt. Speeds are rounded to the nearest 5 kt, with halfway
  values rounded upward. A circle denotes a vector magnitude that rounds to
  zero, which may reflect cancellation between ensemble members. Glyphs use
  Northern Hemisphere feather orientation for these map domains.
- The source remains 0.1 degrees. Rendering resolution does not add forecast
  detail. No gust forecast, ensemble spread, or flight probability is shown.
- H/L annotations are extrema in the sampled ensemble-mean pressure field;
  they are not member tracks or cyclone intensity guidance.

The September 20, 2026 12Z run, valid September 24 at 15Z (11 a.m. EDT),
rendered successfully with Earth Engine painting 984 continental and 1,056
Northeast wind barbs (previously 240 and 272). The Earth Engine rasters measure
4,000 × 2,292 and 4,000 × 3,833 pixels; the finished charts measure
5,760 × 4,164 and 5,760 × 6,383 pixels. Each map used two aligned requests.
Rendering, downloading, and joining those strips took 65.05 seconds for the
continental view and 60.58 seconds for the Northeast, producing map PNGs of
3,352,755 and 3,374,959 bytes respectively. These are individual measurements,
excluding annotation extraction and local framing, not latency guarantees or
measurements of billed compute. Requests have a three-minute client timeout.

All four raw KCDW values—pressure, scalar wind speed, U, and V—matched saved
BigQuery values exactly. Checks also covered source run/lead/grid identity,
complete finite annotation data, physical ranges, mean-vector magnitude,
PNG dimensions and opacity, artifact hashes, and visual inspection of both
finished charts. The annotation fields also matched the previous render within
floating-point roundoff (maximum absolute difference 2.3e-13 hPa for pressure
and 4.7e-14 kt for wind). A separate Earth Engine symbol strip previously
verified 0, 5, 10, 15, 50, 65, and 100 kt symbols, including filled flags.

The wind-barb tests exercise standard speed symbols, filled-flag geometry,
upwind direction, nearest-5-kt rounding, calm circles, projection rotation,
vector magnitude rather than scalar mean, invalid values, and rejection of
older renderer caches. A resolution-invariance test checks that standard and
retina exports use identical barb locations and wind values; a strip-joining
test checks projected row alignment and exact pixel preservation:

```sh
var/earth-engine-charts-venv/bin/python -m unittest discover -s tests \
  -p 'test_wn3_earth_engine_chart.py' -v
```

Sources: [WeatherNext on Earth Engine](https://developers.google.com/weathernext/guides/earth-engine),
[Earth Engine pixel rendering](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels),
[NWS wind-barb conventions](https://www.weather.gov/hfo/windbarbinfo),
[WeatherNext experimental data terms](https://storage.googleapis.com/weathernext-public/terms-of-use.pdf).
