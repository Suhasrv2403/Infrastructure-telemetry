"""Tests for Stage 2 canonicalization (P1-08: "Stage 2 canonicalization via signal catalog").

Done when (Build backlog.md): "Canonical names, types and units for all pilot signals."

Honest scope note - same pattern as every other Phase 0/1 ticket in this repo (see e.g.
tests/unit/test_supercharger_parsers.py's own docstring): these tests run against the v0 DRAFT
catalog copied from branch P0-10-signal-catalog-v0 (catalog/signals.yaml) and the synthetic
fixture generator (tests/fixtures/generators/supercharger.py) - real content, but neither is
firmware-SME-reviewed, and both may diverge from real Supercharger hardware once that review
happens.
"""
from __future__ import annotations

import pytest

from parsers.framework import parse_messages
from parsers.supercharger_cabinet.firmware_1_8_2 import (
    parse_supercharger_cabinet_1_8_2,  # noqa: F401
)
from parsers.supercharger_cabinet.firmware_1_9_0 import (
    parse_supercharger_cabinet_1_9_0,  # noqa: F401
)

# Importing each firmware module triggers its @register_parser(...) decorator - see
# tests/unit/test_supercharger_parsers.py for why all five are imported here rather than
# relying on import order from other test modules.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from parsers.supercharger_stall.firmware_2_3_0 import parse_supercharger_stall_2_3_0  # noqa: F401
from parsers.supercharger_stall.firmware_3_0_1 import parse_supercharger_stall_3_0_1  # noqa: F401
from pipeline.stage2_canonical.canonicalize import (
    CastError,
    CatalogError,
    canonicalize_row,
    canonicalize_rows,
    load_catalog,
)
from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE, GeneratorConfig, generate


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _stall_row(**overrides) -> dict:
    row = {
        "device_id": "supercharger_stall-2.1.4-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "arrival_ts_ms": 1_780_358_400_777,
        "session_id": "sess-0",
        "state": "charging",
        "output_voltage_v": 400.1,
        "output_current_a": 150.2,
        "output_power_kw": 60.06,
        "connector_temp_c": 37.5,
        "energy_delivered_kwh": 1.234,
        "fault_code": 0,
        "device_ts_ms": 1_780_358_400_000,
        "payload_hash": "sha1:deadbeef",
    }
    row.update(overrides)
    return row


def _cabinet_row(**overrides) -> dict:
    row = {
        "device_id": "supercharger_cabinet-1.8.2-0000",
        "device_class": "supercharger_cabinet",
        "firmware_version": "1.8.2",
        "site_id": "site-000",
        "arrival_ts_ms": 1_780_358_400_777,
        "grid_voltage_v": 480.2,
        "grid_frequency_hz": 60.01,
        "transformer_temp_c": 42.3,
        "contactor_closed": True,
        "active_stall_count": 4,
        "aggregate_power_kw": 180.5,
        "fault_code": 0,
        "device_ts_ms": 1_780_358_400_000,
        "payload_hash": "sha1:cafef00d",
    }
    row.update(overrides)
    return row


# --- catalog loading ---------------------------------------------------------------------


def test_catalog_loads_and_covers_all_default_firmware_pairs(catalog):
    for firmware_version in DEFAULT_FIRMWARE["supercharger_stall"]:
        assert ("supercharger_stall", firmware_version) in catalog
    for firmware_version in DEFAULT_FIRMWARE["supercharger_cabinet"]:
        assert ("supercharger_cabinet", firmware_version) in catalog


# --- fully-covered row canonicalizes correctly -------------------------------------------


def test_stall_row_fully_covered_by_catalog_canonicalizes_correctly(catalog):
    result = canonicalize_row(_stall_row(), catalog)

    # Renames per signals.yaml's documented rename list.
    assert result.fields["session_state"] == "charging"
    assert result.fields["stall_fault_code"] == 0
    assert "state" not in result.fields
    assert "fault_code" not in result.fields

    # Type casts: float fields stay float, int (fault_code -> stall_fault_code) becomes int.
    assert result.fields["output_voltage_v"] == pytest.approx(400.1)
    assert isinstance(result.fields["output_voltage_v"], float)
    assert isinstance(result.fields["stall_fault_code"], int)

    # Units carried forward as metadata.
    assert result.units["output_voltage_v"] == "volts"
    assert result.units["stall_fault_code"] == "enum"

    # session_id (type: string) passes through as its canonical name unchanged.
    assert result.fields["session_id"] == "sess-0"

    # site_id has no catalog entry and isn't an identity field per this module's docstring -
    # it is dropped, and that's recorded.
    assert "site_id" not in result.fields
    assert "site_id" in result.dropped_fields


def test_cabinet_row_fully_covered_by_catalog_canonicalizes_correctly(catalog):
    result = canonicalize_row(_cabinet_row(), catalog)

    assert result.fields["cabinet_fault_code"] == 0
    assert "fault_code" not in result.fields

    # bool type cast.
    assert result.fields["contactor_closed"] is True
    assert isinstance(result.fields["contactor_closed"], bool)

    # int type cast.
    assert result.fields["active_stall_count"] == 4
    assert isinstance(result.fields["active_stall_count"], int)

    # float type cast + unit metadata.
    assert result.fields["grid_voltage_v"] == pytest.approx(480.2)
    assert result.units["grid_voltage_v"] == "volts"
    assert result.units["active_stall_count"] == "count"


# --- uncataloged field is dropped, not an error -------------------------------------------


def test_field_not_in_catalog_for_its_firmware_is_dropped_without_erroring(catalog):
    row = _stall_row(some_unmapped_field="whatever")

    result = canonicalize_row(row, catalog)

    assert "some_unmapped_field" not in result.fields
    assert "some_unmapped_field" in result.dropped_fields
    # The rest of the row still canonicalizes normally.
    assert result.fields["session_state"] == "charging"


def test_canonicalize_rows_counts_dropped_fields_in_aggregate(catalog):
    rows = [_stall_row(), _stall_row(session_id="sess-1"), _cabinet_row()]

    result = canonicalize_rows(rows, catalog)

    assert result.rows_seen == 3
    assert result.rows_canonicalized == 3
    assert result.rows_failed == 0
    assert result.reconciles()
    # site_id is dropped from every one of these 3 rows.
    assert result.fields_dropped_total == 3
    assert result.dropped_field_counts() == {"site_id": 3}


# --- identity fields always pass through ---------------------------------------------------


def test_identity_fields_pass_through_regardless_of_catalog_coverage(catalog):
    # Use a firmware_version with NO catalog entry at all - identity fields must still pass.
    row = {
        "device_id": "dev-x",
        "device_class": "supercharger_stall",
        "firmware_version": "9.9.9",
        "arrival_ts_ms": 42,
        "device_ts_ms": 1_000,
        "payload_hash": "sha1:aaaa",
        "output_voltage_v": 400.0,
    }

    result = canonicalize_row(row, catalog)

    assert result.fields["device_id"] == "dev-x"
    assert result.fields["device_class"] == "supercharger_stall"
    assert result.fields["firmware_version"] == "9.9.9"
    assert result.fields["arrival_ts_ms"] == 42
    assert result.fields["device_ts_ms"] == 1_000
    assert result.fields["payload_hash"] == "sha1:aaaa"
    # output_voltage_v has no catalog entry for firmware 9.9.9 - dropped, not identity.
    assert "output_voltage_v" not in result.fields
    assert "output_voltage_v" in result.dropped_fields


def test_identity_fields_pass_through_even_when_device_ts_ms_is_none(catalog):
    # The generator can legitimately produce a null device_ts_ms (injected clock messiness) -
    # identity passthrough must not choke on that.
    row = _stall_row(device_ts_ms=None)

    result = canonicalize_row(row, catalog)

    assert result.fields["device_ts_ms"] is None


# --- type casting -----------------------------------------------------------------------


def test_uncastable_value_raises_cast_error_rather_than_silently_coercing(catalog):
    row = _stall_row(output_voltage_v="not-a-number")

    with pytest.raises(CastError):
        canonicalize_row(row, catalog)


def test_canonicalize_rows_degrades_uncastable_row_to_a_recorded_failure(catalog):
    good_row = _stall_row()
    bad_row = _stall_row(output_current_a="not-a-number")

    result = canonicalize_rows([good_row, bad_row], catalog)

    assert result.rows_seen == 2
    assert result.rows_canonicalized == 1
    assert result.rows_failed == 1
    assert result.reconciles()
    failed_row, exc = result.failed_rows[0]
    assert failed_row is bad_row
    assert isinstance(exc, CastError)


def test_bool_cast_rejects_string_that_is_not_true_or_false(catalog):
    row = _cabinet_row(contactor_closed="not-a-bool")
    with pytest.raises(CastError):
        canonicalize_row(row, catalog)


def test_bool_cast_accepts_string_true_false_case_insensitive(catalog):
    row = _cabinet_row(contactor_closed="False")
    result = canonicalize_row(row, catalog)
    assert result.fields["contactor_closed"] is False


def test_load_catalog_rejects_malformed_entry(tmp_path):
    bad_catalog = tmp_path / "bad_signals.yaml"
    bad_catalog.write_text(
        "supercharger_stall:\n"
        "  '2.1.4':\n"
        "    some_field:\n"
        "      unit: A\n"  # missing canonical_name and type
    )
    with pytest.raises(CatalogError):
        load_catalog(bad_catalog)


# --- full synthetic generator run: "for all pilot signals" ---------------------------------


def test_full_generator_output_canonicalizes_without_exceptions_and_reports_coverage(catalog):
    """The ticket's concrete evidence for "for all pilot signals": run the full synthetic
    generator's output through the real parsers, then through canonicalize_rows, and confirm
    it runs to completion with no exceptions. Prints the real per-raw-field coverage/gap
    numbers this ticket's report is required to state honestly (see this module's own
    docstring and pipeline/stage2_canonical/canonicalize.py's)."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=3))
    messages = list(fixtures.all_messages())
    assert messages, "fixture generator produced no messages"

    parse_result = parse_messages(messages)
    assert parse_result.reconciles()
    assert parse_result.messages_quarantined == 0
    assert parse_result.rows_parsed > 0

    canon_result = canonicalize_rows(list(parse_result.rows), catalog)

    assert canon_result.reconciles()
    assert canon_result.rows_seen == parse_result.rows_parsed
    # Every row canonicalizes without a cast failure against this catalog/generator pairing.
    assert canon_result.rows_failed == 0
    assert canon_result.rows_canonicalized == canon_result.rows_seen

    dropped_counts = canon_result.dropped_field_counts()
    # site_id is the one raw field every row emits that has no catalog entry (see module
    # docstring's "Explicitly NOT covered" section) - confirmed here against real generator
    # output, not just a hand-built fixture row.
    assert dropped_counts == {"site_id": canon_result.rows_seen}

    # Every cataloged raw field for both device classes actually appears, canonicalized, in at
    # least one row - concrete coverage evidence, not an assumption.
    stall_canonical_names = {
        "session_id", "session_state", "output_voltage_v", "output_current_a",
        "output_power_kw", "connector_temp_c", "energy_delivered_kwh", "stall_fault_code",
        "device_ts_ms", "payload_hash",
    }
    cabinet_canonical_names = {
        "grid_voltage_v", "grid_frequency_hz", "transformer_temp_c", "contactor_closed",
        "active_stall_count", "aggregate_power_kw", "cabinet_fault_code", "device_ts_ms",
        "payload_hash",
    }
    seen_stall_names: set = set()
    seen_cabinet_names: set = set()
    for row in canon_result.rows:
        if row.fields.get("device_class") == "supercharger_stall":
            seen_stall_names |= set(row.fields.keys())
        elif row.fields.get("device_class") == "supercharger_cabinet":
            seen_cabinet_names |= set(row.fields.keys())

    assert stall_canonical_names <= seen_stall_names
    assert cabinet_canonical_names <= seen_cabinet_names
