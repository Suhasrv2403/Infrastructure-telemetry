"""Tests for the synthetic Megapack/Powerpack fixture generator.

PREP WORK for P2-13, mirroring tests/unit/test_supercharger_fixture_generator.py's own scope
note: these test the *generator* (deterministic, has the schema a future P2-13 parser will
expect, actually injects the messiness categories it claims to), not a parser - real parser
fixture tests land with P2-13 itself. Additionally (the genuinely new part relative to the
Supercharger generator) these check that the pack-level and cell-level streams are honestly
distinct message/record types with the documented cadence relationship, and that every
generated value respects catalog/signals.yaml's declared valid_range for its field - a schema-
consistency check the Supercharger generator's own test file doesn't need, because this
generator's catalog entries were authored in lockstep with it on this same branch (see
catalog/signals.yaml's Megapack/Powerpack PROVENANCE note).
"""
from __future__ import annotations

import json

import pytest

from pipeline.stage2_canonical.canonicalize import load_catalog
from tests.fixtures.generators.megapack_powerpack import (
    CELL_DEVICE_CLASS,
    DEFAULT_FIRMWARE,
    PRODUCT_SPECS,
    GeneratorConfig,
    generate,
    write,
)


def _dense_config(seed: int = 2026) -> GeneratorConfig:
    """More devices than the default so every injected-issue type is near-certain to appear at
    least once, keeping these tests non-flaky without pinning exact counts - same rationale as
    supercharger.py's own test file's _dense_config."""
    return GeneratorConfig(seed=seed, devices_per_firmware=6)


def _stringify_group_keys(messages_by_group):
    return {f"{k[0]}/{k[1]}": v for k, v in messages_by_group.items()}


def _expected_groups() -> set[tuple[str, str]]:
    groups = set()
    for product, firmwares in DEFAULT_FIRMWARE.items():
        spec = PRODUCT_SPECS[product]
        for firmware in firmwares:
            groups.add((spec.device_class, firmware))
            groups.add((spec.cell_device_class, firmware))
    return groups


def test_generate_is_deterministic():
    a = generate(GeneratorConfig(seed=1, devices_per_firmware=2))
    b = generate(GeneratorConfig(seed=1, devices_per_firmware=2))
    a_json = json.dumps(_stringify_group_keys(a.messages_by_group), sort_keys=True)
    b_json = json.dumps(_stringify_group_keys(b.messages_by_group), sort_keys=True)
    assert a_json == b_json


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1, devices_per_firmware=2))
    b = generate(GeneratorConfig(seed=2, devices_per_firmware=2))
    a_json = json.dumps(_stringify_group_keys(a.messages_by_group), sort_keys=True)
    b_json = json.dumps(_stringify_group_keys(b.messages_by_group), sort_keys=True)
    assert a_json != b_json


def test_all_device_class_firmware_groups_present():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    assert set(fixtures.messages_by_group.keys()) == _expected_groups()
    for group, messages in fixtures.messages_by_group.items():
        assert messages, f"group {group} produced no messages"


def test_message_and_reading_envelope_schema():
    """Same envelope shape as supercharger.py's output (required for parser-framework/Stage 0
    compatibility), for both pack-level and cell-level streams."""
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


def test_pack_and_cell_are_distinct_streams_sharing_device_id():
    """The genuinely new structural element (see module docstring): cell-level messages are a
    separate device_class from pack-level, not extra pack-row columns, but a cell message's
    device_id/firmware_version match its parent pack device's - same physical unit, two
    streams."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))

    pack_device_ids = {
        m["device_id"]
        for (device_class, _fw), msgs in fixtures.messages_by_group.items()
        if device_class == "megapack"
        for m in msgs
    }
    cell_device_ids = {
        m["device_id"]
        for (device_class, _fw), msgs in fixtures.messages_by_group.items()
        if device_class == "megapack_cell"
        for m in msgs
    }
    assert pack_device_ids, "expected at least one megapack pack device"
    assert cell_device_ids == pack_device_ids

    pack_reading = fixtures.messages_by_group[("megapack", "1.42.3")][0]["readings"][0]
    cell_reading = fixtures.messages_by_group[("megapack_cell", "1.42.3")][0]["readings"][0]
    assert "pack_soc_pct" in pack_reading
    assert "cell_id" not in pack_reading
    assert "cell_id" in cell_reading
    assert "pack_soc_pct" not in cell_reading


def test_cell_reading_is_keyed_by_device_id_cell_id_device_ts():
    """The cell child table's intended key, per P2-13's scope (see catalog/signals.yaml's
    FLAGGED GAP note): at a fixed device_ts_ms, distinct cell_id values should appear rather
    than one cell_id repeating - i.e. this really is a (device_id, cell_id, device_ts_ms)-grain
    stream, not (device_id, device_ts_ms) with one arbitrary cell picked."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=1))
    readings_by_ts: dict[int, set[str]] = {}
    for message in fixtures.messages_by_group[("megapack_cell", "1.42.3")]:
        for reading in message["readings"]:
            ts = reading["device_ts_ms"]
            if ts is None:
                continue
            readings_by_ts.setdefault(ts, set()).add(reading["cell_id"])

    multi_cell_timestamps = [ts for ts, cells in readings_by_ts.items() if len(cells) > 1]
    assert multi_cell_timestamps, "expected at least one device_ts_ms with multiple cell_ids"


def test_cell_cadence_is_coarser_than_pack_cadence():
    """Real BMS cell-detail reporting is modeled as much less frequent than pack-summary
    reporting (see module docstring's cell-reporting-cadence rationale) - checked here via
    distinct device_ts_ms counts per device rather than pinning the exact ratio, so the test
    survives a future tuning of CELL_REPORTING_RATIO without being rewritten."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=1))
    for product in ("megapack", "powerpack"):
        spec = PRODUCT_SPECS[product]
        firmware = DEFAULT_FIRMWARE[product][0]
        pack_ts = {
            r["device_ts_ms"]
            for m in fixtures.messages_by_group[(spec.device_class, firmware)]
            for r in m["readings"]
            if r["device_ts_ms"] is not None
        }
        cell_ts = {
            r["device_ts_ms"]
            for m in fixtures.messages_by_group[(spec.cell_device_class, firmware)]
            for r in m["readings"]
            if r["device_ts_ms"] is not None
        }
        assert len(cell_ts) < len(pack_ts), (
            f"{product}: expected fewer distinct cell-sweep timestamps than pack-reading "
            f"timestamps (cell={len(cell_ts)}, pack={len(pack_ts)})"
        )


def test_megapack_has_more_cell_channels_than_powerpack():
    """Explicit check on the "keep cell count per device plausible for Megapack vs. Powerpack"
    requirement - the two products must not share one reused number."""
    assert PRODUCT_SPECS["megapack"].cell_count > PRODUCT_SPECS["powerpack"].cell_count

    fixtures = generate(GeneratorConfig(devices_per_firmware=1))
    megapack_cells = {
        r["cell_id"]
        for m in fixtures.messages_by_group[("megapack_cell", "1.42.3")]
        for r in m["readings"]
    }
    powerpack_cells = {
        r["cell_id"]
        for m in fixtures.messages_by_group[("powerpack_cell", "1.19.2")]
        for r in m["readings"]
    }
    assert len(megapack_cells) == PRODUCT_SPECS["megapack"].cell_count
    assert len(powerpack_cells) == PRODUCT_SPECS["powerpack"].cell_count


def test_megapack_and_powerpack_have_distinct_pack_fields_and_ranges():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    megapack_reading = fixtures.messages_by_group[("megapack", "1.42.3")][0]["readings"][0]
    powerpack_reading = fixtures.messages_by_group[("powerpack", "1.19.2")][0]["readings"][0]

    # Same field *names* (firmware/product-identical schema, same simplification as
    # supercharger.py - see catalog/signals.yaml's note), but scaled to different products.
    assert set(megapack_reading) == set(powerpack_reading)
    assert PRODUCT_SPECS["megapack"].pack_power_kw_max > PRODUCT_SPECS["powerpack"].pack_power_kw_max


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


def test_curtailment_is_injected():
    """The one messiness category this generator adds beyond supercharger.py's set - a grid-
    interconnection behavior (curtailment) Supercharger telemetry has no analog for."""
    fixtures = generate(_dense_config())
    found = any(
        reading.get("inverter_status") == "curtailed"
        for message in fixtures.messages_by_group[("megapack", "1.42.3")]
        for reading in message["readings"]
    )
    assert found, "expected at least one curtailed reading in a dense megapack run"


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


def test_generated_values_respect_catalog_valid_range():
    """Every field this generator emits must (a) have a catalog/signals.yaml entry for its
    (device_class, firmware_version) and (b) fall within that entry's declared valid_range,
    where one is declared - i.e. the catalog is an honest description of this generator's own
    output, not aspirational."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    catalog = load_catalog()

    checked_numeric_fields = 0
    for (device_class, firmware), messages in fixtures.messages_by_group.items():
        fw_catalog = catalog[(device_class, firmware)]
        for message in messages:
            for reading in message["readings"]:
                for field, value in reading.items():
                    entry = fw_catalog.get(field)
                    assert entry is not None, (
                        f"generator emits {field!r} for ({device_class}, {firmware}) with no "
                        "catalog entry"
                    )
                    valid_range = entry.get("valid_range")
                    if not valid_range or value is None or isinstance(value, bool):
                        continue
                    if not isinstance(value, (int, float)):
                        continue
                    lo, hi = valid_range
                    if lo is not None:
                        assert value >= lo, f"{device_class}/{firmware} {field}={value} < {lo}"
                    if hi is not None:
                        assert value <= hi, f"{device_class}/{firmware} {field}={value} > {hi}"
                    checked_numeric_fields += 1

    assert checked_numeric_fields > 0


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


def test_cell_device_class_mapping_matches_product_specs():
    for product, spec in PRODUCT_SPECS.items():
        assert CELL_DEVICE_CLASS[product] == spec.cell_device_class
