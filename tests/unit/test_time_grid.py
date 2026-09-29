"""Tests for the Stage 3a time grid (P2-03: "Stage 3a time grid (5-min / 1-min) with coverage
and mode labels").

Covers pipeline/stage3_enrich/time_grid.py: build_grid() directly (hand-traceable small
examples for measured buckets, gap forward-fill, and the multiple-readings-in-one-bucket
resolution rule) and rebuild_dirty_grid_buckets() (the dirty-key-driven targeted rebuild path,
mirroring test_targeted_recompute.py's control-hour proof one stage further - P2-03's own
"done when" is specifically that proof at the grid grain).

Scope note (see time_grid.py's module docstring): only supercharger_stall and
supercharger_cabinet are covered here, because those are the only device classes with real
fixtures/catalog entries anywhere in this repo. No Powerwall/Powerpack 5-min grid test exists,
deliberately - there is nothing real to test it against yet.
"""
from __future__ import annotations

from pipeline.stage1_parsed.dirty_keys import DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import device_hour_key
from pipeline.stage2_canonical.canonicalize import CanonicalRow, load_catalog
from pipeline.stage3_enrich.time_grid import (
    BUCKET_MS,
    GAP,
    MEASURED,
    UnsupportedDeviceClassError,
    build_grid,
    rebuild_dirty_grid_buckets,
)

CATALOG = load_catalog()

# Arbitrary, real epoch-ms, minute-aligned - only "minute-aligned" and "same UTC hour as its
# sibling offsets" matter here (mirrors test_targeted_recompute.py's HOUR_START_MS convention).
HOUR_START_MS = 1_780_358_400_000
assert HOUR_START_MS % 60_000 == 0


def _stall_row(device_ts_ms: int, *, session_state: str, device_id: str = "stall-A") -> CanonicalRow:
    """A hand-built Stage 2 CanonicalRow for a supercharger_stall reading - just enough fields
    (device identity + session_state, the catalog field this ticket grounds stall `mode` in)
    to exercise build_grid() without going through full canonicalize_row()/the fixture
    generator.
    """
    return CanonicalRow(
        fields={
            "device_id": device_id,
            "device_class": "supercharger_stall",
            "firmware_version": "2.1.4",
            "device_ts_ms": device_ts_ms,
            "session_state": session_state,
        },
        units={"session_state": "enum"},
        dropped_fields=(),
    )


def _cabinet_row(
    device_ts_ms: int, *, contactor_closed: bool, device_id: str = "cabinet-A"
) -> CanonicalRow:
    """A hand-built Stage 2 CanonicalRow for a supercharger_cabinet reading - contactor_closed
    is the real cataloged field this ticket's cabinet `mode` judgment call derives from.
    """
    return CanonicalRow(
        fields={
            "device_id": device_id,
            "device_class": "supercharger_cabinet",
            "firmware_version": "1.8.2",
            "device_ts_ms": device_ts_ms,
            "contactor_closed": contactor_closed,
        },
        units={"contactor_closed": "bool"},
        dropped_fields=(),
    )


# ---------------------------------------------------------------------------
# A full run of on-time readings -> all-measured grid, correct per-bucket mode.
# ---------------------------------------------------------------------------


def test_full_run_of_on_time_readings_all_measured_correct_modes():
    # Stalls report every ~15s (tests/fixtures/generators/supercharger.py's
    # GeneratorConfig.reading_interval_s) -> 4 readings per 1-minute bucket. Three minutes,
    # hand-built and hand-traceable: minute 0 is plugged_in throughout, minute 1 transitions to
    # charging on its 2nd reading, minute 2 is charging throughout.
    rows = [
        _stall_row(HOUR_START_MS + 0_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 15_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 30_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 45_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 60_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 75_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 90_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 105_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 120_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 135_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 150_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 165_000, session_state="charging"),
    ]

    grid = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=HOUR_START_MS,
        range_end_ms=HOUR_START_MS + 3 * BUCKET_MS,
    )

    assert len(grid) == 3
    assert [b.bucket_start_ms for b in grid] == [
        HOUR_START_MS,
        HOUR_START_MS + BUCKET_MS,
        HOUR_START_MS + 2 * BUCKET_MS,
    ]
    assert [b.coverage for b in grid] == [MEASURED, MEASURED, MEASURED]
    assert [b.reading_count for b in grid] == [4, 4, 4]
    # Minute 0: last reading in the bucket is still plugged_in.
    # Minute 1: last reading in the bucket (75s) is charging - "latest wins" per bucket.
    # Minute 2: charging throughout.
    assert [b.mode for b in grid] == ["plugged_in", "charging", "charging"]
    for bucket in grid:
        assert bucket.device_id == "stall-A"
        assert bucket.device_class == "supercharger_stall"


# ---------------------------------------------------------------------------
# A gap in reporting -> gap-coverage bucket with forward-filled mode; leading gap is None.
# ---------------------------------------------------------------------------


def test_gap_bucket_forward_fills_last_known_mode():
    # Reading only in minute 0 (charging) and minute 3 (fault); minutes 1-2 are a genuine gap.
    rows = [
        _stall_row(HOUR_START_MS + 10_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 3 * BUCKET_MS + 10_000, session_state="fault"),
    ]

    grid = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=HOUR_START_MS,
        range_end_ms=HOUR_START_MS + 4 * BUCKET_MS,
    )

    assert len(grid) == 4
    assert [b.coverage for b in grid] == [MEASURED, GAP, GAP, MEASURED]
    # Minutes 1 and 2 forward-fill minute 0's mode ("charging"), not null/"unknown".
    assert [b.mode for b in grid] == ["charging", "charging", "charging", "fault"]
    assert [b.reading_count for b in grid] == [1, 0, 0, 1]


def test_leading_gap_before_any_reading_has_no_mode():
    # No reading at all until minute 2 - minutes 0-1 have nothing to forward-fill from.
    rows = [_stall_row(HOUR_START_MS + 2 * BUCKET_MS + 5_000, session_state="plugged_in")]

    grid = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=HOUR_START_MS,
        range_end_ms=HOUR_START_MS + 3 * BUCKET_MS,
    )

    assert [b.coverage for b in grid] == [GAP, GAP, MEASURED]
    assert [b.mode for b in grid] == [None, None, "plugged_in"]


# ---------------------------------------------------------------------------
# Multiple readings landing in the same bucket resolve to the latest one.
# ---------------------------------------------------------------------------


def test_multiple_readings_in_one_bucket_latest_wins():
    rows = [
        _stall_row(HOUR_START_MS + 5_000, session_state="plugged_in"),
        _stall_row(HOUR_START_MS + 25_000, session_state="charging"),
        _stall_row(HOUR_START_MS + 55_000, session_state="fault"),  # latest in this bucket
    ]

    grid = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=HOUR_START_MS,
        range_end_ms=HOUR_START_MS + BUCKET_MS,
    )

    assert len(grid) == 1
    bucket = grid[0]
    assert bucket.coverage == MEASURED
    assert bucket.reading_count == 3
    assert bucket.mode == "fault"


# ---------------------------------------------------------------------------
# Cabinet mode derivation (the documented judgment call): contactor_closed -> energized /
# de_energized.
# ---------------------------------------------------------------------------


def test_cabinet_mode_derived_from_contactor_closed():
    rows = [
        _cabinet_row(HOUR_START_MS + 0, contactor_closed=True, device_id="cabinet-A"),
        _cabinet_row(HOUR_START_MS + BUCKET_MS, contactor_closed=False, device_id="cabinet-A"),
    ]

    grid = build_grid(
        rows,
        device_id="cabinet-A",
        device_class="supercharger_cabinet",
        range_start_ms=HOUR_START_MS,
        range_end_ms=HOUR_START_MS + 2 * BUCKET_MS,
    )

    assert [b.mode for b in grid] == ["energized", "de_energized"]


def test_unsupported_device_class_raises():
    try:
        build_grid(
            [],
            device_id="pw-1",
            device_class="powerwall",
            range_start_ms=HOUR_START_MS,
            range_end_ms=HOUR_START_MS + BUCKET_MS,
        )
        assert False, "expected UnsupportedDeviceClassError"
    except UnsupportedDeviceClassError:
        pass


# ---------------------------------------------------------------------------
# Targeted rebuild from dirty keys: only dirty device-hours are rebuilt, a control device-hour
# is provably untouched - mirrors test_targeted_recompute.py's
# test_recompute_touches_only_dirty_device_hours_control_hour_untouched, one stage further.
# ---------------------------------------------------------------------------


def _raw_stall_row(device_id: str, device_ts_ms: int, payload_hash: str, *, state: str = "charging"):
    """A raw Stage 1 row (pre-canonicalization) for the dirty-key/recompute path - same shape
    as test_targeted_recompute.py's `_row` helper, with `state` (the raw field session_state is
    canonicalized from) added so built grids have a real, checkable mode.
    """
    return {
        "device_id": device_id,
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "device_ts_ms": device_ts_ms,
        "payload_hash": payload_hash,
        "state": state,
        "output_voltage_v": 400.0,
        "fault_code": 0,
    }


def test_rebuild_touches_only_dirty_device_hours_control_hour_untouched():
    tracking_store = DirtyKeyTrackingStore()

    # Initial merge: first-time population of three distinct device-hours - none dirty yet.
    tracking_store.merge(
        [
            _raw_stall_row("stall-A", HOUR_START_MS, "sha1:a1", state="plugged_in"),
            _raw_stall_row("stall-B", HOUR_START_MS, "sha1:b1", state="charging"),  # CONTROL
            _raw_stall_row("stall-C", HOUR_START_MS, "sha1:c1", state="plugged_in"),
        ]
    )
    assert len(tracking_store.dirty_keys) == 0

    # Late merge: new rows land in stall-A's and stall-C's hours only. stall-B's hour must
    # remain untouched by any rebuild.
    tracking_store.merge(
        [
            _raw_stall_row("stall-A", HOUR_START_MS + 60_000, "sha1:a2", state="charging"),
            _raw_stall_row("stall-C", HOUR_START_MS + 60_000, "sha1:c2", state="fault"),
        ]
    )

    key_a = device_hour_key(_raw_stall_row("stall-A", HOUR_START_MS, "sha1:a1"))
    key_b = device_hour_key(_raw_stall_row("stall-B", HOUR_START_MS, "sha1:b1"))
    key_c = device_hour_key(_raw_stall_row("stall-C", HOUR_START_MS, "sha1:c1"))
    assert tracking_store.dirty_keys.keys() == frozenset({key_a, key_c})
    assert key_b not in tracking_store.dirty_keys

    result = rebuild_dirty_grid_buckets(tracking_store, tracking_store.dirty_keys, CATALOG)

    # Exactly the two dirty device-hours were rebuilt.
    assert result.device_hours_seen == 2
    assert result.device_hours_rebuilt == 2
    assert result.reconciles()
    assert set(result.per_key) == {key_a, key_c}

    rebuilt_device_ids = {b.device_id for b in result.buckets}
    assert rebuilt_device_ids == {"stall-A", "stall-C"}
    # The control device/hour is nowhere in the rebuilt grid output, even though its row is
    # still sitting untouched in the store.
    assert "stall-B" not in rebuilt_device_ids

    # Each rebuilt key gets a full 60-bucket, hour-long grid (event_hour_range_ms's window),
    # not just the two literal readings.
    assert len(result.per_key[key_a].buckets) == 60
    assert len(result.per_key[key_c].buckets) == 60

    # Hand-traceable per-key detail: stall-A's hour has readings at minute 0 (plugged_in) and
    # minute 1 (charging); everything else is a gap forward-filling minute 1's mode onward.
    a_buckets = result.per_key[key_a].buckets
    assert a_buckets[0].coverage == MEASURED and a_buckets[0].mode == "plugged_in"
    assert a_buckets[1].coverage == MEASURED and a_buckets[1].mode == "charging"
    assert a_buckets[2].coverage == GAP and a_buckets[2].mode == "charging"

    # Every dirty key was acknowledged by the underlying P1-13 recompute - a caller can loop
    # without reprocessing them.
    assert len(tracking_store.dirty_keys) == 0

    # Re-running with no new dirty keys is a no-op.
    noop_result = rebuild_dirty_grid_buckets(tracking_store, tracking_store.dirty_keys, CATALOG)
    assert noop_result.device_hours_seen == 0
    assert noop_result.device_hours_rebuilt == 0
    assert noop_result.buckets == ()
    assert noop_result.per_key == {}


def test_rebuild_reconciles_with_a_partial_cast_failure():
    """Reconciliation-style check, mirroring test_targeted_recompute.py's reconciles() test:
    device_hours_seen == device_hours_rebuilt always holds, and a dirty hour with one bad row
    among otherwise-good rows still builds a real grid from whatever survived Stage 2 cast.
    """
    tracking_store = DirtyKeyTrackingStore()
    tracking_store.merge([_raw_stall_row("stall-D", HOUR_START_MS, "sha1:d1")])
    assert len(tracking_store.dirty_keys) == 0

    # Late arrival: the only new row has an uncastable fault_code - the whole hour's rows (this
    # one plus the original) get recomputed, but this one fails cast.
    bad_row = _raw_stall_row("stall-D", HOUR_START_MS + 60_000, "sha1:d2")
    bad_row["fault_code"] = "not-an-int"
    tracking_store.merge([bad_row])

    key_d = device_hour_key(_raw_stall_row("stall-D", HOUR_START_MS, "sha1:d1"))
    assert tracking_store.dirty_keys.keys() == frozenset({key_d})

    result = rebuild_dirty_grid_buckets(tracking_store, tracking_store.dirty_keys, CATALOG)

    assert result.reconciles()
    assert result.device_hours_seen == 1
    assert result.device_hours_rebuilt == 1
    # The good row (sha1:d1) still canonicalizes and builds a real 60-bucket grid for the hour.
    per_key_d = result.per_key[key_d]
    assert per_key_d.rows_seen == 2
    assert per_key_d.rows_failed == 1
    assert per_key_d.device_class == "supercharger_stall"
    assert len(per_key_d.buckets) == 60
    assert key_d not in tracking_store.dirty_keys


def test_rebuild_all_rows_cast_failure_yields_empty_grid_not_a_crash():
    """The edge case rebuild_dirty_grid_buckets's docstring calls out explicitly: a dirty hour
    where EVERY row fails Stage 2 cast has no surviving canonical row to read a device_class
    off of, so its grid is an empty tuple (not guessed at, not a crash) - but the key is still
    present in `per_key` and counted as seen/rebuilt, mirroring P1-13's "attempted, not silently
    dropped" stance on a cast failure.
    """
    tracking_store = DirtyKeyTrackingStore()
    bad_initial = _raw_stall_row("stall-E", HOUR_START_MS, "sha1:e1")
    bad_initial["fault_code"] = "also-not-an-int"
    tracking_store.merge([bad_initial])
    assert len(tracking_store.dirty_keys) == 0

    bad_late = _raw_stall_row("stall-E", HOUR_START_MS + 60_000, "sha1:e2")
    bad_late["fault_code"] = "still-not-an-int"
    tracking_store.merge([bad_late])

    key_e = device_hour_key(bad_initial)
    assert tracking_store.dirty_keys.keys() == frozenset({key_e})

    result = rebuild_dirty_grid_buckets(tracking_store, tracking_store.dirty_keys, CATALOG)

    assert result.reconciles()
    assert result.device_hours_seen == 1
    assert result.device_hours_rebuilt == 1
    per_key_e = result.per_key[key_e]
    assert per_key_e.rows_seen == 2
    assert per_key_e.rows_failed == 2
    assert per_key_e.device_class is None
    assert per_key_e.buckets == ()
    assert result.buckets == ()  # nothing from this key leaked into the overall bucket list
    assert key_e not in tracking_store.dirty_keys
