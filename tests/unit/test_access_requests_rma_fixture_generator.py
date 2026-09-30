"""Tests for the synthetic RMA fixture generator (P0-11 synthetic substitute).

These test the *generator*, not a real RMA schema - there is no real schema to compare
against (see the module's own ASSUMED SCHEMA docstring). What matters here: deterministic
output, the assumed schema shape and nullability rules, and that device_id/site_id values
overlap with tests/fixtures/generators/supercharger.py's own scheme so cross-source joins are
possible.
"""
from __future__ import annotations

import json

from tests.fixtures.generators.access_requests._common import iter_supercharger_devices
from tests.fixtures.generators.access_requests.rma import (
    RESOLUTIONS,
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


def test_schema_and_nullability():
    generated = generate(GeneratorConfig(num_records=200))
    required_keys = {
        "rma_id",
        "device_id",
        "site_id",
        "opened_ts",
        "closed_ts",
        "reason_code",
        "resolution",
    }
    seen_open = False
    seen_closed = False
    for record in generated.records:
        assert required_keys == set(record.keys())
        assert isinstance(record["opened_ts"], int)
        assert record["resolution"] in RESOLUTIONS or record["resolution"] is None
        if record["closed_ts"] is None:
            # Still open: no resolution yet, since a case isn't resolved until it's closed.
            assert record["resolution"] is None
            seen_open = True
        else:
            assert isinstance(record["closed_ts"], int)
            assert record["closed_ts"] > record["opened_ts"]
            assert record["resolution"] is not None
            seen_closed = True
    assert seen_open, "expected at least one still-open RMA case in a 200-record run"
    assert seen_closed, "expected at least one closed RMA case in a 200-record run"


def test_device_and_site_ids_overlap_with_supercharger_scheme():
    generated = generate(GeneratorConfig(num_records=100, devices_per_firmware=4))
    valid_devices = {d.device_id for d in iter_supercharger_devices(4)}
    valid_sites = {d.site_id for d in iter_supercharger_devices(4)}
    for record in generated.records:
        assert record["device_id"] in valid_devices
        assert record["site_id"] in valid_sites


def test_records_sorted_by_opened_ts():
    generated = generate(GeneratorConfig(num_records=100))
    opened = [r["opened_ts"] for r in generated.records]
    assert opened == sorted(opened)


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(num_records=30))
    out_path = write(generated, tmp_path / "rma.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)  # true JSONL, one object per line
