"""Tests for the arrival-shape profiler (P0-06: "Report: protocol, batching, message sizes per
class and firmware").

Works entirely on in-memory message-envelope dicts - no S3/moto needed, since the profiler
never touches a landing bucket. Hand-built fixtures give exact expected numbers; the broader
test against the real generator only checks structural sanity (every group present, counts
line up), since the generator's exact batch sizes/byte counts aren't something a test should
hand-derive and re-pin.

`_debug_injected_issues` is used in a couple of these tests (as sanctioned by the ticket) only
as an independent cross-check of a metric the profiler itself computes without reading that
field - e.g. summing computed batch sizes and comparing against the generator's own
`readings_total` stat. profiler.py itself never reads `_debug_injected_issues`.
"""
from __future__ import annotations

import json

from profiling.arrival_shape.profiler import (
    DistributionStats,
    format_report,
    profile_arrival_shape,
)
from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE, GeneratorConfig, generate


def _reading(device_ts_ms: int, **fields) -> dict:
    base = {"device_ts_ms": device_ts_ms, "payload_hash": "sha1:deadbeef"}
    base.update(fields)
    return base


def _envelope(
    *,
    message_id: str,
    device_id: str,
    device_class: str,
    firmware_version: str,
    protocol: str,
    arrival_ts_ms: int,
    readings: list[dict],
    site_id: str = "site-000",
) -> dict:
    return {
        "message_id": message_id,
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": firmware_version,
        "site_id": site_id,
        "protocol": protocol,
        "sent_ts_ms": arrival_ts_ms,
        "arrival_ts_ms": arrival_ts_ms,
        "readings": readings,
        "_debug_injected_issues": [],
    }


# --- hand-built fixtures with known-exact expected output ------------------------------------


def test_batch_size_stats_on_hand_built_fixtures():
    # Three stall messages, batch sizes 1, 2, 3 -> min=1 median=2 max=3, mean=2.
    msgs = [
        _envelope(
            message_id="m1",
            device_id="stall-a",
            device_class="supercharger_stall",
            firmware_version="3.0.1",
            protocol="mqtt_batch",
            arrival_ts_ms=1000,
            readings=[_reading(900)],
        ),
        _envelope(
            message_id="m2",
            device_id="stall-a",
            device_class="supercharger_stall",
            firmware_version="3.0.1",
            protocol="mqtt_batch",
            arrival_ts_ms=2000,
            readings=[_reading(1900), _reading(1915)],
        ),
        _envelope(
            message_id="m3",
            device_id="stall-a",
            device_class="supercharger_stall",
            firmware_version="3.0.1",
            protocol="mqtt_batch",
            arrival_ts_ms=3000,
            readings=[_reading(2900), _reading(2915), _reading(2930)],
        ),
    ]

    report = profile_arrival_shape(msgs)

    assert report.total_messages == 3
    assert report.total_readings == 6
    key = ("supercharger_stall", "3.0.1")
    assert set(report.groups.keys()) == {key}

    group = report.groups[key]
    assert group.message_count == 3
    assert group.reading_count == 6
    assert group.protocol_counts == {"mqtt_batch": 3}

    batch = group.batch_size
    assert isinstance(batch, DistributionStats)
    assert batch.count == 3
    assert batch.min == 1
    assert batch.median == 2
    assert batch.max == 3
    assert batch.mean == 2

    assert group.batch_size_histogram == {1: 1, 2: 1, 3: 1}


def test_message_size_matches_json_dumps_byte_length():
    msg = _envelope(
        message_id="m1",
        device_id="cab-a",
        device_class="supercharger_cabinet",
        firmware_version="1.9.0",
        protocol="https_poll",
        arrival_ts_ms=5000,
        readings=[_reading(4900)],
    )
    expected_size = len(json.dumps(msg, sort_keys=True).encode("utf-8"))

    report = profile_arrival_shape([msg])

    group = report.groups[("supercharger_cabinet", "1.9.0")]
    assert group.message_size_bytes.count == 1
    assert group.message_size_bytes.min == expected_size
    assert group.message_size_bytes.max == expected_size
    assert group.message_size_bytes.median == expected_size
    # Exactly one message lands in whichever bucket its size falls into.
    assert sum(group.message_size_histogram.values()) == 1


def test_protocol_distribution_is_deterministic_per_class_in_two_message_group():
    # mqtt_batch for stalls, https_poll for cabinets - not a surprising finding, just what the
    # generator (and, per CLAUDE.md, the real protocols) actually use per class today.
    stall_msg = _envelope(
        message_id="s1",
        device_id="stall-a",
        device_class="supercharger_stall",
        firmware_version="3.0.1",
        protocol="mqtt_batch",
        arrival_ts_ms=1000,
        readings=[_reading(900)],
    )
    cabinet_msg = _envelope(
        message_id="c1",
        device_id="cab-a",
        device_class="supercharger_cabinet",
        firmware_version="1.9.0",
        protocol="https_poll",
        arrival_ts_ms=1000,
        readings=[_reading(900)],
    )

    report = profile_arrival_shape([stall_msg, cabinet_msg])

    assert report.groups[("supercharger_stall", "3.0.1")].protocol_counts == {"mqtt_batch": 1}
    assert report.groups[("supercharger_cabinet", "1.9.0")].protocol_counts == {"https_poll": 1}


def test_interarrival_gap_computed_per_device_and_pooled_across_devices_in_group():
    # Device A: arrivals at 0s, 10s, 30s -> gaps [10, 20].
    # Device B: arrivals at 0s, 5s -> gap [5].
    # Pooled group distribution: [10, 20, 5] -> min=5 median=10 max=20.
    def msg(device_id, arrival_s, mid):
        return _envelope(
            message_id=mid,
            device_id=device_id,
            device_class="supercharger_stall",
            firmware_version="3.0.1",
            protocol="mqtt_batch",
            arrival_ts_ms=arrival_s * 1000,
            readings=[_reading(arrival_s * 1000 - 50)],
        )

    msgs = [
        msg("stall-a", 0, "a1"),
        msg("stall-a", 10, "a2"),
        msg("stall-a", 30, "a3"),
        msg("stall-b", 0, "b1"),
        msg("stall-b", 5, "b2"),
    ]

    report = profile_arrival_shape(msgs)
    gap = report.groups[("supercharger_stall", "3.0.1")].interarrival_gap_s
    assert gap is not None
    assert gap.count == 3
    assert gap.min == 5
    assert gap.median == 10
    assert gap.max == 20


def test_single_message_per_device_yields_no_interarrival_gap():
    msg = _envelope(
        message_id="m1",
        device_id="stall-a",
        device_class="supercharger_stall",
        firmware_version="3.0.1",
        protocol="mqtt_batch",
        arrival_ts_ms=1000,
        readings=[_reading(900)],
    )
    report = profile_arrival_shape([msg])
    assert report.groups[("supercharger_stall", "3.0.1")].interarrival_gap_s is None


def test_format_report_runs_and_mentions_every_group():
    msgs = [
        _envelope(
            message_id="m1",
            device_id="stall-a",
            device_class="supercharger_stall",
            firmware_version="3.0.1",
            protocol="mqtt_batch",
            arrival_ts_ms=1000,
            readings=[_reading(900)],
        ),
    ]
    report = profile_arrival_shape(msgs)
    text = format_report(report)
    assert "supercharger_stall" in text
    assert "3.0.1" in text
    assert "1 messages" in text


# --- broader structural test against the real generator --------------------------------------


def test_structural_sanity_against_generator_output():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    all_messages = list(fixtures.all_messages())

    report = profile_arrival_shape(all_messages)

    expected_groups = {
        (device_class, firmware)
        for device_class, firmwares in DEFAULT_FIRMWARE.items()
        for firmware in firmwares
    }
    assert set(report.groups.keys()) == expected_groups
    assert report.total_messages == len(all_messages)
    assert report.total_readings == sum(len(m["readings"]) for m in all_messages)

    for (device_class, firmware), group in report.groups.items():
        group_messages = fixtures.messages_by_group[(device_class, firmware)]
        assert group.message_count == len(group_messages)
        assert group.reading_count == sum(len(m["readings"]) for m in group_messages)

        # Every message in this group used the same class, so exactly one protocol appears -
        # sanity-checking the generator's current design decision (see the profiler's own
        # docstring: this is a plain finding, not something the profiler asserts as universal).
        assert len(group.protocol_counts) == 1

        # Independent cross-check against the generator's own bookkeeping. readings_total in
        # stats_by_group is computed by _finalize_readings() before outage-drop, batching or
        # duplication run, so it's a baseline that reading_count (summed post-outage,
        # post-duplication batch sizes across every landed message) must stay within a known
        # band of: outage drops only ever remove readings from that baseline, and a duplicate
        # message only ever re-sends one already-counted batch (at most max_batch_size
        # readings) again. This does NOT read _debug_injected_issues; readings_total,
        # outage_readings_dropped and duplicate_message are plain Counter stats, not part of
        # the per-message injected-issue list.
        stats = fixtures.stats_by_group[(device_class, firmware)]
        max_batch_size = fixtures.config.max_batch_size
        assert group.reading_count >= stats["readings_total"] - stats["outage_readings_dropped"]
        assert (
            group.reading_count
            <= stats["readings_total"] + stats["duplicate_message"] * max_batch_size
        )

        # Batch size histogram must sum back to the message count, and reading_count must equal
        # the sum of (batch_size * count) over the histogram - two ways of deriving the same
        # total that should agree exactly.
        assert sum(group.batch_size_histogram.values()) == group.message_count
        assert (
            sum(size * count for size, count in group.batch_size_histogram.items())
            == group.reading_count
        )

        # Every message size bucketed exactly once.
        assert sum(group.message_size_histogram.values()) == group.message_count
