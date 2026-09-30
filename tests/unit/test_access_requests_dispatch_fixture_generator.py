"""Tests for the synthetic dispatch fixture generator (P0-11 synthetic substitute). See rma's
test module docstring for why these tests exist and what they can and can't validate (there is
no real dispatch schema to compare against yet).
"""
from __future__ import annotations

import json

from tests.fixtures.generators.access_requests._common import iter_supercharger_devices
from tests.fixtures.generators.access_requests.dispatch import (
    OUTCOMES,
    GeneratorConfig,
    generate,
    write,
)


def test_generate_is_deterministic():
    a = generate(GeneratorConfig(seed=42, num_records=50))
    b = generate(GeneratorConfig(seed=42, num_records=50))
    assert json.dumps(a.records, sort_keys=True) == json.dumps(b.records, sort_keys=True)


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1, num_records=50))
    b = generate(GeneratorConfig(seed=2, num_records=50))
    assert json.dumps(a.records, sort_keys=True) != json.dumps(b.records, sort_keys=True)


def test_schema_and_completion_nullability():
    generated = generate(GeneratorConfig(num_records=200))
    required_keys = {
        "dispatch_id",
        "device_id",
        "site_id",
        "scheduled_ts",
        "completed_ts",
        "technician_outcome",
        "ticket_id",
    }
    saw_incomplete = False
    saw_complete = False
    saw_ticket_link = False
    for record in generated.records:
        assert required_keys == set(record.keys())
        if record["completed_ts"] is None:
            # No outcome before a technician actually visits.
            assert record["technician_outcome"] is None
            saw_incomplete = True
        else:
            assert record["completed_ts"] >= record["scheduled_ts"]
            assert record["technician_outcome"] in OUTCOMES
            saw_complete = True
        if record["ticket_id"] is not None:
            saw_ticket_link = True
    assert saw_incomplete, "expected at least one not-yet-completed dispatch"
    assert saw_complete, "expected at least one completed dispatch"
    assert saw_ticket_link, "expected at least one dispatch linked to a ticket_id"


def test_device_and_site_ids_overlap_with_supercharger_scheme():
    generated = generate(GeneratorConfig(num_records=100, devices_per_firmware=4))
    valid_devices = {d.device_id for d in iter_supercharger_devices(4)}
    valid_sites = {d.site_id for d in iter_supercharger_devices(4)}
    for record in generated.records:
        assert record["device_id"] in valid_devices
        assert record["site_id"] in valid_sites


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(num_records=25))
    out_path = write(generated, tmp_path / "dispatch.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)
