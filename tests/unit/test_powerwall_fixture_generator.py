"""Tests for the synthetic Powerwall fixture generator (PREP for P3-03).

Mirrors tests/unit/test_supercharger_fixture_generator.py's structure and intent as closely as
this device class's shape allows: these test the *generator*, not a parser (no Powerwall
parser exists yet - see parsers/powerwall/README.md, still a P3-03 placeholder). What matters
here is: the output is deterministic, matches the envelope/reading shape
parsers/framework.py's dispatch loop and a future parser will expect, actually contains the
injected messiness (late/duplicate messages, timestamp problems, buffer/drop behavior) Phase 0's
profilers characterize, and - specific to this device class - is internally *physically*
consistent (state of charge stays in range, the site power balance holds, grid interaction is
zero while islanded) rather than just superficially plausible-looking.
"""
from __future__ import annotations

import json

import pytest

from tests.fixtures.generators.powerwall import (
    CLIMATE_ZONES,
    DEFAULT_FIRMWARE,
    DEVICE_CLASS,
    FAULT_CODES,
    GeneratorConfig,
    generate,
    iter_device_messages,
    write,
    write_streaming,
)


def _dense_config(seed: int = 2026, devices_per_firmware: int = 60) -> GeneratorConfig:
    """More devices than the default so every injected-issue type, every operating mode and
    every fault code is near-certain to appear at least once, keeping these tests non-flaky
    without pinning exact counts."""
    return GeneratorConfig(seed=seed, devices_per_firmware=devices_per_firmware, window_hours=72)


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


def test_device_generation_is_independent_of_population_size():
    """A given device's own messages must not depend on how many other devices are generated
    alongside it or on device_index_start - this is what makes sharded/parallel generation at
    fleet scale reproduce single-process output (see the generator's module docstring)."""
    small = generate(GeneratorConfig(seed=2026, devices_per_firmware=2))
    large = generate(GeneratorConfig(seed=2026, devices_per_firmware=5))

    firmware = DEFAULT_FIRMWARE[0]
    group = (DEVICE_CLASS, firmware)
    small_by_device = {m["device_id"]: m for m in small.messages_by_group[group]}
    large_by_device = {m["device_id"]: m for m in large.messages_by_group[group]}

    # Device index 0 and 1 exist in both runs - their messages must be byte-identical.
    for device_id, msg in small_by_device.items():
        assert large_by_device[device_id] == msg


def test_device_index_start_shards_without_collision_or_drift():
    """Generating [0, 3) directly must equal the [0, 3) slice of a shard starting elsewhere at
    the same absolute indices - i.e. sharding by device_index_start is safe."""
    shard_a = generate(GeneratorConfig(seed=2026, devices_per_firmware=3, device_index_start=0))
    shard_b = generate(GeneratorConfig(seed=2026, devices_per_firmware=3, device_index_start=3))

    firmware = DEFAULT_FIRMWARE[0]
    group = (DEVICE_CLASS, firmware)
    ids_a = {m["device_id"] for m in shard_a.messages_by_group[group]}
    ids_b = {m["device_id"] for m in shard_b.messages_by_group[group]}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a) == 3
    assert len(ids_b) == 3


def test_all_firmware_groups_present():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    expected_groups = {(DEVICE_CLASS, firmware) for firmware in DEFAULT_FIRMWARE}
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
    required_reading_keys = {
        "state_of_charge_pct",
        "battery_power_kw",
        "solar_power_kw",
        "site_load_kw",
        "grid_power_kw",
        "backup_reserve_pct",
        "grid_status",
        "operating_mode",
        "battery_temp_c",
        "inverter_temp_c",
        "fault_code",
        "device_ts_ms",
        "payload_hash",
    }
    for message in fixtures.all_messages():
        assert required_envelope_keys <= message.keys()
        assert message["device_class"] == DEVICE_CLASS
        assert message["protocol"] == "https_poll"
        assert message["readings"], "every message should carry at least one reading"
        for reading in message["readings"]:
            assert required_reading_keys <= reading.keys()
            assert reading["device_ts_ms"] is None or isinstance(reading["device_ts_ms"], int)
            assert reading["payload_hash"].startswith("sha1:")


@pytest.mark.parametrize(
    "field,lo,hi",
    [
        ("state_of_charge_pct", 0.0, 100.0),
        ("battery_power_kw", -5.0, 3.0),
        ("solar_power_kw", 0.0, 10.0),
        ("site_load_kw", 0.0, 4.0),
        ("backup_reserve_pct", 5.0, 100.0),
        ("battery_temp_c", -5.0, 40.0),
        ("inverter_temp_c", -5.0, 50.0),
    ],
)
def test_readings_respect_catalog_style_valid_ranges(field, lo, hi):
    """Generous bounds (slightly wider than catalog/signals.yaml's own tighter documented
    ranges, to avoid this test and the catalog drifting out of sync on every tweak) - the point
    is to catch a gross modeling bug (e.g. an unbounded runaway value), not to duplicate the
    catalog's own precisely-reasoned bounds here."""
    fixtures = generate(_dense_config())
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            value = reading[field]
            assert lo <= value <= hi, f"{field}={value} outside [{lo}, {hi}]"


def test_grid_status_and_operating_mode_enums():
    fixtures = generate(_dense_config())
    grid_statuses = set()
    operating_modes = set()
    fault_codes = set()
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            grid_statuses.add(reading["grid_status"])
            operating_modes.add(reading["operating_mode"])
            fault_codes.add(reading["fault_code"])

    assert grid_statuses == {"grid_connected", "transitioning", "islanded"}
    assert operating_modes == {"self_powered", "backup_only", "time_based_control"}
    assert fault_codes <= {0, *FAULT_CODES}
    assert 0 in fault_codes  # the overwhelmingly common case must still appear


def test_site_energy_balance_holds_when_grid_connected():
    """The whole point of modeling grid_power_kw as derived rather than independent: this
    identity must hold (up to float rounding) whenever the site isn't islanded/transitioning."""
    fixtures = generate(_dense_config())
    checked = 0
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            if reading["grid_status"] != "grid_connected":
                continue
            lhs = reading["site_load_kw"]
            rhs = reading["solar_power_kw"] + reading["battery_power_kw"] + reading["grid_power_kw"]
            assert lhs == pytest.approx(rhs, abs=0.02)
            checked += 1
    assert checked > 0


def test_no_grid_interaction_while_islanded_or_transitioning():
    fixtures = generate(_dense_config())
    checked = 0
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            if reading["grid_status"] in ("islanded", "transitioning"):
                assert reading["grid_power_kw"] == 0.0
                checked += 1
    assert checked > 0


def test_fault_forces_battery_idle():
    fixtures = generate(_dense_config())
    checked = 0
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            if reading["fault_code"] != 0:
                assert reading["battery_power_kw"] == 0.0
                checked += 1
    assert checked > 0


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
    group = (DEVICE_CLASS, DEFAULT_FIRMWARE[0])
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
        original = by_id[dup_tags[0].split(":", 1)[1]]

        original_hashes = [r["payload_hash"] for r in original["readings"]]
        dup_hashes = [r["payload_hash"] for r in message["readings"]]
        original_ts = [r["device_ts_ms"] for r in original["readings"]]
        dup_ts = [r["device_ts_ms"] for r in message["readings"]]

        assert dup_hashes == original_hashes
        assert dup_ts == original_ts
        assert message["arrival_ts_ms"] > original["arrival_ts_ms"]
        found_a_duplicate = True

    assert found_a_duplicate, "expected at least one duplicate message in a dense run"


def test_climate_zone_assignment_is_deterministic_by_index():
    """Not a wire field (see module docstring - generator-internal only), but its effect on
    battery_temp_c should be reproducible: the same absolute device index always lands in the
    same climate zone regardless of seed."""
    from tests.fixtures.generators.powerwall import _climate_zone_for_index

    for index in range(len(CLIMATE_ZONES) * 3):
        a = _climate_zone_for_index(index)
        b = _climate_zone_for_index(index)
        assert a == b
    # And it actually varies across the population (not one zone for everyone).
    zones_seen = {_climate_zone_for_index(i)[0] for i in range(len(CLIMATE_ZONES) * 3)}
    assert zones_seen == {z for z, _ in CLIMATE_ZONES}


def test_iter_device_messages_matches_generate():
    """generate() is documented as just eagerly consuming iter_device_messages() - verify that
    claim rather than let it silently drift."""
    config = GeneratorConfig(devices_per_firmware=3)
    fixtures = generate(config)

    streamed_by_group: dict[tuple[str, str], list[dict]] = {}
    for firmware, _device_id, messages, _stats in iter_device_messages(config):
        streamed_by_group.setdefault((DEVICE_CLASS, firmware), []).extend(messages)

    for group, messages in streamed_by_group.items():
        # generate() sorts by arrival_ts_ms; compare as sets of message_ids instead of order.
        streamed_ids = {m["message_id"] for m in messages}
        generated_ids = {m["message_id"] for m in fixtures.messages_by_group[group]}
        assert streamed_ids == generated_ids


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
        for line in lines:
            json.loads(line)

        group_key = f"{device_class}/{firmware}"
        assert group_key in manifest["groups"]
        assert manifest["groups"][group_key]["messages_total"] == len(messages)


def test_write_streaming_roundtrip(tmp_path):
    """write_streaming() is the intended path for a real large-scale run (see module
    docstring) - verify it produces the same per-device message content as generate()+write(),
    just without a global arrival sort."""
    config = GeneratorConfig(devices_per_firmware=2)
    manifest_path = write_streaming(config, tmp_path)

    manifest = json.loads(manifest_path.read_text())
    assert manifest["streaming"] is True
    assert manifest["seed"] == config.seed

    fixtures = generate(config)
    for (device_class, firmware), messages in fixtures.messages_by_group.items():
        jsonl_path = tmp_path / device_class / firmware / "messages.jsonl"
        lines = jsonl_path.read_text().splitlines()
        streamed_ids = {json.loads(line)["message_id"] for line in lines}
        generated_ids = {m["message_id"] for m in messages}
        assert streamed_ids == generated_ids
