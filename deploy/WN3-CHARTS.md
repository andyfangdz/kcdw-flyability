# WN3 pressure and wind charts

`scripts/wn3_synoptic_chart.py` renders a continental US map from the official
WeatherNext 3 statistics Zarr store on Google Cloud Storage. It uses the existing
GCS reader and cache; rendering does not query BigQuery.

## Setup and rendering

Install the optional plotting dependencies in a separate environment:

```sh
uv venv var/charts-venv
uv pip install --python var/charts-venv/bin/python -r requirements-charts.txt
```

Use Application Default Credentials for an identity with access to the WN3
statistics bucket, or the existing reader's `gcloud auth print-access-token`
fallback. If credentials are stored in a file, set
`GOOGLE_APPLICATION_CREDENTIALS` to that file's path.

Render the 72-hour forecast initialized at 00Z on September 20, 2026:

```sh
var/charts-venv/bin/python scripts/wn3_synoptic_chart.py \
  --run 2026-09-20T00:00:00Z \
  --valid 2026-09-23T00:00:00Z
```

Both timestamps require a timezone. Initialization must be at 00, 06, 12, or
18 UTC; valid time must be an integer forecast hour from 1 through 360.
The command requires an available run and does not select a replacement run.

Default output directory: `var/charts/wn3-20260920T00Z-f072/`.

- `wn3-mslp-wind-f072.png`: 4800 × 3390 pixels at 300 dpi.
- `fields.npz`: regional coordinates and the four converted model fields.
- `provenance.json`: initialization, valid time, units, source object generations
  and checksums, data and image hashes, and rendering settings.

Use `--output-dir` to choose another directory or `--dpi` to change image
resolution (100–600). An existing output directory is reused only when its
forecast timestamps and data checksum match. Use a new directory for another
forecast.

The first fetch reads four compressed global hourly planes before cropping the
map region. These totaled about 89 MB for the example above. The existing reader
verifies and caches those objects under `var/wn3-zarr/`; `--cache-dir` overrides
that location. Cartopy downloads Natural Earth 50m boundaries on first render
and stores them under `var/charts/cartopy-data/`; use `--cartopy-dir` to change
that location. Once the regional fields and boundaries are cached, the same
chart can be rendered offline. Generated files remain under ignored `var/`.

An [Earth Engine rendering alternative](WN3-EARTH-ENGINE-CHARTS.md) produces
continental and Northeast views without downloading global Zarr planes.
Earth Engine computes the barb geometry and pressure labels and renders the
complete chart, including boundaries, titles, and legend. Python downloads
finished image strips and joins them without sampling model-value grids.

## Reading the chart

- Pressure contours: ensemble-mean sea-level pressure, converted from Pa to
  hPa/mb, at 2 hPa intervals. A Gaussian filter with a 0.2° standard deviation
  smooths contours and the local extrema used for H/L labels.
- Wind shading: ensemble-mean scalar 10 m wind speed, converted from m/s to
  knots, in 0.5 kt increments from 0 to 60 kt, with an overflow color above 60.
- Wind barbs: ensemble-mean U/V components in knots, sampled every 1.5° latitude
  and 2° longitude. Their vector magnitude may be lower than the mean scalar
  speed because ensemble members can have different wind directions.

The source grid remains 0.1°. Finer colors and higher image resolution improve
presentation without adding model detail. The chart has no KCDW marker and
does not display ensemble spread. It is experimental model guidance.

Source: [Google WeatherNext model documentation](https://developers.google.com/weathernext/guides/models).
The chart records the source terms link in its provenance file.

## Verification

```sh
var/charts-venv/bin/python -m unittest discover -s tests \
  -p 'test_wn3_synoptic_chart.py' -v
```

These tests check forecast hours, geographic indexing, unit conversions,
invalid fields, and cache integrity without accessing GCS. They skip when the
optional chart dependencies are absent.
