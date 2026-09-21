# WeatherNext charts rendered entirely by Earth Engine

`scripts/wn3_earth_engine_chart.py` exports complete wind and pressure charts.
Earth Engine samples the model fields, rotates wind vectors, constructs barb
lines and flags, finds pressure centers, chooses pressure-label positions,
and draws the weather layers, text, titles, and legend. The server-side
expressions live in `scripts/wn3_earth_engine_layers.py`.

Python submits the run/time, static layout, bundled font outlines, and Natural
Earth boundaries. It downloads rendered RGB strips, joins them without
resampling, and saves the PNG and provenance. The only other responses are
source metadata and validation booleans/counts. No model-value grid or
weather-derived geometry is downloaded, and no local weather analysis or
Matplotlib annotation is required.

## Render

Install the separate Earth Engine requirements and use the project's Google
credentials with WeatherNext and Earth Engine access:

```sh
uv venv var/earth-engine-charts-venv
uv pip install --python var/earth-engine-charts-venv/bin/python \
  -r requirements-earth-engine-charts.txt
scripts/with-runtime.sh \
  var/earth-engine-charts-venv/bin/python scripts/wn3_earth_engine_chart.py \
  --run 2026-09-20T12:00:00Z \
  --valid 2026-09-24T15:00:00Z \
  --view both \
  --output-dir var/charts/earth-engine-20260920T12Z-f099-server
```

Natural Earth 50m shapefiles are reused from
`var/charts/cartopy-data/shapefiles/natural_earth/`. The existing `--cartopy-dir`
argument overrides this static boundary directory. Cartopy, SciPy, and
Matplotlib are no longer runtime dependencies. No NumPy model arrays are
downloaded or analyzed.

The default `--width 4000` produces 5,760-pixel-wide finished charts at 2× the
original pixel density. `--width 2000` produces a 2,880-pixel-wide chart;
allowed settings are 800–4,000. Earth Engine renders the entire canvas,
including the frame and text, at the final pixel resolution. Requests use
aligned row strips of at most 4 million pixels to fit the API size limit,
with a five-minute client timeout. No airport marker, white text boxes, or
text halos are drawn.

## Outputs and cache

Each view saves:

- `wn3-earth-engine-wind-pressure.png`: the finished chart.
- `earth-engine-map.png`: identical server-rendered pixels, retained for
  compatibility with the earlier workflow.
- `earth-engine-expression.json`: the complete serialized EE expression,
  including server-side model processing and annotation construction.
- `provenance.json`: source identity, dimensions, server validation results,
  strip timings, rendering ownership, and SHA-256 hashes.
- `render-tiles/`: checked RGB strips, keyed by the complete expression and
  output grid, so interrupted exports can resume without repeating finished work.

Renderer version 5 rejects earlier hybrid-renderer caches. Use a fresh output
directory for changed dates or settings. Matching caches are checksum-checked
and reused; the CLI still validates source metadata online. This renderer
produces no `annotation-fields.npz` or downloaded `barb-geometries.json`.

## Data and symbols

- Wind shading uses ensemble-mean scalar speed, converted from m/s to knots,
  with a 0–60 kt palette.
- Pressure uses ensemble-mean sea-level pressure, converted from Pa to hPa.
  Earth Engine applies a normalized Gaussian filter with sigma 2 native pixels
  (0.2 degrees), then draws contours every 2 hPa.
- Wind barbs use ensemble-mean U/V, which may have a smaller magnitude than
  mean scalar speed. Earth Engine performs the same geographic-grid vector
  rotation as the previous chart and points each staff upwind. The Northern
  Hemisphere convention is used: 5 kt half feather, 10 kt full feather,
  50 kt filled flag. Values round to the nearest 5 kt, halfway upward. A circle
  means a magnitude that rounds to zero, including ensemble cancellation.
- A fixed 500-column grid stays on the server for symbol and label placement.
  Barbs occur every 12 cells in the continental view and 15 in the Northeast:
  984 and 1,056 barbs. Pixel-center coordinates are converted to integer grid
  indices before applying this spacing. Glyph length is 1% of map width.
- Earth Engine chooses contour-label positions near isobars and rotates their
  text along the local pressure gradient's perpendicular. Central differences
  on the projected chart grid keep those angles aligned with the drawn lines.
  H/L labels mark
  sampled pressure extrema, separated by at least 800 km for the continental
  view and 450 km for the Northeast, with at most four of each symbol.
- The forecast remains on a 0.1-degree model grid. Display resolution adds no
  forecast detail. These experimental charts do not show gusts, ensemble
  spread, or flight probabilities.

## Verification

The renderer verifies source run/time/native-grid identity and rejects missing
or physically invalid weather samples. Server-side checks enforce complete
sampling, the expected barb count, upright pressure labels, at least one
pressure label, and mean U/V magnitude no greater than mean scalar speed. Only the check results return to
Python. The downloader verifies strip dimensions, full opacity, and hashes.

Offline tests prevent weather-value reads while the full chart expression is
built, enforce PNG-only downloads, exercise cache integrity, and verify lossless
strip alignment, interrupted-export recovery, and retina layout:

```sh
var/earth-engine-charts-venv/bin/python -m unittest discover -s tests \
  -p 'test_wn3_earth_engine_chart.py' -v
```

Opt-in live tests exercise Earth Engine's actual wind-barb shapes, speed
rounding, direction, grid alignment, and contour-label angles. A synthetic
chart checks that the entire map, legend, and text survive composition. The
weather check compares the September 20,
2026 12Z run at September 24 15Z with the previously verified BigQuery values.
Earth Engine returns comparison booleans, not the forecast values. The frame
check downloads only a synthetic RGB chart:

```sh
WN3_EE_LIVE_TESTS=1 scripts/with-runtime.sh \
  var/earth-engine-charts-venv/bin/python -m unittest discover -s tests \
  -p 'test_wn3_earth_engine_live.py' -v
```

The September 20 12Z / F099 example was exported and visually checked with
renderer version 5:

| View | Finished pixels | Barbs | Export time |
| --- | --- | --- | --- |
| Continental | 5,760 × 4,164 | 984 | 293 seconds |
| Northeast | 5,760 × 6,383 | 1,056 | 473 seconds |

These are single observations with the two views exporting concurrently, after
development requests may have warmed Earth Engine caches. No local strips were
reused. Timings include rendering, transfer, and assembly, and exclude the
earlier metadata and validation calls. They are not latency or billing guarantees.

## Bundled text font

`scripts/earth_engine_assets/font.json` contains static DejaVu Sans outlines, generated
once by `scripts/build_earth_engine_font.py`. Earth Engine constructs and paints
the labels from these outlines using server-side strings and coordinates.
The generator requires the optional `requirements-charts.txt` environment;
normal rendering does not. The bundled font's license is in
`scripts/earth_engine_assets/LICENSE_DEJAVU`.

Sources: [WeatherNext on Earth Engine](https://developers.google.com/weathernext/guides/earth-engine),
[EE client and server operations](https://developers.google.com/earth-engine/guides/client_server),
[Earth Engine pixel rendering](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels),
[NWS wind-barb conventions](https://www.weather.gov/hfo/windbarbinfo),
[WeatherNext experimental data terms](https://storage.googleapis.com/weathernext-public/terms-of-use.pdf).
