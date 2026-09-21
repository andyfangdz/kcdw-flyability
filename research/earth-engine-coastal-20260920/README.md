# Earth Engine coastal-low comparison

Published on the [commercial-checkride event page](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride#coastal-low).

[manifest.json](manifest.json) records 36 immutable PNGs: WeatherNext 3 mean,
WeatherNext 2 mean, and deterministic ECMWF IFS; September 19 / 20 12Z
initializations; six matching valid times spanning September 23 12Z through
September 25 12Z. It retains the exact source image IDs, initialization / valid
times, leads, dimensions, image checksums and public paths. Prefix image paths
with `https://kcdw-flyability.andyfang.workers.dev` to retrieve them.

All maps are 1800 × 1725 pixels, with a common extent, 0–60+ kt wind scale,
2 hPa MSLP contours and 1,056 wind barbs. Total PNG storage is 43,775,640 bytes.
Every frame passed server-side data coverage, wind consistency and annotation
checks; every uploaded image was downloaded and verified against its SHA-256.
No model-value grids or weather-derived geometry were downloaded.

The maps were prepared September 21, 2026 at 03:18 UTC and published in event
archive `20260921T032029Z.maps-881338`. This is a saved comparison of selected
runs, separate from the page's refreshed point forecasts. Source metadata,
serialized Earth Engine expressions and render receipts remain in the host's
`var/charts/coastal-comparison-20260920/` directory. The small public manifest
here contains no local paths or credentials.

See [generation and publication instructions](../../deploy/EARTH-ENGINE-COASTAL-MAPS.md).
