# Forecast reference

Source contracts, chart behavior, and interpretation rules for the rolling report and dated events. For setup and common commands, see the [README](../README.md). All paths and commands are relative to the repository root.

## NBM station guidance

The collector includes both KCDW [NBM text products](https://www.weather.gov/mdl/nbm_text): **NBH**, the 24-hour hourly product (forecast leads 1–25), and **NBS**, the 72-hour product (three-hourly leads 6–72). It fetches operational NOMADS bulk bulletins, trying the current cycle and up to five prior hourly cycles independently for each product. Bulk downloads are capped at 40 MB each; only the KCDW card (at most 12 KB per product) enters the snapshot. Parsed cards are cached by product/cycle to avoid repeating large bulletin downloads.

Original fixed-width rows, model version, cycle time, source URL, forecast hours, and UTC valid times are retained. Validation checks station/product identity, cycle freshness (at most six hours), time axes, and complete core weather rows. The report displays both products and their cycle times. Product outages are recorded independently; these supplemental sources do not replace the existing forecast/context quorum.

The agent weighs NBM guidance alongside observations, proxy TAFs, and AFD reasoning, consulting the linked version-specific card key for units and special codes. NBH and NBS are products of the same blend, not independent model votes. The agent can research missing guidance at its discretion; NBS time steps do not imply exact two-hour hazard timing.

## WeatherNext 3 integration

`kcdw.weathernext3` reads the official WeatherNext 3 surface-statistics BigQuery view. It discovers published six-hourly runs using initialization and geographic filters, validates all 360 forecast hours, and selects the rolling week and configured event hours (plus three following hours) locally. The 13 aviation-relevant fields retain mean/p10/p90: temperature, dew point, three distinct hourly precipitation products, sea-level pressure, 10 m wind speed and U/V, and low/mid/high/total cloud fraction. Complete point runs are cached under `var/wn3-bigquery` with original job/retrieval provenance, process locks, atomic writes, and a 128-entry bound; recent runs refresh after six hours. Both reports reuse that cache. No production WN3 collection downloads global GCS planes. Archived Zarr envelopes remain valid with their original source metadata. Gaps outside selected hours stay gaps; ceiling, visibility, gust and convection remain unavailable. Collection and pre-publication validation still fail closed.

BigQuery uses the existing allowlisted ADC and `aviation-486817` billing project (`WN3_BIGQUERY_PROJECT` override). `WN3_BIGQUERY_TABLE` selects the official or linked `weathernext_3_0_0_0p1deg` view; `WN3_BIGQUERY_CACHE_DIR` changes the cache path. Forecast jobs have an 8 TiB estimated-byte admission cap (`WN3_BIGQUERY_MAX_BYTES_BILLED`), while run discovery is capped at 32 GiB or the lower configured cap. These are maximum billing bounds, not expected usage: the uncached 39-column/168-hour benchmark billed 136 MiB and took 3.78 seconds. BigQuery's conservative clustered-table estimates required a much larger cap than actual scans. Monitor actual billed bytes and use project quotas for aggregate cost control; the default full-query cap permits up to $50 at $6.25/TiB before credits. See [benchmark and migration notes](../deploy/WN3-BIGQUERY.md). Permission, billing, malformed, partial and stale data failures are unavailable; only an absent run permits trying one older published cycle.

Fresh WN3 leads the model interpretation beyond 48 hours; AIFS-ENS is the independent comparator/fallback, with WN2 retained only for independent native member diagnostics. A failed/stale source is unavailable, not favorable weather; it cannot satisfy the short-term publication quorum. WN3 supplies cloud fraction and U/V wind components, but not ceiling, visibility, gust or convection. Its percentiles are not standard deviations or flyability probabilities, and summed hourly percentiles are not event-total percentiles. Other sources still supply essential aviation context.

The owner-facing forecast intentionally includes numerical WN3 guidance: rolling analysis receives full-week hourly mean/p10/p90 summaries, while the checkride page combines models on shared per-variable charts, with expandable WN3 dew-point and total-cloud charts and an event-window numerical summary. Ensemble bands are visible by default; model and band toggles remain available. Comparison axes use the same timestamps and units; WN3 mean/p10–p90, conventional ensemble median/p10–p90 and WN2 mean/±1 SD remain explicitly distinct. Hourly precipitation amounts use member-derived fans, not rain-member fractions or cumulative percentile sums; older archives without those fans omit the unavailable series. Qualitative-only filtering is not enabled. Forecast arrays and credentials are not exposed through a new raw-data API. The existing report URL is publicly reachable (not access-controlled); access settings are unchanged. Google attribution, experimental-use notice and source terms remain linked.

## Interim WN3 initializations

The rolling and event refreshes also collect the newest published interim hourly
WN3 run as a separate supplemental source. These non-00/06/12/18Z runs have
**48 forecast hours from initialization**, so they cannot replace the 15-day
six-hourly run. The collector retains all 13 existing surface fields plus 100 m
wind, each with mean/p10/p90, at KCDW's 0.1-degree point. Complete 48-hour runs
share the bounded BigQuery cache; missing or invalid runs remain unavailable.

Rolling narrative evidence uses only covered hours. The event page and narrative
identify full, partial or absent flight-window coverage; a run ending before the
checkride contributes no checkride weather values. The existing full-range
charts and readiness policy retain the six-hourly source. Interim runs are the
same model, not independent confirmation; 100 m wind is not a gust estimate.

To gather several recent interim initializations for a comparison:

```sh
scripts/with-runtime.sh python scripts/wn3_hourly_point.py \
  --output var/research/wn3-hourly --limit 5
```

This executes bounded BigQuery queries and saves each run with its original
query/retrieval provenance. Google targets interim BigQuery availability at
initialization plus 7 hours 25 minutes; publication can vary. See the
[dissemination schedule](https://developers.google.com/weathernext/guides/dissemination).

## Weekly model ranges and official outlooks

The rolling seven-day page includes six shared charts (rain, sustained wind,
low cloud, pressure, temperature and gusts). GEFS, ECMWF ENS, AIFS-ENS and GEPS
show p50/p10–p90; WN3 shows mean/p10–p90 including low-cloud fraction;
operational GFS is a deterministic line without a band. Each source is collected
and revalidated independently. Ranges default on, charts pan together, y-axes
stay fixed, and touch/small-screen tooltips remain disabled. Today, Now and Full
week controls navigate the collection-date-anchored week, including DST.

`weekly_guidance` is supplemental snapshot data, not an aviation-readiness
source. Large plotted arrays stay out of the analysis prompt. UTC instants are
matched across source timestamp encodings (`Z` and `.000Z`), preserving real
gaps rather than fabricating coverage.

The root page exposes WPC/NHC/SPC navigation and a full-width regional context
section. SPC adds eight independent dated GIS outlooks, KCDW polygon screening,
and a collapsed national Day 4–8 discussion. Empty/malformed/stale data is
unknown; explicit Potential Too Low and Predictability Too Low remain distinct.
SPC severe-weather area probabilities are not airport rain or flying odds.
Existing WPC/CPC and NHC products retain their own validity and scope, and the
North America cyclone-map filter remains in force.

## Snapshot-bound model initialization times

New event snapshots opt into `initialization_provenance_version: 1`. The visible
model-times section and narrative evidence distinguish verified response-bound
initialization, latest advertised initialization, source availability, and a
likely older initialization for forecast times beyond a short IFS cycle's horizon.
A likely cycle remains explicitly inferred/unverified, never an exact attribution.
Retrieval time is not initialization. Rendering uses only that snapshot's metadata;
missing/stale metadata remains unknown and cannot satisfy weather readiness.

## Forecast history and full date range

**Currently switched off for charts** (`CHART_HISTORY = False` in `kcdw/event_renderer.py`): saved forecasts for hours already past doubled the chart width and added about 200 KB of faded series without informing the event, and pushed the page over the publisher's size cap. Charts now run from the collection day through the checkride and its following day; run-to-run change is shown by the scorecard and the trend sections. The snapshot still carries the bounded history packet, and the behaviour below applies when the switch is on.

The event's shared chart domain begins at the earliest usable saved forecast valid time, rather than a fixed calendar start or an initialization date. Earlier archived forecast values are clearly styled and labeled as historical forecasts, not observations or a single continuous model run. Current guidance wins overlaps; missing hours stay missing. Each refresh rebuilds this bounded historical prefix from the saved event snapshots and archives it with the current page. Archive selection reserves older and date-spread samples before newer snapshots; file, enumeration, read and output budgets are explicitly reported as sampled coverage when reached. Source values still use latest collection precedence among selected archives. Forecast valid times must be whole UTC hours, while collection timestamps retain their clock seconds. Historical plot values do not enter the current narrative evidence or satisfy aviation readiness.

All forecast charts keep synchronized dates and fixed y-axes, including RH and WN3 detail. The default view stays centered on the checkride. **Full date range** includes the available history; **Today** targets the collection day rather than jumping to the beginning of that history.

## Low-cloud analysis

The dated-event page's **Low cloud** section refreshes with the hourly service and
feeds the live agent narrative. Native GFS provides instantaneous three-hourly
ceiling heights above colocated model terrain and a 25-cell neighborhood screen.
Each downloaded message is bound to initialization/valid time with ecCodes and
verified HTTP byte ranges. Missing ceilings remain unknown. A run/event cache
reuses actual retrieval timestamps for up to 12 hours, then refreshes.

Independent rolling IFS/AIFS Single profiles show surface temperature/dew point,
wind, 925/850 hPa RH and thermal caps. GEFS/ENS/AIFS-ENS screens count actual
members at opening/noon/closing-minus-one-hour, including the same members passing
all three samples. Those are descriptive cloud/moisture counts, not ceiling
probabilities or continuous persistence. These signals do not alter readiness.

Native decoding runs in an isolated, persistent optional environment; create it
from the repository root on a new host:

```sh
uv venv --python /usr/bin/python3 var/native-weather-venv
uv pip install --python var/native-weather-venv/bin/python 'eccodes==2.43.0' 'requests==2.32.5'
```

The bounded decoder fails independently if this environment or a source is
unavailable. Do not commit `var/`, downloaded GRIB data, or narrative artifacts.

## Event surface and low-level humidity

The event page includes four synchronized RH charts: 2 m, 1000 hPa, 925 hPa and 850 hPa. NCEP GEFS, ECMWF ENS and AIFS-ENS show separate member medians and p10–p90 bands, enabled by default, with available-member counts. GFS operational, ECMWF IFS and AIFS Single supply deterministic lines without synthetic bands. Charts use real 3-hour samples while retaining hourly hover values and gap boundaries. Model and range controls do not change the fixed percentage axes or shared event-centered time navigation.

`kcdw.event_moisture` and `kcdw.event_moisture_ensemble` collect explicitly selected models during each hourly event refresh. RH is bounded to 0–100%; missing hours remain gaps. Pressure-level RH is masked below each model/member's own surface pressure, before ensemble quantiles. AIFS Single is not AIFS-ENS; RH spread is not a ceiling-probability forecast. Rolling API metadata is retained separately from retrieval time, without claiming immutable run binding. Supplemental RH does not satisfy aviation readiness. Compact event-window samples and ensemble quantiles are supplied to the Claude Code weather narrative, not raw member arrays.

## Confirmed appointment versus forecast context

`events.json` may contain a `timings` map bound to each event's slug/date. The
commercial checkride's oral portion was completed September 24; the flight portion
starts at **14:00 EDT on October 1 (confirmed)**, with flight expected
**around 14:00–16:00 EDT**, with an expected duration of **2 hours**. Start and end remain approximate, not confirmed. Each hourly snapshot archives its own
validated timing; older snapshots do not inherit newly confirmed times.
The page header and narrative evidence distinguish those statuses. Narration focuses
on expected departure and the flight, without assuming late-afternoon flexibility.
The existing 08–17 shaded window and numerical/history statistics are retained
as **forecast context**, not a confirmed flight duration or departure time.

## Event wind analysis

Hourly event snapshots collect supplemental wind evidence for the **expected flight window**, separately from the broader chart context. The **Wind & runway considerations** section uses scrollable tables for:

- NWS KCDW grid speed, gust and true direction at departure through return, with explicit update/retrieval clocks and interval coverage.
- GEFS, IFS ensemble and AIFS ensemble actual-member speed/direction/gust packets, with sampled member maxima, gust-threshold counts and independently paired runway crosswind screens. Missing gust fields remain unavailable.
- Initialization-bound native GFS/IFS/AIFS surface, 925-hPa and 850-hPa wind profiles, with actual valid times and gust interval semantics. Native caches preserve retrieval clocks; decoding uses the existing isolated ecCodes environment.
- Recent distinct wind/gust values at a fixed flight-window sample from bounded saved snapshots. Repeated values and advertised metadata changes are not independent new cycles.

Crosswind screens use true runway headings (4: 030°, 10: 083°) and assume each member's mean wind direction applies to its gust. They do not resolve actual gust direction, establish runway availability, or set aircraft/pilot limits. Threshold shares are not calibrated probabilities. Pressure-level samples are not exact AGL heights; bracketing samples and vertical wind differences do not establish flight-hour turbulence or LLWS. Native GFS instantaneous gust and IFS interval maxima are not interchangeable.

The sources are separately citable by the hourly narrative, fail independently, and do not alter readiness. Older archives gain no live wind evidence. Lossless column layouts compact existing moisture evidence within the unchanged 60 KB narrative budget; the HTML publication cap also remains unchanged.

## Direct native run discovery

New native GFS ceiling and GFS/IFS/AIFS Single wind packets use NOAA and ECMWF
Open Data directly, including **run discovery**. `kcdw.native_runs` checks a
bounded newest-first set of synoptic cycles no more than 24 hours old, with a
36-second process timeout covering slow HTTP streams and worker shutdown. Every
required sample's native index must contain the correct run, lead and fields;
the isolated worker then independently validates the actual GRIB messages and
HTTP byte ranges. It does not wait for Open-Meteo metadata or require that the
corresponding rolling API source succeed. An incomplete/unavailable cycle can
fall back to an older eligible index set; failed GRIB decoding remains unavailable.
IFS 06/18Z horizons cannot supply a seven-day target; current IFS streams use
`oper`, including 06/18Z. Inside 144 hours ECMWF publishes the gust as `10fg3`, a three-hour maximum, and the
six-hour `10fg` only beyond that; the wind worker reads `10fg3` at the sample step and at the step three
hours earlier, identity-checks both, and reports their larger value as a six-hour maximum (or an explicit
three-hour maximum when the earlier file is unavailable). Before this, native IFS gusts silently went
missing as soon as an event came within six days. AIFS Single's six-hour steps extend to 360 hours.

Native packet version 2 is validated solely against its persisted source run,
mission and collection clock, without network access during rendering. Legacy
version 1 archives retain their original metadata-bound validation. Discovery
runs at every collection before reuse of a same-run cache, and cache reuse never
renews the retrieval clock. Separate native wind rows in the initialization table
identify NOAA/ECMWF direct provenance. These rows **do not relabel** the existing
Open-Meteo chart, humidity, cloud-profile or ensemble packets as a newer run.
ECMWF Open Data remains subject to CC BY 4.0 attribution and ECMWF Terms of Use.

## Direct-first chart and humidity collection

Production event clients explicitly enable `direct_native=True`; ordinary clients
and legacy fixtures retain their original behavior. Operational GFS charts and
GFS/IFS/AIFS Single humidity profiles first request NOAA/ECMWF GRIB data across
the entire current chart range. Validated deterministic humidity proofs also feed
the cloud-layer diagnostics without another download. Production dated-event
clients also explicitly enable `direct_ensembles=True`: GEFS, ECMWF ENS, AIFS-ENS
and GEPS chart/RH adapters read only verified complete native run caches, with
Open-Meteo/eligible whole-packet retention as fallback. Ordinary clients default
to `direct_ensembles=False`, independently of `direct_native`, so supplemental
paired-member runway-wind collection retains its separately labeled source.
The live WeatherNext 2 mean/spread comparator is retired in favor of WN3; archive readers retain its original identity. A native chart does
not relabel other source packets.

Native data carries its actual supplying initialization, provider, grid and
sampling description. Open-Meteo fallback stays explicitly identified; a direct
failure never makes a rolling response run-bound. Native ensemble candidates must
cover every future plotted hour for pressure, sustained wind and rain (or surface
and 850-hPa RH for humidity charts) before replacing a full time series. Event-only
packets remain useful private native evidence but do not displace the lead-up.
If a new chart collection fails or loses coverage, a bounded archive scan can
retain a compatible complete packet no more than 12 hours old, with its original
collection clock and a content-bound visible note. Complete native core fields
also cannot erase an optional gust/cloud/temperature series present in the saved
full-range source. Retention copies a whole model packet (or RH envelope), never
splices different runs or relabels saved Open-Meteo data as native. Repeated use
never renews the original retention age. These archive reads occur only during
collection, not rendering; retained-source clocks also reach narrative evidence.
Native ECMWF ENS uses the available 50 perturbations, not an invented deterministic
control; AIFS-ENS has 51 members and GEFS 31. Missing native gust/low-cloud fields
remain unknown.

Instantaneous native samples are explicitly interpolated for hourly alignment;
no extrapolation or long-gap bridging is permitted. Native accumulated rain is
uniformly allocated over its proven accumulation interval for display and window
arithmetic: this is an interval-average estimate, **not native hourly timing**.
Source/grid/cadence changes are disclosed in saved-snapshot comparisons. Native
and fallback evidence do not add extra model votes or change aviation readiness.

Workers run in the optional ecCodes environment under hard subprocess deadlines.
Run/valid-time/field/level/unit/grid/range checks precede normalization. Cached
points retain original collection and retrieval clocks. Rendering validates only
persisted evidence, with no native discovery or downloads. Snapshots distinguish
collection start from completion; old archives receive no new native labels.
Private proofs/caches stay local, and the 60 KB narrative and 800 KB publication
caps remain unchanged.

### Resumable complete-run ensemble cache

`direct_ensembles=True` now reads committed private run manifests; it does not
launch GRIB downloads from a page refresh. Missing, stale or incomplete caches
enter the existing explicit Open-Meteo fallback/whole-packet retention path.
The independent producer is:

```sh
var/native-weather-venv/bin/python -m kcdw.native_ensemble_cache \
  --models gefs geps ecmwf_ens aifs_ens --seconds 2700 --workers 12
```

The default range follows upcoming event display endpoints; `--end` can supply an
explicit exclusive UTC endpoint. A producer-wide flock prevents duplicate work.
Point downloads are resumable, partial progress is never promoted, and an atomic
packet/hash manifest commits only a complete member × supported-field × native
lead matrix. Newer incomplete cycles do not displace eligible completed cycles.
The cache reader performs no network access and preserves all original clocks.
Each model receives an independent hard process budget; useful incomplete cycles
resume rather than being abandoned at every new cycle. Parent shutdown reaps the
active worker before releasing the global lock. Incomplete upstream catalogs are
invalidated in the worker-owned cache so the next attempt sees publication progress.
Systemd service/timer templates live under `deploy/systemd` (15-minute schedule).

GEFS combines `pgrb2ap5` core fields with `pgrb2bp5` gust/low-cloud data. ECMWF
uses its official Google Cloud replica with origin index fallback and identical
GRIB identity checks. GEPS downloads ECCC grouped-field files containing all 21
members. Persistent per-thread HTTP connections and validated index caches avoid
repeated connection setup and index/HEAD requests. Raw global files are not kept.

Native GEFS gust is instantaneous and its low cloud is an interval average.
IFS gust samples are maxima over actual source intervals; interpolation does
not create hourly gust maxima. AIFS low cloud is native percent, not a fraction.
No native IFS low cloud, AIFS gust, or GEPS gust/low cloud substitutes are invented.
The whole-packet retention safeguard may therefore keep an eligible Open-Meteo
IFS chart packet to preserve its low-cloud curve, while IFS RH uses native data.
GEFS precipitation is a native interval amount; IFS, AIFS and GEPS cumulative
precipitation is differenced within the same member before disaggregation.
Where cumulative totals decrease, collection retrieves SHA-bound GRIB packing
precision only for the affected pairs. Differences within combined packing error
are zero increments; larger decreases remain unknown, with true per-hour sample
counts and `rain_unknown_member_hours` metadata. Raw totals and clocks are unchanged.
Pressure-level RH is masked below the same member's surface; supersaturation
remains intact. Complete-run versioned metadata leaves historical packets' source
and sampling contracts unchanged.

## Event model scorecard

`kcdw.event_model_matrix` adds one dense table directly under the event briefing: the latest run of ECMWF IFS, ECMWF AIFS,
NCEP GFS, DWD ICON and UKMO Global for the **expected flight window** (falling back to the event window without timing).
Every row comes from Open-Meteo's single-runs API with an explicit `run=`, so values are bound to the requested cycle; a
bounded newest-first search (eight six-hour candidates, 120-second deadline, one 45-second pause-and-retry for rows that hit a rate limit, timeout or error, since this refresh shares Open-Meteo's per-minute allowance with the ensemble collectors) takes the newest run whose hourly fields cover
the window plus the covering run before it, which yields a same-window "vs previous run" change. Short cycles that stop
before the event (ICON/IFS 06/18Z) are skipped rather than relabeled. Because a run-pinned result never changes, results are cached per (model, run) in `var/events/<slug>/model-matrix-cache.json` (reset when the window, thresholds or packet version change): each refresh only probes for newer runs, two requests at a time, and an upstream outage or rate limit serves the cached rows with their true run age instead of dropping the section. A run that answers but stops short of the window is negatively cached only once it is 12 hours old, so a partially ingested cycle is retried. CMC GEM is omitted because that API currently fails
for it.

Columns are mean low-cloud cover, an estimated cloud base (lowest hourly 2 m temperature–dew-point spread × 410 ft/°C — a
mixing estimate that can miss inversion-trapped stratus, never a ceiling), vector-mean wind with the peak hourly gust, window
rain, the run-to-run change and a fixed-threshold screen (rain ≥ 1 mm; overcast ≥ 80% split at a 3,000 ft estimated base;
broken ≥ 50%; gusts ≥ 25 kt demote a favorable row). The headline and summary sentence are deterministic counts and ranges,
and the briefing's *Maneuvers ceiling* card reports how many models keep low overcast. Screens are planning thresholds, not
probabilities, votes or a go/no-go decision. The persisted packet is revalidated before rendering (run bounds, value ranges,
recomputed screens and changes); an absent or invalid packet renders nothing and leaves the card at "Not resolved".

### WeatherNext 2 member screens

The expandable WN2 check uses all 64 members from the official BigQuery table
`871883017250.WeatherNext2.weathernext_2_0_0`, bound to its actual `init_time`.
It shows native six-hour wind-direction counts, 10 m/100 m wind and pressure
quantiles, and rain counts over the complete six-hour intervals enclosing the
flight. Those rain totals are not estimates for the shorter flight window.
The table has no cloud-cover field; primary cloud guidance comes from WN3.
Small negative neural rain predictions are clipped to zero after raw bounds
validation. All 64 distinct member IDs, exact times, grid and physical ranges
are required. Invalid packets render nothing. Raw query results and job usage
are retained in a bounded, locked local cache; rendered packets retain provenance.

The daily WN2 mean/spread feed and event hourly comparator are retired in favor
of WN3. Legacy collectors/validators remain for tests and archived reports;
old WN2 data is never relabeled WN3. See [WN2 BigQuery](../deploy/WN2-BIGQUERY.md).

## Event NWS forecaster readings

Hourly event updates independently collect the latest NWS AFDs from **OKX**
(local), **PHI** (south) and **ALY** (north). The snapshot stores each actual
product, issuance time and retrieval time. Bounded discussion/aviation excerpts
are revalidated for narration and displayed under **Regional AFD readings**,
with links to the exact issued products. Missing or stale offices remain
unavailable without removing the others or changing aviation readiness.

The agent narrative combines forecaster reasoning with model scenarios, respects
each bulletin's stated forecast periods and distinguishes preceding-pattern
context from direct event-date guidance. Nearby offices are geographic
perspectives, not independent model votes; no AFD excerpt is treated as a KCDW
TAF. Existing snapshots without AFD collection do not inherit today's text.

## Event weather narrative

The event page leads with a **Flight assessment** and **Next check**, generated by Claude Code (`claude-opus-5-5`, medium effort, no tools) from a newly collected event snapshot. Narratives archived from the earlier Codex provider remain renderable and are labeled as such. A bounded evidence packet combines validated point guidance, event-window ensemble statistics, matched-cycle trends, regional cyclone samples, and current NHC/CPC/WPC context. The agent explains the consequential weather scenario and operational implications, separates central guidance from adverse tails and conditional mechanisms, and identifies evidence that would change the outlook. It does not turn low-cloud cover into ceiling height or model spread into flight-completion odds.

`kcdw.event_narrative_evidence` selects the source evidence; `kcdw.event_narrative` constrains the response to a JSON schema, binds it to that snapshot, and renders escaped prose with source links and generation time. The CLI has a bounded deadline and no need for tools or independent browsing: its only weather input is the fresh evidence packet. Local generation artifacts remain under `var/events/<slug>/narratives/<run-id>/`, outside the published bundle. Missing, failed, stale, or mismatched narratives do not masquerade as current analysis; deterministic charts can still publish.

`kcdw-flyability-events.timer` now refreshes guidance and the narrative hourly at **:25 UTC**, with up to three minutes jitter. The separate **:10 UTC** run-history poll remains enabled and can trigger an additional full refresh when actual new cycles arrive. Both serialize through the existing event lock. Event and history services need the installed Claude Code CLI on PATH and its existing local state directory writable, just as the main weekly-report service does.

The narrative also compares the same event window with a saved forecast collected 6–18 hours earlier, preferring comparable source coverage and then an approximately 12-hour interval. It explains GFS cloud/RH reversals or persistence, independent ensemble cloud and wet-side rainfall changes, and competing deterministic moisture signals alongside genuine WN3 initialization history. These are forecast-snapshot changes, not observations or exact rolling-model initialization trends. Historical sources are validated at their original collection clocks; current pairs are revalidated and bound to the current snapshot. Missing comparisons do not block fresh guidance. Archive scanning and reads are bounded independently; the 60 KB narrative input budget compacts redundant hourly detail before dropping whole source summaries.

## Forecast chart navigation

The dated-event collector now fetches from the collection day's Eastern midnight through the day after the checkride. All six multimodel charts and the two supplemental WN3 charts share that exact valid-time axis. Their horizontal viewports pan together, while each y-axis remains outside the scroller. The initial view centers the provisional checkride window; Today, Center checkride, and Full date range controls reposition all forecast charts. Historical initialization/fetch-time diagrams remain separate because their x-axes represent different quantities.

`kcdw/assets/forecast-navigation.js` is a small dependency-free, network-free controller. Its exact inline content is authorized by a generated CSP SHA-256 hash; arbitrary inline scripts remain blocked. Compact per-series numeric attributes preserve hourly hover values without a DOM node for every sample, keeping the expanded report under the publisher's HTML cap. The static SVGs, fixed y-axes and native scrolling still work without JavaScript; synchronized panning and initial centering require the controller. Persisted legacy centered-range snapshots remain valid independently of the current date.

## Fixed-event ensemble trends

The event updater rebuilds `snapshot.ensemble_trends` from bounded local event archives and persists it before rendering/publication. Trends compare the **same event date and window**, not a moving forecast lead time: pressure and sustained wind use the fixed midpoint-hour sample (12:00 Eastern for the provisional 08–17 checkride), while rain uses complete preceding-hour intervals 09–17. Conventional rainfall bands describe member-total quantiles; WN3 rain is a sum of means with no invented total-percentile band; operational GFS has no ensemble band.

The trend view retains up to eight distinct updates per model over 72 hours. Repeated unchanged guidance is deduplicated. WN3 has response-bound initialization; other models are rolling retrievals with advertised provenance, not immutable cycle attribution. Historical snapshots are validated at their collection time; current availability is checked at render/build time. Missing sources cannot silently become current last-good guidance. GFS pressure-gap changes compare paired values from the same archived snapshot, while acknowledging that model cycles within that snapshot can differ. Trend history is supplemental and cannot satisfy aviation readiness.

### Recovered initialization history

`var/events/<slug>/backfill.json` holds initialization-indexed cycles, loaded by `kcdw.run_history` on each event refresh and persisted as `snapshot.ensemble_run_history`. Initialization-time charts lead the Trends section; earlier page-fetch history remains in a collapsed disclosure. The original backfill is retained and extended automatically with WN3 and operational GFS runs. Every record keeps its real retrieval time; older report snapshots are never rewritten.

`kcdw-flyability-run-history.timer` runs `python3 -m kcdw.history_update --var var` hourly at minute 10 UTC (up to one minute jitter). It checks six-hour initialization cycles after an initial five-hour availability delay, retries gaps, and bounds each catch-up batch to eight missing cycles per model. WN3 uses validated archived records first, then initialization-bound BigQuery queries with a local complete-run cache. GFS uses the run-selected archive API. Up to 64 cycles per model cover the event horizon without the old 16-run rolling cutoff. Weather-only originals remain in `backfill-sources/`; per-cycle failures/check status are in `run-history-status.json`.

The hourly job shares `var/events-update.lock` with the hourly forecast/narrative updater. Only changed history (or a previously failed publication) triggers an additional fresh forecast collection and publication; unchanged history checks do not recollect all models. Successful publication is acknowledged in `var/events/run-history-publication.json`; errors remain retryable. Both models' run histories remain independent of the aviation-readiness quorum. Install the matching service/timer from `deploy/systemd/` and enable the timer with `systemctl --user enable --now kcdw-flyability-run-history.timer`.

WN3 cycles use bounded BigQuery queries with requested/returned initialization equality and preserve table, job, usage and retrieval provenance. Operational GFS cycles use Open-Meteo's documented `single-runs-api.open-meteo.com/v1/forecast`, exact `models=gfs_global`, and `run=`. Request the full `forecast_days=16` horizon and select target hours locally: the live archive endpoint rejects `start_date`. Preserve response/request provenance under `backfill-sources/`, validate units/grid/time axes, reject invalid-run/model controls, and label GFS initialization as archive-request-bound rather than response-echoed. Compare pressure gaps only at identical initialization and valid times. Do not interpret unavailable conventional ensemble archives, HTTP-200 streaming error bodies, or `past_days` stitched forecasts as historical member runs.

## Atlantic cyclone map

Both reports restrict WN3 displayed tracks and proximity/member counts to the Atlantic, including the Gulf and Caribbean. Explicitly non-Atlantic storm identifiers are excluded; numerical genesis tracks use an approximate geographic boundary through Central America, not an official basin classification. Old snapshots are re-filtered when rendered. Real public-domain Natural Earth 1:110m land outlines are bundled in `kcdw/assets/atlantic-land.json`, with pinned source and checksum. Land, tracks, and labels use the same plate-carree projection; no runtime map service is required.

## Operational GFS comparator

The event charts include a default-on **GFS operational (deterministic)** dark line on the same six variable axes. The request explicitly selects `gfs_global`, not GEFS control, an ensemble median, or a seamless HRRR blend. There is no GFS ensemble band and it is not counted among ensemble members or used to replace the preferred WN3/AIFS policy. Rain sums the preceding-hour endpoints 09–17 Eastern; sampled wind/gust maxima use 08–16. Gaps and stale/unavailable GFS stay explicit. After forecast hour 120, Open-Meteo interpolates native 3-hour GFS data hourly; this is not added timing skill. Latest advertised dataset metadata is provenance, not an immutable cycle binding for rolling point values.

## Operational event-page layout

The checkride page starts with one assessment and one next check. A valid snapshot-bound narrative supplies them; missing or stale narratives use the deterministic briefing. Full-day model statistics and NWS forecaster excerpts are expandable. Sustained-wind statistics are labeled separately from flight-window runway crosswinds.

Cloud, ceiling and humidity evidence share one group, followed by wind and runway guidance. The model comparison contains six shared charts ordered rain, wind, low cloud, pressure, temperature and gusts, with ensemble ranges on by default. WN3 already appears on those charts; only dew point and total cloud need additional charts. Model-run trends, supplemental WN3 fields, WN2 member checks and regional outlooks are expandable. Source clocks, methodology and licenses are collected under Notes & sources.

New event narratives target 250–350 words: a short lead, three sections (Cloud & visibility, Wind & rain, What changed), and at most two concrete next checks. The generation schema enforces character limits of 120 for the headline, 420 for the lead and next check, 64 per section heading and 600 per section body. The lead and next check remain visible; reasoning and citations are expandable. Older archived prose remains readable under its original validation limits.

The rolling outlook keeps day categories, narratives and hazards visible, combines window reasons and confidence into one disclosure per day, and places assessment changes before model charts. New prose prioritizes useful windows and controlling hazards. Neither report changes scoring, weather evidence or aviation-readiness rules.

## Long-range ensemble policy

- **WeatherNext 2:** BigQuery native diagnostics, 64 members, 0.25° grid, six-hour steps, 15-day horizon, four cycles daily. Independent member diagnostics only; primary Google guidance comes from WN3. The former Open-Meteo mean/spread feed is archive-only.
- **ECMWF AIFS-ENS:** `ecmwf_aifs025_ensemble_mean`, 51 members, 0.25° dissemination grid, native 6-hour steps, 15-day horizon, four cycles daily. It is the independent comparison and fallback model guidance.
- The adapter retains ensemble **mean and spread together** for temperature, precipitation, low/mid/high cloud fraction, 10 m wind, mean sea-level pressure, and weather code. Open-Meteo interpolates native 6-hour values to hourly steps; this does not create real 2-hour timing skill. The feeds do not provide aerodrome ceiling, visibility, gust, lightning, or deterministic convective timing. Low-cloud fraction is not a ceiling.
- Agreement can support confidence. Disagreement is disclosed and lowers confidence; the models are not blindly averaged. Missing model data lowers confidence but does not mechanically lower a flyability score.
- The rolling point response does not carry an immutable run identifier. The snapshot therefore records the same API host's latest advertised initialization, modification, availability, and data-end metadata and states that binding limitation explicitly.
- Both collection and pre-publication validation reject non-finite values and values outside deliberately broad corruption guards: temperature -120..80 °C; temperature spread 0..100 K; precipitation and its spread 0..50 inches per returned hour; cloud fraction and spread 0..100%; wind speed and spread 0..300 knots; wind direction 0..360°; mean sea-level pressure 750..1150 hPa; and pressure spread 0..150 hPa. Weather codes must be integers in Open-Meteo's supported WMO code set. These are input-integrity limits, not operational aviation thresholds.

WeatherNext 3 is Google's August 2026 flagship 64-member system, with native hourly initialization, 0.1° gridded surface fields, selected 0.05° station outputs, 15-day synoptic runs, and 48-hour interim runs. Its direct real-time datasets require Google allowlist access and separate experimental-data terms; the configured Google identity can read the official statistics and full-ensemble GCS buckets. The official BigQuery statistics view is the production WN3 source through `kcdw/weathernext3.py`; the former loopback Weather Lab bridge is no longer used. No NWS/AWC/radar precedence rule changes.

`scripts/wn3_bigquery_point.py` is the optional dry-run/point-query probe; `scripts/wn3_bigquery_pruning.py` compares no-charge scan estimates. `scripts/wn3_zarr_point.py` remains a research/parity probe, not a production collector. Install the main `requirements.txt`, then pass an explicit synoptic run, one or more valid hours, and exact statistics-array names. It uses generation-bound whole-object GCS gRPC reads and validates the consolidated Zarr schema, coordinates, codec pipeline, units, decoded size, and finite point value. The upstream arrays are unsharded single-frame Zstd global planes, so every selected point downloads its complete hourly statistics plane.

Open-Meteo API data are used under CC BY 4.0 and are attributed in the rendered report. The free endpoint is appropriate for this private non-commercial site; commercial use requires Open-Meteo's customer service/API key.

## Nearby-office forecast discussions

AFDs are collected independently for New York/Upton (OKX), Philadelphia/Mount Holly (PHI), Binghamton (BGM), Albany (ALY), Boston/Norton (BOX), and State College (CTP). Each retains its office identity, source URL, latest issuance at or before collection time, age, and the complete discussion, bounded to 14 KB with an explicit truncation flag, preserving modern section headings and marine reasoning. Empty or mismatched products are unavailable; one office outage does not suppress the others. Source status displays issuance and fetch times separately.

The agent weighs geographic relevance and weather trajectory toward KCDW when using neighboring discussions. It can research additional offices as needed. The added AFDs supplement the existing local aviation/context requirement.

The NBM v5.0 NBH/NBS station-card key is bundled in `kcdw/references/nbm-v5.0-station-card.txt`, extracted from the official NOAA/MDL page and verified on 2026-09-08. Prompt construction includes it automatically when a supplied NBM card reports version 5.0. It covers missing/unlimited codes, ceiling and visibility units, winds/gusts, probabilities, and the other NBH/NBS fields. The agent can use these definitions offline; different model versions require their own verified reference. Update the bundled key and version selection when upgrading NBM.

## Confidence and ensemble variance

Daily confidence explicitly considers within-model ensemble dispersion, between-model disagreement, source freshness/coverage, official forecast reasoning, and horizon. The existing spread fields are standard deviations (variance is their square); the agent uses native-unit spread alongside the means, emphasizing precipitation, low cloud, and wind during actionable local windows. It does not average unlike units, treat hourly interpolation as independent members, or infer exceedance probabilities from mean/spread alone.

High confidence requires a consistent controlling scenario; medium reflects uncertainty that could change some windows; low reflects materially different plausible flying outcomes. Observations can dominate near-term confidence, while missing spread cannot count as zero uncertainty. Dispersion that does not change the operational verdict need not force low confidence. This is a qualitative assessment, not a calibrated numerical confidence calculation, and variance does not mechanically lower flyability scores. Every day includes a visible `confidence_reason` explaining the controlling uncertainty and the role of spread.

## Dated checkride outlook

[Commercial checkride · October 1, 2026](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride) · [Event history](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride/history)

`events.json` defines dated pages. The checkride's **08:00–17:00 Eastern window is forecast context**, separate from the confirmed 08:00 appointment and expected 10:00–12:00 EDT flight. The independent deterministic `kcdw.event_update` pipeline collects GEFS (31), ECMWF ENS (51), AIFS-ENS (51), and GEPS (21) individual-member distributions plus WeatherNext 3 **mean/p10/p90** statistics and separate run-bound WeatherNext 2 member wind/rain diagnostics from BigQuery. Fresh WN3 is preferred beyond 48 hours, with AIFS-ENS the independent comparator/fallback. WN2 native diagnostics use all 64 individual members; archived mean/spread packets are never converted to member counts or percentiles. Each source can fail independently.

New collections request guidance from the collection day through the day after the event, bounded to at most sixteen days before it. Archived centered-range snapshots keep their original range. Explicit UTC `start_hour`/`end_hour` requests and exact returned-axis validation avoid the UTC-date/EDT-midnight mismatch. The nine-hour precipitation total sums preceding-hour intervals ending 09:00–17:00 Eastern, excluding the interval ending 08:00. Conventional-member window statistics use those same nine endpoints, not continuous extrema. WN3 instantaneous wind, temperature and pressure instead use 08:00–16:00 samples (start inclusive/end exclusive); WN3 rainfall uses 09:00–17:00 interval endpoints. Input validation checks finite nearby grid coordinates, exact contiguous hours, units, physical corruption bounds, exact member IDs, duplicate IDs, missing series and per-hour sample counts. Persisted normalized data are revalidated before rendering and uploading.

The page presents separate model medians/p10–p90 distributions and counts, **not pooled favorable percentages or calibrated flyability probabilities**. Missing cloud is unavailable, not favorable. Point pressure cannot rule out hurricanes, nearby storms, or convection; cloud fraction is not ceiling height. The page now fetches NHC tropical guidance and CPC/WPC context; AWC and local aviation products remain linked to the current report and official briefing sites.

Latest advertised dataset metadata is displayed separately from fetch time. IFS 06/18Z short cycles may not cover the extended rolling response supplied by earlier long cycles; there is no immutable per-value run binding. Event health therefore returns `status: provenance_unverified` (or `stale` when the fetch is over eight hours old), never a current marker derived solely from a successful fetch. This status is intentional, not a publication outage.

```sh
python3 -m kcdw.event_update --no-publish  # live collection and local archive only
python3 -m kcdw.event_update               # publish using private var/cloudflare.json
systemctl --user start kcdw-flyability-events.service
systemctl --user status kcdw-flyability-events.timer
```

The event timer template runs hourly at **:25 UTC**, plus up to three minutes jitter. Actual upstream latency varies. Its service uses `flock`, runs independently of the main hourly report, and stops collecting after the event date. Event archives and logs are under `var/events/<slug>/runs/` and `var/events/update.log`. R2 uses separate `events/<slug>/runs/` and latest pointers; the main report pointer and stored report HTML are not rewritten. Current homepage navigation is augmented from the event index at response time; old reports remain historical. Readbacks verify publication and index state. History is retained; no automated deletion is installed.

Public routes: `/events`, `/api/events`, `/events/<slug>`, `/events/<slug>/health.json`, `/events/<slug>/history`, `/api/events/<slug>/history`, and `/events/<slug>/runs/<run-id>`. Publishing routes require the existing bearer token. `npm --prefix cloudflare test` now builds a fresh dry-run bundle before Miniflare tests, avoiding accidental tests against stale `dist/` output.

## Tropical and extended context

Both the rolling report and dated-event pages collect `synoptic_context`, separately from the aviation-readiness quorum. `kcdw/synoptic_context.py` isolates source failures, renders each source against the exact planning window, and supplies bounded, time-checked summaries to the rolling assessment rather than raw track arrays.

- **WN3 tropical guidance:** public `WNV3` ensemble/cyclogenesis CSV, explicitly WeatherNext 3 Cyclones (r0), separate from the point-statistics feed. A bounded search of six-hour URL candidates confirms response initialization before acceptance. The Atlantic-sector SVG and unique-member counts use only sampled track centers within the mission window. The documented denominator is 64 members; 500/1,000 km center-proximity counts are not landfall, airport-impact or flyability probabilities. Six-hour sampling misses between-point closest approaches, remote rain and remnant impacts. Run/fetch freshness limit: 36 hours.
- **NWS/NHC:** actual Atlantic 48-hour/7-day formation outlook, current-storm inventory, and official track/intensity advisories when Atlantic cyclones exist. Product issuance, valid periods, inventory refresh and retrieval times stay distinct. Current inventory absence is not absence of formation risk. Formation outlooks extend seven days; active-storm track products extend approximately five days. Render-time limits: eight hours for products and three hours for the inventory/retrieval cache.
- **CPC:** 6–10/8–14-day discussions and New Jersey state categories, plus official NOAA GIS polygons intersecting KCDW for temperature and precipitation. The map contour is a period-average tercile category probability, not rain chance or an exact airport probability. Date-only GIS issue metadata is labeled, not assigned a fictitious issuance hour. CPC validity uses inclusive KCDW local dates; maximum issue age is 48 hours.
- **WPC:** medium-range and excessive-rainfall discussions with actual UTC valid windows and Northeast-focused excerpts. These are regional narratives, not a computed KCDW QPF or excessive-rainfall polygon category. Maximum issue age is 36 hours. Missing, expired and out-of-window products never establish an all-clear.

The visible highlights emphasize relevant categories and horizon gaps; full narratives and product-level provenance use native disclosures. `kcdw/context.css` is embedded in both reports; no new JavaScript or remote image dependency is required. Sources refresh with the existing main/event jobs and are retained in each immutable report archive. Tests use synthetic fixtures; live collection and published page readbacks are separate checks.

## Report design

The report and history share the Field Notes design: a seven-day overview, direct links to each day, concise planning picks, two-hour cards, and always-visible hazard/daylight details. Styles live in `kcdw/report.css`; the renderer embeds them into each archived report, while the Worker imports them for history. Subset WOFF2 Barlow Condensed and IBM Plex Sans fonts are bundled in `kcdw/assets/fonts.css` with their OFL licenses, so viewing does not require third-party font requests. Historical reports retain their original presentation.

Health checks read the small R2 publication pointer, which includes the assessment timestamp and freshness threshold, instead of downloading the complete report. Older pointers continue to work through a compatibility fallback. Generated narratives target 180–240 characters to leave room below the enforced 360-character limit.

WN3 collection covers seven Eastern calendar dates plus configured event windows and three following hours. Its scorecard row uses native ensemble means and derives meteorological wind direction from mean u/v components. Gusts are explicitly unavailable in the published WN3 schema; sustained-wind percentiles are not gust estimates. WN3 remains the preferred available model beyond 48 hours, with AIFS-ENS fallback. Production collection reuses complete point runs from the BigQuery cache; global GCS chunks are used only by the optional Zarr parity probe.

### WN3 cloud timing and changes

The event low-cloud analysis includes WN3 low, middle, high and total cloud
coverage at every hour during the expected flight and three hours either side.
It shows hourly mean/p10/p90 and before/during/after mean coverage. These are
marginal hourly distributions, not ceiling heights, clearance probabilities,
whole-window quantile bands or the same members tracked through time. Cloud
layers overlap and are never summed.

A same-window, same-grid comparison shows changes in mean coverage versus the
latest usable distinct earlier WN3 run. Reads are bounded to 64 recent archives
and 32 MiB; missing earlier guidance can be backfilled from the previous six-hour
BigQuery run using the shared cache. Invalid/incomplete comparisons display as
unavailable. The same validated cloud diagnostics feed the event narrative.

WN3 surface moisture and elevated-wind context add derived 2 m RH and 100 m
mean/p10/p90 wind alongside 10 m mean wind for the same cloud-analysis hours.
RH is calculated over liquid water from ensemble-mean temperature/dew point
using the NWS exponential saturation-vapor-pressure approximation, within
−45..60°C, capped at 100%. It is not ensemble-mean RH or an RH percentile.
WN3 925/850 hPa RH remains unavailable through BigQuery; no GCS collection is
introduced. Existing other-model pressure-level RH remains separate.

The 100 m statistics are an optional, separately cached BigQuery query bound to
the primary WN3 run and grid. They describe elevated-wind/mix-down context,
not gust forecasts, gust percentiles or gust upper bounds. Stability and mixing
are not inferred from a single wind level. A missing wind packet leaves derived
surface RH available. The event narrative receives the same explicit limits.
