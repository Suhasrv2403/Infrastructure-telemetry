"""Tests for the production ingest buffer (P1-01: accept-and-spool).

Load-test note (read before assuming any number here claims "10x current production rate")
---------------------------------------------------------------------------------------------
ingest/service/buffer.py's module docstring explains why: there is no measured "current
Supercharger rate" on record (P0-01, the telemetry backend audit, is still "To do" and human-
owned) and no production-sized infrastructure to load-test against (P0-02, the Terraform
baseline, is also still "To do"). The scale used below - `devices_per_firmware=10`, 2.5x the
fixture generator's own illustrative default of 4, spread across the generator's 5 firmware x
device-class combinations (3 supercharger_stall firmwares + 2 supercharger_cabinet firmwares) =
50 distinct synthetic devices, which the generator's own batching/duplicate/late-arrival
injection turns into 6,557 message envelopes - is chosen only to be "comfortably above the
generator's default illustrative scale" and large enough to exercise real batching, spooling
and multi-round retry behavior, not to stand in for any real production number.

Uses moto to mock S3, exactly like tests/unit/test_stage0_capture.py, since IngestBuffer's
production sink (capture_sink()) is a thin wrapper around capture_messages().
"""
from __future__ import annotations

import random

import boto3
import pytest
from moto import mock_aws

from ingest.buffer.config import BufferConfig
from ingest.service.buffer import IngestBuffer, capture_sink
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"

# See module docstring: 5x the generator's illustrative default (4), NOT a stand-in for any
# measured production rate.
LOAD_TEST_DEVICES_PER_FIRMWARE = 10


def _envelope(**overrides) -> dict:
    base = {
        "message_id": "msg-stall-2140001-1780358400000-1",
        "device_id": "stall-2140001-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_000,
        "readings": [{"device_ts_ms": 1_780_358_400_000, "payload_hash": "sha1:deadbeef"}],
        "_debug_injected_issues": [],
    }
    base.update(overrides)
    return base


class FlakySink:
    """Wraps a real Sink, simulating a downstream outage / intermittent backpressure.

    Models the two failure shapes the ticket asks for: "raises for the first N calls" (a clean
    outage-then-recovery) via `fail_first_n_calls`, and "randomly" via `fail_rate`. Either way,
    a "failed" call never reaches the inner sink at all - no partial writes happen, matching a
    real downstream that is simply unreachable rather than one that half-processes a batch.
    """

    def __init__(
        self, inner, *, fail_first_n_calls: int = 0, fail_rate: float = 0.0, seed: int = 0
    ):
        self._inner = inner
        self._remaining_forced_failures = fail_first_n_calls
        self._fail_rate = fail_rate
        self._rng = random.Random(seed)
        self.calls = 0
        self.failures = 0

    def __call__(self, batch) -> None:
        self.calls += 1
        if self._remaining_forced_failures > 0:
            self._remaining_forced_failures -= 1
            self.failures += 1
            raise RuntimeError("simulated downstream outage")
        if self._rng.random() < self._fail_rate:
            self.failures += 1
            raise RuntimeError("simulated intermittent downstream failure")
        self._inner(batch)


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture(scope="module")
def load_test_messages() -> list[dict]:
    fixtures = generate(GeneratorConfig(devices_per_firmware=LOAD_TEST_DEVICES_PER_FIRMWARE))
    messages = list(fixtures.all_messages())
    assert len(messages) > 5_000, (
        "load test scale drifted - update the module docstring if intentional"
    )
    return messages


# --- accept-time validation -------------------------------------------------------------


def test_unsupported_device_class_is_rejected_not_spooled(s3_client):
    buffer = IngestBuffer(capture_sink(bucket=BUCKET, s3_client=s3_client))
    bad = _envelope(device_class="powerwall", message_id="msg-bad-class")

    buffer.submit([bad])
    result = buffer.result()

    assert result.messages_accepted == 0
    assert result.messages_rejected == 1
    assert result.messages_delivered == 0
    assert result.messages_spooled_pending == 0
    assert result.rejected[0][0] == "msg-bad-class"
    assert "unsupported device_class" in result.rejected[0][1]
    assert result.reconciles()


def test_missing_required_field_is_rejected_not_spooled(s3_client):
    buffer = IngestBuffer(capture_sink(bucket=BUCKET, s3_client=s3_client))
    incomplete = _envelope(message_id="msg-no-arrival")
    del incomplete["arrival_ts_ms"]

    buffer.submit([incomplete])
    result = buffer.result()

    assert result.messages_accepted == 0
    assert result.messages_rejected == 1
    assert "missing required field" in result.rejected[0][1]
    assert result.reconciles()


def test_valid_message_is_accepted_and_delivered(s3_client):
    buffer = IngestBuffer(capture_sink(bucket=BUCKET, s3_client=s3_client))

    buffer.submit([_envelope(message_id="msg-good")])
    result = buffer.result()

    assert result.messages_accepted == 1
    assert result.messages_rejected == 0
    assert result.messages_delivered == 1
    assert result.messages_spooled_pending == 0
    assert result.reconciles()


# --- synthetic load: zero loss with a healthy downstream --------------------------------


def test_large_synthetic_batch_delivers_with_zero_loss_when_sink_never_fails(
    s3_client, load_test_messages
):
    buffer = IngestBuffer(
        capture_sink(bucket=BUCKET, s3_client=s3_client), config=BufferConfig(batch_size=200)
    )

    buffer.submit(load_test_messages)
    result = buffer.result()

    assert result.messages_accepted == len(load_test_messages)
    assert result.messages_rejected == 0
    assert result.messages_delivered == len(load_test_messages)
    assert result.messages_spooled_pending == 0
    assert result.reconciles()

    listed_keys = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        listed_keys.update(obj["Key"] for obj in page.get("Contents", []))
    assert len(listed_keys) == len(load_test_messages), (
        "every accepted message must land exactly once"
    )


# --- synthetic load: downstream outage, then recovery ------------------------------------


def test_large_synthetic_batch_survives_a_downstream_outage_with_zero_loss(
    s3_client, load_test_messages
):
    """The core accept-and-spool claim: a downstream outage degrades to latency, not loss."""
    real_sink = capture_sink(bucket=BUCKET, s3_client=s3_client)
    # Fail enough initial calls to guarantee a real outage window given batch_size=200 and
    # ~6.5k messages (~33 batches): the first several submit() batches all fail outright.
    flaky = FlakySink(real_sink, fail_first_n_calls=20)
    buffer = IngestBuffer(flaky, config=BufferConfig(batch_size=200))

    buffer.submit(load_test_messages)
    mid_result = buffer.result()

    # Mid-outage: every accepted message is either delivered or correctly spooled - the
    # reconciliation invariant holds even while the run is still in a degraded state.
    assert mid_result.messages_accepted == len(load_test_messages)
    assert mid_result.messages_rejected == 0
    assert mid_result.messages_spooled_pending > 0, (
        "the injected outage should have spooled something"
    )
    assert mid_result.reconciles()

    # Downstream recovers; drive the spool down.
    buffer.drain(max_rounds=50)
    final_result = buffer.result()

    assert final_result.messages_accepted == len(load_test_messages)
    assert final_result.messages_delivered == len(load_test_messages)
    assert final_result.messages_spooled_pending == 0
    assert final_result.reconciles()

    listed_keys = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        listed_keys.update(obj["Key"] for obj in page.get("Contents", []))
    assert len(listed_keys) == len(load_test_messages), "no message may be lost or duplicated in S3"


def test_large_synthetic_batch_survives_intermittent_random_failures(s3_client, load_test_messages):
    real_sink = capture_sink(bucket=BUCKET, s3_client=s3_client)
    flaky = FlakySink(real_sink, fail_rate=0.3, seed=42)
    buffer = IngestBuffer(flaky, config=BufferConfig(batch_size=200))

    buffer.submit(load_test_messages)
    assert buffer.result().reconciles()
    assert flaky.failures > 0, "the injected intermittent failure rate should have triggered"

    delivered = buffer.drain(max_rounds=200)
    final_result = buffer.result()

    assert final_result.messages_spooled_pending == 0, (
        "intermittent failures must not leave a stuck spool"
    )
    assert final_result.messages_delivered == len(load_test_messages)
    assert final_result.reconciles()
    assert delivered >= 0


# --- reconciliation invariant, checked at an arbitrary midpoint --------------------------


def test_reconciliation_holds_at_any_point_during_a_run(s3_client):
    messages = [_envelope(message_id=f"msg-{i}") for i in range(50)]
    real_sink = capture_sink(bucket=BUCKET, s3_client=s3_client)
    flaky = FlakySink(real_sink, fail_first_n_calls=1)
    buffer = IngestBuffer(flaky, config=BufferConfig(batch_size=10))

    for i in range(0, len(messages), 10):
        buffer.submit(messages[i : i + 10])
        assert buffer.result().reconciles(), (
            "accepted must always equal delivered + spooled_pending"
        )

    buffer.drain(max_rounds=10)
    final = buffer.result()
    assert final.messages_accepted == 50
    assert final.messages_spooled_pending == 0
    assert final.messages_delivered == 50
    assert final.reconciles()
