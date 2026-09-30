"""Tests for targeted Stage 2 recompute from dirty keys (P1-13: "Targeted Stage 2 recompute
from dirty keys").

Covers pipeline/stage2_canonical/targeted_recompute.py: recompute_dirty_device_hours() and
rows_for_device_hour(), the wire between P1-07's DirtyKeysTable/DirtyKeyTrackingStore and
P1-08's canonicalize_rows(). CLAUDE.md invariant 5: "Late data recomputes only dirty (device,
hour) keys." The "done when" this file exists to prove: late data recomputes only touched
device-hours - a control device-hour that was never touched by a late merge must never show up
in recompute output, even though it's still sitting in the store.
"""
from __future__ import annotations

from pipeline.stage1_parsed.dirty_keys import DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import device_hour_key, natural_key
from pipeline.stage2_canonical.canonicalize import load_catalog
from pipeline.stage2_canonical.targeted_recompute import (
    recompute_dirty_device_hours,
    rows_for_device_hour,
)

ONE_HOUR_MS = 3600 * 1000
HOUR_START_MS = 1_780_358_400_000  # arbitrary, real epoch-ms, hour-aligned is not required -
# only "same UTC hour as its sibling offsets below" matters for device_hour_key().

CATALOG = load_catalog()


def _row(
    device_id: str,
    device_ts_ms: int,
    payload_hash: str,
    *,
    output_voltage_v: float = 400.0,
    fault_code=0,
    device_class: str = "supercharger_stall",
    firmware_version: str = "2.1.4",
):
    """A hand-built Stage 1 row with exact, controlled device_id/device_ts_ms so the test can
    say precisely which (device_id, event_hour) key it lands in. `output_voltage_v` (float) and
    `fault_code` (int, catalog enum_values [0, 1001, 1042, 2010]) are real catalog-covered
    supercharger_stall/2.1.4 fields (see catalog/signals.yaml) - enough surface to exercise
    canonicalization without needing every field the real generator emits.
    """
    return {
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": firmware_version,
        "device_ts_ms": device_ts_ms,
        "payload_hash": payload_hash,
        "output_voltage_v": output_voltage_v,
        "fault_code": fault_code,
    }


# ---------------------------------------------------------------------------
# Core "done when": only dirty device-hours are recomputed; a control hour is untouched.
# ---------------------------------------------------------------------------


def test_recompute_touches_only_dirty_device_hours_control_hour_untouched():
    tracking_store = DirtyKeyTrackingStore()

    # Initial merge: first-time population of three distinct device-hours. Per P1-07's rule,
    # none of these are dirty yet.
    tracking_store.merge(
        [
            _row("stall-A", HOUR_START_MS, "sha1:a1"),
            _row("stall-B", HOUR_START_MS, "sha1:b1"),  # this one stays a CONTROL hour
            _row("stall-C", HOUR_START_MS, "sha1:c1"),
        ]
    )
    assert len(tracking_store.dirty_keys) == 0

    # Late merge: new rows land in stall-A's and stall-C's hours only. stall-B is never
    # touched again - it must remain a control hour, absent from any recompute output.
    tracking_store.merge(
        [
            _row("stall-A", HOUR_START_MS + 60_000, "sha1:a2"),
            _row("stall-C", HOUR_START_MS + 60_000, "sha1:c2"),
        ]
    )

    key_a = device_hour_key(_row("stall-A", HOUR_START_MS, "sha1:a1"))
    key_b = device_hour_key(_row("stall-B", HOUR_START_MS, "sha1:b1"))
    key_c = device_hour_key(_row("stall-C", HOUR_START_MS, "sha1:c1"))
    assert tracking_store.dirty_keys.keys() == frozenset({key_a, key_c})
    assert key_b not in tracking_store.dirty_keys  # the control hour never went dirty

    result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)

    # Exactly the two dirty device-hours were processed, each with both its rows (the whole
    # hour is redone, not just the newly-landed row).
    assert result.device_hours_seen == 2
    assert result.device_hours_recomputed == 2
    assert result.rows_seen == 4  # 2 rows in stall-A's hour + 2 in stall-C's hour
    assert result.rows_recomputed == 4
    assert result.rows_failed == 0
    assert set(result.per_key) == {key_a, key_c}

    recomputed_device_ids = {row.fields["device_id"] for row in result.rows}
    assert recomputed_device_ids == {"stall-A", "stall-C"}
    # The control hour's device is nowhere in the output, even though its row is still sitting
    # untouched in the store.
    assert "stall-B" not in recomputed_device_ids
    control_row_key = natural_key(_row("stall-B", HOUR_START_MS, "sha1:b1"))
    assert tracking_store.get(control_row_key) is not None

    # Every dirty key was acknowledged - the caller can loop without reprocessing them.
    assert len(tracking_store.dirty_keys) == 0

    # Re-running with no new dirty keys is a no-op: nothing recomputed, no errors.
    noop_result = recompute_dirty_device_hours(
        tracking_store, tracking_store.dirty_keys, CATALOG
    )
    assert noop_result.device_hours_seen == 0
    assert noop_result.device_hours_recomputed == 0
    assert noop_result.rows_seen == 0
    assert noop_result.rows == ()
    assert noop_result.failed_rows == ()
    assert len(tracking_store.dirty_keys) == 0


def test_rows_for_device_hour_finds_only_that_key_rows():
    tracking_store = DirtyKeyTrackingStore()
    tracking_store.merge(
        [
            _row("stall-A", HOUR_START_MS, "sha1:a1"),
            _row("stall-B", HOUR_START_MS, "sha1:b1"),
        ]
    )
    tracking_store.merge([_row("stall-A", HOUR_START_MS + 60_000, "sha1:a2")])

    key_a = device_hour_key(_row("stall-A", HOUR_START_MS, "sha1:a1"))
    rows = rows_for_device_hour(tracking_store, key_a)

    assert {row["payload_hash"] for row in rows} == {"sha1:a1", "sha1:a2"}


# ---------------------------------------------------------------------------
# A row that can't cast degrades to a per-row failure without crashing the pass.
# ---------------------------------------------------------------------------


def test_uncastable_value_is_a_per_row_failure_not_a_crash():
    tracking_store = DirtyKeyTrackingStore()

    # Initial population - not dirty yet.
    tracking_store.merge([_row("stall-D", HOUR_START_MS, "sha1:d1")])
    assert len(tracking_store.dirty_keys) == 0

    # Late arrival: one good row, one row with a fault_code value that can't cast to the
    # catalog's declared `int` type - hand-crafted to fail canonicalize_row's _cast_value.
    bad_row = _row("stall-D", HOUR_START_MS + 60_000, "sha1:d2", fault_code="not-an-int")
    good_row = _row("stall-D", HOUR_START_MS + 120_000, "sha1:d3")
    tracking_store.merge([bad_row, good_row])

    key_d = device_hour_key(_row("stall-D", HOUR_START_MS, "sha1:d1"))
    assert tracking_store.dirty_keys.keys() == frozenset({key_d})

    result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)

    # The whole pass survives: 3 rows total in stall-D's hour (initial + 2 late), 2 recompute
    # successfully, 1 is recorded as a failure - not a crash.
    assert result.rows_seen == 3
    assert result.rows_recomputed == 2
    assert result.rows_failed == 1
    failed_row, error = result.failed_rows[0]
    assert failed_row["payload_hash"] == "sha1:d2"
    assert error.raw_field == "fault_code"

    # The device-hour is still acknowledged - a cast failure doesn't leave it dirty forever
    # (re-running the identical recompute would just fail the same row again).
    assert key_d not in tracking_store.dirty_keys
    assert result.per_key[key_d].rows_failed == 1
    assert result.per_key[key_d].reconciles()


# ---------------------------------------------------------------------------
# Reconciliation: rows seen == rows recomputed + rows failed, overall and per key.
# ---------------------------------------------------------------------------


def test_reconciles_across_multiple_dirty_keys_with_a_mixed_failure():
    tracking_store = DirtyKeyTrackingStore()
    tracking_store.merge(
        [
            _row("stall-E", HOUR_START_MS, "sha1:e1"),
            _row("stall-F", HOUR_START_MS, "sha1:f1"),
        ]
    )
    assert len(tracking_store.dirty_keys) == 0

    tracking_store.merge(
        [
            _row("stall-E", HOUR_START_MS + 60_000, "sha1:e2"),
            _row("stall-F", HOUR_START_MS + 60_000, "sha1:f2", fault_code="nope"),
        ]
    )
    assert len(tracking_store.dirty_keys) == 2

    result = recompute_dirty_device_hours(tracking_store, tracking_store.dirty_keys, CATALOG)

    assert result.reconciles()
    assert result.rows_seen == result.rows_recomputed + result.rows_failed
    for per_key_result in result.per_key.values():
        assert per_key_result.reconciles()
