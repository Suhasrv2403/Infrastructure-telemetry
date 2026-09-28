"""Tests for the Stage 0 landing Dagster asset (P0-04: orchestrator + partition model).

Mocks S3 with moto, same as test_stage0_capture.py - these exercise the orchestration layer
(partition windowing, resource wiring, backfill-style sequential materialization), not the
landing logic itself (that's capture.py's own test suite).
"""
from __future__ import annotations

import boto3
import pytest
from dagster import DagsterInstance, materialize
from moto import mock_aws

from pipeline.stage0_landing.capture import arrival_key
from pipeline.stage0_landing.dagster_assets import (
    LandingBucketResource,
    SyntheticSuperchargerSource,
    stage0_landing,
    stage0_landing_partitions,
)

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"

# Small enough to run fast; devices_per_firmware=1 still exercises every device_class/firmware
# group, just with fewer devices per group.
TEST_SOURCE = SyntheticSuperchargerSource(seed=1337, devices_per_firmware=1)


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _resources():
    return {
        "landing_bucket": LandingBucketResource(bucket=BUCKET, endpoint_url=None),
        "supercharger_source": TEST_SOURCE,
    }


def _materialize(partition_key: str, s3_client, instance: DagsterInstance | None = None):
    return materialize(
        [stage0_landing],
        partition_key=partition_key,
        resources=_resources(),
        instance=instance,
    )


def _first_partition_key_with_messages(s3_client) -> str:
    """Find one real partition key that actually has messages, rather than hardcoding a key
    and hoping the generator still puts something there."""
    for key in stage0_landing_partitions.get_partition_keys():
        window = stage0_landing_partitions.time_window_for_partition_key(key)
        if TEST_SOURCE.messages_in_window(window.start, window.end):
            return key
    raise AssertionError("no partition in the defined range has any messages - check the "
                          "STAGE0_PARTITIONS_START/END bounds against the generator's range")


def test_single_partition_materializes_and_reconciles(s3_client):
    partition_key = _first_partition_key_with_messages(s3_client)

    result = _materialize(partition_key, s3_client)

    assert result.success
    event = result.asset_materializations_for_node("stage0_landing")[0]
    metadata = event.metadata
    messages_seen = metadata["messages_seen"].value
    objects_written = metadata["objects_written"].value
    objects_already_present = metadata["objects_already_present"].value
    assert messages_seen > 0
    assert objects_written + objects_already_present == messages_seen


def test_partition_only_captures_messages_in_its_own_hour(s3_client):
    partition_key = _first_partition_key_with_messages(s3_client)
    window = stage0_landing_partitions.time_window_for_partition_key(partition_key)
    expected_messages = TEST_SOURCE.messages_in_window(window.start, window.end)

    _materialize(partition_key, s3_client)

    listed = s3_client.list_objects_v2(Bucket=BUCKET)
    listed_keys = {obj["Key"] for obj in listed.get("Contents", [])}
    assert listed_keys == {arrival_key(m) for m in expected_messages}


def test_rematerializing_the_same_partition_is_idempotent(s3_client):
    partition_key = _first_partition_key_with_messages(s3_client)

    first = _materialize(partition_key, s3_client)
    assert first.success
    first_metadata = first.asset_materializations_for_node("stage0_landing")[0].metadata
    first_written = first_metadata["objects_written"].value

    second = _materialize(partition_key, s3_client)
    assert second.success
    second_metadata = second.asset_materializations_for_node("stage0_landing")[0].metadata
    assert second_metadata["objects_written"].value == 0
    assert second_metadata["objects_already_present"].value == first_written


def test_empty_partition_materializes_successfully_with_zero_messages(s3_client):
    # Not every hour in the defined range has traffic - an empty partition must still
    # materialize cleanly (0 seen, 0 landed, trivially reconciled), not fail.
    empty_key = None
    for key in stage0_landing_partitions.get_partition_keys():
        window = stage0_landing_partitions.time_window_for_partition_key(key)
        if not TEST_SOURCE.messages_in_window(window.start, window.end):
            empty_key = key
            break
    assert empty_key is not None, "expected at least one empty hour in a 528-hour range"

    result = _materialize(empty_key, s3_client)

    assert result.success
    metadata = result.asset_materializations_for_node("stage0_landing")[0].metadata
    assert metadata["messages_seen"].value == 0
    assert metadata["objects_written"].value == 0


def test_backfill_across_several_partitions_reconciles_with_no_overlap_or_loss(s3_client):
    """The actual P0-04 "done when" check: run a small backfill (several partitions
    materialized in sequence, as Dagster's backfill machinery does under the hood for each
    partition run) and confirm every message across the whole span lands exactly once - no
    partition drops messages, and no two partitions double-land the same message."""
    all_keys = stage0_landing_partitions.get_partition_keys()
    # First 72 hours of the defined range (3 days) - enough to span multiple days and several
    # non-empty hours without materializing all 528 partitions in a test.
    backfill_keys = all_keys[:72]

    instance = DagsterInstance.ephemeral()
    total_seen = 0
    for key in backfill_keys:
        result = _materialize(key, s3_client, instance=instance)
        assert result.success, f"partition {key} failed to materialize"
        metadata = result.asset_materializations_for_node("stage0_landing")[0].metadata
        total_seen += metadata["messages_seen"].value

    window_start = stage0_landing_partitions.time_window_for_partition_key(backfill_keys[0]).start
    window_end = stage0_landing_partitions.time_window_for_partition_key(backfill_keys[-1]).end
    expected_messages = TEST_SOURCE.messages_in_window(window_start, window_end)

    assert total_seen == len(expected_messages)

    listed = s3_client.list_objects_v2(Bucket=BUCKET)
    listed_keys = {obj["Key"] for obj in listed.get("Contents", [])}
    assert listed_keys == {arrival_key(m) for m in expected_messages}
