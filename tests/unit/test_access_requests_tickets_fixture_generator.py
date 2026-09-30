"""Tests for the synthetic support/service ticket fixture generator (P0-11 synthetic
substitute). See rma's test module docstring for why these tests exist and what they can and
can't validate (there is no real ticketing schema to compare against yet).
"""
from __future__ import annotations

import json

from tests.fixtures.generators.access_requests._common import iter_supercharger_devices
from tests.fixtures.generators.access_requests.tickets import (
    CATEGORIES,
    SEVERITIES,
    GeneratorConfig,
    generate,
    write,
)


def test_generate_is_deterministic():
    a = generate(GeneratorConfig(seed=42, num_records=60))
    b = generate(GeneratorConfig(seed=42, num_records=60))
    assert json.dumps(a.records, sort_keys=True) == json.dumps(b.records, sort_keys=True)


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1, num_records=60))
    b = generate(GeneratorConfig(seed=2, num_records=60))
    assert json.dumps(a.records, sort_keys=True) != json.dumps(b.records, sort_keys=True)


def test_schema_and_status_nullability():
    generated = generate(GeneratorConfig(num_records=300))
    required_keys = {
        "ticket_id",
        "device_id",
        "opened_ts",
        "closed_ts",
        "category",
        "severity",
        "status",
    }
    saw_null_device = False
    for record in generated.records:
        assert required_keys == set(record.keys())
        assert record["category"] in CATEGORIES
        assert record["severity"] in SEVERITIES
        assert record["status"] in {"open", "pending", "closed"}
        # status is "closed" iff closed_ts is set - the two must never disagree.
        if record["status"] == "closed":
            assert record["closed_ts"] is not None
            assert record["closed_ts"] >= record["opened_ts"]
        else:
            assert record["closed_ts"] is None
        if record["device_id"] is None:
            saw_null_device = True
    assert saw_null_device, "expected at least one ticket with no device_id in a 300-record run"


def test_device_id_when_present_overlaps_with_supercharger_scheme():
    generated = generate(GeneratorConfig(num_records=200, devices_per_firmware=4))
    valid_devices = {d.device_id for d in iter_supercharger_devices(4)}
    for record in generated.records:
        if record["device_id"] is not None:
            assert record["device_id"] in valid_devices


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(num_records=25))
    out_path = write(generated, tmp_path / "tickets.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)
