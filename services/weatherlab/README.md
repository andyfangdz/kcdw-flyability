# Private Weather Lab feed (KCDW)

Server-local experimental WeatherNext 3 point-statistics adapter. **Not an official API or aviation briefing.** The raw feed has no non-loopback route. KCDW rolling/checkride reports now consume it through `kcdw.weathernext3`; report display includes numerical guidance and is governed separately from feed-client authentication. Google dataset approval is separate and remains the preferred migration path when granted.

## API

Origin: `http://127.0.0.1:8796` (loopback only, no Tailscale/Funnel route).

- `GET /health`: unauthenticated process liveness only, not data readiness.
- `GET /v1/status`: bearer required; readiness, authentication, freshness, latest requested/attempted initialization, actual cached run, fetch and attempt times.
- `GET /v1/forecast/KCDW`: bearer required; `{explicit_last_good:false,status,forecast}`. HTTP 503 on stale data or any failed refresh; never treat those errors as current weather.
- `GET /v1/last-good/KCDW`: explicit opt-in to cached data even after failure/staleness; inspect `status` before use. HTTP 503 when no cached data exists.

No query parameters, arbitrary URLs, coordinates, stations, or write endpoints. Invalid/missing bearer: HTTP 401. Fresh advertised fallback may be HTTP 200 with `state:degraded`, `fallback:true`, `available:true`; upstream errors never silently become success.

Token: `~/.local/state/weatherlab-feed/api-token`, owner-only mode 0600 outside the repository. Do not place it in shell arguments, logs, source, or chat. Server-side example:

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen
request = Request('http://127.0.0.1:8796/v1/forecast/KCDW', headers={
    'Authorization': 'Bearer ' + Path.home().joinpath('.local/state/weatherlab-feed/api-token').read_text().strip()
})
with urlopen(request, timeout=10) as response:
    data = json.load(response)
assert data['status']['available']
print(data['forecast']['response_init_utc'])
```

## Data contract

Fixed location: KCDW `40.8752,-74.2814`; model enum 12 (WeatherNext 3). Each field has 360 mean, p10 and p90 values aligned with `valid_time_utc`, hourly +1 through +360 hours:

- `temperature_2m`: degC
- `precipitation_1h`: mm, hourly total (not a rate conversion)
- `sea_level_pressure`: Pa
- `wind_speed_10m`: m/s

Mean is not median and need not lie between p10/p90. These are model distributions, not calibrated flyability/checkride probabilities. No cloud fields or individual ensemble members are available through this adapter.

The response initialization must equal the requested initialization. Units and field enums follow the observed Google client contract. Missing/duplicate fields, missing percentiles, shifted timelines, nonfinite/out-of-range values and inverted percentile bounds fail closed. Forecast coordinates are exact request provenance, not an independently returned geolocation.

## Reliability and supervision

`weatherlab-feed.service` starts after the separately supervised dedicated Chromium browser. It refreshes immediately at startup and hourly afterward, with nonoverlap and a 100-second worker watchdog. Each worker first polls actual loopback CDP readiness for up to 30 seconds; systemd reporting the browser active is not sufficient. Transient calls retry once; run discovery merges the model-specific index (response field 2) with the broader advertised index (field 3), then selects at most the two latest six-hour-cycle candidates within 24 hours. The model-specific list can lag available point statistics. Broader candidates are never assumed to contain WN3: every attempt still requests model 12 and must pass exact response-initialization, field/unit and 360-hour validation. Failure of the latest advertised run permits a flagged previous-run fallback. Maximum usable model-run age is 18 hours, regardless of fetch time.

Immutable content-addressed records contain raw meteorological arrays, normalized data and request context. Atomic fsynced `cache/state.json` tracks the latest record and refresh outcome. Bad upstream/auth/validation results preserve last good. Cache integrity is revalidated on restart. Cache retention removes forecast records older than 30 days and caps the retained set at 720 records, always preserving the pointed last-good record even if older. Pruning runs after durable pointer updates and ignores unrelated files. Pruning errors do not invalidate successful collection; monitor disk usage/permissions. Do not run multiple feed processes against the same cache directory. Stop the service before deleting all cache records.

Only browser code handles Google session cookies/CSRF. Node connects to **loopback CDP port 9233**, verifies the exact Weather Lab origin, and receives data or sanitized error codes. Workers exit without closing the browser. Do not expose CDP. This isolation is from API clients, not other processes running as the same Unix user. The dedicated profile is private, but Chromium's basic password store is not encrypted against account/root access. Do not use this profile for unrelated browsing.

The browser must remain signed in. Google session expiry/revocation can require manual sign-in; there is no promise of permanent unattended auth. `auth_required` is explicit, last-good stays available separately, and normal forecasts return 503. Other upstream changes may report `degraded` instead. Inspect status; this version has no outbound alert delivery. If a page bootstrap token expires, reload the dedicated Weather Lab tab, sign in if prompted, then restart only the feed to trigger collection. Never export cookies or enter credentials in logs/chat. Temporary remote-login access should be capability-protected, tailnet-only, and removed immediately after use.

Commands:

```sh
npm ci
npm test
systemctl --user status weatherlab-feed.service weatherlab-browser.service
systemctl --user restart weatherlab-feed.service
journalctl --user -u weatherlab-feed.service -n 30 --no-pager
```

Sample unit `weatherlab-feed.service` uses this host's Node path and requires the pre-existing `weatherlab-browser.service` on port 9233. Install in `~/.config/systemd/user/`, create the token and cache parent privately, run `systemctl --user daemon-reload`, then `systemctl --user enable --now weatherlab-feed.service`. User lingering must be enabled for boot without login. Verify real protected forecast reads, not just health. To revoke downstream access, rotate the token and restart. To retire, stop/disable the feed and browser, delete private cache/token/profile paths after confirming scope; preserve unrelated browser profiles.

## Source and use boundary

Reviewed 2026-09-12: [Google experimental-data terms](https://storage.googleapis.com/weathernext-public/terms-of-use.pdf), last modified 12 November 2025. Section 2(a) permits internal use. Formatting/subsetting/raw API hosting does not itself qualify as a value-added service. This feed is for the account owner's internal server processing, not redistribution. Re-review before any public integration, other recipients, or change of use. No model training or third-party AI bridge is installed here.

Source: Google Weather Lab. Copyright 2024-5 Google LLC. Modification: geographic point request, RPC decoding, schema normalization and caching; numerical values unchanged. This data is intended for experimental modelling only and is not intended, validated, or approved for real world use. Do not replace NWS/AWC warnings, observations, approved briefings, or operational flight decisions with it. Official dataset access approval does not come from Weather Lab sign-in.
