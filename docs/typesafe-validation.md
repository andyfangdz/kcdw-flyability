# TypeSafe validation — September 20, 2026

Complete weekly and event generation passed with TypeSafe and the Codex fallback.
The user authorized project-data transfers and Codex when Claude is out of credit.
Both scheduled services now load `KCDW_TYPESAFE=required`, and both timers are
enabled. Validation used archived weather at its original assessment time and
saved private previews without publishing historical weather as a current report.

| Check | Result |
| --- | --- |
| Input archive | `var/runs/20260920T160146Z.694793` |
| Assessment time | September 20, 2026, 16:01:46 UTC |
| Model | `jev-1.13.0` |
| Draft and final writer | Codex `gpt-6-astra`, high effort |
| NWS passage ranking | Six offices completed |
| Daily outlook and weather-confidence judgments | All seven days completed |
| Requested two-hour windows | All 16 completed |
| Claim and change reviews | Seven daily scopes and weekly summary completed |
| TypeSafe requests | 43 |
| Input tokens | 1,075,372 |
| Cumulative TypeSafe API time | 27.788 seconds |
| Complete workflow time | 236.2 seconds |
| Artifacts | `var/typesafe-evaluation/end-to-end-1789925151852396655/` |
| Final offline regression suite | 758 tests passed, six skipped |

At the documented $0.042 per million input tokens, this completed TypeSafe-only
evaluation represents approximately $0.045 of TypeSafe input usage. This excludes
earlier diagnostic attempts and prose-model usage; it is a calculation from the published
rate, not an invoice. [TypeSafe model pricing](https://docs.typesafe.ai/models).

The reviewer labeled 24 claims supported, two contradicted, and 30 insufficient
out of 56. These are unverified model judgments, not labeled forecast accuracy.
Broad summary claims may need more detail than the deliberately limited summary
packet provides. Reviews therefore remain observations and do not gate reports.
The separate synthetic API check rejected a claim of clearing before a flight
ended when the supplied evidence placed clearing afterward.

The complete orchestration check exercised a real Claude usage-credit error and
automatic Codex fallback. Codex completed both the initial image-based
interpretation and final writing pass. All seven daily reviews and the summary
review completed; the report passed domain validation and rendered seven daily
flyability and weather-confidence indices. Offline tests cover the two-pass flow,
fixed-score enforcement, renderer, failure preservation, and archives. They also
verify that ordinary errors and successful prose mentioning a quota message do
not trigger fallback, and that event provenance records the actual writer.

The event workflow completed three-office NWS ranking, Codex generation after a
Claude quota failure, TypeSafe claim/change review, and citation-bound rendering.
It labeled 13 claims supported and seven insufficient. Artifacts are in
`var/typesafe-evaluation/event-generation-1789924916657241434/`. CLI JSONL logs
in the live fallback contract and event tests contain agent messages and no tool
executions. The exact event hashes, approved destination and scope, and enabled
service names are recorded in `var/typesafe-evaluation/rollout-manifest.json`.

Live checks also found probability rounding, context limits, and a transient
choice/distribution inconsistency. Input packing preserves selected values,
timestamps and missingness, with explicit omissions. Typed-answer failures get
one retry with identical validation, never a retry based on whether the weather
judgment is favorable. Persistent failures preserve the previous report.
