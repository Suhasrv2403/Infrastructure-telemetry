"""Regression tests for the fixture generator's arrival-time anchoring.

Covers the bug fixed in this branch: _emit_device_messages used to derive a batch's
arrival_ts_ms from its last reading's *device_ts_ms* - the device's own, possibly missing,
epoch-default, future-corrupted or systematically drifted timestamp. That meant:
  (a) a batch whose last reading got corrupted to missing/epoch-default silently snapped
      arrival to a fixed constant (SYNTHETIC_NOW_MS), making an ordinary batch look days late
      (this is what P0-08's profiling report traced the ~7-day p99/max lateness tail to);
  (b) a future-corrupted last reading pushed arrival inappropriately far forward too;
  (c) the injected per-device clock_drift_ms appeared in both arrival_ts_ms and device_ts_ms,
      so it mostly canceled out of (arrival - device_ts) and the clock-quality profiler's drift
      estimate barely correlated with the actual injected drift (see
      profiling/clock_quality/profiler.py's estimate_clock_drift docstring).

Arrival is now anchored on each reading's ground-truth event time (__true_ts_ms), which is
never missing/corrupted and never carries clock_drift_ms - see _finalize_readings and
_emit_device_messages in tests/fixtures/generators/supercharger.py.
"""
from __future__ import annotations

from tests.fixtures.generators.supercharger import GeneratorConfig, generate

MS_PER_S = 1000
S_PER_DAY = 86_400


def test_arrival_does_not_snap_to_synthetic_now_when_last_reading_is_epoch_default():
    # Force every reading's timestamp to corrupt to epoch-default (device_ts_ms = 0), which
    # used to make every batch's arrival snap to the fixed SYNTHETIC_NOW_MS fallback.
    config = GeneratorConfig(
        devices_per_firmware=1,
        missing_timestamp_rate=0.0,
        epoch_default_rate=1.0,
        future_timestamp_rate=0.0,
    )
    fixtures = generate(config)
    messages = list(fixtures.all_messages())
    assert messages, "generator produced no messages to check"

    for message in messages:
        assert all(r["device_ts_ms"] == 0 for r in message["readings"]), (
            "test setup assumption failed: expected every reading corrupted to epoch-default"
        )

    # The real assertion: the whole arrival range should still be the tight, realistic window
    # driven by the simulated session timeline, not smeared out to the fixed SYNTHETIC_NOW_MS
    # fallback for every corrupted-last-reading batch (the bug this test guards against).
    arrivals = [m["arrival_ts_ms"] for m in messages]
    span_days = (max(arrivals) - min(arrivals)) / (MS_PER_S * S_PER_DAY)
    assert span_days < 10, (
        f"arrival span was {span_days:.1f} days - epoch-default corruption on the last "
        "reading is still leaking into the arrival anchor"
    )


def test_arrival_does_not_inflate_when_last_reading_is_future_corrupted():
    # Force every reading's timestamp to corrupt to a far-future value, which used to push
    # arrival_ts_ms inappropriately forward along with it.
    config = GeneratorConfig(
        devices_per_firmware=1,
        missing_timestamp_rate=0.0,
        epoch_default_rate=0.0,
        future_timestamp_rate=1.0,
        future_offset_min_s=14 * S_PER_DAY,
        future_offset_max_s=14 * S_PER_DAY,
    )
    fixtures = generate(config)
    messages = list(fixtures.all_messages())
    assert messages

    for message in messages:
        for reading in message["readings"]:
            assert reading["device_ts_ms"] is not None
        # device_ts_ms is ~14 days ahead of the true event time by construction; arrival must
        # not have followed it out that far.
        assert message["arrival_ts_ms"] < message["readings"][-1]["device_ts_ms"] - 10 * S_PER_DAY * MS_PER_S, (
            "arrival_ts_ms tracked the future-corrupted device_ts_ms instead of the true event "
            "time"
        )

    arrivals = [m["arrival_ts_ms"] for m in messages]
    span_days = (max(arrivals) - min(arrivals)) / (MS_PER_S * S_PER_DAY)
    assert span_days < 10, (
        f"arrival span was {span_days:.1f} days - future-timestamp corruption on the last "
        "reading is still leaking into the arrival anchor"
    )


def test_true_ts_ms_never_leaks_into_the_emitted_envelope():
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            assert "__true_ts_ms" not in reading, (
                "internal ground-truth timestamp leaked into a reading a real device would "
                "never send"
            )


def test_clock_drift_now_correlates_with_arrival_minus_device_ts():
    """With arrival independent of device_ts_ms, a device's median(arrival_ts_ms -
    device_ts_ms) across its clean readings - exactly what
    profiling/clock_quality/profiler.py's estimate_clock_drift computes - should now track
    its injected clock_drift_ms. Before this fix, the drift term appeared in both sides of
    that subtraction and mostly canceled, giving near-zero correlation (see that profiler's
    own docstring, written before this fix landed).

    Note the expected relationship is a strong *negative* correlation, not positive: a device
    with positive clock_drift_ms thinks time is further ahead, so its device_ts_ms runs
    higher, which makes (arrival - device_ts) smaller/more negative for that device. This
    matches estimate_clock_drift's own docstring ("positive means the device's clock runs
    *behind* arrival time").
    """
    import random
    import statistics
    from collections import defaultdict
    from unittest import mock

    devices_per_firmware = 6
    config = GeneratorConfig(
        devices_per_firmware=devices_per_firmware,
        missing_timestamp_rate=0.0,
        epoch_default_rate=0.0,
        future_timestamp_rate=0.0,
        outage_probability_per_device=0.0,
        late_message_rate=0.0,
    )

    # Pin every device's clock_drift_ms to a known, deliberately large and distinct value per
    # device index, instead of the generator's own random draw, so correlation can be checked
    # against ground truth. devices_per_firmware is chosen equal to len(forced_drifts_s) so
    # this cycles cleanly at each firmware group's boundary (device index 0..5 within every
    # group), matching how the generator visits devices.
    forced_drifts_s = [-600, -400, -150, 150, 400, 600]  # within clock_drift_max_s=600 default
    assert devices_per_firmware == len(forced_drifts_s)

    original_randint = random.Random.randint

    def patched_randint(self, a, b):
        if a == -config.clock_drift_max_s and b == config.clock_drift_max_s:
            # Can't rely on call order to identify which device this is for (the generator
            # sorts each group's messages by arrival_ts_ms before returning, which - correctly
            # - interleaves devices whose simulated windows overlap, e.g. all cabinets in a
            # firmware group share the same window). Instead, cycle through forced_drifts_s
            # by call count modulo devices_per_firmware, and independently verify the mapping
            # by parsing each device_id's own embedded index below.
            patched_randint.calls += 1
            return forced_drifts_s[(patched_randint.calls - 1) % devices_per_firmware]
        return original_randint(self, a, b)

    patched_randint.calls = 0

    with mock.patch.object(random.Random, "randint", patched_randint):
        fixtures = generate(config)

    samples_by_device: dict[str, list[int]] = defaultdict(list)
    for message in fixtures.all_messages():
        for reading in message["readings"]:
            samples_by_device[message["device_id"]].append(
                message["arrival_ts_ms"] - reading["device_ts_ms"]
            )

    estimates_s = []
    injected_s = []
    for device_id, samples in samples_by_device.items():
        # device_id is e.g. "stall-214-0003" or "cab-182-0004" - the last 4-digit group is the
        # index within its firmware group, which is what forced_drifts_s was assigned by.
        index = int(device_id.rsplit("-", 1)[-1])
        estimates_s.append(statistics.median(samples) / MS_PER_S)
        injected_s.append(forced_drifts_s[index % devices_per_firmware])

    assert len(estimates_s) >= 4, "need several devices with data to check correlation"
    correlation = statistics.correlation(injected_s, estimates_s)
    assert correlation < -0.9, (
        f"expected arrival-device_ts drift estimate to correlate strongly (negatively) with "
        f"injected clock_drift_ms, got correlation={correlation:.3f}"
    )
