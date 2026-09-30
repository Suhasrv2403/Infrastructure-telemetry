"""Tests for the pipeline observability watchdog (P1-15: freshness, completeness, cost).

Uses moto to mock S3 for the freshness listing helper, matching
tests/unit/test_stage0_capture.py's pattern, since `list_stage0_landing_timestamps()` is a
thin wrapper around `list_objects_v2` against the same landing bucket layout capture.py writes.
"""
from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from ingest.service.buffer import BufferResult
from pipeline.stage0_landing.capture import CaptureResult, capture_messages
from pipeline.telemetry_health.watchdog import (
    COST_DISCLAIMER,
    Status,
    compute_completeness,
    compute_freshness,
    estimate_cost,
    estimate_cost_from_pipeline,
    list_stage0_landing_timestamps,
    run_watchdog,
)

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"

NOW = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


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


def _row(*, device_id="dev-1", device_ts_ms, lateness_s=0.0):
    return {
        "device_id": device_id,
        "device_ts_ms": device_ts_ms,
        "arrival_ts_ms": device_ts_ms + round(lateness_s * 1000),
    }


# --- freshness -----------------------------------------------------------------------------


def test_freshness_ok_when_last_landed_recently():
    landed = [NOW - dt.timedelta(minutes=5)]
    result = compute_freshness(landed, now=NOW)
    assert result.status is Status.OK
    assert result.staleness_s == pytest.approx(300.0)
    assert result.last_landed_at == landed[0]


def test_freshness_degraded_between_warn_and_critical():
    landed = [NOW - dt.timedelta(minutes=30)]
    result = compute_freshness(landed, now=NOW)
    assert result.status is Status.DEGRADED


def test_freshness_critical_when_stale():
    landed = [NOW - dt.timedelta(hours=3)]
    result = compute_freshness(landed, now=NOW)
    assert result.status is Status.CRITICAL


def test_freshness_uses_most_recent_of_several_timestamps():
    landed = [
        NOW - dt.timedelta(hours=5),
        NOW - dt.timedelta(minutes=2),
        NOW - dt.timedelta(hours=1),
    ]
    result = compute_freshness(landed, now=NOW)
    assert result.last_landed_at == NOW - dt.timedelta(minutes=2)
    assert result.status is Status.OK


def test_freshness_critical_when_nothing_ever_landed():
    result = compute_freshness([], now=NOW)
    assert result.status is Status.CRITICAL
    assert result.last_landed_at is None
    assert result.staleness_s is None
    assert result.objects_seen == 0


def test_freshness_rejects_critical_below_warn():
    with pytest.raises(ValueError):
        compute_freshness([NOW], now=NOW, warn_after_s=100, critical_after_s=50)


def test_list_stage0_landing_timestamps_against_moto_bucket(s3_client):
    messages = [
        _envelope(message_id="msg-a", arrival_ts_ms=1_780_358_400_000),
        _envelope(message_id="msg-b", arrival_ts_ms=1_780_358_400_000 + 3600 * 1000),
    ]
    capture_messages(messages, bucket=BUCKET, s3_client=s3_client)

    timestamps = list_stage0_landing_timestamps(BUCKET, s3_client=s3_client)

    assert len(timestamps) == 2
    assert all(isinstance(ts, dt.datetime) for ts in timestamps)


def test_list_stage0_landing_timestamps_empty_bucket(s3_client):
    assert list_stage0_landing_timestamps(BUCKET, s3_client=s3_client) == []


# --- completeness ----------------------------------------------------------------------------


def test_completeness_ok_when_fully_on_time():
    # 240 readings/hour at the default 15s interval, all delivered instantly (0 lateness).
    rows = [
        _row(device_ts_ms=1_780_358_400_000 + i * 15_000, lateness_s=0.0) for i in range(240)
    ]
    result = compute_completeness(rows)
    assert result.buckets_seen == 1
    assert result.expected_total == 240
    assert result.on_time_total == 240
    assert result.missing_total == 0
    assert result.completeness_ratio == pytest.approx(1.0)
    assert result.status is Status.OK


def test_completeness_degraded_or_critical_with_real_gaps():
    # Only 100 of the expected 240 readings for this device-hour actually showed up.
    rows = [
        _row(device_ts_ms=1_780_358_400_000 + i * 15_000, lateness_s=0.0) for i in range(100)
    ]
    result = compute_completeness(rows)
    assert result.buckets_seen == 1
    assert result.expected_total == 240
    assert result.on_time_total == 100
    assert result.missing_total == 140
    assert result.completeness_ratio == pytest.approx(100 / 240)
    assert result.status is Status.CRITICAL


def test_completeness_counts_late_readings_as_landed_not_missing():
    # All 240 readings show up, but past the on-time threshold - late, not missing.
    rows = [
        _row(device_ts_ms=1_780_358_400_000 + i * 15_000, lateness_s=200.0) for i in range(240)
    ]
    result = compute_completeness(rows)
    assert result.on_time_total == 0
    assert result.late_total == 240
    assert result.missing_total == 0
    assert result.completeness_ratio == pytest.approx(1.0)
    # Fully complete but fully late is still OK by the completeness check (lateness isn't this
    # metric's concern - see module docstring); on_time_ratio reflects the timeliness gap.
    assert result.status is Status.OK
    assert result.on_time_ratio == pytest.approx(0.0)


def test_completeness_critical_with_zero_rows():
    result = compute_completeness([])
    assert result.buckets_seen == 0
    assert result.status is Status.CRITICAL
    assert result.completeness_ratio == 0.0


def test_completeness_aggregates_across_multiple_device_hour_buckets():
    hour_one = [
        _row(device_id="dev-1", device_ts_ms=1_780_358_400_000 + i * 15_000) for i in range(240)
    ]
    hour_two_partial = [
        _row(device_id="dev-2", device_ts_ms=1_780_362_000_000 + i * 15_000) for i in range(120)
    ]
    result = compute_completeness(hour_one + hour_two_partial)
    assert result.buckets_seen == 2
    assert result.expected_total == 480
    assert result.on_time_total == 360
    assert result.missing_total == 120
    assert result.completeness_ratio == pytest.approx(360 / 480)


def test_completeness_rejects_invalid_expected_interval():
    with pytest.raises(ValueError):
        compute_completeness([], expected_interval_s=0)


# --- cost proxy ------------------------------------------------------------------------------


def test_cost_estimate_labels_output_as_illustrative():
    result = estimate_cost(messages=1_000_000, bytes_landed=0)
    assert COST_DISCLAIMER in result.note
    assert "ILLUSTRATIVE" in result.note


def test_cost_estimate_scales_with_message_volume():
    low = estimate_cost(messages=1_000_000, bytes_landed=0)
    high = estimate_cost(messages=10_000_000, bytes_landed=0)
    assert high.estimated_usd == pytest.approx(low.estimated_usd * 10)
    assert high.estimated_usd > low.estimated_usd


def test_cost_estimate_scales_with_byte_volume():
    low = estimate_cost(messages=0, bytes_landed=1_000_000_000)
    high = estimate_cost(messages=0, bytes_landed=5_000_000_000)
    assert high.estimated_usd == pytest.approx(low.estimated_usd * 5)


def test_cost_estimate_zero_volume_is_zero_and_ok():
    result = estimate_cost(messages=0, bytes_landed=0)
    assert result.estimated_usd == 0.0
    assert result.status is Status.OK


def test_cost_estimate_status_escalates_with_budget():
    small = estimate_cost(messages=1_000, bytes_landed=0)
    huge = estimate_cost(messages=1_000_000_000, bytes_landed=0)
    assert small.status is Status.OK
    assert huge.status is Status.CRITICAL


def test_cost_estimate_rejects_negative_volume():
    with pytest.raises(ValueError):
        estimate_cost(messages=-1)


def test_estimate_cost_from_pipeline_uses_buffer_result_when_given():
    buffer_result = BufferResult(
        messages_accepted=1000,
        messages_rejected=0,
        messages_delivered=1000,
        messages_spooled_pending=0,
        rejected=(),
    )
    capture_result = CaptureResult(
        messages_seen=1000, objects_written=1000, objects_already_present=0, keys_written=()
    )
    result = estimate_cost_from_pipeline(
        buffer_result=buffer_result, capture_result=capture_result
    )
    assert result.messages == 1000
    assert result.bytes_landed > 0


def test_estimate_cost_from_pipeline_falls_back_to_capture_result():
    capture_result = CaptureResult(
        messages_seen=500, objects_written=500, objects_already_present=0, keys_written=()
    )
    result = estimate_cost_from_pipeline(capture_result=capture_result)
    assert result.messages == 500


# --- watchdog --------------------------------------------------------------------------------


def _healthy_completeness_rows() -> list[dict]:
    return [
        _row(device_ts_ms=1_780_358_400_000 + i * 15_000, lateness_s=0.0) for i in range(240)
    ]


def test_watchdog_all_healthy_is_ok():
    report = run_watchdog(
        landed_timestamps=[NOW - dt.timedelta(minutes=1)],
        completeness_rows=_healthy_completeness_rows(),
        cost_messages=1_000,
        cost_bytes=0,
        now=NOW,
    )
    assert report.status is Status.OK
    assert report.freshness.status is Status.OK
    assert report.completeness.status is Status.OK
    assert report.cost.status is Status.OK


def test_watchdog_overall_status_is_worst_of_the_three_stale_freshness():
    # Freshness alone is critical (nothing landed in 3 hours); completeness and cost are fine.
    report = run_watchdog(
        landed_timestamps=[NOW - dt.timedelta(hours=3)],
        completeness_rows=_healthy_completeness_rows(),
        cost_messages=1_000,
        cost_bytes=0,
        now=NOW,
    )
    assert report.freshness.status is Status.CRITICAL
    assert report.completeness.status is Status.OK
    assert report.cost.status is Status.OK
    assert report.status is Status.CRITICAL


def test_watchdog_overall_status_is_worst_of_the_three_gappy_completeness():
    # Freshness and cost are fine; completeness alone has real gaps (degraded, not critical).
    rows = [
        _row(device_ts_ms=1_780_358_400_000 + i * 15_000, lateness_s=0.0) for i in range(235)
    ]
    report = run_watchdog(
        landed_timestamps=[NOW - dt.timedelta(minutes=1)],
        completeness_rows=rows,
        cost_messages=1_000,
        cost_bytes=0,
        now=NOW,
    )
    assert report.freshness.status is Status.OK
    assert report.completeness.status is Status.DEGRADED
    assert report.cost.status is Status.OK
    assert report.status is Status.DEGRADED


def test_watchdog_summary_lines_mention_illustrative_cost():
    report = run_watchdog(
        landed_timestamps=[NOW - dt.timedelta(minutes=1)],
        completeness_rows=_healthy_completeness_rows(),
        cost_messages=1_000,
        cost_bytes=0,
        now=NOW,
    )
    lines = report.summary_lines()
    assert any("overall=ok" in line for line in lines)
    assert any("ILLUSTRATIVE" in line for line in lines)
