"""Tests for the parser framework (P1-03: "Parser framework with versioned per-firmware
parsers").

Covers the registry/dispatch/quarantine machinery in parsers/framework.py, plus its one
demonstration parser (parsers/supercharger_stall/firmware_2_1_4.py). Real per-firmware parser
coverage is P1-04 - these tests only need to prove the framework works end to end, not that
every firmware/device-class combination has a real parser (the opposite, in fact: everything
except supercharger_stall/2.1.4 must land in quarantine).
"""
from __future__ import annotations

import copy

import pytest

from parsers.framework import (
    DuplicateParserError,
    ParseResult,
    ReconciliationError,
    Registry,
    parse_messages,
    reconcile,
    register_parser,
)

# Importing the demo parser module triggers its @register_parser("supercharger_stall",
# "2.1.4") decorator, populating the framework's global registry - exactly like any other
# caller of parse_messages() would rely on.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from tests.fixtures.generators.supercharger import GeneratorConfig, generate


def _sample_stall_message(**overrides) -> dict:
    base = {
        "message_id": "msg-stall-2140001-1780358400000-1",
        "device_id": "stall-2140001-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_000,
        "readings": [
            {
                "session_id": "sess-stall-2140001-0000-0",
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
        ],
        "_debug_injected_issues": [],
    }
    base.update(overrides)
    return base


def test_registered_parser_produces_one_row_per_reading_with_field_passthrough():
    message = _sample_stall_message()
    reading = message["readings"][0]

    result = parse_messages([message])

    assert result.messages_seen == 1
    assert result.messages_parsed == 1
    assert result.messages_quarantined == 0
    assert result.rows_parsed == 1

    row = result.rows[0]
    assert row["device_id"] == message["device_id"]
    assert row["device_class"] == message["device_class"]
    assert row["firmware_version"] == message["firmware_version"]
    assert row["site_id"] == message["site_id"]
    for field in (
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
    ):
        assert row[field] == reading[field]
    assert result.reconciles()


def test_unregistered_pair_is_quarantined_with_reason_naming_the_pair():
    message = _sample_stall_message(firmware_version="9.9.9")
    original = copy.deepcopy(message)

    result = parse_messages([message])

    assert result.messages_seen == 1
    assert result.messages_parsed == 0
    assert result.messages_quarantined == 1

    quarantined = result.quarantined[0]
    assert "supercharger_stall" in quarantined.reason
    assert "9.9.9" in quarantined.reason
    assert quarantined.message == original, "quarantined message must be preserved verbatim"
    assert result.reconciles()


def test_parser_error_quarantines_only_that_message_and_continues():
    """A parser bug must degrade to quarantine for the one bad message, not crash the batch."""

    def _raising_parser(message: dict) -> list[dict]:
        raise ValueError("deliberate test failure")

    local_registry: Registry = {}
    register_parser("fake_device", "9.9.9", registry=local_registry)(_raising_parser)
    register_parser("fake_device", "1.0.0", registry=local_registry)(
        lambda message: [{"ok": True, "device_id": message["device_id"]}]
    )

    good = {"device_class": "fake_device", "firmware_version": "1.0.0", "device_id": "d-1"}
    bad = {"device_class": "fake_device", "firmware_version": "9.9.9", "device_id": "d-2"}

    result = parse_messages([good, bad], registry=local_registry)

    assert result.messages_seen == 2
    assert result.messages_parsed == 1
    assert result.messages_quarantined == 1
    assert result.rows_parsed == 1
    assert result.rows[0]["device_id"] == "d-1"

    quarantined = result.quarantined[0]
    assert quarantined.message == bad
    assert quarantined.reason == "parser_error: deliberate test failure"
    assert result.reconciles()
    reconcile(result)  # should not raise


def test_result_counts_are_internally_consistent_including_zero_row_message():
    local_registry: Registry = {}
    register_parser("fake_device", "0.0.1", registry=local_registry)(lambda message: [])

    message = {"device_class": "fake_device", "firmware_version": "0.0.1"}
    result = parse_messages([message], registry=local_registry)

    assert result.messages_seen == 1
    assert result.messages_parsed == 1
    assert result.rows_parsed == 0
    assert result.messages_quarantined == 0
    assert result.messages_seen == result.messages_parsed + result.messages_quarantined
    assert result.reconciles()


def test_reconcile_raises_when_counts_are_inconsistent():
    bad_result = ParseResult(messages_seen=3, messages_parsed=1, rows=(), quarantined=())
    with pytest.raises(ReconciliationError):
        reconcile(bad_result)


def test_register_parser_rejects_duplicate_registration():
    local_registry: Registry = {}
    register_parser("fake_device", "dup", registry=local_registry)(lambda message: [])
    with pytest.raises(DuplicateParserError):
        register_parser("fake_device", "dup", registry=local_registry)(lambda message: [])


def test_full_generator_output_only_stall_2_1_4_parses_rest_quarantined():
    """The actual P1-03 "done when" check: run the full synthetic generator output through the
    framework and confirm only (supercharger_stall, 2.1.4) - the one registered demo parser -
    produces rows, while every other (device_class, firmware_version) combination is
    quarantined, and nothing is silently dropped."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())
    assert messages, "fixture generator produced no messages to dispatch"

    result = parse_messages(messages)

    assert result.messages_seen == len(messages)
    assert result.reconciles()
    assert result.rows_parsed > 0

    registered_pair = ("supercharger_stall", "2.1.4")
    expected_parsed = sum(
        1
        for m in messages
        if (m["device_class"], m["firmware_version"]) == registered_pair
    )
    expected_quarantined = len(messages) - expected_parsed
    assert expected_parsed > 0, "fixture generator must include the registered demo pair"
    assert expected_quarantined > 0, "fixture generator must include unregistered pairs too"

    assert result.messages_parsed == expected_parsed
    assert result.messages_quarantined == expected_quarantined

    quarantined_pairs = {
        (q.message["device_class"], q.message["firmware_version"]) for q in result.quarantined
    }
    assert registered_pair not in quarantined_pairs
    assert quarantined_pairs == {
        ("supercharger_stall", "2.3.0"),
        ("supercharger_stall", "3.0.1"),
        ("supercharger_cabinet", "1.8.2"),
        ("supercharger_cabinet", "1.9.0"),
    }

    # Every quarantined record must carry its original message verbatim and a reason naming
    # the (device_class, firmware_version) pair that had no parser.
    for q in result.quarantined:
        assert q.reason.startswith("no_parser_registered_for_")
        assert q.message["device_class"] in q.reason or q.message["firmware_version"] in q.reason

    # Every row produced must come from a stall/2.1.4 message and carry that identity through.
    for row in result.rows:
        assert row["device_class"] == "supercharger_stall"
        assert row["firmware_version"] == "2.1.4"
