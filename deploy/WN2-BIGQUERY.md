# WN2 BigQuery member diagnostics

Production WN2 reads `871883017250.WeatherNext2.weathernext_2_0_0` using the
existing Google ADC identity and billing project `aviation-486817`.
`WN2_BIGQUERY_PROJECT`, `WN2_BIGQUERY_MAX_BYTES_BILLED` (default 2 TiB), and
`WN2_BIGQUERY_CACHE_DIR` (default `var/wn2-bigquery`) control the transport.
The event service specifies project/cap explicitly. No new dependency is needed.

The discovery query scans at most the past 24 hours, capped at 32 GiB. Member
queries use exact `init_time`, a constant 100 m spatial predicate on the nearest
0.25° grid center, selected native six-hour times, and only required columns.
At KCDW this is 41.0, -74.25. The cap admits the conservative clustered-table
estimate; it does not predict the final billed scan. Cache entries have a six-hour
TTL, atomic writes and a lock per query identity, limited to 128 JSON entries.

Live check on 2026-09-20: the 2026-09-19 18Z run at Sep 24 12Z/18Z returned
64 members per time. Dry-run estimate: 830,957,460,480 bytes (~774 GiB).
Actual processed: 251,510,616 bytes; billed: 251,658,240 bytes (240 MiB).
Discovery billed the 10 MiB minimum. Job usage is retained in each packet.

The exact grid, run, lead times, all member identities and physical bounds are
validated before caching and again before reduction. Wind speed is computed
from each member's U/V components, then converted to knots. Wind direction uses
meteorological FROM convention; calm members do not count in the NE sector.
Pressure is converted Pa → hPa. Rain converts m → mm; raw values below -1 mm
per six hours are rejected and accepted negative values are clipped to zero.
Rain totals use complete overlapping six-hour periods, joined by member ID,
with the enclosing interval clearly labeled (not the two-hour flight window).

The WN2 BigQuery table has no cloud field. WN3 continues to supply primary cloud
and hourly weather statistics. Live daily and event WN2 mean/spread collection
is retired. Legacy archive validators and renderers remain, preserving model
identity and the original rolling-source limitations.

Validation: new query/cache/member/renderer tests pass; the full 711-test suite
has only the two pre-existing WN3 fixture failures documented in prior work.
Official schema: https://developers.google.com/weathernext/guides/models-wn2

Deployment acceptance (2026-09-20 03:49 UTC): both installed update services
completed with `Result=success`; daily and event cloud publication succeeded.
Public readback confirmed run `20260920T034724Z`, the new WN2 native diagnostic,
absence of the retired WN2 chart legend, and fresh WN3 primary guidance. The
archived event packet is version 2 with BigQuery job provenance and a local
cache hit. Daily health is `ok`; the event retains its pre-existing overall
`provenance_unverified` status for other model provenance limitations.
