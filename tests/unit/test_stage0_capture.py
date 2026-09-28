"""Tests for Stage 0 capture (P0-05: Stage 0 capture for one Supercharger region).

Uses moto to mock S3 rather than requiring LocalStack/Floci to be running - these are unit
tests of the capture logic itself (key layout, immutability, reconciliation), not integration
tests of the real bucket infrastructure (that's covered by the object_store module's own
terraform tests).
"""
from __future__ import annotations

import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from pipeline.stage0_landing.capture import (
    CaptureResult,
    ReconciliationError,
    UnsupportedDeviceClassError,
    arrival_key,
    capture_messages,
    reconcile,
)
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"


def _envelope(**overrides) -> dict:
    base = {
        "message_id": "msg-stall-2140001-1780358400000-1",
        "device_id": "stall-2140001-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_000,  # 2026-06-01T00:00:00Z
        "readings": [{"device_ts_ms": 1_780_358_400_000, "payload_hash": "sha1:deadbeef"}],
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


def test_arrival_key_partitions_by_arrival_hour_not_device_ts():
    # device_ts is deliberately absent from the message-level fields used for keying; only
    # arrival_ts_ms (envelope-level, ingest-side receipt time) may drive the partition.
    message = _envelope(arrival_ts_ms=1_780_358_400_000 + 3 * 3600 * 1000)  # +3h
    key = arrival_key(message)
    expected_arrival = dt.datetime.fromtimestamp(
        message["arrival_ts_ms"] / 1000, tz=dt.timezone.utc
    )
    assert key == f"raw/arrival_date={expected_arrival:%Y-%m-%d}/hour={expected_arrival:%H}/{message['message_id']}.json"


def test_arrival_key_is_unique_per_message_id_including_retries():
    original = _envelope(message_id="msg-abc-1")
    retry = _envelope(message_id="msg-abc-1-retry", arrival_ts_ms=original["arrival_ts_ms"] + 5000)
    assert arrival_key(original) != arrival_key(retry)


def test_capture_writes_one_object_per_message(s3_client):
    messages = [_envelope(message_id=f"msg-{i}") for i in range(5)]

    result = capture_messages(messages, bucket=BUCKET, s3_client=s3_client)

    assert result.messages_seen == 5
    assert result.objects_written == 5
    assert result.objects_already_present == 0
    assert result.reconciles()

    listed = s3_client.list_objects_v2(Bucket=BUCKET)
    assert listed["KeyCount"] == 5


def test_capture_preserves_message_content(s3_client):
    message = _envelope(message_id="msg-content-check")

    capture_messages([message], bucket=BUCKET, s3_client=s3_client)

    key = arrival_key(message)
    body = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert json.loads(body) == message


def test_capture_is_idempotent_never_overwrites_landed_object(s3_client):
    message = _envelope(message_id="msg-idempotent")
    key = arrival_key(message)

    first = capture_messages([message], bucket=BUCKET, s3_client=s3_client)
    assert first.objects_written == 1
    assert first.objects_already_present == 0

    # A real re-run (e.g. retrying after a partial-batch failure) must never mutate an object
    # that already landed - simulate that by hand-planting different bytes at the same key and
    # confirming a second capture pass leaves them untouched.
    sentinel = b'{"tampered": true}'
    s3_client.put_object(Bucket=BUCKET, Key=key, Body=sentinel)

    second = capture_messages([message], bucket=BUCKET, s3_client=s3_client)
    assert second.objects_written == 0
    assert second.objects_already_present == 1
    assert second.reconciles()

    body = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert body == sentinel, "capture overwrote an object that had already landed"


def test_capture_rejects_unsupported_device_class(s3_client):
    bad_message = _envelope(device_class="powerwall")

    with pytest.raises(UnsupportedDeviceClassError):
        capture_messages([bad_message], bucket=BUCKET, s3_client=s3_client)


def test_reconcile_passes_when_counts_match():
    result = CaptureResult(
        messages_seen=3, objects_written=2, objects_already_present=1, keys_written=("a", "b")
    )
    reconcile(result)  # should not raise


def test_reconcile_fails_when_a_message_did_not_land():
    result = CaptureResult(
        messages_seen=3, objects_written=2, objects_already_present=0, keys_written=("a", "b")
    )
    with pytest.raises(ReconciliationError):
        reconcile(result)


def test_end_to_end_against_generated_supercharger_fixtures_reconciles(s3_client):
    """The actual P0-05 "done when" check: run a full synthetic capture and confirm counts
    reconcile with source, exactly like the CLI entrypoint does against a real bucket."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())
    assert messages, "fixture generator produced no messages to capture"

    result = capture_messages(messages, bucket=BUCKET, s3_client=s3_client)

    assert result.messages_seen == len(messages)
    reconcile(result)  # raises on any discrepancy

    listed_keys = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        listed_keys.update(obj["Key"] for obj in page.get("Contents", []))
    assert listed_keys == {arrival_key(m) for m in messages}

    # Every key must fall under the documented Stage 0 layout and partition by arrival, not
    # device or event time.
    for message in messages:
        key = arrival_key(message)
        arrival = dt.datetime.fromtimestamp(message["arrival_ts_ms"] / 1000, tz=dt.timezone.utc)
        assert key.startswith(f"raw/arrival_date={arrival:%Y-%m-%d}/hour={arrival:%H}/")
