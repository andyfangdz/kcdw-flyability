# KCDW Cloudflare hosting

Live report: https://kcdw-flyability.andyfang.workers.dev/ · [History](https://kcdw-flyability.andyfang.workers.dev/history). Deployed in the personal Cloudflare account; viewing is public. R2 bucket: `kcdw-flyability-reports`.

The host generates forecasts. The Worker serves the latest report and historical reports entirely from its private R2 binding; it never fetches content from the host. No public R2 bucket URL is needed.

## Storage and publication

Each `reports/<reverse-run-timestamp>-<run-id>.json` object contains the rendered HTML, validated analysis, health metadata, and change summary. Reverse timestamp keys let R2 list the newest reports first with cursor pagination. Raw snapshots, prompts, credentials, and Codex logs stay on the generator host.

`POST /api/publish` requires the `PUBLISH_TOKEN` Worker secret. It accepts a bounded complete report, stores it conditionally without overwriting an existing ID, then updates `latest.json` with an ETag compare-and-swap. Repeated identical uploads are safe. Backfills cannot replace newer assessments. If updating the pointer fails, the complete report remains in history and publication can be retried.

The host validates the full analysis before upload. The Worker checks the transport contract and matching timestamps; the publisher credential is trusted to submit the rendered HTML. Read requests cannot mutate R2. No user-supplied object path is passed through to the bucket.

## Routes

- `/`: latest report, with a history link.
- `/health.json`: freshness calculated at request time.
- `/history`: paginated report history from R2.
- `/api/history?cursor=...`: the same history in JSON (50 reports per page).
- `/api/latest`: latest analysis and change summary.
- `/reports/<run-id>`: a historical report, explicitly marked historical.
- `/reports/<run-id>/analysis.json`: historical analysis and change summary.
- `/api/publish`: authenticated publisher endpoint.

All routes use `Cache-Control: no-store` so latest pointers and freshness are never hidden by a stale HTTP cache. Report pages retain their in-browser freshness timer. `READ_ACCESS=disabled` restricts reads to the publisher credential until publication access is configured; `READ_ACCESS=public` permits public viewing.

## Setup

From this directory, run `npm ci`, `npx wrangler whoami`, and set the selected `account_id` in `wrangler.jsonc`. Create `kcdw-flyability-reports` with `npx wrangler r2 bucket create kcdw-flyability-reports`. Configure read access, run `npm run types`, `npm run check`, and `npx wrangler deploy --dry-run`. Set a randomly generated publisher token with `npx wrangler secret put PUBLISH_TOKEN`, then deploy using `npm run deploy`.

On the generator, save the same token in a private mode-600 file and configure untracked `var/cloudflare.json`:

```json
{"url":"https://<worker-hostname>","token_file":"/absolute/path/to/private/token"}
```

The updater detects this file automatically. It retrieves the previous analysis from R2 before rendering comparisons and uploads each validated run after local archival. Remote lookup outages fall back to the latest local assessment. Upload outages preserve the remote report, record a failed cloud publication, and return a nonzero updater result. Retry/backfill archived runs without calling Codex again:

```sh
python3 -m kcdw.cloud_publish backfill var/runs
python3 -m kcdw.cloud_publish history --output var/cloud-history.json
python3 -m kcdw.cloud_publish fetch <run-id> var/historical-report.json
python3 -m kcdw.cloud_publish previous var/cloud-previous.json
```

The local web service has been disabled after verifying the Worker and a successful systemd update. The hourly generation timer remains enabled. A stopped generator does not interrupt report serving; the last report continues to display and becomes stale. Old reports are retained without automatic deletion.

## Verification

```sh
npm run types
npm run check
npx wrangler deploy --dry-run --outdir dist
npm test
```

The integration suite runs the Worker against Miniflare's actual R2 implementation. It covers authentication, malformed/oversized uploads, immutable IDs, idempotent retry, backfill ordering, concurrent publication, history pagination, archived rendering, dynamic health, HEAD, and route restrictions. Python publisher tests are part of the root `make test` suite.
