# Homeserver deployment

The production scheduler is the **andy user systemd manager on homeserver**.
Project: `/home/andy/projects/kcdw-flyability`.
Public hosting remains `https://kcdw-flyability.andyfang.workers.dev` (Cloudflare Worker/R2). No inbound homeserver port is required.

## Runtime

- Main Python: `var/runtime-venv/bin/python` (uv-managed Python 3.11; `requirements.txt`).
- Native GRIB Python: `var/native-weather-venv/bin/python` (`eccodes==2.43.0`, `requests==2.32.5`). Virtual environments were rebuilt for x86_64, not copied from the ARM source host.
- Claude Code: `/home/andy/.local/bin/claude`, version 2.1.280, pinned to `claude-opus-5-5` at medium effort for report and event updates.
- Installed units: `/home/andy/.config/systemd/user/kcdw-flyability-*`. The four active service/timer pairs are also saved under `deploy/systemd/` with the homeserver paths. The unused optional web-service template is not installed.
- User linger is enabled, so the scheduler survives logout and starts at boot.

## Credentials (never commit)

- Google authorized-user ADC: `/home/andy/.config/kcdw-flyability/google-adc.json`; units set `GOOGLE_APPLICATION_CREDENTIALS`. This is the original allowlisted identity, with refresh capability. The old gcloud database and unrelated Google identities were not copied.
- Claude OAuth: `/home/andy/.config/kcdw-flyability/claude/.credentials.json`; units set `CLAUDE_CONFIG_DIR` to that directory. Only the Claude OAuth record was transferred, not unrelated MCP tokens or user sessions. The ordinary homeserver Claude configuration was not overwritten.
- Cloudflare publication token: `var/cloudflare-publish-token`, referenced by `var/cloudflare.json` with the homeserver path. No broad Cloudflare account-management token is required for scheduled publication.
- Credential files are mode 0600, the private config directory and project `var` directory are mode 0700. Units retain filesystem hardening and use umask 0077.

## Scheduled jobs

Schedules preserve the original installed units:

- `kcdw-flyability-update.timer`: hourly, up to two minutes jitter.
- `kcdw-flyability-events.timer`: minute 25 UTC, up to three minutes jitter.
- `kcdw-flyability-run-history.timer`: minute 10 UTC, up to one minute jitter.
- `kcdw-flyability-native-ensembles.timer`: minutes 00/15/30/45, up to 30 seconds jitter; also two minutes after boot.

The main and event refresh services retain their two-hour pipeline deadlines; WN3 now uses BigQuery point queries and the shared `var/wn3-bigquery` cache, rather than global GCS-plane downloads. The query billing project and cap are explicit in the WN3 service units; the hourly timers do not overlap an already running service.

Inspect from another host:

```sh
ssh andy@homeserver 'systemctl --user list-timers "kcdw-flyability-*" --all'
ssh andy@homeserver 'journalctl --user -u kcdw-flyability-update.service -n 30 --no-pager'
```

Use the installed service for manual runs so the scoped credentials, Python PATH, locking and filesystem sandbox match production:

```sh
ssh andy@homeserver 'systemctl --user start kcdw-flyability-update.service'
ssh andy@homeserver 'systemctl --user start kcdw-flyability-events.service'
```

For manual commands, use `scripts/with-runtime.sh COMMAND [ARGS...]` from the project; for example, `scripts/with-runtime.sh make test`. This selects the same Python, Google ADC, and isolated Claude login as production. It does not provide the systemd filesystem sandbox, so use the installed services for publishing runs.

The event updater and history updater share a lock: do not start them concurrently for manual verification.

## Migration and rollback

Code, Git history, report/event archives, current symlinks and private caches were transferred via SSH/rsync. Node modules and obsolete bridge runtime dependencies were not transferred; they are not needed to publish to the already deployed Worker.

The source-host four KCDW timers are disabled. Its obsolete Weather Lab feed/browser/private display/window-manager services are disabled rather than migrated: production reads the official WN3 BigQuery statistics view. Source files and credentials remain as a disabled rollback copy; do not enable both schedulers.

To roll back, stop/disable all four destination timers, wait for destination services to finish (or explicitly stop them), transfer any new destination archives back as appropriate, then enable the source timers. Do not reuse homeserver's rewritten publisher-config path on the source.

## Test baseline

The destination offline suite ran 721 tests: five failures, one error and four skips. All six failing cases were individually reproduced on the unmodified source host (chart-count expectations and WN3 historical/publication fixture expectations). Migration does not claim the repository test suite is green. Real production service runs and public readback are separate deployment acceptance checks.
