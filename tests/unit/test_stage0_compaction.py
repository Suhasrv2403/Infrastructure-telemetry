"""Tests for Stage 0 compaction (P1-02: Stage 0 compaction and retention tiering).

Uses moto to mock S3, the same way tests/unit/test_stage0_capture.py does - these are unit
tests of the compaction logic (consolidation, invariant-1 safety, idempotency, reconciliation),
not integration tests of the real bucket infrastructure.
"""
from __future__ import annotations

import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from pipeline.stage0_landing.capture import arrival_key, capture_messages
from pipeline.stage0_landing.compaction import (
    CompactionReconciliationError,
    compact_partition,
    compacted_key,
    partition_prefix,
    reconcile,
)

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"

# 2026-06-02T00:00:00Z, in milliseconds - lands every fixture message in the ARRIVAL_DATE/HOUR
# partition below unless a test overrides arrival_ts_ms. Derive ARRIVAL_DATE/HOUR from this
# timestamp via arrival_key's own conversion (dt.datetime.fromtimestamp(..., tz=utc)) rather
# than hardcoding a date string by hand, so the two can never silently drift apart.
ARRIVAL_TS_MS = 1_780_358_400_000
_ARRIVAL_DT = dt.datetime.fromtimestamp(ARRIVAL_TS_MS / 1000, tz=dt.timezone.utc)
ARRIVAL_DATE = f"{_ARRIVAL_DT:%Y-%m-%d}"
HOUR = f"{_ARRIVAL_DT:%H}"


def _envelope(**overrides) -> dict:
    base = {
        "message_id": "msg-stall-2140001-1780358400000-1",
        "device_id": "stall-2140001-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": ARRIVAL_TS_MS,
        "arrival_ts_ms": ARRIVAL_TS_MS,
        "readings": [{"device_ts_ms": ARRIVAL_TS_MS, "payload_hash": "sha1:deadbeef"}],
        "_debug_injected_issues": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _land(client, messages):
    result = capture_messages(messages, bucket=BUCKET, s3_client=client)
    assert result.reconciles()
    return result


def test_compaction_of_empty_partition_is_a_documented_no_op(s3_client):
    result = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)

    assert result.messages_seen == 0
    assert result.small_object_keys == ()
    assert result.consolidated_written is False
    assert result.bytes_before == 0
    assert result.bytes_after == 0
    assert result.messages_in_consolidated == 0
    assert result.reconciles()
    reconcile(result)  # should not raise

    # No consolidated object should exist in the bucket for a partition that had nothing to
    # compact - "no consolidated object" and "no traffic" must coincide.
    listed = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="compacted/")
    assert listed.get("KeyCount", 0) == 0


def test_compaction_consolidates_every_message_exactly_once(s3_client):
    messages = [_envelope(message_id=f"msg-{i}") for i in range(6)]
    _land(s3_client, messages)

    result = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)

    assert result.messages_seen == 6
    assert result.consolidated_written is True
    assert result.messages_in_consolidated == 6
    assert result.reconciles()
    reconcile(result)  # should not raise

    body = s3_client.get_object(Bucket=BUCKET, Key=result.consolidated_key)["Body"].read()
    lines = body.decode("utf-8").splitlines()
    assert len(lines) == 6
    consolidated_ids = {json.loads(line)["message_id"] for line in lines}
    assert consolidated_ids == {f"msg-{i}" for i in range(6)}

    # No duplicates: every id appears on exactly one line.
    all_ids = [json.loads(line)["message_id"] for line in lines]
    assert len(all_ids) == len(set(all_ids))


def test_compaction_never_touches_the_small_originals(s3_client):
    messages = [_envelope(message_id=f"msg-orig-{i}") for i in range(4)]
    _land(s3_client, messages)

    originals_before = {}
    for message in messages:
        key = arrival_key(message)
        originals_before[key] = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)

    # Every small original must still exist, byte-identical, still readable under raw/.
    for key, body_before in originals_before.items():
        body_after = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert body_after == body_before, f"compaction mutated small original {key!r}"

    listed = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=partition_prefix(ARRIVAL_DATE, HOUR))
    assert listed["KeyCount"] == 4


def test_compaction_is_idempotent_on_repeated_runs(s3_client):
    messages = [_envelope(message_id=f"msg-idem-{i}") for i in range(5)]
    _land(s3_client, messages)

    first = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)
    first_body = s3_client.get_object(Bucket=BUCKET, Key=first.consolidated_key)["Body"].read()

    second = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)
    second_body = s3_client.get_object(Bucket=BUCKET, Key=second.consolidated_key)["Body"].read()

    assert first.consolidated_key == second.consolidated_key
    assert first_body == second_body, "re-running compaction must produce byte-identical output"
    assert second.reconciles()
    reconcile(second)  # should not raise

    # Running it a third time doesn't error either, and still reconciles.
    third = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)
    assert third.reconciles()


def test_compaction_uses_a_separate_prefix_from_raw_so_reruns_dont_double_count(s3_client):
    messages = [_envelope(message_id=f"msg-prefix-{i}") for i in range(3)]
    _land(s3_client, messages)

    compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)
    # A second compaction run must still see exactly the 3 small originals, not 3 + the
    # consolidated object from the first run - proving the consolidated object doesn't live
    # under the same prefix compact_partition lists as "small objects".
    second = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour=HOUR, s3_client=s3_client)

    assert second.messages_seen == 3
    assert second.small_object_keys == tuple(sorted(arrival_key(m) for m in messages))
    assert second.consolidated_key == compacted_key(ARRIVAL_DATE, HOUR)
    assert second.consolidated_key not in second.small_object_keys


def test_compaction_scoped_to_only_its_own_partition(s3_client):
    hour_00 = [_envelope(message_id="msg-h00", arrival_ts_ms=ARRIVAL_TS_MS)]
    hour_01 = [_envelope(message_id="msg-h01", arrival_ts_ms=ARRIVAL_TS_MS + 3600 * 1000)]
    _land(s3_client, hour_00 + hour_01)

    result = compact_partition(bucket=BUCKET, arrival_date=ARRIVAL_DATE, hour="00", s3_client=s3_client)

    assert result.messages_seen == 1
    body = s3_client.get_object(Bucket=BUCKET, Key=result.consolidated_key)["Body"].read()
    assert json.loads(body.decode("utf-8").strip())["message_id"] == "msg-h00"


def test_reconcile_raises_when_consolidated_content_is_incomplete():
    from pipeline.stage0_landing.compaction import CompactionResult

    bad_result = CompactionResult(
        arrival_date=ARRIVAL_DATE,
        hour=HOUR,
        small_object_keys=("raw/a.json", "raw/b.json"),
        consolidated_key="compacted/x.ndjson",
        consolidated_written=True,
        bytes_before=200,
        bytes_after=90,
        messages_in_consolidated=1,  # one message went missing
    )
    with pytest.raises(CompactionReconciliationError):
        reconcile(bad_result)


def test_end_to_end_against_generated_supercharger_fixtures_reconciles(s3_client):
    """Land a real synthetic corpus, compact one of its partitions, and confirm the
    consolidated object accounts for every message that landed in that hour, exactly once."""
    from tests.fixtures.generators.supercharger import GeneratorConfig, generate

    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    all_messages = list(fixtures.all_messages())
    assert all_messages, "fixture generator produced no messages to capture"

    capture_result = capture_messages(all_messages, bucket=BUCKET, s3_client=s3_client)
    assert capture_result.reconciles()

    # Pick one arrival partition that actually has messages in it.
    sample = all_messages[0]
    arrival = dt.datetime.fromtimestamp(sample["arrival_ts_ms"] / 1000, tz=dt.timezone.utc)
    arrival_date, hour = f"{arrival:%Y-%m-%d}", f"{arrival:%H}"
    expected_ids = {
        m["message_id"]
        for m in all_messages
        if dt.datetime.fromtimestamp(m["arrival_ts_ms"] / 1000, tz=dt.timezone.utc).strftime(
            "%Y-%m-%d/%H"
        )
        == f"{arrival_date}/{hour}"
    }

    result = compact_partition(bucket=BUCKET, arrival_date=arrival_date, hour=hour, s3_client=s3_client)
    reconcile(result)  # raises on any discrepancy

    assert result.messages_seen == len(expected_ids)
    body = s3_client.get_object(Bucket=BUCKET, Key=result.consolidated_key)["Body"].read()
    consolidated_ids = {json.loads(line)["message_id"] for line in body.decode("utf-8").splitlines()}
    assert consolidated_ids == expected_ids
