# KCDW Flyability

[Live report](https://kcdw-flyability.andyfang.workers.dev/) · [Report history](https://kcdw-flyability.andyfang.workers.dev/history)

A Python pipeline that collects current weather evidence for Essex County Airport (KCDW), asks the installed Codex CLI for a strictly structured seven-day interpretation covering today plus the following six days, validates it, and atomically publishes a single self-contained HTML report.

The first three days show two-hour planning categories between 08:00 and 20:00 America/New_York; elapsed windows are omitted at assessment time. Days four through seven show broad daily outlooks. Categories come from internal ordinal scores, not calibrated probabilities, and the display contains no flyability percentages. An open page updates freshness and elapsed/current states automatically; health is recalculated on each request.

## Architecture

1. `kcdw.collector` fetches each source independently with an explicit User-Agent, 15-second timeout, two limited retries, response-size limits, timestamps, and bounded retained fields. It also downloads the six latest NOAA/NWS MRMS quality-controlled base-reflectivity frames for a fixed eastern-US bounding box, rejects a loop whose newest frame is over 30 minutes old, decodes each bounded PNG with Pillow, and records frame validity/ingest times, KCDW's image pixel, nearest displayed-echo geometry, and 25/50 NM local-echo flags. `kcdw.ensemble_guidance` independently fetches 192 hours of version-pinned WeatherNext 2 and ECMWF AIFS-ENS means and standard-deviation spreads, validates exact fields/units/hour axes, and records the models' advertised initialization and availability metadata. Source failures are recorded separately in the snapshot. Publication requires usable forecast coverage through the next two hours and fresh aviation/context evidence. Empty successful responses and metadata alone cannot satisfy readiness.
2. `kcdw.prompt` serializes that deterministic snapshot with stable key ordering, a 500 KB hard limit, and a rubric that treats the snapshot as a starting evidence packet and all fetched text as untrusted data rather than instructions. The agent chooses when live web research is useful to fill gaps, resolve conflicts, or check freshness; there is no fixed research checklist. Supplemental products must be issued by the snapshot assessment time, and material additions are attributed with source, product time, and URL in the narrative or summary. It maps each radar attachment to timestamped metadata and requires loop-based local/regional/upstream interpretation without unsupported precision. Fresh WeatherNext 3 is the preferred *model* guidance after 48 hours for available fields, with AIFS-ENS as an independent comparator/fallback and WeatherNext 2 secondary; neither outranks official NWS forecasts, AFD reasoning, SPC outlooks, or the established short-term aviation evidence hierarchy.
3. `scripts/update_report.sh` takes a nonblocking `flock`, binds each radar attachment to exact consecutive snapshot metadata, decodes every size-capped PNG before use, records the CLI version, and invokes `codex -c 'web_search="live"' exec` non-interactively with the ordered radar PNGs plus `--ephemeral --sandbox read-only --output-schema ... --output-last-message ...`. Files without matching metadata, missing/extra filenames, symlinks, malformed images, and loops outside the two-to-six-frame contract fail closed before Codex runs. OAuth comes from the service user's existing CLI login; no API key is read or stored.
4. `kcdw.validation` applies semantic checks beyond JSON Schema: exact dates/windows, no duplicates, 5-point scoring, length bounds, timestamps tied to the snapshot, and ordered/fresh radar metadata.
5. `kcdw.renderer` HTML-escapes every model/source string. The updater stages the entire run, then atomically switches the current-run pointer only after the whole analysis succeeds. A failed collection, Codex call, or validation leaves the last known-good report untouched.

Data comes from the NOAA/NWS MRMS time-enabled base-reflectivity ImageServer; NWS points endpoint and its hourly/text/grid links; NWS OKX, PHI, BGM, ALY, BOX, and CTP AFD product listings/details; AWC three-hour METAR history, valid proxy TAFs, and airsigmet feed; NWS point alerts; SPC convective outlooks; Open-Meteo best-match extended guidance; and version-pinned WeatherNext 2 and AIFS-ENS ensemble-mean feeds via Open-Meteo. The six-frame radar loop covers longitude -82 to -73 and latitude 38 to 43, so it includes KCDW plus plausible upstream Pennsylvania/Ohio convection. KCDW has no routine TAF, so KTEB and KEWR are clearly treated as local proxies. Every Open-Meteo product is labeled supplemental model guidance, not official aviation guidance.

## NBM station guidance

The collector includes both KCDW [NBM text products](https://www.weather.gov/mdl/nbm_text): **NBH**, the 24-hour hourly product (forecast leads 1–25), and **NBS**, the 72-hour product (three-hourly leads 6–72). It fetches operational NOMADS bulk bulletins, trying the current cycle and up to five prior hourly cycles independently for each product. Bulk downloads are capped at 40 MB each; only the KCDW card (at most 12 KB per product) enters the snapshot. Parsed cards are cached by product/cycle to avoid repeating large bulletin downloads.

Original fixed-width rows, model version, cycle time, source URL, forecast hours, and UTC valid times are retained. Validation checks station/product identity, cycle freshness (at most six hours), time axes, and complete core weather rows. The report displays both products and their cycle times. Product outages are recorded independently; these supplemental sources do not replace the existing forecast/context quorum.

The agent weighs NBM guidance alongside observations, proxy TAFs, and AFD reasoning, consulting the linked version-specific card key for units and special codes. NBH and NBS are products of the same blend, not independent model votes. The agent can research missing guidance at its discretion; NBS time steps do not imply exact two-hour hazard timing.

## WeatherNext 3 integration

The fixed loopback feed `http://127.0.0.1:8796/v1/forecast/KCDW` supplies exact-KCDW temperature (degC), preceding-hour precipitation (mm), sea-level pressure (Pa) and surface wind speed (m/s), each with mean/p10/p90 and 360 hourly valid times. `kcdw.weathernext3` uses the private mode-0600 token at `~/.local/state/weatherlab-feed/api-token` (or `WEATHERLAB_TOKEN_FILE`), rejects redirects/proxies, bounds reads, and sanitizes failures. Raw numerical data remain in private archives; prompt preparation provides compact local daylight-window reductions without fabricating daily quantiles. Validation runs at collection and before publication.

Fresh WN3 leads the model interpretation beyond 48 hours; AIFS-ENS is the independent comparator/fallback, followed by WeatherNext 2. A failed/stale source is unavailable, not favorable weather; it cannot satisfy the short-term publication quorum. WN3 has no cloud, ceiling, visibility, gust, wind direction or convection fields. Its percentiles are not standard deviations or flyability probabilities, and summed hourly percentiles are not event-total percentiles. Other sources still supply essential aviation context.

The owner-facing forecast intentionally includes numerical WN3 guidance: rolling analysis receives actual daily mean/p10/p90 summaries, while the checkride page combines models on shared per-variable charts, alongside dedicated WN3 mean/p10–p90 charts and an expandable event-window numerical summary. Ensemble bands are visible by default; model and band toggles remain available. Comparison axes use the same timestamps and units; WN3 mean/p10–p90, conventional ensemble median/p10–p90 and WN2 mean/±1 SD remain explicitly distinct. Hourly precipitation amounts use member-derived fans, not rain-member fractions or cumulative percentile sums; older archives without those fans omit the unavailable series. Qualitative-only filtering is not enabled. Forecast arrays and credentials are not exposed through a new raw-data API; the underlying server feed remains loopback and bearer-protected. The existing report URL is publicly reachable (not access-controlled); access settings are unchanged. Google attribution, experimental-use notice and source terms remain linked. See `services/weatherlab/README.md` for session-expiry recovery and service operation.

## Operational event-page layout

The checkride page starts with a deterministic mission briefing: window rain, runway wind, unresolved maneuvers ceiling, model disagreement, and the next useful reassessment. It identifies the current primary source and treats stale snapshots as outdated rather than reassuring. Rain/wind screening is not a checkride-completion probability. Within 48 hours the briefing explicitly directs the decision back to official aviation guidance, which this model-only event collector does not fetch.

Comparison charts remain visible and ordered rain, wind, low cloud, pressure, temperature, gusts, followed by dedicated WN3 mean/range charts. Ensemble ranges are on by default. Tables, threshold diagnostics, provenance, licensing and legacy-comparator details are collapsed; section links and chart/model controls remain keyboard accessible.

## Long-range ensemble policy

- **WeatherNext 2:** `google_weathernext2_ensemble_mean`, 64 members, 0.25° grid, native 6-hour steps, 15-day horizon. Google produces four cycles daily, but Open-Meteo currently processes the 00 and 12 UTC cycles. It is secondary/legacy guidance, not an independent vote from the Google model family.
- **ECMWF AIFS-ENS:** `ecmwf_aifs025_ensemble_mean`, 51 members, 0.25° dissemination grid, native 6-hour steps, 15-day horizon, four cycles daily. It is the independent comparison and fallback model guidance.
- The adapter retains ensemble **mean and spread together** for temperature, precipitation, low/mid/high cloud fraction, 10 m wind, mean sea-level pressure, and weather code. Open-Meteo interpolates native 6-hour values to hourly steps; this does not create real 2-hour timing skill. The feeds do not provide aerodrome ceiling, visibility, gust, lightning, or deterministic convective timing. Low-cloud fraction is not a ceiling.
- Agreement can support confidence. Disagreement is disclosed and lowers confidence; the models are not blindly averaged. Missing model data lowers confidence but does not mechanically lower a flyability score.
- The rolling point response does not carry an immutable run identifier. The snapshot therefore records the same API host's latest advertised initialization, modification, availability, and data-end metadata and states that binding limitation explicitly.
- Both collection and pre-publication validation reject non-finite values and values outside deliberately broad corruption guards: temperature -120..80 °C; temperature spread 0..100 K; precipitation and its spread 0..50 inches per returned hour; cloud fraction and spread 0..100%; wind speed and spread 0..300 knots; wind direction 0..360°; mean sea-level pressure 750..1150 hPa; and pressure spread 0..150 hPa. Weather codes must be integers in Open-Meteo's supported WMO code set. These are input-integrity limits, not operational aviation thresholds.

WeatherNext 3 is Google's August 2026 flagship 64-member system, with native hourly initialization, 0.1° gridded surface fields, selected 0.05° station outputs, 15-day synoptic runs, and 48-hour interim runs. Its direct real-time datasets currently require Google allowlist access and separate experimental-data terms. Official downloadable dataset approval is still pending. The separately authenticated internal Weather Lab feed now supplies verified **WeatherNext 3** numerical mean/p10/p90 guidance through `kcdw/weathernext3.py`; sign-in is not approval for the separate datasets. The private adapter validates actual run binding and freshness independently from the older Open-Meteo adapters. No NWS/AWC/radar precedence rule changes.

Open-Meteo API data are used under CC BY 4.0 and are attributed in the rendered report. The free endpoint is appropriate for this private non-commercial site; commercial use requires Open-Meteo's customer service/API key.

## Local commands

```sh
make test                 # offline unit suite; no network or Codex
make sample               # deterministic offline fixture render
make collect              # live collection only
make update               # live collection + required Codex analysis + publish
make serve                # 127.0.0.1:${PORT:-8794}
```

Runtime is Python 3 with Pillow (`python3 -m pip install -r requirements.txt`) plus ordinary host tools used by the shell wrapper (`bash`, `flock`, `timeout`, and the standalone `codex` CLI). The updater pins `gpt-6-astra` with `model_reasoning_effort="medium"` and uses the global `codex` CLI on PATH (override with `CODEX_BIN`). Install or upgrade it with `npm install -g @openai/codex@0.153.4`; older CLI versions may reject Astra. The Open-Meteo adapters need no Google/ECMWF credential. The WN3 adapter uses a private local bearer token; the dedicated browser retains its Google session separately. No token or Google session material enters the weather snapshot or public report. `public/index.html` and `public/health.json` are checked-in offline examples and are replaced in production only by a successful update.

## Cloudflare hosting

The public R2-backed Worker serves current and historical reports independently of this host. See [Cloudflare setup and operations](cloudflare/README.md). When `var/cloudflare.json` exists, each update retrieves the previous assessment from the Worker and uploads the validated result to R2. The local web service is disabled; `make serve` remains available for optional local inspection. The hourly generation timer stays enabled.

## Operations

Sample user-unit files live in `deploy/systemd/`. Hermes can copy or link them into the user systemd directory, reload the user manager, enable the web service, and enable the timer. This repository does not perform those steps. The web server binds **only** to `127.0.0.1:8794`; expose it, if desired, through a tailnet-only Tailscale Service. Do not bind the backend publicly.

The timer runs hourly with persistence and slight jitter. The updater's `flock` also prevents overlap if a manual run coincides with the timer. `var/update.log` records collector time, source readiness, radar attachment count, sanitized Codex CLI version, Codex exit code, validation, and publication. `var/codex.log` contains CLI process output but no prompt and must remain untracked. Verify Codex ran on every published update by matching a run's radar attachment count, `codex_exit=0`, `validation=success`, and `publication=success` lines. The snapshot and validated analysis for the last successful run are copied to untracked `var/latest-*.json`; a complete successful radar set is archived with its run and exposed through the untracked `var/latest-radar` compatibility symlink. Its `manifest.json` retains the matching collection time and complete radar source metadata. A successful report generated during a radar outage leaves the prior radar evidence and manifest intact rather than relabeling or deleting them.

Successful runs are archived under `var/runs/<run-id>/` with snapshot, analysis, prompt, Codex log, radar, rendered report, changes, and manifest. Failed runs retain available inputs and an outcome record. The server reads the complete run selected by the atomically replaced `var/current` symlink. Compatibility exports in `public/` and `var/latest-*` are not the publication commit. For rollback, atomically replace `var/current` with a symlink to a prior validated run. Routine failed updates leave the current pointer intact. The server exposes only the report and health endpoint. Archives are private and currently require manual retention management.

## Security boundary

Codex receives a bounded JSON snapshot through stdin plus at most six size-capped PNG radar attachments from the fixed NOAA/NWS service and bounding box. It can also search the live web and retrieve supplemental weather products. It has a read-only shell sandbox, uses an ephemeral session, and may write only its designated final-message file through the CLI host mechanism. Weather prose and imagery are explicitly treated as untrusted evidence. Model output is never evaluated or interpolated raw: JSON Schema constrains its shape, Python enforces domain invariants, and rendering escapes it. The static backend is loopback-only; the intended ingress is a tailnet-restricted Tailscale Service managed separately by Hermes.

The report is a planning aid, not an official briefing. Pilots must obtain current official weather/NOTAM information and apply aircraft, pilot, daylight, runway, and personal limitations.

## Agent product-request log

Each update records a concise, structured assessment of which additional weather products would help most in private, untracked `var/agent-feedback.jsonl`. The agent ranks up to five requests with a stable product ID, priority, evidence gap, expected benefit, known source URL, and retrieval outcome. An empty request list means it needed nothing else. Products successfully researched can be requested for direct collection to save future work.

Each JSONL entry retains the run ID, collection time, source availability, process exit code, and feedback status. Failed runs with no usable response are marked missing or invalid rather than counted as needing no additional data. Valid feedback is retained even if a later report step fails. Logging runs before temporary files are removed; a log-write failure is reported in `update.log` and does not undo a published report. Overlapping runs skipped by the lock do not create feedback entries.

Run `python3 -m kcdw.feedback summary` (or pass a different log path) to tally requests by product ID, ordered by request frequency then priority points (high=3, medium=2, low=1). The summary includes high-priority counts, successful retrieval counts, the latest request details, and total/valid/no-request run counts. Review the reasons and source availability before choosing new feeds; product IDs are agent-generated and aliases may need consolidation. This records actionable feedback, not private reasoning transcripts. Historical runs are not backfilled.

## Nearby-office forecast discussions

AFDs are collected independently for New York/Upton (OKX), Philadelphia/Mount Holly (PHI), Binghamton (BGM), Albany (ALY), Boston/Norton (BOX), and State College (CTP). Each retains its office identity, source URL, latest issuance at or before collection time, age, and the complete discussion, bounded to 14 KB with an explicit truncation flag, preserving modern section headings and marine reasoning. Empty or mismatched products are unavailable; one office outage does not suppress the others. Source status displays issuance and fetch times separately.

The agent weighs geographic relevance and weather trajectory toward KCDW when using neighboring discussions. It can research additional offices as needed. The added AFDs supplement the existing local aviation/context requirement.

The NBM v5.0 NBH/NBS station-card key is bundled in `kcdw/references/nbm-v5.0-station-card.txt`, extracted from the official NOAA/MDL page and verified on 2026-09-08. Prompt construction includes it automatically when a supplied NBM card reports version 5.0. It covers missing/unlimited codes, ceiling and visibility units, winds/gusts, probabilities, and the other NBH/NBS fields. The agent can use these definitions offline; different model versions require their own verified reference. Update the bundled key and version selection when upgrading NBM.

## Confidence and ensemble variance

Daily confidence explicitly considers within-model ensemble dispersion, between-model disagreement, source freshness/coverage, official forecast reasoning, and horizon. The existing spread fields are standard deviations (variance is their square); the agent uses native-unit spread alongside the means, emphasizing precipitation, low cloud, and wind during actionable local windows. It does not average unlike units, treat hourly interpolation as independent members, or infer exceedance probabilities from mean/spread alone.

High confidence requires a consistent controlling scenario; medium reflects uncertainty that could change some windows; low reflects materially different plausible flying outcomes. Observations can dominate near-term confidence, while missing spread cannot count as zero uncertainty. Dispersion that does not change the operational verdict need not force low confidence. This is a qualitative assessment, not a calibrated numerical confidence calculation, and variance does not mechanically lower flyability scores. Every day includes a visible `confidence_reason` explaining the controlling uncertainty and the role of spread.

## Evidence replay and source ablation

`python3 -m kcdw.replay var/runs/<run-id> var/replays/baseline --run` reinterprets the archived evidence at its original assessment time with web search disabled. Run a paired experiment with a new output directory and `--omit nbm_nbh --omit nbm_nbs` (or any snapshot source key). Omit `--run` to prepare inputs only. Replay never changes the published report. Compare operational categories, confidence, reasons, and requests across matched runs; repeat trials to distinguish source effects from model variability. These experiments measure sensitivity, not forecast accuracy. Accuracy and probability calibration require outcomes that this project does not yet collect.

Prompt preparation compresses repeated grid intervals, decodes version-matched NBM core fields, and computes daily ensemble mean/spread ranges and peak-variance times. Raw evidence remains in the archive. The report also compares matching actionable windows and daily outlooks with the previous assessment; new reasons explain the revised assessment without claiming a proven cause for every change.

## Dated checkride outlook

[Commercial checkride · September 24, 2026](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride) · [Event history](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride/history)

`events.json` defines dated pages. The checkride's **08:00–17:00 Eastern window is provisional**, not a confirmed appointment. The independent deterministic `kcdw.event_update` pipeline collects GEFS (31), ECMWF ENS (51), AIFS-ENS (51), and GEPS (21) individual-member distributions plus WeatherNext 2 **mean and standard-deviation** guidance and the private WeatherNext 3 **mean/p10/p90** source. Fresh WN3 is preferred beyond 48 hours, with AIFS-ENS the independent comparator/fallback. WN2 is never treated as individual members or converted to percentile/exceedance probabilities. Each source can fail independently.

The display covers two days before through one day after the event (September 22–25 Eastern). Explicit UTC `start_hour`/`end_hour` requests and exact returned-axis validation avoid the UTC-date/EDT-midnight mismatch. The nine-hour precipitation total sums preceding-hour intervals ending 09:00–17:00 Eastern, excluding the interval ending 08:00. Conventional-member window statistics use those same nine endpoints, not continuous extrema. WN3 instantaneous wind, temperature and pressure instead use 08:00–16:00 samples (start inclusive/end exclusive); WN3 rainfall uses 09:00–17:00 interval endpoints. Input validation checks finite nearby grid coordinates, exact contiguous hours, units, physical corruption bounds, exact member IDs, duplicate IDs, missing series and per-hour sample counts. Persisted normalized data are revalidated before rendering and uploading.

The page presents separate model medians/p10–p90 distributions and counts, **not pooled favorable percentages or calibrated flyability probabilities**. Missing cloud is unavailable, not favorable. Point pressure cannot rule out hurricanes, nearby storms, or convection; cloud fraction is not ceiling height. Near-term official-product links are explicitly not fetched by this page.

Latest advertised dataset metadata is displayed separately from fetch time. IFS 06/18Z short cycles may not cover the extended rolling response supplied by earlier long cycles; there is no immutable per-value run binding. Event health therefore returns `status: provenance_unverified` (or `stale` when the fetch is over eight hours old), never a current marker derived solely from a successful fetch. This status is intentional, not a publication outage.

```sh
python3 -m kcdw.event_update --no-publish  # live collection and local archive only
python3 -m kcdw.event_update               # publish using private var/cloudflare.json
systemctl --user start kcdw-flyability-events.service
systemctl --user status kcdw-flyability-events.timer
```

The installed user timer polls at **05:20, 11:20, 17:20, 23:20 UTC**, plus up to three minutes jitter. Actual upstream latency varies. Its service uses `flock`, runs independently of the main hourly Codex report, and stops collecting after the event date. Event archives and logs are under `var/events/<slug>/runs/` and `var/events/update.log`. R2 uses separate `events/<slug>/runs/` and latest pointers; the main report pointer and stored report HTML are not rewritten. Current homepage navigation is augmented from the event index at response time; old reports remain historical. Readbacks verify publication and index state. History is retained; no automated deletion is installed.

Public routes: `/events`, `/api/events`, `/events/<slug>`, `/events/<slug>/health.json`, `/events/<slug>/history`, `/api/events/<slug>/history`, and `/events/<slug>/runs/<run-id>`. Publishing routes require the existing bearer token. `npm --prefix cloudflare test` now builds a fresh dry-run bundle before Miniflare tests, avoiding accidental tests against stale `dist/` output.

## Report design

The report and history share the Field Notes design: a seven-day overview, direct links to each day, concise planning picks, two-hour cards, and always-visible hazard/daylight details. Styles live in `kcdw/report.css`; the renderer embeds them into each archived report, while the Worker imports them for history. Subset WOFF2 Barlow Condensed and IBM Plex Sans fonts are bundled in `kcdw/assets/fonts.css` with their OFL licenses, so viewing does not require third-party font requests. Historical reports retain their original presentation.

Health checks read the small R2 publication pointer, which includes the assessment timestamp and freshness threshold, instead of downloading the complete report. Older pointers continue to work through a compatibility fallback. Generated narratives target 180–240 characters to leave room below the enforced 360-character limit.
