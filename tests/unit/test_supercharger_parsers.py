"""Tests for full per-firmware parser coverage of both Supercharger device classes (P1-04:
"Supercharger stall and cabinet parsers").

Honest scope note: real Supercharger payloads don't exist yet - P0-05 only captures
tests/fixtures/generators/supercharger.py's synthetic output until P1-01 replaces the source,
and that generator does not currently vary raw reading field *names* across firmware versions
within a device class (only the rates of injected clock/lateness/retry messiness - see its own
module docstring). So these are fixture tests from the synthetic generator, not from real
payloads - the same pattern every other Phase 0/1 ticket in this repo has used so far, not a
shortcut invented for this ticket.

Covers:
- each of the 5 registered (device_class, firmware_version) parsers individually, spot-
  checking field passthrough correctness including the arrival_ts_ms addition;
- the ticket's concrete "done when" evidence: running the full default generator's output
  through parse_messages() quarantines zero messages, because every (device_class,
  firmware_version) pair in DEFAULT_FIRMWARE now has a registered parser.
"""
from __future__ import annotations

import pytest

from parsers.framework import parse_messages, registered_parsers
from parsers.supercharger_cabinet.firmware_1_8_2 import (
    parse_supercharger_cabinet_1_8_2,  # noqa: F401
)
from parsers.supercharger_cabinet.firmware_1_9_0 import (
    parse_supercharger_cabinet_1_9_0,  # noqa: F401
)

# Importing each firmware module triggers its @register_parser(...) decorator, populating the
# framework's global registry - exactly like any other caller of parse_messages() would rely
# on. Importing all five here (rather than relying on other test modules to do it first) makes
# this module's coverage self-sufficient regardless of test run order/selection.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from parsers.supercharger_stall.firmware_2_3_0 import parse_supercharger_stall_2_3_0  # noqa: F401
from parsers.supercharger_stall.firmware_3_0_1 import parse_supercharger_stall_3_0_1  # noqa: F401
from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE, GeneratorConfig, generate

_STALL_FIRMWARE = DEFAULT_FIRMWARE["supercharger_stall"]
_CABINET_FIRMWARE = DEFAULT_FIRMWARE["supercharger_cabinet"]

_STALL_READING_FIELDS = (
    "output_current_a",
    "output_voltage_v",
    "output_power_kw",
    "connector_temp_c",
    "energy_delivered_kwh",
    "fault_code",
    "state",
    "session_id",
    "device_ts_ms",
    "payload_hash",
)

_CABINET_READING_FIELDS = (
    "grid_voltage_v",
    "grid_frequency_hz",
    "transformer_temp_c",
    "contactor_closed",
    "active_stall_count",
    "aggregate_power_kw",
    "fault_code",
    "device_ts_ms",
    "payload_hash",
)


def _sample_message(device_class: str, firmware_version: str, reading: dict) -> dict:
    return {
        "message_id": f"msg-{device_class}-{firmware_version}-1",
        "device_id": f"{device_class}-{firmware_version}-0000",
        "device_class": device_class,
        "firmware_version": firmware_version,
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_777,
        "readings": [reading],
        "_debug_injected_issues": [],
    }


def _stall_reading() -> dict:
    return {
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


def _cabinet_reading() -> dict:
    return {
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


@pytest.mark.parametrize("firmware_version", ["2.1.4", "2.3.0", "3.0.1"])
def test_stall_firmware_parser_field_passthrough(firmware_version):
    reading = _stall_reading()
    message = _sample_message("supercharger_stall", firmware_version, reading)

    result = parse_messages([message])

    assert result.reconciles()
    assert result.messages_quarantined == 0
    assert result.rows_parsed == 1

    row = result.rows[0]
    assert row["device_id"] == message["device_id"]
    assert row["device_class"] == "supercharger_stall"
    assert row["firmware_version"] == firmware_version
    assert row["site_id"] == message["site_id"]
    assert row["arrival_ts_ms"] == message["arrival_ts_ms"]
    for field in _STALL_READING_FIELDS:
        assert row[field] == reading[field]


@pytest.mark.parametrize("firmware_version", ["1.8.2", "1.9.0"])
def test_cabinet_firmware_parser_field_passthrough(firmware_version):
    reading = _cabinet_reading()
    message = _sample_message("supercharger_cabinet", firmware_version, reading)

    result = parse_messages([message])

    assert result.reconciles()
    assert result.messages_quarantined == 0
    assert result.rows_parsed == 1

    row = result.rows[0]
    assert row["device_id"] == message["device_id"]
    assert row["device_class"] == "supercharger_cabinet"
    assert row["firmware_version"] == firmware_version
    assert row["site_id"] == message["site_id"]
    assert row["arrival_ts_ms"] == message["arrival_ts_ms"]
    for field in _CABINET_READING_FIELDS:
        assert row[field] == reading[field]


def test_all_default_firmware_pairs_are_registered():
    """Concrete registry check: every (device_class, firmware_version) pair in the fixture
    generator's DEFAULT_FIRMWARE has its own registered parser entry."""
    pairs = set(registered_parsers())
    for firmware_version in _STALL_FIRMWARE:
        assert ("supercharger_stall", firmware_version) in pairs
    for firmware_version in _CABINET_FIRMWARE:
        assert ("supercharger_cabinet", firmware_version) in pairs


def test_full_generator_output_has_zero_quarantined_messages():
    """The ticket's concrete "done when" evidence: every (device_class, firmware_version) pair
    the synthetic generator produces now has a registered parser, so running its full default
    output through parse_messages() quarantines nothing."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())
    assert messages, "fixture generator produced no messages to dispatch"

    result = parse_messages(messages)

    assert result.messages_seen == len(messages)
    assert result.reconciles()
    assert result.messages_quarantined == 0
    assert result.quarantined == ()
    assert result.messages_parsed == len(messages)
    assert result.rows_parsed > 0

    seen_pairs = {(row["device_class"], row["firmware_version"]) for row in result.rows}
    expected_pairs = {
        ("supercharger_stall", fw) for fw in _STALL_FIRMWARE
    } | {("supercharger_cabinet", fw) for fw in _CABINET_FIRMWARE}
    assert seen_pairs == expected_pairs

    # Every row must carry arrival_ts_ms (P1-05 needs it for timestamp sanity checks).
    for row in result.rows:
        assert "arrival_ts_ms" in row
        assert isinstance(row["arrival_ts_ms"], int)
