# KCDW Flyability

A dependency-free Python pipeline that collects current weather evidence for Essex County Airport (KCDW), asks the installed Codex CLI for a strictly structured seven-day interpretation covering today plus the following six days, validates it, and atomically publishes a single self-contained HTML report.

The six daily scores cover 08–10, 10–12, 12–14, 14–16, 16–18, and 18–20 America/New_York. Today's elapsed windows remain visible but are dimmed and labeled; the active window is highlighted. A score estimates the chance that an ordinary local VFR pattern session is *comfortably flyable*. It is not a probability of safety or a go/no-go recommendation.

## Architecture

1. `kcdw.collector` fetches each source independently with an explicit User-Agent, 15-second timeout, two limited retries, response-size limits, timestamps, and bounded retained fields. Source failures are recorded in the snapshot. Publication requires at least one forecast source, one aviation/context source, and three successful sources overall.
2. `kcdw.prompt` serializes that deterministic snapshot with stable key ordering, a 500 KB hard limit, and a rubric that treats all fetched text as untrusted data rather than instructions.
3. `scripts/update_report.sh` takes a nonblocking `flock`, records the CLI version, and invokes `codex exec` non-interactively with `--ephemeral --sandbox read-only --output-schema ... --output-last-message ...`. OAuth comes from the service user's existing CLI login; no API key is read or stored.
4. `kcdw.validation` applies semantic checks beyond JSON Schema: exact dates/windows, no duplicates, 5-point scoring, length bounds, and timestamps tied to the snapshot.
5. `kcdw.renderer` HTML-escapes every model/source string. The updater renders HTML and health JSON into temporary files and renames them only after the whole analysis succeeds. A failed collection, Codex call, or validation leaves the last known-good report untouched.

Data comes from the NWS points endpoint and its hourly/text/grid links; NWS OKX AFD product listing/detail; AWC three-hour METAR history, valid proxy TAFs, and airsigmet feed; NWS point alerts; and Open-Meteo extended hourly model guidance. KCDW has no routine TAF, so KTEB and KEWR are clearly treated as local proxies. Open-Meteo is labeled model guidance, not official aviation guidance.

## Local commands

```sh
make test                 # offline unit suite; no network or Codex
make sample               # deterministic offline fixture render
make collect              # live collection only
make update               # live collection + required Codex analysis + publish
make serve                # 127.0.0.1:${PORT:-8794}
```

Runtime is Python 3 with its standard library plus ordinary host tools used by the shell wrapper (`bash`, `flock`, `timeout`, and the standalone `codex` CLI). `public/index.html` and `public/health.json` are checked-in offline examples and are replaced in production only by a successful update.

## Operations

Sample user-unit files live in `deploy/systemd/`. Hermes can copy or link them into the user systemd directory, reload the user manager, enable the web service, and enable the timer. This repository does not perform those steps. The web server binds **only** to `127.0.0.1:8794`; expose it, if desired, through a tailnet-only Tailscale Service. Do not bind the backend publicly.

The timer runs hourly with persistence and slight jitter. The updater's `flock` also prevents overlap if a manual run coincides with the timer. `var/update.log` records collector time, source readiness, sanitized Codex CLI version, Codex exit code, validation, and publication. `var/codex.log` contains CLI process output but no prompt and must remain untracked. Verify Codex ran on every published update by matching a run's `codex_exit=0`, `validation=success`, and `publication=success` lines. The snapshot and validated analysis for the last successful run are copied to untracked `var/latest-*.json` for audit.

For rollback, stop/disable the sample units as appropriate and restore a known-good `public/index.html` and `public/health.json` from the prior release or version control. Because publication uses same-filesystem atomic rename and failure preservation, routine failed updates require no rollback.

## Security boundary

Codex receives a bounded JSON snapshot through stdin, has a read-only sandbox, uses an ephemeral session, and may write only its designated final-message file through the CLI host mechanism. Weather prose is explicitly declared untrusted. Model output is never evaluated or interpolated raw: JSON Schema constrains its shape, Python enforces domain invariants, and rendering escapes it. The static backend is loopback-only; the intended ingress is a tailnet-restricted Tailscale Service managed separately by Hermes.

The report is a planning aid, not an official briefing. Pilots must obtain current official weather/NOTAM information and apply aircraft, pilot, daylight, runway, and personal limitations.
