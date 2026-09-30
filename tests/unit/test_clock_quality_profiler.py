"""Tests for the timestamp/clock-quality profiler (P0-07).

Two kinds of coverage:

1. Hand-constructed envelopes with known missing/epoch-default/future/clean timestamps, so the
   exact rates and drift numbers are asserted against values worked out by hand (see the
   comments next to each fixture).
2. A cross-check against the synthetic Supercharger fixture generator
   (tests/fixtures/generators/supercharger.py) at higher density (devices_per_firmware=4, this
   repo's default), comparing the profiler's independently-computed missing/epoch/future rates
   against the generator's own internal `stats_by_group` counters.

`GeneratedFixtures.stats_by_group` (like `_debug_injected_issues`) is generator-only test
metadata: it's the generator's own bookkeeping of what it injected, present only because this
is a synthetic fixture and never available on real telemetry. Using it here, in the test file,
as ground truth to sanity-check the profiler's independently-computed rates is exactly the kind
of validation this synthetic setup exists to enable - see profiling/clock_quality/profiler.py's
module docstring for why the profiler itself (profiler.py) must never read it or
`_debug_injected_issues`.

Note on exact match: `stats_by_group["readings_total"]` counts readings once, at the point the
generator finalizes them (before duplicate messages and outage buffering/dropping are applied),
while the profiler counts readings actually present across the messages it's given - which
includes any duplicated envelope's readings a second time and excludes any reading an outage
dropped before emission. So the two totals differ (duplicates net add, outages net remove); the
rates are compared as a ballpark cross-check, not asserted equal, per the ticket's instructions.
"""
from __future__ import annotations

from profiling.clock_quality.profiler import (
    FUTURE_THRESHOLD_MS,
    FirmwareGroupKey,
    profile_messages,
)
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

MS_PER_S = 1000
MS_PER_HOUR = 3600 * MS_PER_S


def _reading(device_ts_ms, **overrides) -> dict:
    reading = {"device_ts_ms": device_ts_ms, "payload_hash": "sha1:deadbeef"}
    reading.update(overrides)
    return reading


def _envelope(*, device_id, arrival_ts_ms, readings, device_class="test_class",
              firmware_version="1.0.0", message_id="msg-1") -> dict:
    return {
        "message_id": message_id,
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": firmware_version,
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": arrival_ts_ms,
        "arrival_ts_ms": arrival_ts_ms,
        "readings": readings,
        # Deliberately populated to prove the profiler ignores it (see module docstring).
        "_debug_injected_issues": ["this_should_never_be_read_by_the_profiler"],
    }


def test_hand_constructed_rates_and_drift():
    arrival_a = 1_000_000_000_000
    arrival_b = 1_000_000_500_000

    # dev-1, message A: one clean reading (100s behind arrival), one missing, one epoch-default.
    msg_a = _envelope(
        device_id="dev-1",
        arrival_ts_ms=arrival_a,
        message_id="msg-a",
        readings=[
            _reading(arrival_a - 100 * MS_PER_S),  # clean; drift sample = +100s
            _reading(None),  # missing
            _reading(0),  # epoch-default
        ],
    )
    # dev-1, message B: one future reading (7h ahead - past the 6h threshold), one borderline
    # clean reading (5h ahead - under the 6h threshold, so NOT flagged future).
    msg_b = _envelope(
        device_id="dev-1",
        arrival_ts_ms=arrival_b,
        message_id="msg-b",
        readings=[
            _reading(arrival_b + 7 * MS_PER_HOUR),  # future
            _reading(arrival_b + 5 * MS_PER_HOUR),  # clean; drift sample = -5h in seconds
        ],
    )
    # dev-2, message C: one clean reading, different device so it contributes a second
    # per-device drift estimate to the distribution.
    msg_c = _envelope(
        device_id="dev-2",
        arrival_ts_ms=arrival_a,
        message_id="msg-c",
        readings=[_reading(arrival_a - 500 * MS_PER_S)],  # clean; drift sample = +500s
    )

    reports = profile_messages([msg_a, msg_b, msg_c])

    key = FirmwareGroupKey(device_class="test_class", firmware_version="1.0.0")
    assert set(reports) == {key}
    report = reports[key]

    # 6 readings total: 1 missing, 1 epoch, 1 future, 3 clean.
    assert report.readings_total == 6
    assert report.missing_timestamp_rate == 1 / 6
    assert report.epoch_default_rate == 1 / 6
    assert report.future_timestamp_rate == 1 / 6

    # dev-1's clean drift samples: +100s (msg A) and -5h=-18000s (msg B) -> median -8950s.
    # dev-2's clean drift sample: +500s -> median 500s (single sample).
    assert report.drift.device_count == 2
    assert report.drift.min_s == -8950.0
    assert report.drift.max_s == 500.0
    assert report.drift.median_s == (-8950.0 + 500.0) / 2
    # p90 over sorted [-8950, 500], nearest-rank interpolation at rank 0.9*(2-1)=0.9.
    assert report.drift.p90_s == -8950.0 + (500.0 - -8950.0) * 0.9


def test_future_threshold_is_exclusive_boundary_documented_in_module():
    # A device_ts exactly at the threshold is not "implausibly far ahead" - only strictly past
    # it counts as future, matching the module docstring's ">" comparison.
    arrival = 2_000_000_000_000
    at_threshold = _envelope(
        device_id="dev-1",
        arrival_ts_ms=arrival,
        readings=[_reading(arrival + FUTURE_THRESHOLD_MS)],
    )
    just_past = _envelope(
        device_id="dev-1",
        arrival_ts_ms=arrival,
        message_id="msg-2",
        readings=[_reading(arrival + FUTURE_THRESHOLD_MS + 1)],
    )

    reports = profile_messages([at_threshold, just_past])
    key = FirmwareGroupKey(device_class="test_class", firmware_version="1.0.0")
    report = reports[key]

    assert report.readings_total == 2
    assert report.future_timestamp_rate == 0.5  # only the "just past" reading is flagged


def test_missing_epoch_future_readings_excluded_from_drift_samples():
    # A device whose readings are entirely missing/epoch/future contributes no drift samples -
    # the distribution should reflect "no clean data", not a spurious zero.
    arrival = 3_000_000_000_000
    msg = _envelope(
        device_id="dev-only-bad",
        arrival_ts_ms=arrival,
        readings=[_reading(None), _reading(0), _reading(arrival + FUTURE_THRESHOLD_MS + 1)],
    )
    reports = profile_messages([msg])
    key = FirmwareGroupKey(device_class="test_class", firmware_version="1.0.0")
    report = reports[key]

    assert report.drift.device_count == 0
    assert report.drift.median_s is None


def test_groups_are_per_device_class_and_firmware():
    arrival = 4_000_000_000_000
    stall = _envelope(
        device_id="stall-1",
        arrival_ts_ms=arrival,
        device_class="supercharger_stall",
        firmware_version="2.1.4",
        readings=[_reading(arrival)],
    )
    cabinet = _envelope(
        device_id="cab-1",
        arrival_ts_ms=arrival,
        device_class="supercharger_cabinet",
        firmware_version="1.8.2",
        message_id="msg-2",
        readings=[_reading(arrival), _reading(None)],
    )

    reports = profile_messages([stall, cabinet])

    assert set(reports) == {
        FirmwareGroupKey("supercharger_stall", "2.1.4"),
        FirmwareGroupKey("supercharger_cabinet", "1.8.2"),
    }
    assert reports[FirmwareGroupKey("supercharger_stall", "2.1.4")].readings_total == 1
    assert reports[FirmwareGroupKey("supercharger_cabinet", "1.8.2")].readings_total == 2
    assert reports[FirmwareGroupKey("supercharger_cabinet", "1.8.2")].missing_timestamp_rate == 0.5


def test_profiler_rates_are_in_the_same_ballpark_as_generator_ground_truth():
    # Denser than the generator's own default (devices_per_firmware=4 is this repo's stated
    # default too, but pinned explicitly here per the ticket's instruction), for a more
    # reliable ground-truth comparison.
    config = GeneratorConfig(devices_per_firmware=4)
    fixtures = generate(config)

    reports = profile_messages(fixtures.all_messages())

    assert set(reports) == {FirmwareGroupKey(*k) for k in fixtures.stats_by_group}

    for group_key, stats in fixtures.stats_by_group.items():
        key = FirmwareGroupKey(*group_key)
        report = reports[key]
        ground_truth_total = stats["readings_total"]
        assert ground_truth_total > 0

        ground_truth_missing_rate = stats["missing_timestamp"] / ground_truth_total
        ground_truth_epoch_rate = stats["epoch_default_timestamp"] / ground_truth_total
        ground_truth_future_rate = stats["future_timestamp"] / ground_truth_total

        # "Same ballpark", not exact equality - see module docstring's note on why totals
        # differ (duplicate messages, outage-dropped readings). The generator's injected rates
        # are all on the order of 1-2%, so an absolute tolerance of 2 percentage points is
        # generous relative to the signal while still catching a badly broken detector (e.g.
        # one that always reports ~0 or inverts missing/epoch/future).
        assert abs(report.missing_timestamp_rate - ground_truth_missing_rate) < 0.02, key
        assert abs(report.epoch_default_rate - ground_truth_epoch_rate) < 0.02, key
        assert abs(report.future_timestamp_rate - ground_truth_future_rate) < 0.02, key

        # Every group in this generator has multiple devices with clock_drift_max_s=600 (see
        # GeneratorConfig) injected, so a working drift estimator should find clean readings
        # for every device and land its per-device estimates within a couple minutes of that
        # +/-600s injected range (arrival-side network jitter and late-arrival delay add some
        # extra spread on top, per estimate_clock_drift's docstring).
        assert report.drift.device_count == config.devices_per_firmware
        assert report.drift.min_s > -900
        assert report.drift.max_s < 900
