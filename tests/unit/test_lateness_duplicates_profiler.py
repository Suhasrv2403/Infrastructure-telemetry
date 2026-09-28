"""Tests for the lateness/duplicate profiler (P0-08).

Two hand-built cases with known-by-construction expected values (lateness percentiles,
duplicate key/message classification), plus one integration case against the synthetic
Supercharger fixture generator that cross-checks the profiler's independently-computed
duplicate count against the generator's own ground truth. That ground truth
(`_debug_injected_issues` / the generator's stats counters) is generator-only test
provenance - used here, in the test file, to sanity-check the profiler, and nowhere near
profiling/lateness_duplicates/profiler.py itself (see that module's docstring).
"""
from __future__ import annotations

from profiling.lateness_duplicates.profiler import profile_messages
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

BASE_ARRIVAL_MS = 1_780_358_400_000  # 2026-06-01T00:00:00Z, arbitrary fixed instant


def _reading(device_ts_ms, payload_hash, **extra) -> dict:
    return {"device_ts_ms": device_ts_ms, "payload_hash": payload_hash, **extra}


def _envelope(message_id, device_id, device_class, arrival_ts_ms, readings, **extra) -> dict:
    return {
        "message_id": message_id,
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": "3.0.1",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": arrival_ts_ms,
        "arrival_ts_ms": arrival_ts_ms,
        "readings": readings,
        "_debug_injected_issues": [],
        **extra,
    }


def test_lateness_distribution_matches_hand_computed_values():
    # 10 readings in one batched message, device_ts_ms 0..90s apart, arrival anchored at the
    # *last* reading (matching the generator's own model - see profiler.py's module
    # docstring), i.e. lateness values are exactly {0, 10, 20, ..., 90} seconds, just in
    # reverse order (first reading is latest relative to arrival).
    arrival_ts_ms = BASE_ARRIVAL_MS + 90_000
    readings = [
        _reading(BASE_ARRIVAL_MS + i * 10_000, f"sha1:h{i}") for i in range(10)
    ]
    message = _envelope("msg-1", "stall-0001", "supercharger_stall", arrival_ts_ms, readings)

    profiles = profile_messages([message])
    lateness = profiles["supercharger_stall"].lateness

    assert lateness.readings_considered == 10
    assert lateness.readings_skipped_implausible_ts == 0
    assert lateness.min_s == 0.0
    assert lateness.max_s == 90.0
    # Hand-computed via linear-interpolation percentile over {0,10,...,90} (n=10):
    #   median: rank=4.5 -> interpolate values[4]=40, values[5]=50 -> 45.0
    #   p90:    rank=8.1 -> interpolate values[8]=80, values[9]=90 -> 81.0
    #   p99:    rank=8.91 -> interpolate values[8]=80, values[9]=90 -> 89.1
    assert lateness.median_s == 45.0
    assert lateness.p90_s == 81.0
    assert abs(lateness.p99_s - 89.1) < 1e-9


def test_lateness_skips_missing_and_epoch_default_timestamps():
    arrival_ts_ms = BASE_ARRIVAL_MS
    readings = [
        _reading(None, "sha1:missing"),  # missing -> skipped
        _reading(0, "sha1:epoch"),  # epoch-default -> skipped
        _reading(BASE_ARRIVAL_MS - 5_000, "sha1:ok"),  # plausible, lateness = 5s
    ]
    message = _envelope("msg-2", "stall-0002", "supercharger_stall", arrival_ts_ms, readings)

    profiles = profile_messages([message])
    lateness = profiles["supercharger_stall"].lateness

    assert lateness.readings_considered == 1
    assert lateness.readings_skipped_implausible_ts == 2
    assert lateness.min_s == lateness.max_s == 5.0


def test_duplicate_rate_matches_hand_computed_values():
    device_id = "cab-0001"
    device_class = "supercharger_cabinet"
    key1 = _reading(BASE_ARRIVAL_MS, "sha1:key1")
    key2 = _reading(BASE_ARRIVAL_MS + 1_000, "sha1:key2")
    key3 = _reading(BASE_ARRIVAL_MS + 2_000, "sha1:key3")

    # A: introduces key1, key2. Arrives first.
    msg_a = _envelope(
        "msg-a", device_id, device_class, BASE_ARRIVAL_MS, [dict(key1), dict(key2)]
    )
    # B: exact retransmit of A (both key1 and key2 already seen) -> pure duplicate message.
    # Arrives after A.
    msg_b = _envelope(
        "msg-b", device_id, device_class, BASE_ARRIVAL_MS + 10_000, [dict(key1), dict(key2)]
    )
    # C: repeats key2 (already seen) and introduces key3 (new) -> partial overlap. Arrives
    # after A and B.
    msg_c = _envelope(
        "msg-c", device_id, device_class, BASE_ARRIVAL_MS + 20_000, [dict(key2), dict(key3)]
    )

    profiles = profile_messages([msg_a, msg_b, msg_c])
    dup = profiles[device_class].duplicates

    # key1: 2 occurrences (A, B). key2: 3 occurrences (A, B, C). key3: 1 occurrence (C).
    assert dup.total_reading_instances == 6
    assert dup.distinct_reading_keys == 3
    assert dup.duplicated_reading_keys == 2  # key1, key2
    assert abs(dup.duplicate_key_rate - 2 / 3) < 1e-9
    # excess instances: (2-1) for key1 + (3-1) for key2 + (1-1) for key3 = 3, over 6 total.
    assert abs(dup.excess_reading_instance_rate - 3 / 6) < 1e-9

    assert dup.messages_total == 3
    assert dup.pure_duplicate_messages == 1  # B
    assert dup.partial_duplicate_messages == 1  # C


def test_duplicate_and_lateness_are_scoped_per_device_class():
    # A stall message and a cabinet message that would collide on device_ts_ms/payload_hash
    # if device_class (or device_id) were ignored, but must NOT be counted as duplicates of
    # each other since the reading-key includes device_id and classes are profiled
    # separately.
    shared_reading = _reading(BASE_ARRIVAL_MS, "sha1:shared")
    stall_msg = _envelope(
        "msg-s", "stall-0003", "supercharger_stall", BASE_ARRIVAL_MS, [dict(shared_reading)]
    )
    cabinet_msg = _envelope(
        "msg-c", "cab-0003", "supercharger_cabinet", BASE_ARRIVAL_MS, [dict(shared_reading)]
    )

    profiles = profile_messages([stall_msg, cabinet_msg])

    assert profiles["supercharger_stall"].duplicates.distinct_reading_keys == 1
    assert profiles["supercharger_stall"].duplicates.duplicated_reading_keys == 0
    assert profiles["supercharger_cabinet"].duplicates.distinct_reading_keys == 1
    assert profiles["supercharger_cabinet"].duplicates.duplicated_reading_keys == 0


def test_duplicate_message_count_matches_generator_ground_truth():
    """Integration cross-check against the fixture generator's own ground truth.

    `_maybe_duplicate` in the generator deep-copies an envelope verbatim (same device_ts_ms
    and payload_hash on every reading) and appends it immediately after the original in that
    device's message list, then the whole group gets sorted by arrival_ts_ms. Since the
    duplicate's arrival_ts_ms is always the original's plus a small positive offset, and no
    other batch for that device can reuse the same (device_ts_ms, payload_hash) pair, every
    generator-injected duplicate message should be classified as a *pure* duplicate by our
    profiler, and the count should match the generator's own `duplicate_message` stat exactly
    per device_class. This uses the generator's stats counters and `_debug_injected_issues` as
    ground truth ONLY here in the test file - never inside profiler.py itself.
    """
    fixtures = generate(GeneratorConfig(devices_per_firmware=4))
    messages = list(fixtures.all_messages())
    profiles = profile_messages(messages)

    expected_duplicate_messages_by_class: dict[str, int] = {}
    for (device_class, _firmware), stats in fixtures.stats_by_group.items():
        expected_duplicate_messages_by_class.setdefault(device_class, 0)
        expected_duplicate_messages_by_class[device_class] += stats.get("duplicate_message", 0)

    for device_class, expected in expected_duplicate_messages_by_class.items():
        assert profiles[device_class].duplicates.pure_duplicate_messages == expected, (
            f"{device_class}: profiler found "
            f"{profiles[device_class].duplicates.pure_duplicate_messages} pure duplicate "
            f"messages, generator injected {expected} (via duplicate_message stat)"
        )

    # Secondary sanity check via _debug_injected_issues directly (the field name the ticket
    # calls out), independent of the stats counter path above.
    debug_duplicate_count_by_class: dict[str, int] = {}
    for message in messages:
        issues = message.get("_debug_injected_issues", [])
        # A message carrying "duplicate_of:<id>" IS the retransmit itself (see
        # _maybe_duplicate); count those as the ground-truth duplicate messages.
        if any(issue.startswith("duplicate_of:") for issue in issues):
            device_class = message["device_class"]
            debug_duplicate_count_by_class[device_class] = (
                debug_duplicate_count_by_class.get(device_class, 0) + 1
            )

    for device_class, expected in debug_duplicate_count_by_class.items():
        assert profiles[device_class].duplicates.pure_duplicate_messages == expected


def test_profile_messages_handles_empty_input():
    assert profile_messages([]) == {}
