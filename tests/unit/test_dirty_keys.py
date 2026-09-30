"""Tests for the Stage 1 dirty-keys table (P1-07: "Dirty-keys table from incremental
changes").

Covers pipeline/stage1_parsed/dirty_keys.py: DirtyKeysTable and DirtyKeyTrackingStore, the
wrapper around P1-06's Stage1MergeStore that records which (device_id, event_hour) keys a
late merge touched (CLAUDE.md invariant 5). See that module's docstring for the "what counts
as late" rule this file tests against.
"""
from __future__ import annotations

import copy

from parsers.framework import parse_messages
from pipeline.stage1_parsed.dirty_keys import (
    DirtyKeysTable,
    DirtyKeyTrackingStore,
    device_hour_key_from_natural_key,
)
from pipeline.stage1_parsed.merge import device_hour_key, natural_key
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

ONE_HOUR_MS = 3600 * 1000


def _row(
    device_id="stall-2140001-0000",
    device_class="supercharger_stall",
    device_ts_ms=1_780_358_400_000,
    payload_hash="sha1:deadbeef",
):
    return {
        "device_id": device_id,
        "device_class": device_class,
        "device_ts_ms": device_ts_ms,
        "payload_hash": payload_hash,
    }


def _rows_from_generator() -> list[dict]:
    """Real parsed rows via the synthetic generator + parser framework - same route as
    tests/unit/test_stage1_merge.py's helper of the same name."""
    config = GeneratorConfig(seed=1337, devices_per_firmware=2)
    fixtures = generate(config)
    messages = [
        m
        for m in fixtures.all_messages()
        if m["device_class"] == "supercharger_stall" and m["firmware_version"] == "2.1.4"
    ]
    assert messages, "expected at least one supercharger_stall/2.1.4 message from the generator"
    result = parse_messages(messages)
    assert result.rows_parsed > 0
    return list(result.rows)


# ---------------------------------------------------------------------------
# DirtyKeysTable on its own: idempotent set semantics.
# ---------------------------------------------------------------------------


def test_dirty_keys_table_mark_is_idempotent_and_deduplicates():
    table = DirtyKeysTable()
    key = ("stall-0001", "2026-06-01T00")

    table.mark([key])
    table.mark([key])
    table.mark([key])

    assert len(table) == 1
    assert key in table
    assert table.keys() == frozenset({key})


def test_dirty_keys_table_mark_from_two_different_late_merges_no_duplicate():
    """Marking the same (device_id, event_hour) dirty from two separate late-merge events
    (two separate mark() calls, as two different DirtyKeyTrackingStore.merge() calls would
    produce) must not create a duplicate entry."""
    table = DirtyKeysTable()
    key = ("stall-0001", "2026-06-01T00")

    table.mark([key])  # first late merge marks it dirty
    table.mark([key])  # a second, independent late merge marks the same key dirty again

    assert len(table) == 1
    assert table.keys() == frozenset({key})


def test_dirty_keys_table_clear_removes_keys():
    table = DirtyKeysTable()
    key_a = ("stall-0001", "2026-06-01T00")
    key_b = ("stall-0002", "2026-06-01T01")
    table.mark([key_a, key_b])
    assert len(table) == 2

    table.clear([key_a])
    assert key_a not in table
    assert key_b in table
    assert len(table) == 1

    table.clear()  # no args -> clear everything
    assert len(table) == 0


def test_dirty_keys_table_acknowledge_is_the_same_as_clear():
    table = DirtyKeysTable()
    key = ("stall-0001", "2026-06-01T00")
    table.mark([key])

    table.acknowledge([key])

    assert len(table) == 0


def test_dirty_keys_table_clear_of_unmarked_key_is_a_noop_not_an_error():
    table = DirtyKeysTable()
    key = ("stall-0001", "2026-06-01T00")
    table.clear([key])  # never marked - must not raise
    assert len(table) == 0


# ---------------------------------------------------------------------------
# device_hour_key_from_natural_key reuses merge.py's hour bucketing.
# ---------------------------------------------------------------------------


def test_device_hour_key_from_natural_key_matches_device_hour_key_on_full_row():
    row = _row(device_ts_ms=1_780_358_400_000 + ONE_HOUR_MS)
    key = natural_key(row)

    assert device_hour_key_from_natural_key(key) == device_hour_key(row)


# ---------------------------------------------------------------------------
# The core "what counts as late" rule, on hand-built rows.
# ---------------------------------------------------------------------------


def test_first_time_population_of_a_device_hour_is_not_dirty():
    """A merge call inserting the very first rows ever seen for a (device_id, event_hour)
    must not mark anything dirty - nothing downstream ever processed it, so there's nothing to
    recompute."""
    store = DirtyKeyTrackingStore()
    row = _row(device_ts_ms=1_780_358_400_000, payload_hash="sha1:aaa")

    result = store.merge([row])

    assert result.rows_inserted == 1
    assert len(store.dirty_keys) == 0


def test_first_time_population_of_a_device_hour_within_one_call_is_not_dirty():
    """Two brand-new rows landing in the SAME never-before-seen device-hour within a single
    merge() call are both first-time population - neither should mark the hour dirty. (Once
    that hour has a row from THIS call, any LATER call adding to it is a different story - see
    test_second_merge_call_adding_new_rows_to_populated_hour_marks_exactly_that_key_dirty.)"""
    store = DirtyKeyTrackingStore()
    hour_start = 1_780_358_400_000

    result = store.merge(
        [
            _row(device_ts_ms=hour_start, payload_hash="sha1:aaa"),
            _row(device_ts_ms=hour_start + 60_000, payload_hash="sha1:bbb"),
        ]
    )

    assert result.rows_inserted == 2
    assert len(store.dirty_keys) == 0


def test_second_call_touching_the_same_device_hour_as_the_first_call_is_dirty():
    """A device-hour populated in call 1, then touched again by NEW rows in call 2, is exactly
    the "late" case (CLAUDE.md invariant 5): call 2 must mark it dirty, precisely because call 1
    already gave that device-hour at least one row before call 2 started."""
    store = DirtyKeyTrackingStore()
    hour_start = 1_780_358_400_000

    result_1 = store.merge([_row(device_ts_ms=hour_start, payload_hash="sha1:aaa")])
    result_2 = store.merge([_row(device_ts_ms=hour_start + 60_000, payload_hash="sha1:bbb")])

    assert result_1.rows_inserted == 1
    assert result_2.rows_inserted == 1
    expected_key = device_hour_key(_row(device_ts_ms=hour_start))
    assert store.dirty_keys.keys() == frozenset({expected_key})


def test_second_merge_call_adding_new_rows_to_populated_hour_marks_exactly_that_key_dirty():
    """A second merge call that inserts genuinely new rows into an already-populated
    (device_id, event_hour) marks exactly that key dirty, and no others."""
    store = DirtyKeyTrackingStore()
    device_id = "stall-2140001-0000"
    hour_start = 1_780_358_400_000
    other_device_hour_start = hour_start + 5 * ONE_HOUR_MS

    # Call 1: first-time population of two different device-hours.
    store.merge(
        [
            _row(device_id=device_id, device_ts_ms=hour_start, payload_hash="sha1:aaa"),
            _row(
                device_id="stall-2140001-0001",
                device_ts_ms=other_device_hour_start,
                payload_hash="sha1:zzz",
            ),
        ]
    )
    assert len(store.dirty_keys) == 0

    # Call 2: late data lands in the FIRST device-hour only.
    result_2 = store.merge(
        [_row(device_id=device_id, device_ts_ms=hour_start + 60_000, payload_hash="sha1:bbb")]
    )

    assert result_2.rows_inserted == 1
    expected_key = device_hour_key(_row(device_id=device_id, device_ts_ms=hour_start))
    assert store.dirty_keys.keys() == frozenset({expected_key})
    # The untouched device-hour from call 1 must not show up as dirty.
    other_key = device_hour_key(
        _row(device_id="stall-2140001-0001", device_ts_ms=other_device_hour_start)
    )
    assert other_key not in store.dirty_keys


def test_pure_idempotent_replay_does_not_mark_anything_dirty():
    """A merge call that only replays rows already present (rows_inserted == 0 for that
    device-hour) is a pure no-op - nothing actually changed, so nothing should be marked
    dirty, even though the device-hour was already populated."""
    store = DirtyKeyTrackingStore()
    row = _row(device_ts_ms=1_780_358_400_000, payload_hash="sha1:aaa")

    store.merge([row])
    assert len(store.dirty_keys) == 0

    replay_result = store.merge([copy.deepcopy(row)])

    assert replay_result.rows_inserted == 0
    assert replay_result.rows_already_present == 1
    assert len(store.dirty_keys) == 0


def test_three_calls_call_three_touches_a_device_hour_first_populated_in_call_one():
    """Proves dirty-key tracking checks the FULL prior history of the store, not just the
    immediately preceding call: call 2 populates an unrelated device-hour; call 3 lands new
    data back in the device-hour call 1 first populated. That device-hour must be recognized
    as already-populated (from call 1) and marked dirty, even though call 2 never touched it."""
    store = DirtyKeyTrackingStore()
    device_id = "stall-2140001-0000"
    hour_start = 1_780_358_400_000

    # Call 1: first-time population of device_id's hour.
    call_1 = store.merge([_row(device_id=device_id, device_ts_ms=hour_start, payload_hash="sha1:aaa")])
    assert call_1.rows_inserted == 1
    assert len(store.dirty_keys) == 0

    # Call 2: unrelated device-hour entirely - first-time population of a different device.
    call_2 = store.merge(
        [
            _row(
                device_id="stall-2140001-0009",
                device_ts_ms=hour_start + 10 * ONE_HOUR_MS,
                payload_hash="sha1:unrelated",
            )
        ]
    )
    assert call_2.rows_inserted == 1
    assert len(store.dirty_keys) == 0, "call 2 touched a different device-hour, not call 1's"

    # Call 3: late data lands back in call 1's device-hour.
    call_3 = store.merge(
        [_row(device_id=device_id, device_ts_ms=hour_start + 120_000, payload_hash="sha1:late")]
    )
    assert call_3.rows_inserted == 1

    expected_key = device_hour_key(_row(device_id=device_id, device_ts_ms=hour_start))
    assert store.dirty_keys.keys() == frozenset({expected_key})


def test_duplicate_dirty_key_across_two_separate_late_merges_stays_one_entry():
    """Two different late merges that both land new rows in the same already-populated
    device-hour must not produce two entries for it in the dirty-keys table."""
    store = DirtyKeyTrackingStore()
    device_id = "stall-2140001-0000"
    hour_start = 1_780_358_400_000

    store.merge([_row(device_id=device_id, device_ts_ms=hour_start, payload_hash="sha1:aaa")])

    # Two separate late merges, each adding one new row to the same device-hour.
    store.merge([_row(device_id=device_id, device_ts_ms=hour_start + 60_000, payload_hash="sha1:bbb")])
    store.merge([_row(device_id=device_id, device_ts_ms=hour_start + 120_000, payload_hash="sha1:ccc")])

    expected_key = device_hour_key(_row(device_id=device_id, device_ts_ms=hour_start))
    assert store.dirty_keys.keys() == frozenset({expected_key})
    assert len(store.dirty_keys) == 1


def test_clear_after_recompute_removes_the_key_from_the_dirty_table():
    store = DirtyKeyTrackingStore()
    device_id = "stall-2140001-0000"
    hour_start = 1_780_358_400_000

    store.merge([_row(device_id=device_id, device_ts_ms=hour_start, payload_hash="sha1:aaa")])
    store.merge([_row(device_id=device_id, device_ts_ms=hour_start + 60_000, payload_hash="sha1:bbb")])

    key = device_hour_key(_row(device_id=device_id, device_ts_ms=hour_start))
    assert key in store.dirty_keys

    store.dirty_keys.acknowledge([key])  # P1-13's eventual recompute job acknowledging it

    assert key not in store.dirty_keys
    assert len(store.dirty_keys) == 0


# ---------------------------------------------------------------------------
# Against real generator-derived rows: initial merge + overlapping "backfill" merge.
# ---------------------------------------------------------------------------


def test_backfill_merge_of_overlapping_plus_extra_rows_marks_sensible_dirty_keys():
    """Simulates an initial merge, then a second "backfill" merge of a subset that overlaps
    the first batch's device-hours plus adds genuinely new rows to some of them - using real
    rows produced via the synthetic generator + parser framework, not hand-built rows."""
    all_rows = _rows_from_generator()

    # Timestamp sanity (a missing/epoch-default/future device_ts_ms) is P1-05's job, not this
    # module's or this test's - device_hour_key() (like partition_key()) requires a usable
    # epoch-ms int, so drop rows the generator deliberately gave a bad device_ts_ms (see
    # tests/fixtures/generators/supercharger.py's clock-quality injection) before grouping.
    rows = [
        row
        for row in all_rows
        if isinstance(row.get("device_ts_ms"), int) and not isinstance(row.get("device_ts_ms"), bool)
    ]
    assert rows, "expected at least one generator row with a usable device_ts_ms"

    # Group real rows by (device_id, event_hour) so we can build a batch A / batch B split
    # that's guaranteed to share device-hours, the way a real late-arriving batch would.
    by_hour: dict[tuple, list[dict]] = {}
    for row in rows:
        by_hour.setdefault(device_hour_key(row), []).append(row)

    populated_hours = [h for h, hour_rows in by_hour.items() if len(hour_rows) >= 2]
    assert populated_hours, (
        "test setup needs at least one (device_id, event_hour) with >= 2 distinct rows from "
        "the generator to split into an initial batch + late backfill"
    )

    initial_rows: list[dict] = []
    backfill_new_rows: list[dict] = []
    backfill_replay_rows: list[dict] = []
    expected_dirty: set[tuple] = set()

    for hour_key, hour_rows in by_hour.items():
        if hour_key in populated_hours[: max(1, len(populated_hours) // 2)]:
            # Initial merge gets only the first row of this device-hour; the backfill merge
            # gets the rest (genuinely new keys) plus a replay of the first row.
            initial_rows.append(hour_rows[0])
            backfill_replay_rows.append(copy.deepcopy(hour_rows[0]))
            backfill_new_rows.extend(hour_rows[1:])
            expected_dirty.add(hour_key)
        else:
            # Every other device-hour is populated for the first time entirely in the initial
            # merge - never touched again, so never dirty.
            initial_rows.extend(hour_rows)

    # The generator's own retransmit injection can put two rows sharing a natural key inside
    # the same "initial" or "backfill-new" group above - real messiness, not a test bug - so
    # compare against unique natural keys rather than raw list lengths.
    unique_initial_keys = {natural_key(r) for r in initial_rows}
    unique_backfill_new_keys = {natural_key(r) for r in backfill_new_rows} - unique_initial_keys

    store = DirtyKeyTrackingStore()
    initial_result = store.merge(initial_rows)
    assert initial_result.rows_inserted == len(unique_initial_keys)
    assert len(store.dirty_keys) == 0, "nothing should be dirty after the very first merge"

    backfill_result = store.merge(backfill_replay_rows + backfill_new_rows)

    # The replayed rows are pure no-ops (already present from the initial merge); the
    # genuinely new rows are what land late.
    assert backfill_result.rows_inserted == len(unique_backfill_new_keys)

    assert store.dirty_keys.keys() == frozenset(expected_dirty)
    # Every dirty key really was populated before the backfill call.
    initial_hours = {device_hour_key(r) for r in initial_rows}
    for key in expected_dirty:
        assert key in initial_hours
