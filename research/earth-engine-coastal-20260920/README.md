# Earth Engine coastal-low comparison

Published on the [commercial-checkride event page](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride#coastal-low).

[manifest.json](manifest.json) records 48 immutable PNGs: WeatherNext 3 mean,
WeatherNext 2 mean, deterministic ECMWF IFS, and deterministic NOAA GFS;
September 19 / 20 12Z
initializations; six matching valid times spanning September 23 12Z through
September 25 12Z. It retains the exact source image IDs, initialization / valid
times, leads, dimensions, image checksums and public paths. Prefix image paths
with `https://kcdw-flyability.andyfang.workers.dev` to retrieve them.

All maps are 1800 × 1725 pixels, with a common extent, 0–60+ kt wind scale,
2 hPa MSLP contours and 1,056 wind barbs. Total PNG storage is 60,773,295 bytes.
Every frame passed server-side data coverage, wind consistency and annotation
checks; every uploaded image was downloaded and verified against its SHA-256.
All contour, barb and label geometry is constructed in Earth Engine.

GFS wind comes from Earth Engine's `NOAA/GFS0P25` collection. Its pressure
field is absent there, so Python imports the matching NOAA GRIB2 PRMSL records
as lossless float64 Cloud Optimized GeoTIFFs. No interpolation or weather
geometry is performed locally. The 12 private inputs occupy 27,761,344 bytes.
Earth Engine checks the native spherical grid before combining pressure and
wind. [Live pressure checks](gfs-pressure-verification.json) confirm 60 native
grid samples match the original NOAA records to 0.000001 Pa. The public
manifest retains NOAA URLs and GRIB / GeoTIFF hashes, with no private locations.

The comparison was prepared September 21, 2026 at 03:53 UTC and published in event
archive `20260921T035353Z.maps-890037`. This is a saved comparison of selected
runs, separate from the page's refreshed point forecasts. Source metadata,
serialized Earth Engine expressions and render receipts remain in the host's
`var/charts/coastal-comparison-20260920/` directory. GFS import receipts and
original records remain in `var/charts/gfs-pressure-20260920/`. The public
files here contain no local paths or credentials.

See [generation and publication instructions](../../deploy/EARTH-ENGINE-COASTAL-MAPS.md).
