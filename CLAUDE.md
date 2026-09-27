# Energy Fleet Reliability Pipeline

Batch lakehouse pipeline turning telemetry from ~1.1M energy devices (Powerwall, Megapack,
Powerpack, Supercharger stalls and cabinets) into reliability findings, telemetry-health
metrics and downstream datasets. Proposal and ticket backlog: docs/ (export from the Claude Doc).

## Stages and grain (never change a grain without a design note in docs/decisions/)
- Stage 0 Landing: one row per message as received; partitioned by ARRIVAL hour; immutable.
- Stage 1 Parsed: one row per reading (device_id, device_ts); partitioned by device_class x event
  date/hour, bucketed by device_id hash. Timestamp sanity + dedup happen here.
- Stage 2 Canonical: same grain, canonical signals/units, corrected event time, quality flags.
  Sidecar: completeness and lateness per device x hour.
- Stage 3 Enrich: 3a grid (5-min Powerwall/Powerpack, 1-min others), 3b device x event,
  3c device x day; device history dimension with validity periods (as-of joins).
- Stage 4 Outputs: cohort-day stats, versioned findings, device snapshot, fleet marts, lifetime table.
- Telemetry health: last-seen snapshot (15-min job), silence episodes, dropout by cohort.

## Invariants (reviewers reject changes that break these)
1. Stage 0 is append-only and never rewritten; it is the replay source.
2. Stage 1 writes are MERGE on (device_id, device_ts, payload_hash). Never a plain append.
3. Cleaning flags bad values; it never deletes rows.
4. Never partition by device_id. Stage 0 by arrival time, Stage 1+ by event time.
5. Late data recomputes only dirty (device, hour) keys. Full-partition recompute only via an
   explicit, reviewed backfill job.
6. Windows are provisional until the lateness horizon passes. Findings are versioned, never overwritten.
7. Replaying a closed window from Stage 0 must reproduce production output exactly.
8. No real residential data outside prod. Tests use synthetic fixtures in tests/fixtures/.

## Stack (confirm at Gate 0)
Iceberg tables on object storage, Spark for Stages 1-3, Dagster for orchestration,
Terraform for infra. Python 3.11+, pytest.

## Repo layout
infra/            Terraform
ingest/           ingest service and buffer config
pipeline/stageN/  Dagster assets per stage
parsers/<class>/<firmware>/   versioned parsers with fixture tests
catalog/signals.yaml          canonical signal catalog per firmware
detectors/        detector plugins (declarative config + code)
tests/fixtures/   synthetic payloads per class and firmware
docs/decisions/   one file per design decision

## Ticket workflow
- One ticket per branch, named <ticket-id>-short-name (e.g. P1-06-idempotent-merge).
- A ticket is done only when its "Done when" check is an automated test or a documented result.
- Run tests before reporting a ticket complete. Summarize what changed and what is left.
- Parallel work on separate tickets uses separate branches/worktrees; never two agents on one file.

## Sub-agents (.claude/agents/)
platform-engineer, parser-engineer, reliability-analyst, infra-sre, design-reviewer.
Every change is reviewed by design-reviewer against the invariants before it is proposed for merge.
Humans own: gate reviews, anything touching prod data, firmware fault-injection tests, access requests.
