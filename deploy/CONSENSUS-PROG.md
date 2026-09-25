# Consensus surface progs

`scripts/consensus_prog_chart.py` draws AWC/WPC-style surface prog charts that
synthesize four global models: NOAA GFS, ECMWF IFS, ECMWF AIFS and ECCC
Canadian GDPS. Each chart shows isobars, H/L centers, fronts and 6-hour
precipitation for one valid time. Each chart is rendered in two views:
continental US (4 hPa isobars) and the Northeast (2 hPa isobars).
All inputs are public; no credentials are needed.

## Setup and rendering

```sh
uv venv var/charts-venv
uv pip install --python var/charts-venv/bin/python -r requirements-consensus-prog.txt
var/charts-venv/bin/python scripts/consensus_prog_chart.py \
  --start 2026-09-25T18:00:00Z --end 2026-10-02T00:00:00Z
```

Valid times must fall on 00, 06, 12 or 18 UTC. Use `--start/--end/--every` for a series or
`--times` for specific charts. Without arguments, the series runs from the current synoptic
hour for 144 hours. For each model, the script uses the newest cycle within 48 hours
that covers every requested time. `--run ifs=2026-09-25T00:00Z` pins a cycle.
IFS 06/18Z cycles stop at 144 hours, and GDPS runs only at 00/12Z out to 240 hours.
Each valid time requires at least two models; a missing model is listed in the
manifest, and its charts use the remaining models.

The default output is `var/charts/consensus-prog-<first valid>/`:

- `conus-<valid>.png` and `northeast-<valid>.png`: 2400 × 1590 and 2100 × 1590 pixels at 150 dpi.
- `conus-loop.gif` and `northeast-loop.gif`: half-resolution animations.
- `manifest.json`: model runs and leads, consensus and per-model centers, front
  polylines and image checksums.

Only the needed GRIB messages are downloaded. For GFS and ECMWF, the script reads
byte ranges listed in the published index files. For GDPS, it downloads single-field
files of about 0.7 MB each. Each message is checked for the expected run, valid time,
grid and units, then regridded bilinearly to a common 0.25° grid over 15–62°N,
140–50°W. The cropped fields are cached in `var/charts/consensus-prog-cache/`
(`<model>/<run>/fNNN.npz`, with a provenance JSON listing each source URL, byte
range and SHA-256). A 26-time series is about 85 MB of cache and takes about two minutes on
first run. Re-rendering from the cache takes about a minute.

`pyproj` must be imported before `eccodes`. The ecCodes wheel bundles its own
copies of PROJ and SQLite, and loading it first corrupts the heap at exit.

## On the checkride page

Each scheduled event update (`kcdw.event_update`, hourly) calls `kcdw/consensus_charts.py`. That
module runs this script as `--cycle common --through <flight end> --point <airport>`, using
`var/charts-venv/bin/python`. In common mode, all models come from the newest 00/12Z cycle that
every model has published through the end of the flight. The first frame averages the models'
analyses (F000; no precipitation), and each later frame averages forecasts at the same lead, every 6 hours
through the flight. The output goes to `var/events/<slug>/consensus-prog/<cycle>/`.
Rerunning an unchanged cycle reuses the verified set in about two seconds. A new cycle takes
one to two minutes about twice a day. Only the three newest cycle folders are kept. Cached model runs
older than four days are deleted.

New PNGs are uploaded to the Worker's content-addressed event-map route (the same route and
checksum readback as the coastal-low maps). `published.json` records images already
confirmed, so none are sent twice. The snapshot stores `consensus_prog`: image URLs, model leads,
the KCDW pressure diagnostic and the nearest consensus high, low and front for every frame.
It does not store the images themselves.
The page section *Consensus surface analysis & prog* follows *Surface pattern*. It shows
analysis and flight-time cards with a plain-language summary. A viewer steps through every
frame in the Northeast or continental view.

If a run cannot produce charts, the last good set (`current.json`) is carried over and labeled,
as long as its cycle is under 48 hours old. After that, the section is omitted. With
`--no-publish`, images are rendered but not uploaded, and the page notes it. Each cycle
adds about 35 MB of PNGs to R2. Archived pages keep their images.

The KCDW gradient comes from the smoothed consensus pressure, and the page quotes the
geostrophic speed it implies at 1.2 kg/m³. That speed describes wind above the friction layer;
it is not a surface wind or gust forecast.

## What the chart shows

- **Isobars, H and L:** equal-weight mean sea-level pressure, smoothed with a
  40 km Gaussian. Centers are local extrema that stand at least 1.5 hPa
  (H) or 1.0 hPa (L) apart from their ~1,400 km surroundings.
- **Model letters (G, E, A, C):** each model's own low within 900 km of a
  consensus low. They show how far apart the models place the storm; the mean
  can be weaker than any single model.
- **Fronts:** objective analysis using the Hewson (1998) thermal front parameter
  on the mean 850 hPa equivalent potential temperature, smoothed with a 75 km sigma. Fronts
  lie on the warm edge of the frontal zone. They require a θe gradient
  (≥ 3.2 K/100 km within 200 km), TFP ≥ 0.6 K/(100 km)², a dry 850 hPa temperature
  gradient ≥ 0.7 K/100 km, and a length of at least 600 km. They are removed where GFS surface
  pressure is below 880 hPa. Type comes from the mean 850 hPa wind across the
  front: cold if more than 1.5 m/s blows toward warm air, warm if more than 1.5 m/s blows toward cold air, otherwise stationary.
  No occluded fronts or troughs are drawn.
- **Precipitation:** 6-hour totals ending at the valid time. Light green means at least
  half of the models have measurable precipitation (≥ 0.01 in). Dark green means at least 3/4 of the models agree
  and the mean is ≥ 0.10 in. Red hatching means thunder: at least 3/4 of the models are wet and at least half of the models reporting CAPE
  have ≥ 1000 J/kg. AIFS publishes no CAPE, and CAPE fields are not identical
  across models (GFS surface-based, IFS most-unstable, GDPS surface).
  Snow requires mean 2 m ≤ 1 °C and 850 hPa ≤ −2 °C. Mixed/freezing requires 2 m ≤ 0.5 °C with a warmer 850 hPa layer.

Averaging washes out features the models place differently. At longer leads, the mean
shows fewer fronts and shallower lows. On the Sep 25 18Z–Oct 2 00Z series, the count of
front segments fell from about 30 on day 0 to 4–10 by day 6. The letters and
precipitation agreement reflect that uncertainty, but the chart is not a
probability forecast.

## Checking against WPC

For analysis and 12–48 h prog times, compare with the WPC coded fronts. The event
pipeline already fetches that product (`kcdw/synoptic_pattern.py`). On 2026-09-25, the
18Z consensus was checked against WPC's 15Z analysis. It matched the Atlantic coastal low
(995 hPa), its offshore warm front, and the Southeast/Gulf boundary. It placed the
low's trailing cold front near the Carolina coast, while WPC placed it well offshore. It also
added a few short segments that WPC does not draw: the Lake Michigan shore, the Pacific marine layer, and
Baja. Treat isolated short fronts, especially over water or along coasts, with suspicion.

## Verification

```sh
var/charts-venv/bin/python -m unittest tests.test_consensus_prog -v
```

The tests cover regridding (longitude wrap, latitude flip), θe against Bolton,
center detection, synthetic cold/warm/stationary fronts, rejection of moisture-only
boundaries and high terrain, and precipitation agreement. They skip when the
chart dependencies are absent.

Sources: [NOAA GFS on AWS](https://registry.opendata.aws/noaa-gfs-bdp-pds/),
[ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data) (CC BY 4.0),
[ECCC GDPS](https://eccc-msc.github.io/open-data/msc-data/nwp_gdps/readme_gdps_en/)
(Environment and Climate Change Canada open licence); boundaries: Natural Earth.
Hewson, T. D. (1998): Objective fronts, *Meteorol. Appl.* 5, 37–65.
Experimental model synthesis, not an official WPC/AWC product.
