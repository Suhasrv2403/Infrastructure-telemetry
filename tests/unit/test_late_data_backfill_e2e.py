"""End-to-end test for a 72-hour regional outage followed by a late-data backfill.

Ticket: P1-16 ("Late-data backfill end-to-end test and runbook"), tagged "(Gate 1)" in Build
backlog.md. Its literal "done when" is "Simulated 72 h outage backfill lands correctly."

This drives the REAL chain, not a mock of it: parse_messages() (P1-03/framework.py) ->
DirtyKeyTrackingStore.merge() (P1-06's Stage1MergeStore, wrapped by P1-07's dirty-key
tracking) -> recompute_dirty_device_hours() (P1-13's targeted Stage 2 recompute). Nothing in
merge.py, dirty_keys.py, targeted_recompute.py or parsers/framework.py is modified or
monkeypatched - this file only calls their existing public API, on purpose-built fixture data
from tests/fixtures/generators/outage_backfill.py (see that module's docstring for why a new,
hand-specified generator was used instead of stretching supercharger.py's per-device outage
model, which caps at 3h, well short of the 72h regional outage this ticket needs).

Scope note: P1-09 (clock-offset correction), P1-10 (quality flags) and P1-11 (completeness
sidecar) are NOT in this branch's lineage (see this ticket's brief) - a recomputed row here is
exactly what canonicalize_row produces today: raw device_ts_ms, no quality flags. P1-05's
timestamp-sanity module is also not part of this branch's lineage (confirmed via `git
merge-base --is-ancestor`, despite being listed as available in the ticket brief - it lives on
a sibling branch, `P1-05-timestamp-sanity`, not yet merged into this one's history) - this
scenario's fixtures are hand-built with already-sane device_ts_ms values throughout, so no
timestamp-sanity step is exercised or needed here. P1-14 (replay-from-Stage-0 determinism) is a
distinct, separately-owned ticket and is not what "idempotent replay" is proving below: this
test replays one already-landed BATCH through the merge step a second time (Stage 1's own
idempotent-merge guarantee, exercised end to end), not a full Stage-0 replay.
"""
from __future__ import annotations

from collections import defaultdict

from parsers.framework import ParseResult, parse_messages, reconcile

# Importing the demo parser module triggers its @register_parser("supercharger_stall",
# "2.1.4") decorator, populating the framework's global registry - exactly like any other
# caller of parse_messages() would rely on (same pattern test_parser_framework.py and
# test_dirty_keys.py use).
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from pipeline.stage1_parsed.dirty_keys import DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import MergeResult, device_hour_key, natural_key
from pipeline.stage2_canonical.canonicalize import load_catalog
from pipeline.stage2_canonical.targeted_recompute import recompute_dirty_device_hours
from tests.fixtures.generators.outage_backfill import build_outage_backfill_scenario

# 2024-01-01T00:00:00Z - arbitrary, real epoch-ms, chosen only because it's exactly hour-
# aligned in UTC (see build_outage_backfill_scenario's boundary-hour design).
T0_MS = 1_704_067_200_000

CATALOG = load_catalog()


def _build_scenario():
    """The scenario this whole file drives: 2 outage-affected devices go dark for exactly 72h
    after 2h15m of normal operation (see module/fixture docstrings for why 2h15m, not an exact
    hour boundary), then burst everything buffered back in one backfill batch; 1 control device
    reports normally the entire time and is never touched by the outage."""
    return build_outage_backfill_scenario(
        t0_ms=T0_MS,
        interval_ms=30 * 60_000,
        pre_outage_hours=2,
        boundary_offset_ms=15 * 60_000,
        outage_hours=72,
        outage_devices=("stall-out-1", "stall-out-2"),
        control_device="stall-ctrl-1",
        batch_size=12,
    )


def _ingest_pre_and_control(scenario, tracking_store: DirtyKeyTrackingStore):
    """Phase 1: normal, on-time operation before the outage - the outage-affected devices'
    pre-outage readings, plus the control device's readings for the whole scenario window
    (all on time, never part of the backfill). Everything here is first-time population, so
    per P1-07's rule none of it should mark anything dirty."""
    parse_pre = parse_messages(scenario.pre_outage_messages)
    reconcile(parse_pre)
    merge_pre = tracking_store.merge(parse_pre.rows)
    assert merge_pre.reconciles()

    parse_control = parse_messages(scenario.control_messages)
    reconcile(parse_control)
    merge_control = tracking_store.merge(parse_control.rows)
    assert merge_control.reconciles()

    assert len(tracking_store.dirty_keys) == 0, "first-time population must never be dirty"
    return parse_pre, merge_pre, parse_control, merge_control


def _ingest_backfill(scenario, tracking_store: DirtyKeyTrackingStore):
    """Phase 2: the 72h-buffered backfill batch lands, all at once, via the real chain -
    parse_messages() -> DirtyKeyTrackingStore.merge()."""
    parse_backfill = parse_messages(scenario.backfill_messages)
    reconcile(parse_backfill)
    merge_backfill = tracking_store.merge(parse_backfill.rows)
    assert merge_backfill.reconciles()
    return parse_backfill, merge_backfill


def _expected_boundary_hour_keys(scenario, pre_rows):
    """The (device_id, event_hour) key each outage-affected device's LAST pre-outage reading
    landed in - i.e. the boundary hour, the only hour that had data before the outage AND
    receives more data from the backfill. Derived from the actual parsed rows (their maximum
    device_ts_ms per device), not recomputed from the fixture's timing knobs, so this stays
    correct even if the scenario's parameters change."""
    rows_by_device: dict[str, list[dict]] = defaultdict(list)
    for row in pre_rows:
        rows_by_device[row["device_id"]].append(row)
    return {
        device_hour_key(max(rows, key=lambda r: r["device_ts_ms"]))
        for device_id, rows in rows_by_device.items()
        if device_id in scenario.outage_devices
    }


# ---------------------------------------------------------------------------
# The ticket's core "done when": the 72h outage backfill lands correctly, end to end.
# ---------------------------------------------------------------------------


def test_regional_outage_backfill_lands_correctly_end_to_end():
    scenario = _build_scenario()
    tracking_store = DirtyKeyTrackingStore()

    parse_pre, merge_pre, parse_control, merge_control = _ingest_pre_and_control(
        scenario, tracking_store
    )

    # Real numbers, not narrative: 2 outage devices x 5 pre-outage readings each (4 full
    # hourly-pairs + 1 boundary reading), 1 control device x 149 on-time readings spanning the
    # whole ~76h15m window at the same 30-minute cadence.
    assert scenario.readings_per_outage_device_pre == 5
    assert parse_pre.messages_seen == 10
    assert parse_pre.rows_parsed == 10
    assert parse_pre.messages_quarantined == 0
    assert len(scenario.control_messages) == 149
    assert parse_control.rows_parsed == 149

    # --- the backfill lands ---
    parse_backfill, merge_backfill = _ingest_backfill(scenario, tracking_store)

    # 2 outage devices x 144 buffered readings each = 288 rows, delivered in 24 batched
    # messages (batch_size=12) - the "one large batch on reconnect" the ticket describes.
    assert scenario.readings_per_outage_device_backfill == 144
    assert len(scenario.backfill_messages) == 24
    assert parse_backfill.messages_seen == 24
    assert parse_backfill.rows_parsed == 288
    assert parse_backfill.messages_quarantined == 0
    assert merge_backfill.rows_seen == 288
    assert merge_backfill.rows_inserted == 288
    assert merge_backfill.rows_already_present == 0

    # --- no message lost, anywhere in the chain (ParseResult/MergeResult's own reconciles()) ---
    assert isinstance(parse_pre, ParseResult) and parse_pre.reconciles()
    assert isinstance(parse_control, ParseResult) and parse_control.reconciles()
    assert isinstance(parse_backfill, ParseResult) and parse_backfill.reconciles()
    assert isinstance(merge_pre, MergeResult) and merge_pre.reconciles()
    assert isinstance(merge_control, MergeResult) and merge_control.reconciles()
    assert isinstance(merge_backfill, MergeResult) and merge_backfill.reconciles()

    total_rows_in_store = (
        parse_pre.rows_parsed + parse_control.rows_parsed + parse_backfill.rows_parsed
    )
    assert total_rows_in_store == 10 + 149 + 288 == 447
    assert len(tracking_store) == 447  # every natural key landed exactly once, nothing lost

    # --- exactly the boundary hours are dirty - per P1-07's rule (only a key that ALREADY
    # had data before this merge call counts as late) ---
    expected_dirty = _expected_boundary_hour_keys(scenario, parse_pre.rows)
    assert len(expected_dirty) == 2  # one boundary hour per outage-affected device
    assert tracking_store.dirty_keys.keys() == frozenset(expected_dirty)

    # A hour that is genuinely brand new for the outage window (deep inside the 72h gap, not
    # the boundary hour) must NOT be dirty - it's first-time population, not late data landing
    # on top of something already processed.
    interior_backfill_row = next(
        row
        for row in parse_backfill.rows
        if row["device_id"] == "stall-out-1"
        and device_hour_key(row) not in expected_dirty
    )
    assert device_hour_key(interior_backfill_row) not in tracking_store.dirty_keys

    # The control device - never part of the outage - must never appear in the dirty set,
    # under any of its many hours.
    control_hour_keys = {device_hour_key(row) for row in parse_control.rows}
    assert control_hour_keys.isdisjoint(tracking_store.dirty_keys.keys())

    # --- P1-13's targeted recompute touches ONLY the dirty device-hours ---
    recompute_result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)
    assert recompute_result.reconciles()
    assert recompute_result.device_hours_seen == 2
    assert recompute_result.device_hours_recomputed == 2
    # Each boundary hour has exactly 2 rows: 1 pre-outage + 1 from the backfill (the whole
    # hour is redone, not just the newly-landed row - see targeted_recompute.py's docstring).
    assert recompute_result.rows_seen == 4
    assert recompute_result.rows_recomputed == 4
    assert recompute_result.rows_failed == 0

    recomputed_device_ids = {row.fields["device_id"] for row in recompute_result.rows}
    assert recomputed_device_ids == set(scenario.outage_devices)
    assert scenario.control_device not in recomputed_device_ids

    # Every dirty key was acknowledged - nothing left to loop on.
    assert len(tracking_store.dirty_keys) == 0

    # The control device's rows are still sitting untouched in the store (invariant 5:
    # targeted recompute never touches a hour it wasn't told is dirty).
    a_control_row = parse_control.rows[0]
    assert tracking_store.get(natural_key(a_control_row)) is not None


# ---------------------------------------------------------------------------
# Replaying the exact same backfill batch a second time must be a no-op, end to end - not
# just at merge.py's own unit-test level (Stage1MergeStore's idempotent-merge guarantee is
# already proven there; this proves the guarantee holds through the whole P1-16 chain).
# ---------------------------------------------------------------------------


def test_replaying_the_same_backfill_batch_is_idempotent():
    scenario = _build_scenario()
    tracking_store = DirtyKeyTrackingStore()
    _ingest_pre_and_control(scenario, tracking_store)
    _ingest_backfill(scenario, tracking_store)

    rows_before = len(tracking_store)
    assert rows_before == 10 + 149 + 288
    # The initial backfill already marked the 2 boundary hours dirty; nothing has acknowledged
    # them yet (no recompute has run in this test) - captured here so the replay's effect on
    # dirty-tracking can be checked precisely, not just asserted to be zero.
    dirty_before_replay = tracking_store.dirty_keys.keys()
    assert len(dirty_before_replay) == 2

    # Something (an operator re-running a backfill job that already succeeded, a retried
    # delivery, a re-triggered Dagster run) replays the identical backfill batch.
    parse_replay, merge_replay = _ingest_backfill(scenario, tracking_store)

    assert parse_replay.rows_parsed == 288  # parsing is unaffected by replay - same messages
    assert parse_replay.messages_quarantined == 0
    assert merge_replay.rows_seen == 288
    assert merge_replay.rows_inserted == 0  # every natural key was already present
    assert merge_replay.rows_already_present == 288
    assert merge_replay.reconciles()

    # No new rows, and dirty-tracking is completely unaffected by the replay: the exact same
    # 2 boundary-hour keys remain dirty (untouched, not doubled, not cleared) - a replay of
    # already-merged data must never look like new late data landing on an already-populated
    # hour, precisely because DirtyKeyTrackingStore only marks keys from keys_inserted, which
    # is empty on a pure replay (see dirty_keys.py's merge()).
    assert len(tracking_store) == rows_before
    assert tracking_store.dirty_keys.keys() == dirty_before_replay

    # The still-pending dirty keys recompute exactly as they would have without the replay in
    # between - the replay left no trace for the recompute step to react to.
    recompute_result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)
    assert recompute_result.device_hours_seen == 2
    assert recompute_result.rows_seen == 4
    assert recompute_result.reconciles()
    assert len(tracking_store.dirty_keys) == 0

    # Recomputing again now (nothing dirty left) is itself a no-op.
    noop_result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)
    assert noop_result.device_hours_seen == 0
    assert noop_result.rows == ()
    assert noop_result.failed_rows == ()
