"""Tests for the Stage 3c device-day feature table (P2-05: "Device-day feature table").

Covers pipeline/stage3_enrich/device_day.py: compute_device_day_features() directly (a
hand-traceable full day of grid buckets, an all-measured day, an all-gap day) and
rebuild_dirty_device_day_features() (the dirty-hour-driven whole-day rebuild, mirroring
test_time_grid.py's / test_targeted_recompute.py's control-key proof one grain up - device-day
instead of device-hour).
"""
from __future__ import annotations

from pipeline.stage1_parsed.dirty_keys import DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import device_hour_key
from pipeline.stage2_canonical.canonicalize import CanonicalRow, load_catalog
from pipeline.stage3_enrich.device_day import (
    DeviceDayKey,
    compute_device_day_features,
    event_day_range_ms,
    hour_keys_for_day,
    rebuild_dirty_device_day_features,
)
from pipeline.stage3_enrich.time_grid import BUCKET_MS, GAP, MEASURED, build_grid

CATALOG = load_catalog()

# An arbitrary, real UTC calendar day - only "a valid YYYY-MM-DD" matters.
DAY = "2026-06-15"
DAY_START_MS, DAY_END_MS = event_day_range_ms(DAY)
assert DAY_END_MS - DAY_START_MS == 1440 * BUCKET_MS


def _stall_row(device_ts_ms: int, *, session_state: str, device_id: str = "stall-A"):
    """A hand-built Stage 2 CanonicalRow for a supercharger_stall reading - mirrors
    test_time_grid.py's own _stall_row helper.
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


# ---------------------------------------------------------------------------
# A hand-traceable full day: readings at minute 0, 1, 500, 501 only. Every count below is
# derived by hand from those four minute offsets (see the module's own docstring for the
# arithmetic; also independently checked by running the same arithmetic in a scratch script
# before writing these assertions).
# ---------------------------------------------------------------------------


def test_hand_traceable_full_day_features():
    rows = [
        _stall_row(DAY_START_MS + 0 * BUCKET_MS, session_state="plugged_in"),
        _stall_row(DAY_START_MS + 1 * BUCKET_MS, session_state="charging"),
        _stall_row(DAY_START_MS + 500 * BUCKET_MS, session_state="fault"),
        _stall_row(DAY_START_MS + 501 * BUCKET_MS, session_state="session_complete"),
    ]

    buckets = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=DAY_START_MS,
        range_end_ms=DAY_END_MS,
    )
    assert len(buckets) == 1440

    features = compute_device_day_features(buckets)

    assert features.device_id == "stall-A"
    assert features.device_class == "supercharger_stall"
    assert features.event_day == DAY
    assert features.bucket_count == 1440
    assert features.measured_bucket_count == 4
    assert features.gap_bucket_count == 1436
    assert features.coverage_ratio == 4 / 1440
    assert features.reading_count_total == 4

    # Gap streak 1: buckets 2..499 inclusive = 498 buckets. Gap streak 2: buckets 502..1439
    # inclusive = 938 buckets. Longest is 938.
    assert features.longest_gap_minutes == 938

    # minute 0: plugged_in (measured, 1 minute).
    # minute 1..499: charging (measured at minute 1, forward-filled through minute 499) =
    # 1 + 498 = 499 minutes.
    # minute 500: fault (measured, 1 minute).
    # minute 501..1439: session_complete (measured at minute 501, forward-filled through
    # minute 1439) = 1 + 938 = 939 minutes.
    assert features.minutes_by_mode == {
        "plugged_in": 1,
        "charging": 499,
        "fault": 1,
        "session_complete": 939,
    }
    assert sum(features.minutes_by_mode.values()) == 1440
    assert features.reconciles()


# ---------------------------------------------------------------------------
# Zero-gap day -> coverage_ratio == 1.0, longest_gap_minutes == 0.
# ---------------------------------------------------------------------------


def test_full_coverage_day_has_ratio_one_and_no_gap():
    rows = [
        _stall_row(DAY_START_MS + minute * BUCKET_MS, session_state="charging")
        for minute in range(1440)
    ]

    buckets = build_grid(
        rows,
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=DAY_START_MS,
        range_end_ms=DAY_END_MS,
    )
    assert all(b.coverage == MEASURED for b in buckets)

    features = compute_device_day_features(buckets)

    assert features.coverage_ratio == 1.0
    assert features.measured_bucket_count == 1440
    assert features.gap_bucket_count == 0
    assert features.longest_gap_minutes == 0
    assert features.reading_count_total == 1440
    assert features.minutes_by_mode == {"charging": 1440}
    assert features.reconciles()


# ---------------------------------------------------------------------------
# All-gap day (device silent all day) -> sensible, non-crashing, documented output.
# ---------------------------------------------------------------------------


def test_all_gap_day_is_sensible_not_a_crash():
    buckets = build_grid(
        [],
        device_id="stall-A",
        device_class="supercharger_stall",
        range_start_ms=DAY_START_MS,
        range_end_ms=DAY_END_MS,
    )
    assert len(buckets) == 1440
    assert all(b.coverage == GAP for b in buckets)
    assert all(b.mode is None for b in buckets)

    features = compute_device_day_features(buckets)

    assert features.measured_bucket_count == 0
    assert features.gap_bucket_count == 1440
    assert features.coverage_ratio == 0.0
    assert features.longest_gap_minutes == 1440
    assert features.reading_count_total == 0
    # No mode was ever known - the whole day falls under the literal None key, not "unknown"
    # or silently dropped.
    assert features.minutes_by_mode == {None: 1440}
    assert features.reconciles()


def test_compute_device_day_features_rejects_empty_input():
    try:
        compute_device_day_features(())
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_hour_keys_for_day_covers_all_24_hours_zero_padded():
    keys = hour_keys_for_day("stall-A", DAY)
    assert len(keys) == 24
    assert keys[0] == ("stall-A", f"{DAY}T00")
    assert keys[9] == ("stall-A", f"{DAY}T09")
    assert keys[23] == ("stall-A", f"{DAY}T23")


# ---------------------------------------------------------------------------
# Dirty-hour-driven whole-day rebuild: a subset of a device-day's 24 hours are dirty; the
# WHOLE day's features are recomputed (not just the dirty hours in isolation), and a control
# device/day untouched by any late merge is provably never touched.
# ---------------------------------------------------------------------------


def _raw_stall_row(device_id: str, device_ts_ms: int, payload_hash: str, *, state: str = "charging"):
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


def test_rebuild_whole_day_when_a_subset_of_hours_are_dirty_control_untouched():
    tracking_store = DirtyKeyTrackingStore()

    # Initial merge: stall-A gets readings in hour 00 and hour 12 of DAY (first-time
    # population, nothing dirty yet). stall-B (the CONTROL) gets one reading in hour 00 of
    # DAY too - it must never appear in rebuild output below.
    tracking_store.merge(
        [
            _raw_stall_row("stall-A", DAY_START_MS + 0 * BUCKET_MS, "sha1:a1", state="plugged_in"),
            _raw_stall_row(
                "stall-A", DAY_START_MS + 12 * 3600 * 1000, "sha1:a2", state="plugged_in"
            ),
            _raw_stall_row("stall-B", DAY_START_MS + 0 * BUCKET_MS, "sha1:b1", state="charging"),
        ]
    )
    assert len(tracking_store.dirty_keys) == 0

    # Late merge: a new row lands in stall-A's hour 00 only (hour 12 stays clean, hour 00
    # becomes dirty). stall-B is not touched at all by this late merge.
    tracking_store.merge(
        [
            _raw_stall_row(
                "stall-A", DAY_START_MS + 30 * 60 * 1000, "sha1:a3", state="charging"
            ),
        ]
    )

    dirty_hour_a00 = device_hour_key(_raw_stall_row("stall-A", DAY_START_MS, "sha1:a1"))
    assert tracking_store.dirty_keys.keys() == frozenset({dirty_hour_a00})

    result = rebuild_dirty_device_day_features(tracking_store, tracking_store.dirty_keys, CATALOG)

    day_key_a: DeviceDayKey = ("stall-A", DAY)
    assert result.device_days_seen == 1
    assert result.device_days_rebuilt == 1
    assert result.reconciles()
    assert set(result.per_day) == {day_key_a}

    # The control device never appears anywhere in the rebuild output, even though it's still
    # sitting untouched in the store with real data for the same day.
    assert "stall-B" not in [key[0] for key in result.per_day]

    day_result = result.per_day[day_key_a]
    assert day_result.dirty_hour_keys == {dirty_hour_a00}
    assert day_result.device_class == "supercharger_stall"
    assert len(day_result.buckets) == 1440

    features = day_result.features
    assert features is not None
    assert features.device_id == "stall-A"
    assert features.event_day == DAY
    assert features.reconciles()
    # Hand-traceable: hour 00 has 2 measured minutes (0 and 30), hour 12 has 1 measured
    # minute (via the clean-hour re-derivation path) -> 3 measured buckets total for the day,
    # even though only ONE of the day's 24 hours was actually dirty - proof the WHOLE day was
    # rebuilt, not just the dirty hour's 60 buckets.
    assert features.measured_bucket_count == 3
    assert features.reading_count_total == 3

    # Dirty keys were acknowledged - a caller can loop without reprocessing.
    assert len(tracking_store.dirty_keys) == 0

    # Re-running with no new dirty keys is a no-op.
    noop = rebuild_dirty_device_day_features(tracking_store, tracking_store.dirty_keys, CATALOG)
    assert noop.device_days_seen == 0
    assert noop.per_day == {}


def test_rebuild_all_dirty_hour_rows_cast_failure_yields_no_features_not_a_crash():
    """Mirrors test_time_grid.py's test_rebuild_all_rows_cast_failure_yields_empty_grid_not_a_
    crash one grain up: a device-day whose only data (in its one dirty hour) all fails Stage 2
    cast, and has no other hours' data at all, gets features=None - not guessed at, not a
    crash - while still being counted as seen/rebuilt.
    """
    tracking_store = DirtyKeyTrackingStore()
    bad_initial = _raw_stall_row("stall-E", DAY_START_MS, "sha1:e1")
    bad_initial["fault_code"] = "not-an-int"
    tracking_store.merge([bad_initial])
    assert len(tracking_store.dirty_keys) == 0

    bad_late = _raw_stall_row("stall-E", DAY_START_MS + 60_000, "sha1:e2")
    bad_late["fault_code"] = "still-not-an-int"
    tracking_store.merge([bad_late])
    assert len(tracking_store.dirty_keys) == 1

    result = rebuild_dirty_device_day_features(tracking_store, tracking_store.dirty_keys, CATALOG)

    assert result.reconciles()
    day_key_e: DeviceDayKey = ("stall-E", DAY)
    assert set(result.per_day) == {day_key_e}
    day_result = result.per_day[day_key_e]
    assert day_result.device_class is None
    assert day_result.features is None
    assert day_result.buckets == ()
    assert day_result.rows_seen == 2
    assert day_result.rows_failed == 2
    assert len(day_result.dirty_hour_keys) == 1
    assert len(tracking_store.dirty_keys) == 0
