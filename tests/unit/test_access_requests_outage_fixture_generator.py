"""Tests for the synthetic outage fixture generator (P0-11 synthetic substitute). See rma's
test module docstring for why these tests exist and what they can and can't validate (there is
no real outage/grid-ops schema to compare against yet).
"""
from __future__ import annotations

import json

from tests.fixtures.generators.access_requests.outage import (
    CAUSE_CODES,
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


def test_schema_and_ongoing_nullability():
    generated = generate(GeneratorConfig(num_records=200))
    required_keys = {
        "outage_id",
        "site_id",
        "start_ts",
        "end_ts",
        "cause_code",
        "affected_device_count",
    }
    saw_ongoing = False
    saw_restored = False
    for record in generated.records:
        assert required_keys == set(record.keys())
        assert record["cause_code"] in CAUSE_CODES
        assert record["affected_device_count"] >= 1
        assert record["site_id"].startswith("site-")
        if record["end_ts"] is None:
            saw_ongoing = True
        else:
            assert record["end_ts"] > record["start_ts"]
            saw_restored = True
    assert saw_ongoing, "expected at least one ongoing (unrestored) outage in a 200-record run"
    assert saw_restored, "expected at least one restored outage in a 200-record run"


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(num_records=25))
    out_path = write(generated, tmp_path / "outage.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)
