"""Tests for the synthetic Supercharger fixture generator.

These test the *generator*, not a parser (parser fixture tests land with P1-04). What matters
here is: the output is deterministic, has the schema future parsers will expect, and actually
contains the messiness (late/duplicate messages, timestamp problems, buffer/drop behavior)
that Phase 0's profilers are meant to measure on real data - a generator that "looks realistic"
but never actually injects the mess would be useless for testing dedup/quarantine logic.
"""
from __future__ import annotations

import json

import pytest

from tests.fixtures.generators.supercharger import (
    DEFAULT_FIRMWARE,
    GeneratorConfig,
    generate,
    write,
)


def _dense_config(seed: int = 1337) -> GeneratorConfig:
    """More devices/sessions than the default so every injected-issue type is near-certain to
    appear at least once, keeping these tests non-flaky without pinning exact counts."""
    return GeneratorConfig(seed=seed, devices_per_firmware=10, sessions_per_stall=4)


def _stringify_group_keys(messages_by_group):
    return {f"{k[0]}/{k[1]}": v for k, v in messages_by_group.items()}


def test_generate_is_deterministic():
    a = generate(_dense_config())
    b = generate(_dense_config())
    a_json = json.dumps(_stringify_group_keys(a.messages_by_group), sort_keys=True)
    b_json = json.dumps(_stringify_group_keys(b.messages_by_group), sort_keys=True)
    assert a_json == b_json


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1))
    b = generate(GeneratorConfig(seed=2))
    a_json = json.dumps(_stringify_group_keys(a.messages_by_group), sort_keys=True)
    b_json = json.dumps(_stringify_group_keys(b.messages_by_group), sort_keys=True)
    assert a_json != b_json


def test_all_device_class_firmware_groups_present():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    expected_groups = {
        (device_class, firmware)
        for device_class, firmwares in DEFAULT_FIRMWARE.items()
        for firmware in firmwares
    }
    assert set(fixtures.messages_by_group.keys()) == expected_groups
    for group, messages in fixtures.messages_by_group.items():
        assert messages, f"group {group} produced no messages"


def test_message_and_reading_schema():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    required_envelope_keys = {
        "message_id",
        "device_id",
        "device_class",
        "firmware_version",
        "site_id",
        "protocol",
        "sent_ts_ms",
        "arrival_ts_ms",
        "readings",
        "_debug_injected_issues",
    }
    for message in fixtures.all_messages():
        assert required_envelope_keys <= message.keys()
        assert message["readings"], "every message should carry at least one reading"
        for reading in message["readings"]:
            assert "device_ts_ms" in reading
            assert reading["device_ts_ms"] is None or isinstance(reading["device_ts_ms"], int)
            assert reading["payload_hash"].startswith("sha1:")


def test_stall_and_cabinet_have_distinct_fields():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    stall_group = next(g for g in fixtures.messages_by_group if g[0] == "supercharger_stall")
    cabinet_group = next(g for g in fixtures.messages_by_group if g[0] == "supercharger_cabinet")

    stall_reading = fixtures.messages_by_group[stall_group][0]["readings"][0]
    cabinet_reading = fixtures.messages_by_group[cabinet_group][0]["readings"][0]

    assert "output_current_a" in stall_reading
    assert "session_id" in stall_reading
    assert "grid_voltage_v" in cabinet_reading
    assert "active_stall_count" in cabinet_reading


@pytest.mark.parametrize(
    "issue_key",
    [
        "missing_timestamp",
        "epoch_default_timestamp",
        "future_timestamp",
        "late_arrival",
        "duplicate_message",
        "outage_events",
    ],
)
def test_every_injected_issue_type_appears(issue_key):
    fixtures = generate(_dense_config())
    total = sum(stats.get(issue_key, 0) for stats in fixtures.stats_by_group.values())
    assert total > 0, f"expected at least one '{issue_key}' across all groups"


def test_batch_sizes_vary_within_a_group():
    fixtures = generate(_dense_config())
    group = ("supercharger_stall", "2.1.4")
    batch_sizes = {len(m["readings"]) for m in fixtures.messages_by_group[group]}
    assert len(batch_sizes) > 1, "expected more than one distinct batch size"


def test_duplicate_messages_share_device_ts_and_payload_hash():
    fixtures = generate(_dense_config())
    by_id = {m["message_id"]: m for m in fixtures.all_messages()}

    found_a_duplicate = False
    for message in fixtures.all_messages():
        dup_tags = [
            issue
            for issue in message["_debug_injected_issues"]
            if issue.startswith("duplicate_of:")
        ]
        if not dup_tags:
            continue
        original_id = dup_tags[0].split(":", 1)[1]
        original = by_id[original_id]

        original_hashes = [r["payload_hash"] for r in original["readings"]]
        dup_hashes = [r["payload_hash"] for r in message["readings"]]
        original_ts = [r["device_ts_ms"] for r in original["readings"]]
        dup_ts = [r["device_ts_ms"] for r in message["readings"]]

        assert dup_hashes == original_hashes
        assert dup_ts == original_ts
        # The whole point of a retransmit: same content, later arrival.
        assert message["arrival_ts_ms"] > original["arrival_ts_ms"]
        found_a_duplicate = True

    assert found_a_duplicate, "expected at least one duplicate message in a dense run"


def test_write_roundtrip_and_manifest(tmp_path):
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    manifest_path = write(fixtures, tmp_path)

    manifest = json.loads(manifest_path.read_text())
    assert manifest["seed"] == fixtures.config.seed

    for (device_class, firmware), messages in fixtures.messages_by_group.items():
        jsonl_path = tmp_path / device_class / firmware / "messages.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().splitlines()
        assert len(lines) == len(messages)
        # Every line must be independently parseable JSON (true JSONL, not one big array).
        for line in lines:
            json.loads(line)

        group_key = f"{device_class}/{firmware}"
        assert group_key in manifest["groups"]
        assert manifest["groups"][group_key]["messages_total"] == len(messages)
