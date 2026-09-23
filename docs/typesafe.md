# TypeSafe integration

TypeSafe supplies structured judgments in the Python generation pipeline. The
Cloudflare Worker continues serving completed reports and never receives an API key.
The pinned model is `jev-1.13.0`; request and response artifacts record the model,
questions, evidence, distributions, token usage, and elapsed time.

Daily evidence is limited to overlapping intervals plus bracketing samples.
Products outside the requested period retain their status and issue/valid times,
with their out-of-period forecast text omitted. Expired advisory details are
replaced by explicit validity records, never an all-clear. Days starting more
than 24 hours ahead retain the latest observation per station as baseline context;
unknown observation times and unknown advisory validity remain visible.
The broad summary review uses
daily model ranges, official forecast text, and latest METARs; exact hourly
claims and observation trends belong to the separate daily reviews. Every
selection records its scope and omissions. NBM extracts name the decoded fields
they include and preserve the actual model cycle time.

Repeated text and record keys are encoded losslessly as dictionaries and tables.
Large packets use compact JSON text, which avoids the larger context usage
observed with object inputs. Original request artifacts retain the exact encoding.
Tests reconstruct the original values, including nulls, absent fields, valid
times, and extremes. Claims are reviewed in groups of four to keep each request
within the model context budget.

## Features

1. **NWS passage ranking.** Complete paragraphs are scored for relevance to the
   requested location and periods. Selected original wording retains source IDs,
   office, issue time, and full-product context. It supplements the original
   evidence in Claude's prompt. An omitted paragraph is not an all-clear.
2. **Seven-day assessments.** The prose writer first interprets the collected evidence and
   radar, with the existing research permissions. TypeSafe then chooses daily
   outlooks, scores the exact requested two-hour windows for days 1–3, and assesses
   daily weather confidence. Days 4–7 remain broad outlooks. A second writing pass
   writes explanations with fixed decisions and no new web research. Both its
   JSON Schema and Python validation enforce those decisions before publication.
3. **Claim review.** Each briefing sentence is checked against its evidence;
   event sections retain their source citations. Headline, lead, and next-check
   text are included. Results distinguish supported, contradicted, and
   insufficient-evidence claims. Weekly detail is reviewed by day, with a separate
   broad-summary check. Daily model aggregates cannot verify exact hourly timing.
4. **Change review.** Separate questions check whether retrospective claims have
   comparable supporting evidence and whether the evidence change matters for
   the flight. Retrieval time, initialization, sampling changes, forecast history,
   and actual observations retain their different meanings. An unavailable
   comparison returns unknown rather than an invented prior state.

Reviews run in **observe mode**: results are archived for evaluation and do not
gate publication. Ranking/reviewer failures are recorded as unavailable, not as
passing checks. Once TypeSafe assessment generation is enabled, a failed or
invalid weekly decision stops that update and preserves the previous report.
Event reviewer failures leave valid narrative and deterministic charts available.

The prose writer prefers Claude Code (`claude-opus-5-5`, medium effort). An explicit
failed CLI response reporting exhausted credits or quota switches that run to
Codex (`gpt-6-sol`, high effort), including subsequent writing passes. Other
errors and invalid outputs still fail normally. Each new scheduled run tries
Claude again, allowing it to recover when its quota resets. Logs and TypeSafe
status artifacts record the actual draft/final writers; event envelopes use the
actual provider and model.

Codex receives radar images as native attachments, runs from a temporary working
directory with read-only sandboxing, and disables shell execution, agents, hooks,
plugins, connectors, and browser tools. It ignores user config and rule files.
Live web search is enabled only for the rolling interpretation pass when research
is allowed; event and final-writing passes use their supplied evidence. The CLI
uses its existing login. `CODEX_BIN` can select the installed executable. Its
service write access to `.codex` supports the trusted CLI's login/cache updates;
the model has no filesystem execution tools. Configuration follows the installed
CLI and [official Codex documentation](https://developers.openai.com/codex/config-reference).

## Score meanings

Flyability uses the existing five categories. Code maps the selected category to
a representative ordinal score: practical no-go 10, probably not 35, toss-up 55,
probably flyable 70, strong go 90. Categories are selected directly rather than
averaged across favorable and hazardous outcomes. Best/backup dates rank the best
actionable window, then weather confidence, then the earlier date; elapsed today
is excluded.

Weather confidence is its own three-level Score question: low, medium, high. Its
0–2 score is multiplied by 50 to produce the displayed **0–100 ordinal index**.
Indices below 33⅓ are low, below 66⅔ medium, otherwise high. The rubric considers
operationally relevant ensemble spread, model disagreement, coverage, freshness,
and horizon. High confidence can accompany unfavorable weather. These are rubric
positions, not calibrated forecast probabilities.

TypeSafe's separate `confidence` describes concentration of its answer
distribution. It is retained in private diagnostics and never substituted for
weather confidence. Typed answers do not establish meteorological correctness;
evaluate the judgments on project examples before introducing automatic review
gates. No universal acceptance threshold is configured.

## Configuration

The default is `KCDW_TYPESAFE=off`. Merely placing a key on disk does not send
weather data or activate production assessments.

Store the key in untracked `var/typesafe-api-key` and restrict it to mode 0600.
Alternatively set `TYPESAFE_API_KEY_FILE` to a private regular file, or supply
`TYPESAFE_API_KEY` in the service environment. Keys never enter prompts, snapshots,
request artifacts, or public files. TypeSafe receives collected weather evidence,
the applicable mission timing, and generated briefing text when enabled.

- `KCDW_TYPESAFE=required`: enable integration and fail if the key is missing.
- `KCDW_TYPESAFE=auto`: enable when credentials exist; otherwise use the existing
  Claude pipeline. An API failure after enabling does not silently change provider.
- `KCDW_TYPESAFE=off`: keep the existing generation path.
- `KCDW_TYPESAFE_MODEL`: optional explicit versioned Jev model ID. Floating model
  aliases are rejected so archived judgments remain attributable.

For a scheduled rollout, add `Environment=KCDW_TYPESAFE=required` to both the
update and events service overrides after approving and validating the live data
transfer. The default key path follows the updater's `--var`/`VAR_DIR`. A service
override can instead set an absolute `TYPESAFE_API_KEY_FILE` path. Keep that path
readable under the existing service filesystem restrictions.
The prepared override is `deploy/systemd/typesafe.conf`; installing it as
`~/.config/systemd/user/<service>.service.d/typesafe.conf` and running
`systemctl --user daemon-reload` enables that service on its next scheduled run.
Remove that override and reload to return to the existing default-off path.

## Artifacts and validation

Rolling runs archive diagnostics under `var/runs/<run-id>/typesafe/`, including
failed runs. Event diagnostics are retained under
`var/events/<slug>/narratives/<run-id>/typesafe/` and copied to that event's run
archive. These directories are private and excluded from public publication.
Request bodies contain no authorization headers. HTTP redirects are rejected,
response sizes and timeouts are bounded, and error logs omit service response
bodies and credential-bearing transport messages.
An invalid typed response is retried once and the rejection is archived. Both
attempts use the same validation; low confidence or unfavorable weather never
triggers a retry. Persistent contract violations stop required assessments.

`weekly-decisions.json` binds decisions to the snapshot and draft hashes. Review
artifacts bind results to evidence and prose hashes; call artifacts retain the
exact questions. `afd-ranking.json` retains paragraph indices and original text.
Do not reuse results after their evidence, prose, or rubric changes.

Run `scripts/with-runtime.sh make test` for offline tests, including API contract,
malformed-answer rejection, timeline slicing, fixed-decision enforcement,
credential handling, reviewer failures, rendering, and archive behavior. `make
test` explicitly disables live TypeSafe calls. For evaluation, use an isolated
artifact directory and fixed archived assessment time; do not move a publication
pointer. Label correct and deliberately incorrect claims to measure missed
errors and false alarms separately from request latency and token usage. Forecast
accuracy requires actual weather outcomes, not agreement between models.

API and scoring contracts: [HTTP API](https://docs.typesafe.ai/api),
[models](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence),
[citation checking](https://docs.typesafe.ai/cookbooks/citation_check).

See [the September 20 validation record](typesafe-validation.md) for measured
request usage, live results, and remaining rollout checks.
