"""Tests for row-level quality flags (P1-10: "range, stuck, counter reset, jumps").

Done when (Build backlog.md): "Flags populated; no rows deleted; flag rates on dashboard." The
dashboard half is out of scope for this module (and these tests) - see
pipeline/stage2_canonical/quality_flags.py's module docstring for the full reasoning behind
each flag's definition and threshold; these tests exercise that these definitions actually
behave the way the docstring claims, not the dashboard/reporting side.

Rows are built directly (small hand-constructed per-device sequences via `canonicalize_row`
against the real catalog), not through the full fixture generator - these tests want exact
control over which field crosses which threshold, not generator-realistic messiness.
"""
from __future__ import annotations

import pytest

from pipeline.stage2_canonical.canonicalize import canonicalize_row, load_catalog
from pipeline.stage2_canonical.quality_flags import (
    COUNTER_RESET,
    JUMP,
    RANGE,
    STUCK,
    MixedDeviceError,
    flag_device_rows,
)

STALL_DEVICE_TS_START_MS = 1_780_358_400_000
STALL_INTERVAL_MS = 15_000  # matches GeneratorConfig.reading_interval_s for stalls


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _stall_raw_row(index: int, **overrides) -> dict:
    """One raw (pre-canonicalization) Stage 1-shaped stall reading. `index` only drives the
    default device_ts_ms so a caller building a sequence doesn't have to repeat it."""
    row = {
        "device_id": "stall-214-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "arrival_ts_ms": STALL_DEVICE_TS_START_MS + index * STALL_INTERVAL_MS + 777,
        "device_ts_ms": STALL_DEVICE_TS_START_MS + index * STALL_INTERVAL_MS,
        "payload_hash": f"sha1:fixture{index:04d}",
        "session_id": "sess-0",
        "state": "charging",
        "output_voltage_v": 400.0,
        "output_current_a": 150.0,
        "output_power_kw": 60.0,
        "connector_temp_c": 35.0,
        "energy_delivered_kwh": 1.0,
        "fault_code": 0,
    }
    row.update(overrides)
    return row


def _stall_row(catalog, index: int, **overrides):
    return canonicalize_row(_stall_raw_row(index, **overrides), catalog)


def _flags(flagged_row, kind: str, field: str) -> list:
    return [f for f in flagged_row.flags if f.kind == kind and f.field == field]


# ---------------------------------------------------------------------------------------- #
# range
# ---------------------------------------------------------------------------------------- #


def test_range_flag_fires_outside_valid_range(catalog):
    # catalog: output_current_a valid_range is [0.0, 257.5] (see catalog/signals.yaml).
    row = _stall_row(catalog, 0, output_current_a=300.0)
    result = flag_device_rows([row], catalog)
    assert _flags(result.rows[0], RANGE, "output_current_a")


def test_range_flag_does_not_fire_inside_valid_range(catalog):
    row = _stall_row(catalog, 0, output_current_a=150.0)
    result = flag_device_rows([row], catalog)
    assert not _flags(result.rows[0], RANGE, "output_current_a")


# ---------------------------------------------------------------------------------------- #
# stuck
# ---------------------------------------------------------------------------------------- #


def test_stuck_does_not_fire_below_threshold(catalog):
    # STUCK_RUN_THRESHOLD is 5: four identical consecutive readings must not fire yet.
    rows = [_stall_row(catalog, i, connector_temp_c=35.0) for i in range(4)]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[-1], STUCK, "connector_temp_c")


def test_stuck_fires_at_threshold(catalog):
    # A fifth identical reading crosses STUCK_RUN_THRESHOLD.
    rows = [_stall_row(catalog, i, connector_temp_c=35.0) for i in range(5)]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[3], STUCK, "connector_temp_c")
    assert _flags(result.rows[4], STUCK, "connector_temp_c")


def test_stuck_exempts_output_voltage_v(catalog):
    # output_voltage_v is documented (catalog + generator) as constant-by-design for an entire
    # session; it must never fire stuck even across many more than STUCK_RUN_THRESHOLD readings.
    rows = [_stall_row(catalog, i, output_voltage_v=400.0) for i in range(10)]
    result = flag_device_rows(rows, catalog)
    for flagged_row in result.rows:
        assert not _flags(flagged_row, STUCK, "output_voltage_v")


# ---------------------------------------------------------------------------------------- #
# counter reset
# ---------------------------------------------------------------------------------------- #


def test_counter_reset_fires_on_decrease_within_same_session(catalog):
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=5.0),
        _stall_row(catalog, 1, session_id="sess-0", energy_delivered_kwh=3.0),
    ]
    result = flag_device_rows(rows, catalog)
    assert _flags(result.rows[1], COUNTER_RESET, "energy_delivered_kwh")


def test_counter_reset_suppressed_on_decrease_at_confirmed_session_boundary(catalog):
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=5.0),
        _stall_row(catalog, 1, session_id="sess-1", energy_delivered_kwh=0.0),
    ]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[1], COUNTER_RESET, "energy_delivered_kwh")


def test_counter_reset_not_confused_by_normal_increase(catalog):
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=1.0),
        _stall_row(catalog, 1, session_id="sess-0", energy_delivered_kwh=1.25),
    ]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[1], COUNTER_RESET, "energy_delivered_kwh")


# ---------------------------------------------------------------------------------------- #
# jumps
# ---------------------------------------------------------------------------------------- #


def test_jump_fires_above_max_plausible_rate(catalog):
    # energy_delivered_kwh's max plausible rate is 0.05 kWh/s (see quality_flags.py table).
    # +4.0 kWh over one 15s interval is ~0.267 kWh/s, well above that.
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=1.0),
        _stall_row(catalog, 1, session_id="sess-0", energy_delivered_kwh=5.0),
    ]
    result = flag_device_rows(rows, catalog)
    assert _flags(result.rows[1], JUMP, "energy_delivered_kwh")


def test_jump_does_not_fire_for_normal_magnitude_change(catalog):
    # +0.03 kWh over 15s is 0.002 kWh/s, well under the 0.05 kWh/s threshold.
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=1.0),
        _stall_row(catalog, 1, session_id="sess-0", energy_delivered_kwh=1.03),
    ]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[1], JUMP, "energy_delivered_kwh")


def test_jump_suppressed_across_confirmed_session_boundary(catalog):
    # A huge energy_delivered_kwh drop-then-different-value across a genuine new session is not
    # a physically-implausible rate of change - it is two unrelated sessions.
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=50.0),
        _stall_row(catalog, 1, session_id="sess-1", energy_delivered_kwh=0.0),
    ]
    result = flag_device_rows(rows, catalog)
    assert not _flags(result.rows[1], JUMP, "energy_delivered_kwh")


# ---------------------------------------------------------------------------------------- #
# input contract / reconciliation
# ---------------------------------------------------------------------------------------- #


def test_first_reading_has_no_comparative_flags(catalog):
    # A device's first reading has nothing to compare against yet - counter_reset and jump are
    # inherently comparative and must never fire on it, regardless of its values.
    row = _stall_row(catalog, 0, energy_delivered_kwh=0.0)
    result = flag_device_rows([row], catalog)
    assert not _flags(result.rows[0], COUNTER_RESET, "energy_delivered_kwh")
    assert not _flags(result.rows[0], JUMP, "energy_delivered_kwh")


def test_mixed_device_ids_raise():
    catalog = load_catalog()
    row_a = canonicalize_row(_stall_raw_row(0, device_id="stall-214-0000"), catalog)
    row_b = canonicalize_row(_stall_raw_row(1, device_id="stall-214-0001"), catalog)
    with pytest.raises(MixedDeviceError):
        flag_device_rows([row_a, row_b], catalog)


def test_every_input_row_produces_exactly_one_output_row_with_data_intact(catalog):
    rows = [
        _stall_row(catalog, 0, session_id="sess-0", energy_delivered_kwh=1.0),
        _stall_row(catalog, 1, session_id="sess-0", energy_delivered_kwh=2.0, output_current_a=999.0),
        _stall_row(catalog, 2, session_id="sess-0", energy_delivered_kwh=1.5),  # counter reset
        _stall_row(catalog, 3, session_id="sess-1", energy_delivered_kwh=0.0),  # session boundary
    ]
    result = flag_device_rows(rows, catalog)

    assert result.reconciles()
    assert result.rows_seen == len(rows)
    assert len(result.rows) == len(rows)

    for original, flagged in zip(rows, result.rows):
        # The wrapped row is the exact same object - not a copy, not a mutated version.
        assert flagged.row is original
        assert flagged.row.fields == original.fields
        assert flagged.row.units == original.units
        assert flagged.row.dropped_fields == original.dropped_fields

    # Sanity: this particular sequence really did exercise flags (row 1 out of range, row 2 a
    # counter reset), so reconciles() isn't trivially true only because nothing ever fires.
    assert _flags(result.rows[1], RANGE, "output_current_a")
    assert _flags(result.rows[2], COUNTER_RESET, "energy_delivered_kwh")
