# Runbook: late-data backfill (regional outage recovery)

**Ticket:** P1-16 ("Late-data backfill end-to-end test and runbook"), Phase 1, tagged "(Gate 1)"
in Build backlog.md. **Proven by:** `tests/unit/test_late_data_backfill_e2e.py`, built against
the purpose-built 72h-outage fixture in `tests/fixtures/generators/outage_backfill.py`.

This runbook covers the specific failure mode this ticket exists to prove is handled correctly:
a set of devices (or a whole site/region) stops delivering telemetry for an extended period -
long enough to matter operationally, not routine seconds-to-minutes network lateness - then,
once connectivity is restored, everything buffered during the outage lands as one large late
batch. It does **not** cover routine lateness within the 24h lateness horizon (see
`docs/profiling/P0-13-gate0-report.md` on branch `P0-13...` for that number and its rationale) -
that's handled automatically, every batch, by the same merge/dirty-key/recompute chain below,
with no operator involvement. This runbook is for the case big enough that someone should
understand what happened and confirm it landed correctly.

## 1. Recognizing an outage/backfill situation

**Today, detection is a manual/human step.** There is no automated outage detector or on-call
alert in this codebase yet - that's telemetry-health / on-call territory (P1-12 "last-seen
snapshot + silence episodes", P4-02/P4-03), not built as of this ticket. Until that exists, an
operator (or whoever's monitoring ingestion) recognizes this situation from one or more of:

- A sustained drop in Stage 0 message arrival rate for a device, site or region, visible in
  whatever ingest-side metrics/dashboards exist (arrival counts, `pipeline/stage0_landing/
  capture.py`'s `CaptureResult` counters logged per run).
- A report from the field/network team that a site or region lost connectivity (backhaul,
  cell, site network issue).
- After the fact: a sudden large spike in Stage 0 arrivals for a device/site whose
  `device_ts_ms` values cluster in a past window well outside the routine ~24h lateness
  horizon - i.e. a batch that is very obviously "a burst of old data", not routine late
  arrival.

Once you suspect this, the operational question is: **which devices, and what time window?**
You need both before doing anything below - the backfill batch itself (once it lands) will
carry real `device_ts_ms` values in `device_ts_ms`/`event_hour`, so this doesn't need to be
exact, just enough to sanity-check the numbers in step 3 against what you expect.

## 2. Landing the backfill: what to run

The backfill batch is Stage 0 messages like any other - it goes through the exact same chain as
routine traffic, just with a much larger and older set of readings in it. There is no special
"backfill mode"; the chain's own idempotent-merge and dirty-key-tracking semantics are what
make an arbitrarily-large, arbitrarily-late batch safe to just run through normally.

1. **Parse.** `parsers/framework.py`'s `parse_messages(messages, *, registry=None)` on the
   landed Stage 0 envelopes for the affected devices/window. Check the returned `ParseResult`:
   - `result.reconciles()` must be `True` (or call `parsers.framework.reconcile(result)`,
     which raises `ReconciliationError` if not) - every message must be parsed or quarantined,
     never neither.
   - `result.messages_quarantined` should be 0 for known-good devices/firmware. Any
     quarantined messages (`result.quarantined`, each a `QuarantinedMessage` with a `reason`)
     need separate investigation - a backfill landing is not the place to silently lose
     messages to quarantine.
2. **Merge into Stage 1, tracking dirty keys.** Feed `result.rows` into a
   `pipeline.stage1_parsed.dirty_keys.DirtyKeyTrackingStore` wrapping the real Stage 1 store
   (`Stage1MergeStore` here; a real Iceberg-backed store once that exists - see merge.py's own
   scope note) via `tracking_store.merge(rows)`. Check the returned `MergeResult`:
   - `result.reconciles()` must be `True`.
   - `result.rows_inserted` is the count of genuinely new rows; `result.rows_already_present`
     is the count that were already there (0 on a first landing; equal to the full row count
     if you accidentally re-run an already-landed backfill - see step 4, this is expected and
     safe, not a bug).
   - This step is also where CLAUDE.md invariant 5's dirty-key marking happens automatically:
     a `(device_id, event_hour)` key is marked dirty only if that hour already had at least one
     row from an earlier merge call (see `dirty_keys.py`'s module docstring for the exact
     rule). A device-hour that is genuinely first-ever data (the device never reported before
     the outage, or the whole hour falls inside the outage window with nothing merged into it
     beforehand) is correctly **not** marked dirty - there's nothing downstream to invalidate.
3. **Recompute Stage 2 for the dirty keys.** Call
   `pipeline.stage2_canonical.targeted_recompute.recompute_dirty_device_hours(store,
   dirty_keys, catalog)` (where `dirty_keys` is `tracking_store.dirty_keys`, and `catalog`
   comes from `pipeline.stage2_canonical.canonicalize.load_catalog()`). This is what actually
   satisfies invariant 5 ("late data recomputes only dirty (device, hour) keys") - it pulls
   **only** the currently-dirty device-hours' rows and re-canonicalizes them, acknowledging
   each key once attempted. **Do not** run a full-partition Stage 2 recompute for a backfill of
   this shape - a large backfill is exactly the case invariant 5 exists to keep cheap (only the
   boundary hours that already had data need touching; the newly-populated interior hours get
   canonicalized whenever the normal forward Stage 2 pass next runs over them, same as any
   other first-time data - that ordinary forward path is not part of this runbook).

## 3. Verifying it landed correctly

Every object in the chain above carries the counts you need - this is not "eyeball the logs and
hope":

| Check | What it proves | How |
|---|---|---|
| `ParseResult.reconciles()` | no message silently dropped by the parser dispatch | `messages_seen == messages_parsed + messages_quarantined` |
| `ParseResult.messages_quarantined == 0` (for known-good devices) | no unexpected quarantine | inspect `result.quarantined` if nonzero |
| `MergeResult.reconciles()` | no row silently dropped by the merge | `rows_seen == rows_inserted + rows_already_present` |
| `MergeResult.rows_inserted` matches your expected new-row count | the backfill actually added what you think it added, not more/less | compare against the outage window's expected reading count (devices x readings/interval x outage duration) |
| dirty-key set (`tracking_store.dirty_keys.keys()`) is exactly the boundary hours you expect | dirty-marking fired only where it should - not on every hour the backfill touched | should be a **small** set (one entry per affected device per hour that had pre-outage data), not one entry per device-hour in the whole outage window |
| `TargetedRecomputeResult.reconciles()` | no row silently dropped during targeted recompute | `rows_seen == rows_recomputed + rows_failed` |
| `TargetedRecomputeResult.rows_failed == 0` (or explainable) | canonicalization didn't choke on the backfilled data | inspect `result.failed_rows` (row, `CastError`) pairs if nonzero |
| `len(tracking_store.dirty_keys) == 0` after recompute | every dirty key was acknowledged, nothing left to loop on | |
| A control device/hour you know was NOT affected still shows 0 dirty keys and is absent from recompute output | the backfill process didn't touch anything it shouldn't have | pick any device/hour outside the outage window and confirm |

If you re-run the *exact same* backfill batch a second time (e.g. you weren't sure it landed
and want to be safe, or a job retried after an apparent failure that actually succeeded), the
merge step is idempotent by construction (natural key
`(device_id, device_ts_ms, payload_hash)`, CLAUDE.md invariant 2): expect
`rows_inserted == 0`, `rows_already_present == <row count>`, and **no** new dirty keys (nothing
new was inserted, so nothing new can be marked late). This is exactly what
`tests/unit/test_late_data_backfill_e2e.py::test_replaying_the_same_backfill_batch_is_idempotent`
proves. It is always safe to re-run this runbook's step 2 again if you're unsure whether a
backfill already landed.

## 4. What's still a manual/human step today

- **Detecting the outage in the first place** (step 1) - no automated alerting exists yet.
  This is P1-12 (telemetry-health last-seen snapshot / silence episodes) and P4-02/P4-03
  (on-call, dropout-by-cohort) territory, not built as of this ticket.
- **Deciding the affected device/window set** to actually query/replay from Stage 0 - nothing
  automatically tells you "these N devices, this window" today; that comes from whatever
  evidence step 1 turned up.
- **Deciding whether a batch is "routine late" vs. "outage backfill"** - there's no hard
  automated line; the 24h lateness horizon (`docs/profiling/P0-13-gate0-report.md`) is a
  recommended, not-yet-signed-off placeholder for where "provisional" (CLAUDE.md invariant 6)
  should turn into "past the horizon, treat specially." A 72h-plus regional outage like the one
  this ticket's test simulates is unambiguously past any reasonable version of that horizon;
  a judgment call is still needed near the boundary.
- **Wiring this chain into a real scheduled job.** `parse_messages`/`DirtyKeyTrackingStore.
  merge`/`recompute_dirty_device_hours` are pure, storage-agnostic functions (see each
  module's own scope note) - there is no Dagster asset, alert, or runbook automation that
  calls them for you yet. An operator (or an ad hoc script) runs them today.
- **Reviewing quarantined messages or cast failures** surfaced by steps 2-3 above - those are
  handed back as data (`QuarantinedMessage`, `(row, CastError)` pairs) for a human to look at,
  never auto-resolved.

## 5. What this runbook does NOT cover

- Routine per-message lateness within the horizon - handled automatically by the same chain,
  every batch, no runbook needed.
- Full-partition Stage 2 backfill/recompute - CLAUDE.md invariant 5 requires that to be "an
  explicit, reviewed backfill job", separate from (and much rarer than) the targeted,
  dirty-key-driven path this runbook describes. If you ever find yourself needing to recompute
  an entire partition rather than a specific dirty-key set, that's a different, reviewed
  procedure, not this one.
- Stage-0-replay determinism (reproducing a closed window's production output exactly from
  Stage 0, CLAUDE.md invariant 7) - that's P1-14's scope, a related but distinct ticket.
