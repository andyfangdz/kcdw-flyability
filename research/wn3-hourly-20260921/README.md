# Interim WN3 runs at KCDW

Collected September 21, 2026, at approximately 15:29 EDT using the official
BigQuery 0.1° surface-statistics view, grid point 40.9° N / 74.3° W.

The 07Z, 08Z, 09Z, 10Z and 11Z initializations each returned all 48 forecast
hours and mean/p10/p90 for all 14 requested fields: temperature, dew point,
three distinct precipitation products, sea-level pressure, 10 m speed and U/V,
four cloud layers, and 100 m wind speed. Five forecast queries billed 617 MiB
in total; the separate run-discovery query billed 10 MiB. Raw results and job
provenance are retained privately in `var/research/wn3-hourly-20260921/` and
the shared `var/wn3-bigquery/` cache.

The latest available interim run, 11Z, ends September 23 at 11Z (Wednesday
07:00 EDT). None of these runs reaches Thursday's checkride. Google documents
48-hour horizons for interim initializations and BigQuery delivery targeted at
initialization plus 7 hours 25 minutes:
[model guide](https://developers.google.com/weathernext/guides/models) and
[dissemination schedule](https://developers.google.com/weathernext/guides/dissemination).

## Comparison at identical Tuesday valid times

All numbers below are ensemble means for September 22, not Thursday.
Wind is sustained 10 m wind. Rain is the sum of two hourly mean amounts for
10:00–12:00 EDT, using the standard total-precipitation field and intervals
ending at 11:00 and 12:00; it is not a percentile of total rain.

| Init, Sep 21 UTC | Low cloud 10 a.m. / noon | Wind 10 a.m. / noon | Rain 10 a.m.–noon |
|---|---:|---:|---:|
| 06Z, full-range reference | 5.9% / 18.1% | 7.5 / 8.1 kt | 0.41 mm |
| 07Z | 5.9% / 20.9% | 7.2 / 7.9 kt | 0.27 mm |
| 08Z | 7.9% / 15.4% | 7.3 / 7.8 kt | 0.20 mm |
| 09Z | 8.6% / 20.8% | 7.5 / 7.9 kt | 0.17 mm |
| 10Z | 5.3% / 25.3% | 7.5 / 8.1 kt | 0.13 mm |
| 11Z | 9.7% / 25.8% | 7.5 / 8.2 kt | 0.24 mm |

The newer runs keep similar sustained winds and less mean rainfall than 06Z,
with some increase in low cloud toward noon. Cloud uncertainty remains broad:
11Z low-cloud p90 is 26.6% at 10 a.m., 35.3% at 11 and 58.5% at noon.
These are correlated runs of one model, not independent confirmations.
Cloud fractions do not establish ceiling, and 100 m winds do not establish
gust magnitude or an upper bound on gusts.

## Automatic collection

The rolling and event refreshes now gather the latest available interim run
separately from the full-range six-hourly run. Snapshot archives retain full
48-hour point data and query provenance. Rolling narrative evidence receives
only covered times, and the event narrative/page distinguish full, partial
and absent flight-window coverage. Interim guidance cannot satisfy the
official aviation source-readiness requirement.

Live verification: the September 21 19:37:29Z checkride snapshot collected
11Z hourly guidance and successfully published it; HTTP readback matched the
archived page. The 19:36:34Z rolling snapshot also archived the complete 11Z
hourly source, but its TypeSafe assessment returned HTTP 400 and publication
preserved the previous report. An authorized diagnostic replay confirmed
`max_tokens_exceeded`. Grouping hourly time axes and limiting daily evidence to
covered hours reduced the request, but it still exceeded the model's context.
Sharing repeated ISO timestamps through the existing exact-text dictionary then
allowed the same-snapshot request to succeed (HTTP 200, 34,267 total input tokens
across the state and questions). Original timezone offsets, fractional seconds,
weather values and missing-value distinctions are retained.

The subsequent rolling refresh, `20260921T200452Z.1084364`, collected and
validated the complete 11Z hourly packet, passed assessment and publication
validation, and published successfully at 20:09:06Z (16:09 EDT). Public HTTP
readback matched the archived report apart from the worker's placement of the
dynamic checkride navigation link; the hourly source label and current
assessment timestamp were confirmed. The full test suite passed (788 tests,
23 skipped for optional dependencies).

Reproduce the multi-run collection:

```sh
scripts/with-runtime.sh python scripts/wn3_hourly_point.py \
  --output var/research/wn3-hourly-20260921 --limit 5
```
