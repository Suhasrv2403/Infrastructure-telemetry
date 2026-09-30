"""Tests for the Stage 1 idempotent merge (P1-06: "Idempotent merge on natural key into
event-time partitions").

Covers the in-process merge store in pipeline/stage1_parsed/merge.py - see that module's
docstring for why this is a stand-in for a real Iceberg MERGE INTO rather than the real thing.
The literal "done when" for this ticket ("replaying a message N times yields exactly one row")
is test_replaying_the_same_rows_n_times_yields_exactly_one_row_each below.
"""
from __future__ import annotations

import copy

import pytest

from parsers.framework import parse_messages

# Importing the demo parser registers supercharger_stall/2.1.4 with the framework's global
# registry, the same pattern tests/unit/test_parser_framework.py uses. It's also called
# directly below to build a hand-built row from a single message.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4
from pipeline.stage1_parsed.merge import (
    DEVICE_BUCKET_COUNT,
    InvalidEventTimestampError,
    Stage1MergeStore,
    device_bucket,
    natural_key,
    partition_key,
)
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


def _rows_from_generator() -> list[dict]:
    """A modest batch of real parsed rows, via the synthetic generator + parser framework -
    the same route production data would take into merge()."""
    config = GeneratorConfig(seed=1337, devices_per_firmware=2)
    fixtures = generate(config)
    messages = [
        m
        for m in fixtures.all_messages()
        if m["device_class"] == "supercharger_stall" and m["firmware_version"] == "2.1.4"
    ]
    assert messages, "expected at least one supercharger_stall/2.1.4 message from the generator"
    result = parse_messages(messages)
    assert result.rows_parsed > 0
    return list(result.rows)


# ---------------------------------------------------------------------------
# The literal "done when": replaying N times yields exactly one row per key.
# ---------------------------------------------------------------------------


def test_replaying_the_same_rows_n_times_yields_exactly_one_row_each():
    rows = _rows_from_generator()
    unique_keys = {natural_key(r) for r in rows}

    store = Stage1MergeStore()

    first = store.merge(rows)
    assert first.rows_seen == len(rows)
    assert first.rows_inserted == len(unique_keys)
    assert first.rows_already_present == len(rows) - len(unique_keys)
    assert first.reconciles()
    assert len(store) == len(unique_keys)

    # Replay the exact same rows N times (N=3 here, i.e. 3 additional merges after the first).
    for replay in range(3):
        result = store.merge(copy.deepcopy(rows))
        assert result.rows_seen == len(rows)
        assert result.rows_inserted == 0, f"replay {replay} inserted new rows, not idempotent"
        assert result.rows_already_present == len(rows)
        assert result.reconciles()
        # The store itself never grows past one row per unique natural key, no matter how
        # many times the same rows are merged in.
        assert len(store) == len(unique_keys)

    assert len(store) == len(unique_keys)
    assert {natural_key(r) for r in store.rows()} == unique_keys


def test_replaying_a_single_hand_built_row_n_times_is_a_no_op_after_the_first():
    message = _sample_stall_message()
    row = parse_supercharger_stall_2_1_4(message)[0]

    store = Stage1MergeStore()
    first = store.merge([row])
    assert first.rows_inserted == 1
    assert first.rows_already_present == 0
    assert len(store) == 1

    for _ in range(5):
        replay = store.merge([copy.deepcopy(row)])
        assert replay.rows_inserted == 0
        assert replay.rows_already_present == 1
        assert len(store) == 1


# ---------------------------------------------------------------------------
# Partial overlap: batch B shares some rows with batch A and adds new ones.
# ---------------------------------------------------------------------------


def test_partial_overlap_between_batches_converges_to_union_of_unique_keys():
    rows = _rows_from_generator()
    assert len(rows) >= 4, "need enough rows to split into an overlapping A/B"

    midpoint = len(rows) // 2
    overlap_start = max(0, midpoint - len(rows) // 4)
    batch_a = rows[:midpoint]
    batch_b = rows[overlap_start:]  # shares [overlap_start:midpoint] with batch_a

    unique_keys_a = {natural_key(r) for r in batch_a}
    unique_keys_b = {natural_key(r) for r in batch_b}
    union_keys = unique_keys_a | unique_keys_b
    assert len(union_keys) < len(unique_keys_a) + len(unique_keys_b), (
        "test setup bug: batch_a and batch_b must actually overlap on some natural keys"
    )

    store = Stage1MergeStore()
    store.merge(batch_a)
    assert len(store) == len(unique_keys_a)

    result_b = store.merge(batch_b)
    assert len(store) == len(union_keys)
    assert len(store) != len(batch_a) + len(batch_b)
    # Every row in the overlap was already present; every genuinely new key was inserted.
    assert result_b.rows_inserted == len(unique_keys_b - unique_keys_a)
    assert result_b.rows_already_present == len(batch_b) - result_b.rows_inserted


# ---------------------------------------------------------------------------
# Partition key.
# ---------------------------------------------------------------------------


def _row(device_id="stall-2140001-0000", device_class="supercharger_stall", device_ts_ms=1_780_358_400_000):
    return {"device_id": device_id, "device_class": device_class, "device_ts_ms": device_ts_ms}


def test_partition_key_derives_hour_from_device_ts_ms_event_time():
    row_hour_0 = _row(device_ts_ms=1_780_358_400_000)  # 2026-06-01T00:00:00Z
    row_hour_1 = _row(device_ts_ms=1_780_358_400_000 + 3600 * 1000)  # one hour later

    key_0 = partition_key(row_hour_0)
    key_1 = partition_key(row_hour_1)

    assert "hour=00" in key_0
    assert "hour=01" in key_1
    assert key_0 != key_1


def test_partition_key_uses_device_ts_ms_not_arrival_ts_ms():
    """Invariant 4: Stage 1+ partitions by event time (device_ts_ms), never arrival time.

    A row carrying a wildly different arrival_ts_ms must not affect the partition at all -
    only device_ts_ms may.
    """
    row = _row(device_ts_ms=1_780_358_400_000)
    row_with_unrelated_arrival = dict(row, arrival_ts_ms=1_780_358_400_000 + 999 * 3600 * 1000)

    assert partition_key(row) == partition_key(row_with_unrelated_arrival)


def test_partition_key_bucket_is_not_device_id_alone():
    """Invariant 4: never partition by device_id (alone, or unhashed). The partition key must
    include the device_class/event-time components alongside the hashed bucket - a raw,
    unhashed device_id must never appear in the partition path."""
    row = _row(device_id="stall-2140001-0000")
    key = partition_key(row)

    assert "device_id=" not in key
    assert row["device_id"] not in key
    assert "device_bucket=" in key
    assert "device_class=" in key
    assert "event_date=" in key
    assert "hour=" in key


def test_device_bucket_is_deterministic_and_stable_across_calls():
    device_id = "stall-2140001-0007"
    buckets = {device_bucket(device_id) for _ in range(10)}
    assert buckets == {device_bucket(device_id)}  # always the same single value
    assert 0 <= device_bucket(device_id) < DEVICE_BUCKET_COUNT


def test_different_device_ids_may_or_may_not_share_a_bucket():
    """Hashing isn't perfect: two different device_ids in the same event hour may land in the
    same bucket or different ones. What matters is each one is internally consistent - this
    test just documents that no uniqueness guarantee is claimed across different device_ids."""
    device_ids = [f"stall-2140001-{i:04d}" for i in range(DEVICE_BUCKET_COUNT * 4)]
    buckets = {d: device_bucket(d) for d in device_ids}

    # Every device_id maps to a valid bucket, consistently, regardless of what other
    # device_ids map to.
    for device_id, bucket in buckets.items():
        assert 0 <= bucket < DEVICE_BUCKET_COUNT
        assert device_bucket(device_id) == bucket

    # With enough distinct device_ids, at least two of them are expected to collide into the
    # same bucket - not asserted as a strict requirement of the hash, just noted as expected.
    assert len(set(buckets.values())) <= len(device_ids)


def test_partition_key_rejects_missing_or_invalid_device_ts_ms():
    """Timestamp sanity is P1-05's job, not merge.py's (see module docstring) - but a row that
    reaches partition_key() without a usable device_ts_ms must fail loudly, not silently land
    in a nonsense epoch partition."""
    for bad_ts in (None, "not-a-timestamp", 1_780_358_400.5, True):
        with pytest.raises(InvalidEventTimestampError):
            partition_key(_row(device_ts_ms=bad_ts))


def test_natural_key_uses_device_id_device_ts_ms_and_payload_hash():
    row = _row()
    row["payload_hash"] = "sha1:abc123"
    assert natural_key(row) == (row["device_id"], row["device_ts_ms"], row["payload_hash"])
