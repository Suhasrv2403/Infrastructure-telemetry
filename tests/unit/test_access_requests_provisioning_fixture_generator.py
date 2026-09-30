"""Tests for the synthetic provisioning-history fixture generator (P0-11 synthetic
substitute).

Unlike the other four P0-11 generators, provisioning has a real correctness property worth
testing beyond "looks like plausible JSON": it must be a *history* table that supports an
as-of join, because that's specifically what P2-01 needs and what a current-state snapshot
cannot provide (see the module's own docstring). These tests check that property directly:
for a given device and timestamp, at most one row is ever "current", and the row
`current_as_of()` returns is genuinely the most recent one at or before that timestamp - not
just "a" row that happens to match.
"""
from __future__ import annotations

import json

from tests.fixtures.generators.access_requests._common import iter_supercharger_devices
from tests.fixtures.generators.access_requests.provisioning import (
    GeneratorConfig,
    current_as_of,
    generate,
    write,
)


def test_generate_is_deterministic():
    a = generate(GeneratorConfig(seed=42))
    b = generate(GeneratorConfig(seed=42))
    assert json.dumps(a.records, sort_keys=True) == json.dumps(b.records, sort_keys=True)


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1))
    b = generate(GeneratorConfig(seed=2))
    assert json.dumps(a.records, sort_keys=True) != json.dumps(b.records, sort_keys=True)


def test_schema_shape():
    generated = generate(GeneratorConfig(devices_per_firmware=3))
    required_keys = {
        "device_id",
        "effective_ts",
        "firmware_version",
        "hardware_rev",
        "cell_lot",
        "site_id",
    }
    for record in generated.records:
        assert required_keys == set(record.keys())
        assert isinstance(record["effective_ts"], int)


def test_every_device_has_at_least_one_row_and_no_duplicate_timestamps():
    generated = generate(GeneratorConfig(devices_per_firmware=3))
    devices = iter_supercharger_devices(3)
    by_device: dict[str, list[int]] = {}
    for record in generated.records:
        by_device.setdefault(record["device_id"], []).append(record["effective_ts"])

    for device in devices:
        timestamps = by_device.get(device.device_id)
        assert timestamps, f"{device.device_id} has no provisioning history at all"
        # No two rows for the same device share an effective_ts - otherwise "current as of T"
        # would be ambiguous.
        assert len(timestamps) == len(set(timestamps))
        # A history table must actually be ordered as a history: strictly increasing.
        assert timestamps == sorted(timestamps)


def test_as_of_join_returns_exactly_one_current_row_per_device():
    """The property P2-01 actually depends on: for any device and any timestamp at or after
    that device's earliest record, exactly one provisioning row is "current" - the row with
    the largest effective_ts <= the query timestamp."""
    generated = generate(GeneratorConfig(devices_per_firmware=4, seed=7))
    by_device: dict[str, list[dict]] = {}
    for record in generated.records:
        by_device.setdefault(record["device_id"], []).append(record)

    for device_id, rows in by_device.items():
        rows_sorted = sorted(rows, key=lambda r: r["effective_ts"])
        # Query at each row's own effective_ts, and again just before the *next* row's
        # effective_ts (or a long time after the last row) - the as-of result must be exactly
        # that row both times, and must change the instant the next row becomes effective.
        for i, row in enumerate(rows_sorted):
            current = current_as_of(generated.records, device_id, row["effective_ts"])
            assert current == row

            if i + 1 < len(rows_sorted):
                next_ts = rows_sorted[i + 1]["effective_ts"]
                just_before_next = next_ts - 1
                current_just_before = current_as_of(
                    generated.records, device_id, just_before_next
                )
                assert current_just_before == row
            else:
                far_future = row["effective_ts"] + 10_000 * 24 * 3600 * 1000
                current_far_future = current_as_of(generated.records, device_id, far_future)
                assert current_far_future == row

        # Before the device's first record, there is no current row - a snapshot can't be
        # backdated to before the device existed.
        earliest_ts = rows_sorted[0]["effective_ts"]
        assert current_as_of(generated.records, device_id, earliest_ts - 1) is None


def test_as_of_result_fields_are_internally_consistent_with_that_row():
    """Sanity check that current_as_of() isn't just returning *a* matching row by accident -
    every field on the returned row belongs together (same device, timestamp within bounds)."""
    generated = generate(GeneratorConfig(devices_per_firmware=2, seed=99))
    device_id = generated.records[0]["device_id"]
    as_of_ts = generated.records[0]["effective_ts"] + 1
    current = current_as_of(generated.records, device_id, as_of_ts)
    assert current is not None
    assert current["device_id"] == device_id
    assert current["effective_ts"] <= as_of_ts


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(devices_per_firmware=2))
    out_path = write(generated, tmp_path / "provisioning.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)
