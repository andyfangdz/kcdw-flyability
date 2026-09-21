# WeatherNext wind and pressure maps rendered by Earth Engine

`scripts/wn3_earth_engine_chart.py` asks Earth Engine to render the map raster:
10 m wind shading, smoothed sea-level pressure contours, Natural Earth
boundaries, and projection to EPSG:5070. Local Matplotlib adds pressure labels,
wind barbs, H/L labels, the KCDW marker, timestamps, and the color legend. It
does not draw the visible pressure contours or wind shading locally.

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
  --output-dir var/charts/earth-engine-20260920T12Z-f099
```

The initialization and valid time are explicit; unavailable source images fail.
Each view saves the original `earth-engine-map.png`, a finished
`wn3-earth-engine-wind-pressure.png`, an annotation grid, and `provenance.json`.
Matching cached files are checksum-checked and reused; the CLI still checks
the source image metadata online. Use a new output directory for a new run,
valid time, or changed rendering settings.

## Reading and verification

- Wind shading: ensemble-mean scalar speed, m/s converted to knots, 0–60 kt.
- Contours: ensemble-mean sea-level pressure, Pa converted to hPa, every 2 hPa.
  Earth Engine applies a normalized Gaussian filter with sigma 2 native pixels
  (0.2 degrees) and radius 6 pixels, followed by bilinear display interpolation.
- Barbs: ensemble-mean U/V components, rotated into the map projection.
  Their magnitude can be smaller than the mean scalar speed.
- The source remains 0.1 degrees. Rendering resolution does not add forecast
  detail. No gust forecast, ensemble spread, or flight probability is shown.
- H/L annotations are extrema in the sampled ensemble-mean pressure field;
  they are not member tracks or cyclone intensity guidance.

The September 20, 2026 12Z run, valid September 24 at 15Z (11 a.m. EDT),
rendered successfully. Earth Engine's PNG calls took 19.67 seconds for the
continental view and 18.03 seconds for the Northeast view, returning 1,278,600
and 1,284,537 bytes respectively. These are individual request measurements,
excluding annotation extraction and local framing, not latency guarantees or
measurements of billed compute.

All four raw KCDW values—pressure, scalar wind speed, U, and V—matched saved
BigQuery values exactly. Checks also covered source run/lead/grid identity,
complete finite annotation data, physical ranges, mean-vector magnitude,
PNG dimensions and opacity, artifact hashes, and visual inspection of both
finished charts.

Sources: [WeatherNext on Earth Engine](https://developers.google.com/weathernext/guides/earth-engine),
[Earth Engine pixel rendering](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels),
[WeatherNext experimental data terms](https://storage.googleapis.com/weathernext-public/terms-of-use.pdf).
