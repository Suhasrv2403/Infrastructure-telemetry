# Build backlog

The build is 65 epic-sized tickets across 5 phases, about 141 focused engineer-weeks; each ticket should be split into 1–3 day stories at sprint planning.

**Conventions**

- **ID:** phase and sequence, e.g. P1-06. Gate reviews close each phase.
- **Component:** INGEST (buffer, ingest service), S0–S4 (pipeline stages), DET (detectors), TH (telemetry health), PLAT (infra, orchestration, observability), GOV (privacy, security), UI (dashboards), DISC (discovery).
- **Owner level:** who should lead the ticket. Juniors always pair with a named senior reviewer.
- **Estimate:** focused engineer-weeks. Plan calendar time at 1.5–2x for reviews, on-call and meetings.
- **Done when:** the acceptance check that closes the ticket; every ticket also needs tests, a design note if it changes a table's grain, and monitoring if it runs in production.

**Capacity check:** \~140 focused engineer-weeks, or 210–280 calendar-adjusted, against \~11 engineers over 12 months. That leaves room for unplanned work, which Phase 0 findings will almost certainly create.

## Phase 0 · Discover and capture (months 0–2)

14 tickets, \~27 engineer-weeks. Goal: replace assumptions with measurements before locking storage and grain decisions.

| ID | Ticket | Component | Owner level | Est. (wk) | Depends on | Done when | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P0-01 | Audit existing telemetry backend and data flows | DISC | Staff | 2 | — | Written map of sources, formats, retention and known drops (real audit needs access to the live backend and stakeholder interviews - human-owned; see branch P0-01-telemetry-backend-audit for a ready-to-run audit template and interview question set) | To do |
| P0-02 | Cloud accounts, IaC baseline, networking, IAM | PLAT | Senior SRE | 2 | — | Dev, staging, prod created from Terraform | To do |
| P0-02b | Prod cloud account, IaC baseline, networking, IAM | PLAT | Senior SRE | 1 | P0-02 | Prod environment created from Terraform, mirroring dev/staging | To do |
| P0-03 | Object store layout and Iceberg catalog | PLAT | Senior | 1.5 | P0-02 | Buckets, catalog and naming conventions documented and live | Done |
| P0-04 | Deploy orchestrator (Dagster) with partition model | PLAT | Senior | 2 | P0-02 | A partitioned asset runs and backfills in staging | Done |
| P0-05 | Stage 0 capture for one Supercharger region | S0 | Senior | 3 | P0-03 | Raw messages land by arrival hour; counts reconcile with source | Done |
| P0-06 | Arrival-shape profiler | DISC | Senior | 1.5 | P0-05 | Report: protocol, batching, message sizes per class and firmware | Done |
| P0-07 | Timestamp and clock-quality profiler | DISC | Senior | 1.5 | P0-05 | Missing, epoch-default, future and drift rates per firmware | Done |
| P0-08 | Lateness and duplicate profiler | DISC | Senior | 1.5 | P0-05 | Lateness distribution and duplicate rate per class | Done |
| P0-09 | Device retry-behavior test with fault injection | DISC | Senior SRE + firmware | 2 | P0-05 | Retry, buffer or drop behavior documented per firmware (real fault injection - human/firmware-owned, see branch P0-09-device-retry-behavior-profiler for a synthetic-proxy analysis and a ready-to-run test plan) | To do |
| P0-10 | Signal catalog v0 (top 3 firmware per class) | S2 | Senior + firmware SME | 3 | P0-06 | Canonical names, units, semantics reviewed by firmware (firmware SME sign-off is human-owned; see branch P0-10-signal-catalog-v0 for a drafted v0 catalog covering Supercharger stall/cabinet, awaiting that review) | To do |
| P0-11 | Access to RMA, tickets, dispatch, outage, provisioning data | DISC | EM + senior DS | 3 | — | Read access granted; sample extracts loaded (granting real access is human/IT-owned; see branch P0-11-data-access-requests for drafted access requests per source) | To do |
| P0-12 | Privacy and security review kickoff | GOV | EM | 2 | — | Data classification drafted; residential and utility-site reviews booked (booking the real reviews is human-owned; see branch P0-12-privacy-security-review for a drafted data classification and review brief) | To do |
| P0-13 | Profiling report and design lock | DISC | Staff | 1 | P0-06–10 | Lateness horizon, grains and firmware asks signed off (Gate 0) (sign-off is human-owned; see branch P0-13-profiling-report-design-lock for a consolidated report with a recommended 24h lateness horizon and an unchecked sign-off checklist, ready for review) | To do |

## Phase 1 · Core pipeline, Supercharger pilot (months 2–5)

16 tickets, \~32 engineer-weeks. Goal: Stages 0–2 in production for stalls and cabinets, with dedup, late data and replay proven.

| ID | Ticket | Component | Owner level | Est. (wk) | Depends on | Done when | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P1-01 | Production ingest buffer with accept-and-spool | INGEST | Senior | 3 | P0-13 | Load test at 10x current Supercharger rate with zero loss (accept-and-spool logic built and proven zero-loss under simulated backpressure/outage at ~6.5k synthetic messages; the literal "10x current rate" can't be honestly claimed - no real baseline exists without P0-01's audit, no production-sized infra exists without P0-02's Terraform baseline - see branch P1-01-ingest-buffer) | To do |
| P1-02 | Stage 0 compaction and retention tiering | S0 | Mid | 1 | P1-01 | Hourly compaction; 30-day hot then archive lifecycle active (compaction logic built and proven message-safe without violating invariant 1 - consolidated objects live in a separate prefix, originals never touched; lifecycle Terraform written but unverified - no terraform/tofu/docker in this sandbox, and P0-02's real infra still isn't up - see branch P1-02-stage0-compaction) | To do |
| P1-03 | Parser framework with versioned per-firmware parsers | S1 | Senior | 3 | P0-10 | Parsers registered by firmware; unknown formats routed to quarantine (framework built with one demo parser registered, all other firmware/classes correctly quarantine - see branch P1-03-parser-framework; full real coverage across firmware versions is P1-04) | Done |
| P1-04 | Supercharger stall and cabinet parsers | S1 | Mid | 2 | P1-03 | Top firmware versions parse with fixture tests from real payloads (all 5 firmware/class pairs registered, zero quarantined on full generator output; "real payloads" is still the synthetic generator - real Supercharger data needs P1-01 - see branch P1-04-supercharger-parsers) | Done |
| P1-05 | Timestamp sanity checks and quarantine table | S1 | Mid | 1.5 | P1-03 | Epoch, future and implausible times quarantined with reason codes (classification logic built and tested against synthetic data, matching P0-07's threshold/reasoning; writing to a real quarantine table is follow-up wiring, not built here - see branch P1-05-timestamp-sanity-quarantine) | Done |
| P1-06 | Idempotent merge on natural key into event-time partitions | S1 | Senior | 2 | P1-03 | Replaying a message N times yields exactly one row (proven against an in-process store keyed on device_id/device_ts_ms/payload_hash, replay and partial-overlap tests pass; real Iceberg MERGE wiring is follow-up, not built here - see branch P1-06-idempotent-merge) | Done |
| P1-07 | Dirty-keys table from incremental changes | PLAT | Senior | 2 | P1-06 | Every late merge records its (device, hour) keys (built as a wrapper around P1-06's merge store, only marks a device-hour dirty when new data lands in an already-populated hour, not first-time population - see branch P1-07-dirty-keys-table) | Done |
| P1-08 | Stage 2 canonicalization via signal catalog | S2 | Senior | 2.5 | P1-04 | Canonical names, types and units for all pilot signals (all stall/cabinet raw fields from the P0-10 draft catalog canonicalize; catalog itself still unreviewed by firmware SMEs - see branch P1-08-stage2-canonicalization; site_id has no catalog entry and is currently dropped, worth a look) | Done |
| P1-09 | Per-device clock offset and corrected event time | S2 | Senior | 2 | P1-08 | Offset estimates stored; corrected time within agreed tolerance on test set (median-of-(arrival-device_ts) per-device estimator, stored in a ClockOffsetStore, 5s tolerance validated against directly-constructed rows with known injected offsets - the real generator's arrival_ts_ms doesn't carry independent ground truth on this branch's lineage, see branch note; no durable persistence/scheduled recompute yet - see branch P1-09-clock-offset) | Done |
| P1-10 | Row-level quality flags (range, stuck, counter reset, jumps) | S2 | Junior (paired) | 2 | P1-08 | Flags populated; no rows deleted; flag rates on dashboard (all four flags implemented and tested; range from catalog valid_range, stuck via 5-consecutive-identical-value threshold on float fields, counter_reset scoped to energy_delivered_kwh honoring session boundaries, jump via an own-judgment per-field max-rate table that is explicitly not firmware-SME-reviewed; dashboard/reporting is future UI work, out of scope here - see branch P1-10-row-quality-flags) | Done |
| P1-11 | Completeness and lateness sidecar (device × hour) | TH | Mid | 2.5 | P1-08 | Expected, on-time, late, missing counts per device-hour (expected_interval_s defaults from the generator's own cadence as an explicit P2-01 placeholder, on-time threshold reasoned from P0-08's documented batching-lateness scale; "missing" honestly scoped as "not yet seen by this run," not confirmed permanent loss - see branch P1-11-completeness-lateness-sidecar) | Done |
| P1-12 | Last-seen snapshot and silence-episode job | TH | Mid | 2 | P1-01 | Runs every 15 min; opens and closes episodes with cause field (snapshot/episode logic built and tested across repeated ticks - 30min silence threshold reasoned from job cadence + generator's outage model; cause field is honestly just "unknown" pending real correlation data; no real 15-min scheduler deployed - see branch P1-12-last-seen-silence) | To do |
| P1-13 | Targeted Stage 2 recompute from dirty keys | PLAT | Senior | 2 | P1-07 | Late data recomputes only touched device-hours (wires P1-07's dirty-keys table to P1-08's canonicalization; proven against a control device-hour that is never touched by a late merge and provably absent from recompute output; in-process O(n) scan stand-in, not viable against a real Iceberg table - see branch P1-13-stage2-dirty-recompute) | Done |
| P1-14 | Replay-from-Stage-0 determinism test | PLAT | Mid | 1.5 | P1-13 | Rebuild of a closed window matches production exactly (proven: messages landed to and read back from a moto-mocked Stage 0, re-run through parse/merge/canonicalize independently, compared by natural key against an in-memory "production" run - exact match, and a deliberate mismatch is provably detected; in-process stand-ins only, not a real Iceberg write-path replay - see branch P1-14-replay-determinism) | Done |
| P1-15 | Pipeline observability: freshness, completeness, cost | PLAT | Senior SRE | 2 | P1-01 | Dashboards and alerts live; watchdog runs outside orchestrator (three metrics built and tested, watchdog verified dagster-independent at runtime; "dashboards and alerts live" needs real observability infra that doesn't exist here - see branch P1-15-pipeline-observability) | To do |
| P1-16 | Late-data backfill end-to-end test and runbook | PLAT | Junior + senior | 1 | P1-13 | Simulated 72 h outage backfill lands correctly (Gate 1) (proven end to end with real numbers - 2 outage devices, 288 buffered rows landing as one batch, exactly the 2 boundary hours go dirty, targeted recompute touches only those, replay is idempotent; runbook at docs/runbooks/late-data-backfill.md - see branch P1-16-backfill-e2e. "(Gate 1)" not resolved as a formal sign-off; CLAUDE.md marks gate reviews human-owned generally, kept To do pending that call) | To do |

## Phase 2 · Enrich and detect, add Megapack (months 4–8)

15 tickets, \~38 engineer-weeks. Goal: Stage 3 tables and the first credible detectors, with Megapack and Powerpack onboarded.

| ID | Ticket | Component | Owner level | Est. (wk) | Depends on | Done when | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P2-01 | Device history dimension (firmware, hardware rev, cell lot, site, climate) | S3 | Mid | 3 | P0-11 | As-of joins return the firmware a device ran at any timestamp | To do |
| P2-02 | Ingest external sources: dispatch, outages, weather, RMA and tickets | S3 | Mid | 3 | P0-11 | Daily loads with freshness checks | To do |
| P2-03 | Stage 3a time grid (5-min / 1-min) with coverage and mode labels | S3 | Senior | 3 | P1-13 | Grid rebuilt incrementally from dirty keys (1-min Supercharger grid built - stall mode from catalog session_state, cabinet mode is an unreviewed derivation from contactor_closed; targeted rebuild proven against a control hour, mirroring P1-13's proof; no Powerwall/Powerpack 5-min grid - no fixtures exist for those classes yet - see branch P2-03-stage3-time-grid) | Done |
| P2-04 | Event detection and event table (sessions, dispatch, outages) | S3 | Senior + DS | 3 | P2-02 | Events reconcile with session logs and dispatch records | To do |
| P2-05 | Device-day feature table | S3 | Mid | 2 | P2-03 | Features for all pilot devices, recomputed on late data (coverage ratio, longest gap, minutes-by-mode built from real P2-03 grid data only, nothing fabricated; dirty-day rebuild reasoned explicitly since features don't decompose per-hour - see branch P2-05-device-day-features) | Done |
| P2-06 | Detector plugin framework and findings schema | DET | Senior DS + senior | 3 | P2-03 | Detectors declared in config; findings carry version, evidence, quality (registry + YAML config + one demo detector; findings are content-hash-deduped, append-only, provisional/final transitions are new versions never mutations - core invariant 6 proof personally verified; provisional-to-final timing itself is P2-11's job - see branch P2-06-detector-framework) | Done |
| P2-07 | Cohort-day statistics (median, MAD, rates) | S4 | Mid DS | 2 | P2-01 | Reference stats per cohort per day | To do |
| P2-08 | Rule detectors v1 (session failures, derates, module faults, thermal) | DET | Mid | 2 | P2-06 | Rules live with thresholds reviewed by charger SMEs (all 4 rules built and tested - module_fault_v1/thermal_event_v1 on catalog fault codes, session_failure_v1 on unresolved session_state==fault, derate_v1 on a sustained-drop-then-plateau heuristic distinguishing real derates from the fixture's modeled natural taper; every threshold's provenance documented as catalog-value or own-judgment; no charger SME review has happened or could happen here - see branch P2-08-rule-detectors-v1) | To do |
| P2-09 | Cohort outlier detectors (robust z by firmware, hardware, site) | DET | Senior DS | 2 | P2-07 | Outliers flagged with peer comparison as evidence | To do |
| P2-10 | Telemetry-health detectors (correlated vs isolated dropout) | TH | Mid DS | 2 | P1-12 | Dropout attributed by firmware, region, carrier where known | To do |
| P2-11 | Provisional-to-final window lifecycle and finding versions | S4 | Senior | 2 | P2-06 | Windows finalize after horizon; revisions recorded, never overwritten (lifecycle.py's finalize_due_windows() walks the store, finalizes any PROVISIONAL finding whose hour ended ≥ 24h ago (P0-13's recommended horizon, cited, not yet Gate-0-signed-off), idempotent, inclusive boundary tested; original provisional row proven byte-identical in history after finalization - see branch P2-11-window-lifecycle) | Done |
| P2-12 | Backtest harness against RMA and tickets | DET | Senior DS | 3 | P2-06 | Precision and lead time reported per detector version | To do |
| P2-13 | Megapack and Powerpack parsers, incl. cell-level child table | S1 | Mid + junior | 3 | P1-03 | Top firmware versions parse; cell table partitioned and compacted | To do |
| P2-14 | Megapack and Powerpack onboarding | S2 | Senior | 2 | P2-13 | Catalog, completeness expectations and capacity signed off | To do |
| P2-15 | Battery detectors v1 (cell imbalance, capacity fade, dispatch under-delivery) | DET | Senior DS | 3 | P2-14 | Backtested; reviewed by battery SMEs (Gate 2) | To do |

## Phase 3 · Powerwall at scale, dashboards (months 7–11)

12 tickets, \~31 engineer-weeks. Goal: all 1M Powerwalls onboarded safely, and findings reaching the people who act on them.

| ID | Ticket | Component | Owner level | Est. (wk) | Depends on | Done when | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P3-01 | Ingest capacity plan and autoscaling for Powerwall | INGEST | Senior SRE | 2 | P1-01 | Scaling policy sized from measured peak and surge rates | To do |
| P3-02 | 10x reconnection surge load test | INGEST | Senior SRE + senior | 2 | P3-01 | Synthetic 72 h regional backfill ingested with zero loss | To do |
| P3-03 | Powerwall parsers and signal catalog | S1 | Mid + junior | 3 | P1-03 | Top firmware versions parse; catalog reviewed by firmware | To do |
| P3-04 | Staged Powerwall rollout (1% → 10% → 100%) | S0 | Senior | 4 | P3-02 | Each stage holds SLOs for a week before the next | To do |
| P3-05 | Mode-aware expected counts from outage and VPP records | TH | Mid DS | 2 | P2-02 | Completeness uses event-mode expectations; lower bound otherwise | To do |
| P3-06 | Scheduled reconciliation for cross-device outputs | PLAT | Senior | 2 | P2-11 | Cohort and mart recompute bounded in cost after an outage | To do |
| P3-07 | Stage 4 fleet health marts | S4 | Mid analytics | 3 | P2-11 | Region, site, class × day marts; dashboards never scan Stage 3 | To do |
| P3-08 | Device health snapshot table | S4 | Mid analytics | 1.5 | P3-07 | One row per device with score, open findings, last seen | To do |
| P3-09 | Dashboards: fleet, cohort, site, telemetry health | UI | Mid analytics + junior | 3 | P3-07 | Used in weekly ops review | To do |
| P3-10 | Alert routing to NOC and support queues | S4 | Senior | 3 | P2-11 | Dedup and suppression live; utility-site findings reach ops | To do |
| P3-11 | Lifetime table and survival analysis by cohort | DET | Senior DS | 3 | P2-12 | Failure curves per cohort; silence treated as censored | To do |
| P3-12 | Privacy controls for residential data | GOV | Senior + SRE | 2 | P0-12 | Access tiers, retention limits and aggregation approved (Gate 3) | To do |

## Phase 4 · Harden and hand off (months 10–12)

8 tickets, \~14 engineer-weeks. Goal: a pipeline that runs on SLOs and runbooks, plus the evidence-backed firmware asks for year 2.

| ID | Ticket | Component | Owner level | Est. (wk) | Depends on | Done when | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P4-01 | SLOs and error budgets per device class | PLAT | Senior SRE | 1 | P1-15 | SLOs published with error-budget alerts | To do |
| P4-02 | Runbooks and game day | PLAT | Senior SRE + team | 2 | P4-01 | Ingest outage, bad parse and mass backfill drills passed | To do |
| P4-03 | On-call rotation and escalation paths | PLAT | EM | 1 | P4-02 | Rotation staffed; escalation to firmware and ops agreed | To do |
| P4-04 | Cost optimization pass | PLAT | Senior | 2 | P3-04 | Cost per device at or below target | To do |
| P4-05 | Firmware requirements from measured blind spots | DISC | Staff | 2 | P3-05 | Ranked asks (sequence numbers, buffering, profiles) with data | To do |
| P4-06 | Detector precision review and tuning with SMEs | DET | Senior DS | 2 | P2-12 | High-severity precision at or above target | To do |
| P4-07 | Year-2 ML detector scoping | DET | Mid DS | 2 | P4-06 | Proposal with features, labels and backtest plan | To do |
| P4-08 | Consumer documentation and onboarding guide | UI | Mid analytics + junior | 1.5 | P3-09 | Table and dashboard docs published (Gate 4) | To do |

**Critical path:** P0-05 → P0-13 → P1-03 → P1-06 → P1-07 → P1-13 → P2-03 → P2-06 → P2-11 → P3-06 → P3-04. Slips in dedup and dirty-key recompute delay everything downstream, so staff those tickets with the strongest seniors.
