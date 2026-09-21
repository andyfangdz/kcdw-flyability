# Coastal-low evolution on the event page

The commercial-checkride event can embed a saved Earth Engine comparison of
WeatherNext 3 mean, WeatherNext 2 mean, deterministic ECMWF IFS, and deterministic
NOAA GFS. Both the
initialization and valid-time selectors apply to all panels. Playback waits for
every image to load before advancing. Failed loads hide the prior image, so the
new timestamp never labels an old forecast.

## Generate a comparison

Use the Earth Engine environment described in [WN3 chart setup](WN3-EARTH-ENGINE-CHARTS.md).
To include GFS, install the additional GRIB / GeoTIFF tools and prepare its
pressure inputs first, as described below. To use only catalog fields, pass
`--models wn3 wn2 ifs` and omit `--gfs-pressure-dir`.
The original full-chart command still works unchanged. The comparison command
exports map-only PNGs, with shared labels and legend supplied by the event page:

```sh
scripts/with-runtime.sh var/gfs-earth-engine-venv/bin/python \
  scripts/earth_engine_coastal_maps.py \
  --runs 2026-09-19T12:00:00Z 2026-09-20T12:00:00Z \
  --times 2026-09-23T12:00:00Z 2026-09-24T00:00:00Z \
          2026-09-24T12:00:00Z 2026-09-24T18:00:00Z \
          2026-09-25T00:00:00Z 2026-09-25T12:00:00Z \
  --gfs-pressure-dir var/charts/gfs-pressure-20260920 \
  --output-dir var/charts/coastal-comparison-20260920 --jobs 3
```

The example produces 48 maps (4 models × 2 runs × 6 valid times), each
1800 × 1725 pixels. This supports 2× display at up to 900 CSS pixels wide; the
inline cards are smaller. The native model grid remains 0.1° for WN3 and 0.25°
for WN2 / IFS / GFS. Four models use a two-by-two desktop layout; phones stack
the maps. `--probe` checks source availability without rendering. Use only
00Z / 12Z IFS cycles for long-range comparisons, and common six-hour leads.
Expanded maps retain model / run / valid-time labels and the shared legend;
their display width is capped below 900 CSS pixels for 2× rendering.

Each frame validates the exact source run, valid time, lead, native projection,
band grids, regional data coverage, wind-vector consistency, barb count, and
pressure labels. Scalar wind is the ensemble-mean speed for WeatherNext, and
the server-side magnitude of U/V for deterministic IFS / GFS. Pressure is MSLP in
hPa, smoothed with a Gaussian sigma of 0.2° before drawing 2 hPa contours.
The fixed annotation grid yields 1,056 barbs per frame. H/L markers are local
extrema, not associated storm tracks or probabilities. Ensemble means can
blur lows and suppress extrema.

All chart calculations, geometry and rasterization run in Earth Engine.
Python transfers static fonts and boundaries and downloads metadata,
validation booleans/counts, and completed PNGs. WeatherNext, IFS, and GFS wind
grids stay in Earth Engine. The separate GFS pressure import does download
NOAA pressure grids to convert the file format, as described below.
Source metadata, serialized expressions, checks, request timings and
checksums stay under the output directory. Re-running the same command resumes
validated frames and completed PNG strips. Change `VERSION` / output directory
when changing the comparison renderer. A complete `manifest.json` is written
only after every requested frame succeeds.

## GFS pressure import

`NOAA/GFS0P25` includes 10 m U/V, but no mean sea-level pressure. The import
command fetches only each selected NOAA GRIB2 `PRMSL:mean sea level` record
using a bounded HTTP byte range. It checks initialization, lead, valid time,
instantaneous pressure in Pa, scan order, grid dimensions and Earth radius.
It shifts longitude columns from 0–360 to −180–180 without interpolation,
writes a lossless float64 Cloud Optimized GeoTIFF, and uploads it under its
SHA-256 to private Google Cloud Storage. Readback verifies every uploaded file.

```sh
uv venv var/gfs-earth-engine-venv
uv pip install --python var/gfs-earth-engine-venv/bin/python \
  -r requirements-gfs-earth-engine.txt
scripts/with-runtime.sh var/gfs-earth-engine-venv/bin/python \
  scripts/gfs_earth_engine_pressure.py \
  --runs 2026-09-19T12:00:00Z 2026-09-20T12:00:00Z \
  --times 2026-09-23T12:00:00Z 2026-09-24T00:00:00Z \
          2026-09-24T12:00:00Z 2026-09-24T18:00:00Z \
          2026-09-25T00:00:00Z 2026-09-25T12:00:00Z \
  --bucket aviation-486817-earth-engine-inputs \
  --output-dir var/charts/gfs-pressure-20260920
```

The existing bucket is private, with public access prevention and uniform
bucket access, in `US-CENTRAL1`. Another installation needs an existing bucket
in a region supported by `ee.Image.loadGeoTIFF`, storage write/read permissions
for the importer and object/bucket read permissions for the EE caller.
These 12 inputs occupy about 28 MB. Keep them for reproducible rerenders;
published pages only need the finished PNGs in R2. No public bucket access is
needed. Each render rechecks the imported grid, spherical CRS, exact pressure
run/time and catalog wind metadata before combining them in Earth Engine.
Public provenance retains NOAA URLs and GRIB/GeoTIFF hashes; private input
locations and local files remain on the host.

## Publish and retain through scheduled updates

Deploy the tested Worker first (`npm ci`, `npm run check`, `npm test`, then
`npm run deploy` in `cloudflare/`). Deploying requires a Cloudflare deployment
login; the homeserver's existing `PUBLISH_TOKEN` only publishes content.
The existing deployment login for this installation is on the OCI host under
`ubuntu`; scheduled content publishing remains on the homeserver.

```sh
scripts/with-runtime.sh python -m kcdw.coastal_publish \
  --manifest var/charts/coastal-comparison-20260920/manifest.json
```

The publisher checks local PNG hashes and dimensions, uploads each immutable
image through the authenticated Worker route, then downloads it again and
verifies its checksum. Once all assets are available it takes the existing
event-update lock, adds the public manifest to the latest event snapshot,
renders and publishes a new archive, and atomically activates
`var/events/commercial-checkride/coastal-maps.json` for future scheduled runs.
The original snapshot collection time is preserved. Local paths and private
Earth Engine expressions are not published. PNG assets are separate from the
bounded event HTML and share the Worker's existing read-access policy.

Maps are a **saved run comparison**, not an hourly live product. The page labels
the selected cycles and map preparation time separately from the current
briefing. Scheduled updates copy the active manifest into each new snapshot;
they do not trigger Earth Engine rendering. To refresh the comparison, generate
new runs / times and publish the completed manifest again. Archived pages keep
their original images through content-addressed URLs. Keep the renderer output
and active manifest with the normal host backups.

The first published comparison's [public manifest](../research/earth-engine-coastal-20260920/manifest.json)
is retained in the repository for source and image provenance.

Sources: [WeatherNext 3](https://developers.google.com/weathernext/guides/earth-engine),
[WeatherNext 2 mean](https://developers.google.com/earth-engine/datasets/catalog/projects_gcp-public-data-weathernext_assets_weathernext_2_0_0_mean),
[ECMWF IFS](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_NRT_FORECAST_IFS_OPER),
[GFS catalog winds](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GFS0P25),
[NOAA GFS pressure](https://www.nco.ncep.noaa.gov/pmb/products/gfs/),
[Earth Engine GeoTIFF inputs](https://developers.google.com/earth-engine/apidocs/ee-image-loadgeotiff).
The IFS adapter verifies the live `mean_sea_level_pressure_sfc` band.
