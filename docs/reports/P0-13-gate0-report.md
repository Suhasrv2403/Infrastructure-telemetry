<!--
PROVENANCE: copied from branch `P0-13-profiling-report-design-lock` (commit 185df2d), path `docs/profiling/P0-13-gate0-report.md`.
This branch is not yet merged into trunk. This file is a read-only snapshot
for easy reading; the source branch remains the canonical, editable copy.
Consolidated into docs/reports/ on trunk (P0-04-orchestrator-partition-model).
-->

# P0-13: Gate 0 profiling report and design-lock prep

**Ticket:** P0-13 ("Profiling report and design lock"), Phase 0, DISC, Staff.
**Depends on:** P0-06, P0-07, P0-08, P0-09, P0-10.
**Done when (per Build backlog.md):** "Lateness horizon, grains and firmware asks signed off
(Gate 0)."

## Status: prep material for a human-owned gate, not a sign-off

Per `CLAUDE.md`, gate reviews are explicitly **human-owned** ("Humans own: gate reviews,
anything touching prod data, firmware fault-injection tests, access requests"). **This document
does not close P0-13 and does not constitute Gate 0 sign-off.** It consolidates the findings
already produced on branches `P0-06-arrival-shape-profiler`, `P0-07-timestamp-clock-quality-profiler`,
`P0-08-lateness-duplicate-profiler`, `P0-09-device-retry-behavior-profiler` and
`P0-10-signal-catalog-v0` into the single document a Staff reviewer and the Gate 0 approver
would actually read, with a specific recommended lateness horizon and a firmware-ask list ready
for review. Everything below that is presented as a recommendation is exactly that -
recommended, not approved. The checklist at the end is where a human records the actual
decisions.

## What was measured vs. what's still assumed

Every number in this report - arrival shape, clock quality, lateness, duplicates - was
**measured**, but measured against the **synthetic Supercharger fixture generator**
(`tests/fixtures/generators/supercharger.py`), run through P0-05's Stage 0 capture path, not
against real Supercharger telemetry. Real device/network access is gated on later tickets (see
`pipeline/stage0_landing/capture.py`'s scope note), so P0-06 through P0-08 are the best evidence
that currently exists, but they describe the generator's modeled behavior, not confirmed
production reality. Treat every rate, percentile and distribution below as "true of this
synthetic proxy, at seed 1337," pending re-validation once real Stage 0 objects exist.

Two gaps are larger than "re-run against real data eventually" and are worth naming explicitly
at this gate, because they're exactly the kind of thing a design lock could get wrong without
them:

- **P0-09 (fault injection) has not been run against real hardware.** Branch
  `P0-09-device-retry-behavior-profiler` documents a synthetic heuristic detector's honest
  accuracy against the generator's own ground truth (recall ~40-65%, precision ~3-4% - most
  flagged candidates are false alarms) and a concrete real-hardware test plan, but the actual
  "retry, buffer or drop behavior documented per firmware" deliverable requires a human/firmware
  team to run that plan against real devices. **We do not know today how real Supercharger
  firmware behaves on reconnect** - whether it buffers and flushes, drops silently, retries
  aggressively, or reboots - and how large its local buffer is. This is the single biggest
  unknown feeding into the lateness-horizon recommendation below.
- **P0-10 (signal catalog v0) has not been reviewed by a firmware SME.** Branch
  `P0-10-signal-catalog-v0` drafts a full field-by-field catalog for both device classes,
  inferred entirely from reading the generator's source, with an explicit `catalog_status:
  v0_draft_unreviewed` flag and a per-field `source_ref` for a reviewer to check against real
  hardware. Until that review happens, none of its canonical names, units or fault-code meanings
  should be treated as correct, and P1-03/P1-04 real parsers should not be built against it.

Everything else in this report (arrival shape, clock quality, lateness/duplicates) is measured
data from the synthetic proxy, not an open question about whether the measurement happened -
the open question is whether the generator's model matches real device behavior, which only
real telemetry can answer.

## Arrival shape summary (P0-06)

Source: `docs/profiling/P0-06-arrival-shape.md` on `P0-06-arrival-shape-profiler`, run against
`GeneratorConfig()` defaults (seed 1337), 2,631 messages / 17,111 readings across 5
`(device_class, firmware)` groups.

- **Protocol is deterministic per class today**: every `supercharger_stall` message uses
  `mqtt_batch`, every `supercharger_cabinet` message uses `https_poll` (100% each way, no
  variation observed). Worth re-checking once real telemetry exists, since a real fleet could
  mix protocols within a class mid-migration in a way the generator doesn't model.
- **Batching**: uniformly distributed batch sizes in `[1, 12]` for all groups (generator's
  `max_batch_size=12`, no strong skew). Cabinets batch very slightly larger on average
  (mean ~6.4-6.8) than stalls (mean ~6.2-6.3) - a minor effect from group size, not a real
  protocol distinction.
- **Message size**: almost entirely in the 512 B - 4 KB range (single-reading messages ~500-600
  B, full 12-reading batches just under 4 KB). Stall messages run slightly larger than cabinet
  messages at the same batch size (extra fields: session id, fault code, multiple
  power/energy/temperature values).
- **Sizing takeaway for the design lock**: nothing here challenges an envelope-size assumption
  in the 4-8 KB range per message; Stage 0/1 storage and Iceberg file-size planning can use "a
  few KB per message, tens of readings per KB" as a working number, revisited once real payloads
  exist (real payloads will likely be marginally *smaller* than measured here, since the
  measured sizes include a generator-only debug field a real message won't carry).

## Clock quality summary (P0-07)

Source: `docs/profiling/P0-07-timestamp-clock-quality.md` on
`P0-07-timestamp-clock-quality-profiler`, same generator defaults.

| device_class | firmware | missing | epoch_default | future | drift median (s) |
|---|---|---:|---:|---:|---:|
| supercharger_cabinet | 1.8.2 | 0.98% | 2.00% | 0.59% | 212.5 |
| supercharger_cabinet | 1.9.0 | 1.13% | 1.29% | 0.85% | 241.0 |
| supercharger_stall | 2.1.4 | 1.15% | 1.37% | 1.15% | 50.0 |
| supercharger_stall | 2.3.0 | 1.46% | 1.58% | 0.63% | 60.5 |
| supercharger_stall | 3.0.1 | 0.85% | 1.59% | 0.63% | 60.0 |

- **Missing/epoch/future rates are flat across firmware within a class** (roughly 0.9-1.6%
  each) - but this is a fact about the generator (these rates are global constants in
  `GeneratorConfig`, not scaled by its firmware-quirk multiplier the way outage/drop behavior
  is), not a claim that real firmware will show equally flat clock-quality issues across
  versions. Real firmware may show much more per-firmware spread; nothing here should be read
  as "clock quality is firmware-independent."
- **The "drift" numbers in this generator run are not trustworthy as clock-skew estimates.**
  P0-07's own report found the generator anchors a batch's `arrival_ts_ms` off the device's own
  (already-skewed) last `device_ts_ms`, so the injected per-device clock offset mostly cancels
  out of the drift calculation (measured correlation ≈ -0.07 between injected offset and
  estimated drift). The ~50-240s "drift" figures above mostly reflect batch-position latency
  (reading interval x batch size), not real clock skew. The estimation *method* is validated by
  hand-built unit tests with independent arrival/device timestamps; it's the generator's arrival
  simulation, not the profiler, that undermines the drift numbers here.
- **Implication for P1-09 (per-device clock offset correction)**: P1-09 cannot validate its
  offset-estimation approach against this generator's drift output as-is, because the
  generator's arrival/device timestamps aren't independent. P1-09 should either (a) fix the
  generator's arrival simulation to model an independent ingest-side clock before using it as a
  test oracle, or (b) validate primarily against hand-constructed fixtures with independently
  set device/arrival times (as P0-07's own unit tests already do) until real telemetry provides
  a genuine independent arrival clock. Missing/epoch/future rates (0.9-2.0%) are low enough that
  a downstream consumer dropping clock-flagged readings loses only a small fraction of data on
  this synthetic proxy - useful as a sanity bound, not a production number.

## Lateness and duplicates summary (P0-08), and recommended lateness horizon

Source: `docs/profiling/P0-08-lateness-duplicates.md` on `P0-08-lateness-duplicate-profiler`,
same generator defaults, computed on Stage 1's actual future merge key
(`device_id, device_ts_ms, payload_hash`).

| device_class | median lateness (s) | p90 (s) | p99 (s) | max (s) |
|---|---:|---:|---:|---:|
| supercharger_cabinet | 240.0 | 605.0 | 71,101.4 | 1,143,515.0 |
| supercharger_stall | 60.0 | 946.0 | 597,310.0 | 1,181,186.0 |

| device_class | duplicate key rate | pure-duplicate messages |
|---|---:|---:|
| supercharger_cabinet | 2.64% | 2.52% |
| supercharger_stall | 2.55% | 2.60% |

**Reading the distribution.** P0-08's own report is explicit that the body of this distribution
(median, p90) is dominated by a batching artifact, not genuine late arrival: the generator
anchors a batch's `arrival_ts_ms` to its *last* reading's timestamp, so every earlier reading in
a batch shows "lateness" purely from sitting earlier in the batch, with zero network delay
involved - up to ~660s for a full 12-reading cabinet batch at 60s cadence, ~165s for a full
stall batch at 15s cadence. The extreme tail (p99, max, and the large negative min) is even less
representative: it's confirmed to come from a **generator-specific bug**, not modeled network or
retry behavior - when a batch's *last* reading has a missing/epoch-default timestamp, the
generator's arrival anchor silently snaps to a fixed synthetic "now," dragging every other
reading in that one corrupted batch to ~7 days "late." This affects only 2.19-2.60% of messages
per class, and accounts for essentially all of the p99/max values above. The negative min
(~-13.8 days) is the mirror case - a future-corrupted device timestamp, a real and useful signal
("this device's clock is wrong") but not "lateness" in the network-delay sense.

**Recommended lateness horizon: 24 hours**, as a starting number for Gate 0 to confirm or
override.

Reasoning:

- The genuinely informative part of the measured distribution - p90 lateness of 605s (cabinet)
  and 946s (stall), i.e. roughly 10-16 minutes - is itself an overestimate of real network
  lateness (it's mostly batching position), so 24h has well over 100x margin over what normal
  operation produces in this synthetic proxy.
- The p99/max tail should explicitly **not** be used to size the horizon: P0-08's report says
  plainly that this specific shape (~7-day snap-to-now) is a generator artifact tied to a
  specific bug in `_emit_device_messages`, not something to extrapolate to production lateness
  expectations. A horizon sized to cover it (multiple days to weeks) would be sizing against a
  bug, not a real failure mode.
- The real open question a lateness horizon should cover is **outage-driven late arrival on
  device reconnect** - and that's exactly what P0-09 has not yet measured against real hardware.
  P0-09's proposed real fault-injection test plan tests blackout durations up to 4 hours, with a
  minimum 2x-duration recovery observation window (so up to ~8h of observation per condition).
  24h gives roughly 3-6x margin over the longest currently-planned real test scenario, which is
  a reasonable placeholder given real device buffer capacity is still unknown.
- 24h also keeps the operational cost of "windows provisional until the lateness horizon passes"
  (CLAUDE.md invariant 6) bounded to a size Stage 2's dirty-key recompute (invariant 5) can
  handle as routine, not exceptional, work.

**This number should be revisited, not treated as final**, once P0-09's real fault-injection
results establish actual device buffer/reconnect behavior. If real firmware turns out to buffer
and flush after outages longer than 24h, either the horizon needs to grow or "very late" data
past the horizon needs an explicit backfill-only policy rather than routine recompute - a
decision for whoever owns Gate 0, informed by real P0-09 data once it exists.

**Duplicates**: ~2.5-2.6% of reading-keys duplicated for both classes, closely tracking the
generator's configured 3% message-retransmit rate. Every observed duplicate in this run is a
pure, full-envelope retransmit (0% partial-overlap messages) - the generator doesn't currently
model a device re-sending only part of a batch. P1-06 (idempotent merge) should design and test
for de-duplication as routine work on every batch (not a rare edge case) using exactly the
`(device_id, device_ts, payload_hash)` key already fixed by CLAUDE.md invariant 2, and should add
hand-built fixtures for partial-overlap and same-key/different-payload cases that this generator
run doesn't exercise.

## Grain confirmation

Walking CLAUDE.md's existing "Stages and grain" section against what P0-06-P0-08 actually
found:

- **Stage 0 (one row per message, partitioned by arrival hour, immutable, append-only)**:
  nothing in P0-06-P0-08 contradicts this. Arrival-hour partitioning is orthogonal to the
  message-size/batching findings (P0-06), and P0-08's lateness analysis is specifically *about*
  the gap between arrival and event time - a gap that Stage 0's arrival-based partitioning is
  designed to be agnostic to. **Holds as-is.**
- **Stage 1 (one row per reading, keyed by `device_id, device_ts`, MERGE dedup on
  `device_id, device_ts, payload_hash`)**: P0-08 measured real duplicate traffic
  (~2.5-2.6% of reading-keys) on exactly this key and confirmed the key correctly recovers every
  injected retransmit with no false positives/misses in this generator model. This is direct
  evidence the chosen merge key works, not just a design assumption. **Holds, with evidence.**
  One caveat: P0-08 only measured pure/full-envelope duplicates; partial-overlap retransmits
  (same key, subtly different scope) aren't yet exercised by any profiler, so the merge key's
  behavior there is untested, not contradicted.
- **Stage 1+/Stage 2 partitioning by device_class x event date/hour, bucketed by device_id
  hash, never by device_id (invariant 4)**: nothing in P0-06-P0-08 touches partitioning
  directly, but P0-08's lateness distribution is relevant context: given the recommended 24h
  horizon, a late-arriving reading can land in an event-hour partition that's already been
  written and closed, which is exactly what invariant 5 (dirty-key recompute) exists to handle.
  No contradiction found; the grain choice is consistent with what a 24h horizon would require
  of Stage 1 write behavior.
- **Stage 2 (same grain as Stage 1, canonical signals/units, corrected event time)**: P0-07's
  clock-quality findings bear directly on the "corrected event time" part of this grain. The
  finding that this generator's own drift estimate isn't currently trustworthy as a clock-skew
  measurement doesn't invalidate the *grain* (still one row per reading, corrected event time is
  still the right column to carry), but it does mean P1-09 needs a better test oracle than this
  generator's current output before it can validate its correction logic - a data-quality gap
  in the profiling input, not a grain problem.
- **Overall: no measured finding from P0-06-P0-08 contradicts any Stage 0/1/2 grain in
  CLAUDE.md.** The one flag worth carrying into Gate 0 is not about grain but about horizon
  sizing (above) and about the P1-09 test-oracle gap (clock quality section) - neither requires
  changing a partitioning or grain decision, both are implementation-readiness notes for the
  tickets that consume this profiling work.

## Firmware asks

An early, synthetic-data-informed draft of the kind of list P4-05 ("Firmware requirements from
measured blind spots") will eventually formalize with real fleet data. Ranked by how directly
each blocks a near-term Phase 1 ticket:

1. **Reconnect/buffer behavior per firmware, from real fault injection (blocks P1-01 sizing
   and the lateness-horizon decision above).** P0-09's synthetic heuristic detector could not
   reliably distinguish "one delayed message" from "a true buffer-then-flush" using arrival
   timing alone (precision ~3-4% against the generator's own ground truth). Ask firmware:
   for each in-scope firmware version, does the device buffer locally and flush on reconnect,
   drop silently, retry aggressively, or reboot/reset its connection stack on a network outage;
   what's the local buffer's capacity in readings/time; does behavior change past some outage
   duration. This is exactly P0-09 Part B's real test plan - ask firmware to co-run it, not
   just answer from documentation, since P0-09's synthetic hypotheses (e.g. that a flushed
   buffer's `device_ts` span, not its message-level batch size, is the reliable flush signal)
   need real-device confirmation.
2. **Signal catalog review (blocks P1-03/P1-04 real parsers).** `catalog/signals.yaml` on
   `P0-10-signal-catalog-v0` is a complete v0 draft (canonical names, units, fault-code
   enumerations, valid ranges) inferred entirely from generator source, explicitly marked
   unreviewed. Ask firmware SMEs to confirm or correct, field by field, using the catalog's own
   `source_ref` citations as the starting point for each check - particularly the stall/cabinet
   fault-code meanings (1001/1042/2010 and 3001/3002), which are the generator author's
   placeholder commentary, not a real fault-code registry.
3. **Retry/backoff pattern and reconnect load shape (informs P1-01's accept-and-spool sizing
   beyond per-device buffer capacity).** Whether retries after an outage are fixed-interval,
   exponential-backoff, or jittered, and whether the device re-authenticates every retry or
   reuses a session, changes how much load a fleet-wide reconnect storm (e.g. after a shared
   network event) puts on ingest. Not measurable from the synthetic proxy at all (the generator
   doesn't model a wire-level retry protocol); this needs firmware documentation or a real
   packet capture from the P0-09 test plan's network-side capture step.
4. **Per-firmware clock-quality variance (informs whether P1-09's per-firmware handling needs
   to differ, lower priority since current synthetic rates are low and flat).** The generator
   currently models missing/epoch/future timestamp rates as firmware-independent constants; ask
   whether real firmware shows meaningfully different clock-quality behavior by version (e.g.
   an older firmware with a cheaper/no RTC backup battery producing more epoch-default
   timestamps after power loss) so P1-09 knows whether to design for per-firmware correction
   parameters or a single fleet-wide one.

## Sign-off checklist (human reviewer only)

The items below are decisions for the Gate 0 reviewer to make, not conclusions this document
draws on its own. Nothing in this repo can check these boxes.

- [ ] Lateness horizon of **24 hours** approved (or a different value set, with reasoning
      recorded)
- [ ] Stage 0/1/2 grains as documented in CLAUDE.md confirmed with no changes required
- [ ] Firmware ask list above approved to send to the firmware team, in the ranked order given
      or as re-prioritized by the reviewer
- [ ] P0-09 (real fault injection) and P0-10 (firmware SME catalog review) explicitly
      acknowledged as open, and scheduled/owned, before any ticket that depends on their
      results (P1-01, P1-03, P1-04, P1-09) is allowed to start
- [ ] Gate 0 approved to close, with this report and the branches it consolidates
      (`P0-06`, `P0-07`, `P0-08`, `P0-09`, `P0-10`) attached as the supporting record
