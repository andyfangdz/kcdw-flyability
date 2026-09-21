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
  --output-dir var/charts/earth-engine-20260920T12Z-f099-barbs
```

The initialization and valid time are explicit; unavailable source images fail.
Each view saves the original `earth-engine-map.png`, a finished
`wn3-earth-engine-wind-pressure.png`, an annotation grid, `barb-geometries.json`,
and `provenance.json`.
Matching cached files are checksum-checked and reused; the CLI still checks
the source image metadata online. Use a new output directory for a new run,
valid time, or changed rendering settings. Renderer version 2 rejects older
caches whose barbs were drawn locally, so they cannot be relabeled as Earth
Engine-rendered barbs.

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
rendered successfully with Earth Engine painting 240 continental and 272
Northeast wind barbs. Its PNG calls took 24.44 seconds for the continental view
and 35.08 seconds for the Northeast view, returning 1,287,089 and 1,295,145 bytes
respectively. These are individual request measurements,
excluding annotation extraction and local framing, not latency guarantees or
measurements of billed compute.

All four raw KCDW values—pressure, scalar wind speed, U, and V—matched saved
BigQuery values exactly. Checks also covered source run/lead/grid identity,
complete finite annotation data, physical ranges, mean-vector magnitude,
PNG dimensions and opacity, artifact hashes, and visual inspection of both
finished charts. Comparing the original Earth Engine rasters without barbs
with the new rasters found 4,030 changed pixels in the continental view and
5,153 in the Northeast view. A separate Earth Engine symbol strip verified
0, 5, 10, 15, 50, 65, and 100 kt symbols, including filled flags.

The wind-barb tests exercise standard speed symbols, filled-flag geometry,
upwind direction, nearest-5-kt rounding, calm circles, projection rotation,
vector magnitude rather than scalar mean, invalid values, and rejection of
older renderer caches:

```sh
var/earth-engine-charts-venv/bin/python -m unittest discover -s tests \
  -p 'test_wn3_earth_engine_chart.py' -v
```

Sources: [WeatherNext on Earth Engine](https://developers.google.com/weathernext/guides/earth-engine),
[Earth Engine pixel rendering](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels),
[NWS wind-barb conventions](https://www.weather.gov/hfo/windbarbinfo),
[WeatherNext experimental data terms](https://storage.googleapis.com/weathernext-public/terms-of-use.pdf).
