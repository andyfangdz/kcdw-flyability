# Pipeline and operations

Collection, analysis, publication, and archive procedures. For a quick start, see the [README](../README.md). All commands run from the repository root.

## Architecture

1. `kcdw.collector` fetches each source independently with an explicit User-Agent, 15-second timeout, two limited retries, response-size limits, timestamps, and bounded retained fields. It also downloads the six latest NOAA/NWS MRMS quality-controlled base-reflectivity frames for a fixed eastern-US bounding box, rejects a loop whose newest frame is over 30 minutes old, decodes each bounded PNG with Pillow, and records frame validity/ingest times, KCDW's image pixel, nearest displayed-echo geometry, and 25/50 NM local-echo flags. `kcdw.ensemble_guidance` independently fetches 192 hours of ECMWF AIFS-ENS means and standard-deviation spreads, validates exact fields/units/hour axes, and records the models' advertised initialization and availability metadata. Source failures are recorded separately in the snapshot. Publication requires usable forecast coverage through the next two hours and fresh aviation/context evidence. Empty successful responses and metadata alone cannot satisfy readiness.
2. `kcdw.prompt` serializes that deterministic snapshot with stable key ordering, a 500 KB hard limit, and a rubric that treats the snapshot as a starting evidence packet and all fetched text as untrusted data rather than instructions. The agent chooses when live web research is useful to fill gaps, resolve conflicts, or check freshness; there is no fixed research checklist. Supplemental products must be issued by the snapshot assessment time, and material additions are attributed with source, product time, and URL in the narrative or summary. It maps each radar attachment to timestamped metadata and requires loop-based local/regional/upstream interpretation without unsupported precision. Fresh WeatherNext 3 is the preferred *model* guidance after 48 hours for available fields, with AIFS-ENS as an independent comparator/fallback and WN2 native member diagnostics on the event page; neither outranks official NWS forecasts, AFD reasoning, SPC outlooks, or the established short-term aviation evidence hierarchy.
3. `scripts/update_report.sh` takes a nonblocking `flock`, binds each radar attachment to exact consecutive snapshot metadata, decodes every size-capped PNG before use, records the CLI version, and invokes Claude Code non-interactively through `kcdw.claude_agent` (`claude --print --model claude-opus-5-5 --effort high --json-schema ... --restricted --strict-mcp-config --tools Read,WebSearch,WebFetch`), listing the ordered radar PNGs for the Read tool. Files without matching metadata, missing/extra filenames, symlinks, malformed images, and loops outside the two-to-six-frame contract fail closed before the agent runs. OAuth comes from the service user's existing CLI login; no API key is read or stored.
4. `kcdw.validation` applies semantic checks beyond JSON Schema: exact dates/windows, no duplicates, 5-point scoring, length bounds, timestamps tied to the snapshot, and ordered/fresh radar metadata.
5. `kcdw.renderer` HTML-escapes every model/source string. The updater stages the entire run, then atomically switches the current-run pointer only after the whole analysis succeeds. A failed collection, agent call, or validation leaves the last known-good report untouched.

Data comes from the NOAA/NWS MRMS time-enabled base-reflectivity ImageServer; NWS points endpoint and its hourly/text/grid links; NWS OKX, PHI, BGM, ALY, BOX, and CTP AFD product listings/details; AWC three-hour METAR history, valid proxy TAFs, and airsigmet feed; NWS point alerts; SPC convective outlooks; Open-Meteo best-match extended guidance; and AIFS-ENS ensemble-mean feeds via Open-Meteo. WeatherNext 3 point statistics and WeatherNext 2 native member diagnostics use BigQuery. The six-frame radar loop covers longitude -82 to -73 and latitude 38 to 43, so it includes KCDW plus plausible upstream Pennsylvania/Ohio convection. KCDW has no routine TAF, so KTEB and KEWR are clearly treated as local proxies. Every Open-Meteo product is labeled supplemental model guidance, not official aviation guidance.

## Local commands

```sh
make test                 # offline unit suite; no network or agent
make sample               # deterministic offline fixture render
make collect              # live collection only
make update               # live collection + validated assessment + publish
make serve                # 127.0.0.1:${PORT:-8794}
```

Tests focus on weather calculations, source validation, missing/stale data,
archive integrity, and publication behavior. Event update orchestration is
covered in `tests/test_event_update.py`, with external collectors stubbed in
one place. Keep new tests focused on a distinct failure or behavior; avoid
exact prose, colors, chart counts, and dependencies on local research archives.
Use module imports when reusing test helpers so unittest does not discover an
imported `TestCase` a second time. To run one area while iterating:
`python3 -m unittest discover -s tests -p 'test_event_update.py'`.

Runtime is Python 3 with Pillow, google-auth and requests for BigQuery (plus the GCS gRPC client and Zstandard for parity probes) (`python3 -m pip install -r requirements.txt`) plus ordinary host tools used by the shell wrapper (`bash`, `flock`, `timeout`, and the `claude` CLI). `kcdw.claude_agent` pins `claude-opus-5-5` at `--effort high`, uses the `claude` CLI on PATH (override with `CLAUDE_BIN`), and rejects a response that the pinned model did not serve. Flags were verified against Claude Code 2.1.280. Archived logs keep their historical `codex.log` filename. The Open-Meteo adapters need no Google/ECMWF credential. The WN3 adapter uses refreshable Application Default Credentials with the cloud-platform scope; that identity needs WeatherNext BigQuery read access and permission to submit query jobs in the billing project. The homeserver runtime wrapper and service units select the existing allowlisted ADC. No token material enters the weather snapshot or public report. `public/index.html` and `public/health.json` are checked-in offline examples and are replaced in production only by a successful update.

### TypeSafe and Codex fallback

The scheduled updater now runs `kcdw.assessment_agent`: optional TypeSafe passage
ranking, an image-capable interpretation, TypeSafe daily/window decisions and
weather-confidence indices, a final prose pass with fixed decisions, then private
claim/change reviews. See [TypeSafe configuration](typesafe.md). Failed required
assessments preserve the previous report; observe-only review failures are recorded.

`kcdw.prose_agent` starts with the Claude adapter described above and switches to
the authenticated Codex CLI only on an explicit failed response reporting quota
or credit exhaustion. Codex is pinned to `gpt-6-astra` with high effort. It receives
the same schema, evidence and native radar attachments. Subsequent passes in that
run keep Codex; the next run tries Claude again. General failures do not trigger
provider switching. Replays and event narratives use the same fallback.

The Codex process ignores user config/rules, disables shell, hooks, plugins,
connectors, browser and agent tools, and uses an isolated temporary working
directory with read-only sandboxing. Only the rolling research pass permits web
search. Final-writing and event passes use supplied evidence only. `CODEX_BIN`
selects the executable. Event provenance and `prose_complete` log entries record
the actual writer; the rolling TypeSafe status records both draft and final writers.

## Cloudflare hosting

The public R2-backed Worker serves current and historical reports independently of this host. See [Cloudflare setup and operations](../cloudflare/README.md). When `var/cloudflare.json` exists, each update retrieves the previous assessment from the Worker and uploads the validated result to R2. The local web service is disabled; `make serve` remains available for optional local inspection. The hourly generation timer stays enabled.

## Operations

Systemd user-unit templates live in `deploy/systemd/`; installation is documented in [homeserver setup](../deploy/HOMESERVER.md). The web service is optional when Cloudflare serves the reports. The web server binds **only** to `127.0.0.1:8794`; expose it, if desired, through a tailnet-only Tailscale Service. Do not bind the backend publicly.

The timer runs hourly with persistence and slight jitter. The updater's `flock` also prevents overlap if a manual run coincides with the timer. `var/update.log` records collector time, source readiness, radar attachment count, sanitized Claude Code CLI version, agent exit code, validation, and publication. `var/codex.log` contains CLI process output but no prompt and must remain untracked. Verify the agent ran on every published update by matching a run's radar attachment count, `codex_exit=0`, `validation=success`, and `publication=success` lines. The snapshot and validated analysis for the last successful run are copied to untracked `var/latest-*.json`; a complete successful radar set is archived with its run and exposed through the untracked `var/latest-radar` compatibility symlink. Its `manifest.json` retains the matching collection time and complete radar source metadata. A successful report generated during a radar outage leaves the prior radar evidence and manifest intact rather than relabeling or deleting them.

Successful runs are archived under `var/runs/<run-id>/` with snapshot, analysis, prompt, agent log, radar, rendered report, changes, and manifest. Failed runs retain available inputs and an outcome record. The server reads the complete run selected by the atomically replaced `var/current` symlink. Compatibility exports in `public/` and `var/latest-*` are not the publication commit. For rollback, atomically replace `var/current` with a symlink to a prior validated run. Routine failed updates leave the current pointer intact. The server exposes only the report and health endpoint. Archives are private and currently require manual retention management.

## Security boundary

The agent receives a bounded JSON snapshot through stdin plus at most six size-capped PNG radar files from the fixed NOAA/NWS service and bounding box, which it views with the Read tool. It can also search the live web and retrieve supplemental weather products. `--restricted` removes command execution and ignores user, project and local settings (hooks, plugins, CLAUDE.md); `--strict-mcp-config` loads no MCP servers; no write or edit tool is offered; sessions are not persisted; and the process runs in an empty temporary directory with only the radar directory added, so tokens and archives under `var/` are outside the file tools' reach. The event narrative runs with no tools at all. The adapter, not the model, writes the analysis file from the CLI's structured output. Weather prose and imagery are explicitly treated as untrusted evidence. Model output is never evaluated or interpolated raw: JSON Schema constrains its shape, Python enforces domain invariants, and rendering escapes it. The local backend is loopback-only. Published reports are publicly served by the Cloudflare Worker; snapshots, prompts, credentials, and agent logs remain private on the generator.

The report is a planning aid, not an official briefing. Pilots must obtain current official weather/NOTAM information and apply aircraft, pilot, daylight, runway, and personal limitations.

## Agent product-request log

Each update records a concise, structured assessment of which additional weather products would help most in private, untracked `var/agent-feedback.jsonl`. The agent ranks up to five requests with a stable product ID, priority, evidence gap, expected benefit, known source URL, and retrieval outcome. An empty request list means it needed nothing else. Products successfully researched can be requested for direct collection to save future work.

Each JSONL entry retains the run ID, collection time, source availability, process exit code, and feedback status. Failed runs with no usable response are marked missing or invalid rather than counted as needing no additional data. Valid feedback is retained even if a later report step fails. Logging runs before temporary files are removed; a log-write failure is reported in `update.log` and does not undo a published report. Overlapping runs skipped by the lock do not create feedback entries.

Run `python3 -m kcdw.feedback summary` (or pass a different log path) to tally requests by product ID, ordered by request frequency then priority points (high=3, medium=2, low=1). The summary includes high-priority counts, successful retrieval counts, the latest request details, and total/valid/no-request run counts. Review the reasons and source availability before choosing new feeds; product IDs are agent-generated and aliases may need consolidation. This records actionable feedback, not private reasoning transcripts. Historical runs are not backfilled.

## Evidence replay and source ablation

`python3 -m kcdw.replay var/runs/<run-id> var/replays/baseline --run` reinterprets the archived evidence at its original assessment time with web search disabled. Run a paired experiment with a new output directory and `--omit nbm_nbh --omit nbm_nbs` (or any snapshot source key). Omit `--run` to prepare inputs only. Replay never changes the published report. Compare operational categories, confidence, reasons, and requests across matched runs; repeat trials to distinguish source effects from model variability. These experiments measure sensitivity, not forecast accuracy. Accuracy and probability calibration require outcomes that this project does not yet collect.

Prompt preparation compresses repeated grid intervals, decodes version-matched NBM core fields, and computes daily ensemble mean/spread ranges and peak-variance times. Raw evidence remains in the archive. The report also compares matching actionable windows and daily outlooks with the previous assessment; new reasons explain the revised assessment without claiming a proven cause for every change.
