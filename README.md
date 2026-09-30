# Infrastructure-telemetry

**A batch lakehouse pipeline for turning raw telemetry from a large fleet of energy devices
(Supercharger stalls/cabinets, Powerwall, Megapack, Powerpack) into trustworthy, queryable
data and actionable reliability findings.**

This is a self-directed systems-design project: I planned it as a 65-ticket, 5-phase backlog
the way a production data platform team would, then built it out ticket by ticket with the
engineering discipline that scale demands — idempotent ingestion, replay-safe recomputation,
versioned findings, architecture decision records, and a real automated test suite. It's not
connected to any company's production systems; all data is synthetic and clearly labeled as
such (see **Honesty about what's real** below). What it demonstrates is how I decompose an
ambiguous, large-scale data engineering problem, make and document the tradeoffs along the
way, and hold the line on correctness under load.

## The problem

Telemetry from a fleet this size doesn't arrive clean. Devices retry, batch, and drop
connections; clocks drift; messages duplicate and arrive out of order; firmware versions
disagree on units and field names; and no matter how careful the pipeline is, some data will
always show up late. A naive "just append it to a table" pipeline silently corrupts itself at
scale: duplicate rows inflate counts, clock drift misattributes events to the wrong hour, and
a late-arriving batch either gets ignored (wrong) or triggers a full table rewrite (too slow
and blast-radius-unsafe once there are ~1.1M devices).

The goal was to design a pipeline where **correctness survives scale, replay, and lateness by
construction** — not by hoping nothing goes wrong.

## How it works

A 5-stage pipeline, each stage narrowing in on a stricter guarantee than the last:

```
raw device        Stage 0        Stage 1         Stage 2          Stage 3        Stage 4
messages    -->    Landing  -->  Parsed     -->  Canonical  -->   Enrich    -->  Outputs
(MQTT/HTTPS)      (append-      (parsed +       (canonical      (time grid,     (cohort stats,
                   only,          deduped +       units,          device-day      versioned
                   arrival-       timestamp-      corrected       features,       findings,
                   time            sane)           event time,     device          device
                   partitioned)                    quality         history,        snapshot)
                                                    flags)          external
                                                                    sources)
```

- **Stage 0 (Landing):** one row per message, exactly as received, partitioned by arrival
  hour, never rewritten. This is the replay source of truth — if anything downstream is ever
  wrong, it gets fixed by replaying from here, not by patching a table in place.
- **Stage 1 (Parsed):** per-firmware versioned parsers turn raw payloads into typed readings.
  Timestamps get sanity-checked and quarantined if implausible. Ingestion is a **MERGE on
  (device_id, device_ts, payload_hash)** — never a plain append — so a retried or duplicated
  message is a no-op, not a duplicate row.
- **Stage 2 (Canonical):** raw per-firmware fields map onto one canonical signal catalog.
  Per-device clock offset is estimated and corrected. Rows get quality flags (stuck values,
  counter resets, out-of-range jumps) — flagged, never deleted, so nothing downstream loses
  information silently.
- **Stage 3 (Enrich):** builds the time-bucketed grid, device-day feature rollups, a
  device-history dimension (firmware/hardware/site, looked up with an as-of/bitemporal join
  so a device's *history* is queryable at any past timestamp, not just its current state),
  and joins in external operational sources (RMA, tickets, dispatch, outages, weather).
- **Stage 4 (Outputs):** rule-based fault/anomaly detectors (session failures, thermal
  events, module faults, derates) produce **append-only, content-hash-deduped findings** with
  a provisional-to-final lifecycle — a finding can be revised as more data arrives within its
  lateness horizon, but the historical record is never overwritten, only versioned. Cohort-day
  aggregates enforce a minimum-cohort-size suppression rule before anything is exposed, so no
  aggregate can indirectly leak a single device's data.

**Late data doesn't trigger a full rebuild.** A dirty-keys table tracks exactly which
`(device, hour)` pairs were touched by newly-arrived data, and only those get recomputed —
proven against a simulated 72-hour outage (288 buffered rows) with exact dirty-key
identification and idempotent, byte-identical replay.

## Built to be infrastructure-agnostic

Every stage that touches object storage goes through one dependency-injection seam
(`pipeline/common/object_store.py`, extending a pattern already used in Stage 0's capture
service): a single `--endpoint-url` config value decides whether the pipeline talks to real
AWS S3, a local MinIO instance, or an in-memory S3-compatible test server. **No pipeline code
changes between those three.** This is proven, not just claimed — there's a runnable
end-to-end demo on the `demo-e2e-mock-pipeline` branch that generates synthetic fleet data
and runs it through all 5 stages against a real local S3-compatible server, with a
`docker-compose.yml` provided to run the identical script against real MinIO. See that
branch's `demo/README.md` for the full walkthrough and an honest note on exactly what was and
wasn't run in which environment.

## Scale it's designed for

- **Partitioned by event time, never by device ID** (an explicit, enforced invariant) — the
  fleet target is ~1.1M devices, and partitioning by device ID would create wildly uneven,
  hot partitions as the fleet grows.
- **Idempotent, replay-safe ingestion** — the same batch of messages can be reprocessed any
  number of times (retries, backfills, disaster recovery) without changing the result.
- **Targeted, not full-table, recomputation** — a late-arriving hour of data recomputes that
  hour alone, not the whole table, which is what actually makes backfill viable once tables
  are large.
- Load-tested the ingest-buffer layer against ~6.5k synthetic messages under simulated
  backpressure and outage conditions with zero data loss; the literal "10x current production
  rate" target is explicitly flagged as needing a real baseline this project doesn't have
  access to (see **Honesty about what's real**).

## Tech stack

Python 3.10/3.11, boto3 (S3-compatible object storage), Dagster (orchestration/partitioning),
pytest + ruff. Target production stack is Iceberg tables on object storage with Terraform-
managed infra (see `docs/decisions/0001-object-store-layout.md`) — this repo doesn't carry an
Iceberg/pyarrow dependency yet, so the runnable demo uses JSONL as an explicitly-labeled
stand-in (see the demo branch's docs for exactly where that simplification is and why).

## Engineering process

- **65-ticket backlog across 5 phases** (`Build backlog.md`), each ticket with an explicit,
  literal "done when" acceptance check — a ticket only moves to Done when that check is a
  real, automated test or a documented, verifiable result, never a self-assessment.
- **Architecture decision records** (`docs/decisions/`) for anything touching a table's grain
  or a real tradeoff with consequences — e.g. the object-store/catalog layout, and a data
  retention & access-tier policy for residential data.
- **A lightweight decision log** (`docs/PENDING-DECISIONS.md`) for open tradeoffs that need a
  real stakeholder call before code gets written against them, rather than guessing and
  hoping the guess was right.
- **Parallel, isolated workstreams** — one ticket per git branch/worktree, so multiple lines
  of work can proceed without stepping on each other's files.
- Automated test suite (270+ tests as of the latest integration branch) plus continuous
  linting; every "Done" ticket was independently re-verified — tests re-run and source code
  re-read — rather than taken on the strength of a summary.

## Honesty about what's real

A handful of tickets have real, tested engineering behind them but are deliberately marked
"Human-owned" rather than "Done" in the backlog, because their literal acceptance criteria
need something this project doesn't have: live access to a real legacy backend, real firmware
fault-injection hardware, an actual granted data-access approval, a real scheduled production
job, or a live production environment. In every one of those cases, the branch still contains
a genuine, working implementation built and tested against clearly-labeled **synthetic or
assumed** data — never presented as if it were a real result. `docs/reports/` and each
ticket's note in `Build backlog.md` say exactly which parts are real and which are assumed.
This project is entirely local/synthetic-data-driven; it has never touched a real cloud
account or real device data.

## Current status

Phase 0 (Discover & capture): 8/14 done · Phase 1 (Core pipeline, Supercharger pilot):
12/16 done · Phase 2 (Enrich & detect, add Megapack): 6/15 done · Phase 3 (Powerwall at
scale, dashboards): not started · Phase 4 (Harden & hand off): not started.
**26/65 tickets done fleet-wide**, plus several more with real, tested "Human-owned" work
completed pending non-engineering steps (see above).

## Where to look

- [CLAUDE.md](CLAUDE.md) — stage grain, invariants, stack, repo layout, ticket workflow.
- [Build backlog.md](Build%20backlog.md) — the full 65-ticket, 5-phase backlog.
- [docs/decisions/](docs/decisions/) — architecture decision records.
- [docs/reports/](docs/reports/) — profiling reports, audit drafts, gate sign-offs, runbooks.
- [docs/PENDING-DECISIONS.md](docs/PENDING-DECISIONS.md) — open stakeholder decisions.
- `pipeline/stage0_landing/` … `pipeline/stage4_outputs/` — the pipeline stages themselves.
- `detectors/` — the rule-based fault/anomaly detection framework.
- `parsers/` — versioned, per-firmware parsers with fixture-backed tests.
- `tests/fixtures/generators/` — synthetic device/telemetry data generators used throughout.
- `demo-e2e-mock-pipeline` branch — the runnable end-to-end demo described above.
