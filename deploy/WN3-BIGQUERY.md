# WN3 BigQuery feasibility probe

Checked 2026-09-19 using the homeserver runtime's existing allowlisted ADC.
The initial research left production unchanged. The production replacement
described below was installed on 2026-09-20 UTC.

**Follow-up result:** actual execution confirms strong spatial pruning. The
333 GiB temperature estimate became 10.13 MiB processed / 11 MiB billed, with
no query-cache hit, and the returned value matches Zarr exactly. See the
pruning investigation below; the initial cap rejection did not demonstrate
an expensive actual scan.

**Full-week benchmark:** all 39 columns across 168 hours returned in 3.78
seconds wall time, with 136 MiB billed and no cache hit. Full results below.

## Findings

- The identity can list BigQuery projects and read the WeatherNext Analytics Hub
  listing and the exact 0.1-degree table schema. The existing `aviation-486817`
  project accepts dry-run query requests; it has no visible datasets.
- The listing identifies `866962084172.WeatherNext_3`. Its
  `weathernext_3_0_0_0p1deg` object is an accessible view over
  `weathernext-data-geo.WeatherNext3.weathernext_3_0_0_0p1deg`. Listing tables in
  the source dataset returns 403, but getting this exact view succeeds.
- All 39 current collector columns (13 fields times mean/p10/p90) exist,
  including total, IMERG, and experimental hourly precipitation.
- No Analytics Hub subscription, new dataset, or billing configuration was
  created. Direct queries against the known view pass dry-run validation.
- The probe uses an explicit synoptic initialization and the nearest regular
  0.1-degree grid center to KCDW, 40.9 N / 74.3 W, with a 100 m spatial filter.

## Measured query estimates

Initialization: `2026-09-19T12:00:00Z`; valid time: `2026-09-20T12:00:00Z`.
Initial requests used US location and on-demand `maximumBytesBilled` of 1 GiB.

| Query | Dry-run bytes processed |
| --- | ---: |
| Temperature mean alone, no coordinate/provenance output | 240,567,494,400 |
| Temperature mean with grid/time provenance, current probe | 357,583,507,200 |
| All 39 columns with grid/time provenance, current probe | 4,804,191,993,600 |

The provenance-preserving single-column query was rejected before execution:
`bytesBilledLimitExceeded`, requiring a cap of at least 357,584,338,944 bytes.
Changing from nearest-point window selection to a direct grid-center spatial
predicate did not reduce that probe's estimate. A polygon-only temperature
query estimated 253,634,803,200 bytes.

These are estimates, NOT actual billed bytes. Cluster pruning can make actual
execution cheaper, but we have not demonstrated that here. Do not multiply
these one-hour estimates by 168 to estimate a weekly query: the forecast is a
nested record, and additional hours may not increase scans proportionally.
The accessible view's zero `numBytes` does not describe its underlying storage.
We have not independently inspected the underlying table's partitioning or
clustering; those are documented by Google, not verified through metadata.

## Reproduce

Default operation is a no-charge dry run using existing runtime dependencies:

```sh
scripts/with-runtime.sh python scripts/wn3_bigquery_point.py \
  --project aviation-486817 \
  --run 2026-09-19T12:00:00Z \
  --valid 2026-09-20T12:00:00Z \
  --array temperature_2m_mean
```

Omit `--array` to query all 39 columns. Repeat `--valid` for multiple hours.
Use `--table PROJECT.DATASET.TABLE` if a linked dataset is later configured.
`--execute` enables a real query but retains the default 1 GiB hard billing cap.
`--maximum-bytes-billed` explicitly changes that cap. An execution with
`--compare-zarr` downloads the corresponding global planes and compares grid,
source units, and raw values (relative tolerance 1e-6, absolute tolerance 1e-8).
Use a single field/hour first to bound Zarr downloads. Output is JSON on stdout;
it includes job statistics and raw forecast values, never credentials. Store
results privately, not under the public site directory.

The script rejects absent/duplicate hours, wrong initialization/lead/grid,
nonfinite/out-of-range values and reversed percentile bands. It is a research
probe, not an adapter implementing production freshness or source envelopes.

## Pruning investigation

The underlying table's metadata endpoint returns 403. The accessible view is
a plain `SELECT *` with no transformations. Execution plans confirm the
initialization and `ST_DWITHIN(geography, constant_point, 100)` predicates are
pushed through the view to the underlying table's READ stage.

Sixteen dry-run variants were tested. The jobs API labels every estimate
`UPPER_BOUND`, not an exact scan prediction. Selected results:

| Temperature-only query variant | Estimated bytes |
| --- | ---: |
| Exact initialization + point + one valid hour | 240,567,494,400 |
| Same query with no initialization filter | 66,840,988,464,000 |
| Empty future initialization | 0 |
| Date filter covering the same initialization day | 240,567,494,400 |
| Literal rather than parameterized point | 240,567,494,400 |
| Add LIMIT 1 | 240,567,494,400 |
| Return 168 leads, filtering on forecast.hours | 240,567,494,400 |
| All leads, reading temperature without time/hour columns | 123,551,481,600 |
| No spatial filter, same init and valid time | 62,553,772,800 |

Partition pruning is demonstrably effective. The smaller no-spatial estimate
reflects omitting the geography column; it is NOT evidence that removing the
spatial filter is cheaper in actual execution. Filtering repeated forecast
hours does not reduce the estimate, whereas removing referenced nested
columns does. This supports fetching required hours together and selecting
only necessary statistics. It does not prove every weekly query has the same
actual scan size. The two-day test included an unpopulated future day and is
not evidence of partition granularity.

Two uncached live executions measured spatial pruning:

| Query | Estimate | Actual processed | Billed bytes | Cap |
| --- | ---: | ---: | ---: | ---: |
| Grid coordinates only | 6,535,468,800 | 208,224 | 10,485,760 | 8 GiB |
| Temperature with init/grid/valid-time provenance | 357,583,507,200 | 10,619,424 | 11,534,336 | 384 GiB |

Both processed one partition and returned one point. Job timeline durations
were approximately 2.36 and 2.96 seconds; these are not end-to-end application
latency benchmarks. The temperature's processed bytes were about 33,673 times
smaller than its estimate. At the published US on-demand rate of $6.25/TiB,
the two jobs' combined 21 MiB billing usage is about $0.000125 before any free
tier/credits. This is a rate calculation, not an inspected billing invoice.
The maximum possible costs allowed by the two caps were about $0.049 and
$2.344. The command-line probe's default remains 1 GiB.

Audit job IDs (project `aviation-486817`, location `US`):

- Coordinates: `job_mtY2-4aMTwR2EKde3Q6bvNyk9zg6`
- Temperature: `job_PmbDMPpSndAB3E_WRB2fMvSV88SK`

Temperature parity passed: both BigQuery and Zarr returned
`289.37384033203125 K`, zero difference, at the same initialization and valid
hour above. The Zarr center is stored with float32 rounding:
`40.900001525878906, -74.29998779296875`; BigQuery reports `40.9, -74.3`.
The parity probe now accepts a 0.00005-degree coordinate tolerance (far below
the 0.1-degree grid spacing); a regression test still rejects adjacent cells.

To reproduce the non-executing estimate matrix:

```sh
scripts/with-runtime.sh python scripts/wn3_bigquery_pruning.py \
  --project aviation-486817 \
  --run 2026-09-19T12:00:00Z \
  --valid 2026-09-20T12:00:00Z
```

## Full-week benchmark

Executed once with query caching explicitly disabled, the same initialization
`2026-09-19T12:00:00Z`, and leads 1..168. Valid times span
`2026-09-19T13:00:00Z` through `2026-09-26T12:00:00Z` inclusive. This is a
fixed-run feasibility benchmark, not a test of latest-run discovery/freshness.

| Measurement | Result |
| --- | ---: |
| Returned hourly rows | 168 |
| Statistics columns per hour | 39 |
| Forecast values | 6,552 |
| Dry-run estimate | 4,804,191,993,600 bytes (4.37 TiB) |
| Explicit execution cap | 4,947,802,324,992 bytes (4.5 TiB) |
| Actual processed | 142,494,624 bytes (135.89 MiB) |
| Actual billed usage | 142,606,336 bytes (136 MiB) |
| Query cache hit | false |
| Partitions processed | 1 |
| Query timeline | 3.16 seconds |
| Wall time, submission through results and job metadata | 3.78 seconds |
| Compact serialized result rows | 298,553 bytes |

Job: `aviation-486817`, `US`, `job_1hTGL8e3ACAtdN_irf2ml_wPItVj`.
Private raw artifact: `var/research/wn3-bigquery-week-20260919T12.json`
(mode 0600, outside public output). Exact requested valid times and columns,
cap, dry-run estimate, job plan and execution statistics are retained there.

All 168 rows passed validation for exact initialization, valid times/lead
hours, unique expected grid location, complete finite values, converted-unit
physical bounds and ordered p10/p90 bands. The validator does not require
means to lie within p10..p90.

Zarr parity also passed: all 39 columns at leads **1, 84, and 168** matched
exactly, for **117/117 comparisons with zero numerical difference**. The
comparison checks source units and grid coordinates as well as raw values.
This is sampled temporal parity, not a comparison of every one of the 6,552
values. The parity check took 49.34 seconds using three workers and the
existing Zarr object cache; this is not a cold-download performance benchmark.
Private evidence: `var/research/wn3-bigquery-week-20260919T12-parity.json`
(mode 0600).

At $6.25/TiB, this job corresponds to about **$0.00081** before credits/free
tier. If this one-run measurement were representative, 120 fetches/month
(four daily for 30 days) would be about **$0.097**, and 720 fetches/month
(hourly) about **$0.584**. These are conditional usage projections, not a
billing invoice or an observed monthly workload. They exclude other queries,
backfills, retries and auxiliary Google Cloud costs. Reuse one locally cached
result per run across reports instead of paying for redundant refreshes.
The 4.5 TiB cap allowed a worst-case on-demand charge of about $28.13 for this
one authorized benchmark; it is not a suggested routine cap without quotas.

## Assessment before the production replacement

BigQuery's full-week point extraction is now measured: all 39 columns across
168 hours are fast and inexpensive for this initialization. Retain Zarr until
the production adapter and operational checks are complete; one run is not a
reliability or publication-latency benchmark. Review parity results alongside
the integrity validation rather than treating shape validation as proof of
cross-transport equivalence.

Keep an explicit initialization filter and a constant spatial predicate on the
documented clustering column `geography`. Avoid wrapping that column in
`ST_X`/`ST_Y` filters or substituting `geography_polygon` without measuring it.
Prefer direct projection/UNNEST over selecting entire forecast records through
window functions. Cache the small local result by run/grid/fields/hours for
both reports. Do not use the dry-run upper bound as a monthly bill prediction;
monitor actual `totalBytesBilled`, retain explicit caps and project quotas.
Google documents that an upper-bound estimate can reject a clustered query
even when its eventual actual scan would fit a smaller cap.

Check publication lag and historical availability separately. Only after
full-field parity should a production adapter introduce BigQuery-specific
provenance, preserve existing completeness/freshness checks, and use cached
point results across both reports. Hourly interim runs require a separate
change because the current collector accepts only six-hourly synoptic runs.

## Production replacement (2026-09-20 UTC)

`kcdw.weathernext3_bigquery` now owns the production transport, run discovery,
validation and point cache; the optional probe imports that same transport.
`kcdw.weathernext3.collect_weather_next3` uses BigQuery exclusively. It fetches
all 360 hourly values for each required column, caches the validated run, and
selects each report's hours locally. The history updater uses the same store
with its seven required statistics columns. Old Weather Lab/GCS history and
Zarr forecast envelopes retain their original provenance and remain readable.
The GCS reader remains available only for research/parity probes.

Query provenance includes table, billing project, job ID/location, original
retrieval time, actual processed/billed bytes and both cache flags. No GCS
object-transfer statistics are fabricated. New narratives link the BigQuery
documentation. Credentials and raw query errors are not published.

Cache records use file locks and atomic mode-0600 writes, are fully validated
on reuse, and are capped at 128 JSON files (under 3 MB per readable entry).
Recent runs refresh after six hours; caller freshness checks always apply.
Discovery is limited to the last 24 hours and synoptic cycles. Only an empty
run allows trying an older cycle, at most two candidates; authorization,
billing, incomplete and corrupt-data failures remain unavailable. History
stops its catch-up batch on these failures instead of repeating costly jobs.

Configuration is documented in README. The installed update/events/history
service units explicitly select `aviation-486817` and an 8 TiB query admission
cap. Discovery has a separate 32 GiB cap (or the lower configured cap).
At the published rate, 8 TiB permits up to $50 for an individual forecast
query; monitor billed bytes and apply project quotas for aggregate limits.
No cloud billing settings, datasets, IAM grants or quotas were modified.

Live acceptance checks:

- Discovered and validated the `2026-09-19T18:00:00Z` run. Latest requested
  cycle was 00Z, so the existing explicit fallback/degraded label was preserved.
- Full 360-hour fetch plus discovery/normalization took 8.97 seconds. The
  report selected 154 relevant hours. Forecast query billed 130 MiB; second
  collection reused the local cache with the original retrieval/job metadata.
- The history adapter returned valid checkride pressure/wind/rain metrics
  for the same run; its seven-column query billed 29 MiB.
- The full offline suite ran 705 tests with the same two failures already
  reproduced before migration: `test_wn3_uses_own_clock_normalizes_units_and_fractional_iso`
  and `test_generation_receives_actual_numeric_summaries`. No new failures
  remained. Additional targeted narrative and WN3 tests passed after the
  source-link change.
- Installed service configuration and restarted the two old GCS-loaded report
  processes at `2026-09-20T03:28:28Z`. Those old processes had been running for
  roughly 48 minutes. Timers remain enabled; history's previous lock-related
  failed state was reset. Fresh report completion/publication is checked
  separately from successful WN3 transport acceptance.
- Both restarted services exited successfully. Checkride publication completed
  at `2026-09-20T03:30:27Z`; daily publication completed at
  `2026-09-20T03:30:54Z`. Their archived snapshots identify the new BigQuery
  source and available WN3 data. Public readback confirmed the checkride's
  BigQuery source link and daily health `status=ok`, `stale=false`, run ID
  `20260920T032828Z.497056`. The checkride retains its broader
  `provenance_unverified` status for the combined report; WN3 availability is
  true in the archived health record. All four scheduling timers remain active.

## Sources

- [Official WN3 BigQuery schema and examples](https://developers.google.com/weathernext/guides/bigquery)
- [WeatherNext listing](https://console.cloud.google.com/bigquery/analytics-hub/discovery/projects/gcp-public-data-weathernext/locations/us/dataExchanges/weathernext_19397e1bcb7/listings/weathernext_3_1a067c1e929)
- [BigQuery cost controls and clustered-table estimates](https://docs.cloud.google.com/bigquery/docs/best-practices-costs)
- [WeatherNext dissemination schedule](https://developers.google.com/weathernext/guides/dissemination)
- [Geography clustering and spatial predicates](https://docs.cloud.google.com/bigquery/docs/geospatial-data#partitioning_and_clustering_geospatial_data)
- [BigQuery on-demand pricing](https://cloud.google.com/bigquery/pricing)


## Raw ensemble availability check (2026-09-20)

Authenticated `tables.get` succeeds for both documented views in
`866962084172.WeatherNext_3`. The 0.1° view contains 116 forecast fields
(time, lead and 114 statistics); the 0.05° view contains 14 (time, lead and
12 statistics). Both have mean/p10/p25/p50/p75/p90 and neither has nested
member records or member identities. Google's current BigQuery guide explicitly
routes the raw 64-member ensemble and pressure levels to GCS/Zarr.

Thus BigQuery supports WN3 ensemble distribution summaries, but not exact
member threshold counts, paired U/V direction counts, joint-event counts, or
memberwise flight-window accumulation distributions. Do not synthesize these
from marginal percentiles. WN2 BigQuery retains the independent member check.
