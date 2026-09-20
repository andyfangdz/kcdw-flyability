# KCDW Flyability

[Live report](https://kcdw-flyability.andyfang.workers.dev/) · [Report history](https://kcdw-flyability.andyfang.workers.dev/history) · [Checkride outlook](https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride)

Weather planning for Essex County Airport (KCDW). A Python pipeline collects weather evidence, generates structured assessments with TypeSafe and Claude Code (with Codex fallback), validates them, and publishes a self-contained HTML report.

The rolling report covers seven days: two-hour windows for the first three days and daily outlooks thereafter. Dated events have model comparison charts, flight-window diagnostics, and a separate narrative. Times are Eastern; categories are planning assessments, not calibrated probabilities.

## Run locally

Install Python dependencies from `requirements.txt` in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
make test
make sample
make serve
```

The sample uses offline fixtures and writes `public/index.html` and `public/health.json`. The local server listens on `127.0.0.1:8794` (`PORT` overrides the port). On the configured homeserver, `scripts/with-runtime.sh make test` uses the installed runtime environment.

| Command | Action |
| --- | --- |
| `make test` | Run the offline Python suite, including chart navigation tests; Node.js is required. |
| `make sample` | Render the checked-in fixtures. |
| `make serve` | Serve the local report. |
| `make collect` | Collect live weather into `var/snapshot.json`. |
| `make update` | Collect, generate analysis, validate, and publish. |
| `python3 -m kcdw.event_update --no-publish` | Collect and archive dated-event reports locally. |

Live analysis prefers the installed Claude Code CLI and its existing login. When Claude reports exhausted credits or quota, it automatically uses the authenticated Codex CLI with `gpt-6-astra`. WeatherNext queries need allowlisted Google Application Default Credentials and a billing project. Native GRIB decoding uses a separate optional environment. See [operations](docs/operations.md), [homeserver setup](deploy/HOMESERVER.md), and [native collection](docs/forecast-guide.md#low-cloud-analysis) for configuration.

TypeSafe integration ranks relevant NWS discussion passages, generates the seven-day flyability and weather-confidence assessments, and reviews briefing claims and forecast-change explanations. Claude, or Codex when Claude's quota is exhausted, interprets radar and writes prose around the fixed assessments. TypeSafe is off by default unless configured in the service; see [setup and behavior](docs/typesafe.md). Its confidence index is ordinal, not a probability of safe flight.

## How it works

1. `kcdw.collector` collects official forecasts, observations, radar, and supplemental models. Each source retains its own status and timestamps.
2. `kcdw.prompt` builds bounded evidence for `kcdw.prose_agent`, which prefers Claude and switches to Codex on explicit credit/quota failures. The rolling assessment may research missing context; the event narrative uses only its collected evidence.
3. `kcdw.validation` checks structure, timing, coverage, and source integrity. `kcdw.renderer` and `kcdw.event_renderer` escape source text and embed the charts, styles, and scripts.
4. A successful run is archived before its publication pointer changes. Failed updates leave the last successful report in place. Cloudflare Workers serves the published reports from R2.

Official NWS/AWC guidance and radar lead near-term interpretation. Fresh WeatherNext 3 is the preferred model beyond 48 hours, with AIFS-ENS as an independent comparison and fallback. WeatherNext 2 supplies separate native member diagnostics. Missing sources remain unavailable.

`events.json` defines dated pages and appointment timing. Collection artifacts, archives, caches, and credentials live under untracked `var/`; generated public examples live under `public/`.

## Development

Tests cover source validation, weather calculations, stale or missing data, archives, and publication. Run one area while iterating:

```sh
python3 -m unittest discover -s tests -p 'test_event_update.py'
```

Use module imports when sharing test helpers so unittest does not discover imported `TestCase` classes twice. Add tests for distinct behavior or failures; avoid assertions tied to prose, colors, or local research archives. Event-update tests stub external collectors in one place.

The Worker has its own checks: `npm --prefix cloudflare run check` and `npm --prefix cloudflare test`. See its [setup instructions](cloudflare/README.md).

## Reference

- [Forecast reference](docs/forecast-guide.md): model sources, event diagnostics, chart ranges, and interpretation limits.
- [Pipeline and operations](docs/operations.md): agent execution, publication, archives, replay, and feedback.
- [Cloudflare hosting](cloudflare/README.md): R2 storage, routes, credentials, and deployment.
- [WeatherNext 3 BigQuery](deploy/WN3-BIGQUERY.md) and [WeatherNext 2 BigQuery](deploy/WN2-BIGQUERY.md): access, query budgets, and caches.
- [Homeserver setup](deploy/HOMESERVER.md): runtime paths and systemd units.

This is a planning aid. Obtain a current official briefing and apply aircraft and pilot limits before flight.
