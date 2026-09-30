"""Tests for pipeline/stage2_canonical/clock_offset.py (P1-09: "Per-device clock offset and
corrected event time").

Done when (Build backlog.md): "Offset estimates stored; corrected time within agreed tolerance
on test set."

Why these tests don't run the real fixture generator (read this before "fixing" that):
tests/fixtures/generators/supercharger.py has a documented bug (see
profiling/clock_quality/profiler.py's `estimate_clock_drift` docstring, "Known limitation") -
a batch's `arrival_ts_ms` is derived from the device's own already-clock-skewed last
`device_ts_ms`, not from an independent arrival clock, so `arrival_ts_ms - device_ts_ms` mostly
measures a reading's position within its batch, not the injected per-device clock_drift_max_s.
That's a real, measured near-zero correlation against this generator, not something a bigger
sample size would fix. It's fixed on a separate branch
(`fix-generator-independent-arrival-clock`) that this ticket's lineage (branched from P1-08,
which predates that fix) does not include, and fixing it is out of scope for P1-09.

So: instead of generating fixtures and hoping arrival_ts_ms reflects true drift, every test
below constructs rows directly with hand-picked, known per-device offsets and INDEPENDENT
jitter on each side:

    device_ts_ms = true_event_ms - true_offset_ms + device_side_noise_ms
    arrival_ts_ms = true_event_ms + arrival_side_jitter_ms

device_side_noise_ms and arrival_side_jitter_ms are drawn independently (different RNG draws,
not derived from one another or from true_offset_ms) - unlike the generator bug above, so the
median-based estimator in clock_offset.py has an honest chance to recover true_offset_ms by
averaging the noise out, the same way it's meant to work against real telemetry.

Tolerance: OFFSET_TOLERANCE_MS = 5_000 (5 seconds). Justification: each synthetic reading's
device_ts_ms carries independent noise drawn uniformly from +/-3s (NOISE_MAX_MS below), and
arrival_ts_ms carries independent jitter from the same range. With N_READINGS_PER_DEVICE = 25
samples per device, the median of 25 differences (each with per-sample noise on the order of a
few seconds) concentrates well within a couple of seconds of the true offset in practice; 5s
gives comfortable headroom above that without being so loose it would pass a badly broken
estimator. This is a synthetic-noise tolerance for this test's own injected jitter, not a
production SLA.
"""
from __future__ import annotations

import random

from pipeline.stage2_canonical.clock_offset import (
    ClockOffsetStore,
    estimate_offsets,
)

MS_PER_S = 1000
OFFSET_TOLERANCE_MS = 5 * MS_PER_S  # see module docstring's "Tolerance" section
NOISE_MAX_MS = 3 * MS_PER_S
N_READINGS_PER_DEVICE = 25

# Arbitrary fixed base so generated timestamps look plausible; value itself doesn't matter.
BASE_TRUE_EVENT_MS = 1_780_000_000_000
READING_SPACING_MS = 15 * MS_PER_S


def _synthetic_readings(
    rng: random.Random, true_offset_ms: int, n: int = N_READINGS_PER_DEVICE
) -> list[tuple[int, int]]:
    """Build n (device_ts_ms, arrival_ts_ms) pairs for one device with a known true offset.

    device_ts_ms = true_event_ms - true_offset_ms + independent_noise
    arrival_ts_ms = true_event_ms + independent_jitter

    Matches the sign convention documented in clock_offset.py / profiler.py: offset_ms is
    median(arrival_ts_ms - device_ts_ms), so a device whose clock runs BEHIND arrival
    (device_ts_ms smaller) has a POSITIVE true_offset_ms here.
    """
    pairs = []
    for i in range(n):
        true_event_ms = BASE_TRUE_EVENT_MS + i * READING_SPACING_MS
        device_noise_ms = rng.randint(-NOISE_MAX_MS, NOISE_MAX_MS)
        arrival_jitter_ms = rng.randint(-NOISE_MAX_MS, NOISE_MAX_MS)
        device_ts_ms = true_event_ms - true_offset_ms + device_noise_ms
        arrival_ts_ms = true_event_ms + arrival_jitter_ms
        pairs.append((device_ts_ms, arrival_ts_ms))
    return pairs


def test_estimate_offsets_recovers_known_per_device_offsets():
    """Multiple devices, distinct known offsets (negative and positive), recovered within
    OFFSET_TOLERANCE_MS."""
    rng = random.Random(42)
    true_offsets_ms = {
        "device-behind": 240_000,  # clock runs 240s behind arrival
        "device-ahead": -90_000,  # clock runs 90s ahead of arrival
        "device-exact": 0,
        "device-large-behind": 3_600_000,  # 1 hour behind
    }
    readings_by_device = {
        device_id: _synthetic_readings(rng, offset_ms)
        for device_id, offset_ms in true_offsets_ms.items()
    }

    estimated = estimate_offsets(readings_by_device)

    assert set(estimated) == set(true_offsets_ms)
    for device_id, true_offset_ms in true_offsets_ms.items():
        assert abs(estimated[device_id] - true_offset_ms) <= OFFSET_TOLERANCE_MS, (
            f"{device_id}: estimated {estimated[device_id]}ms, true {true_offset_ms}ms"
        )


def test_estimate_offsets_single_reading_uses_that_sample_as_median():
    """A device with exactly one reading: median of one sample is that sample."""
    device_ts_ms = 1_000_000
    arrival_ts_ms = 1_005_000
    readings_by_device = {"solo-device": [(device_ts_ms, arrival_ts_ms)]}

    estimated = estimate_offsets(readings_by_device)

    assert estimated == {"solo-device": arrival_ts_ms - device_ts_ms}


def test_estimate_offsets_empty_readings_list_contributes_no_entry():
    """A device_id mapped to an empty readings list yields no estimate (nothing to compute)."""
    estimated = estimate_offsets({"no-data-device": []})
    assert estimated == {}


def test_estimate_offsets_idempotent():
    """Feeding the same readings twice yields the identical result (pure/deterministic)."""
    rng = random.Random(7)
    readings_by_device = {"device-a": _synthetic_readings(rng, 120_000)}

    first = estimate_offsets(readings_by_device)
    second = estimate_offsets(readings_by_device)

    assert first == second


def test_store_unestimated_device_passes_through_unchanged_and_is_flagged():
    """A device never seen before (no stored estimate): corrected_event_ts_ms passes
    device_ts_ms through unchanged, offset_ms is 0, and is_estimated is False - not silently
    treated as a real zero offset."""
    store = ClockOffsetStore()
    row = {"device_id": "brand-new-device", "device_ts_ms": 1_700_000_000_000}

    result = store.correct_row(row)

    assert result.device_ts_ms == row["device_ts_ms"]
    assert result.corrected_event_ts_ms == row["device_ts_ms"]
    assert result.offset_ms == 0
    assert result.is_estimated is False


def test_store_applies_stored_offset_to_produce_corrected_time():
    """A device with a stored offset gets device_ts_ms + offset_ms as corrected_event_ts_ms,
    and device_ts_ms itself is left untouched (a new field, not a mutation)."""
    store = ClockOffsetStore()
    store.update({"device-behind": 240_000})
    row = {"device_id": "device-behind", "device_ts_ms": 1_700_000_000_000}

    result = store.correct_row(row)

    assert result.device_ts_ms == 1_700_000_000_000  # untouched
    assert result.offset_ms == 240_000
    assert result.corrected_event_ts_ms == 1_700_000_000_000 + 240_000
    assert result.is_estimated is True
    # Row itself was never mutated.
    assert row == {"device_id": "device-behind", "device_ts_ms": 1_700_000_000_000}


def test_store_handles_negative_offset():
    """A device whose clock runs ahead (negative offset) is subtracted, not added-as-positive."""
    store = ClockOffsetStore()
    store.update({"device-ahead": -90_000})
    row = {"device_id": "device-ahead", "device_ts_ms": 1_700_000_000_000}

    result = store.correct_row(row)

    assert result.corrected_event_ts_ms == 1_700_000_000_000 - 90_000


def test_store_update_overwrites_prior_estimate():
    """Recomputing offsets (e.g. a later periodic run) overwrites the prior stored value for
    that device_id, per ClockOffsetStore's documented recompute-and-merge behavior."""
    store = ClockOffsetStore({"device-x": 100_000})
    store.update({"device-x": 50_000})

    assert store.get("device-x") == 50_000


def test_store_as_dict_is_a_copy_not_a_live_view():
    store = ClockOffsetStore({"device-x": 100_000})
    snapshot = store.as_dict()
    snapshot["device-x"] = 999

    assert store.get("device-x") == 100_000


def test_store_has_reflects_presence():
    store = ClockOffsetStore()
    assert store.has("device-x") is False
    store.update({"device-x": 0})
    assert store.has("device-x") is True


def test_constructed_from_initial_mapping():
    store = ClockOffsetStore({"device-a": 10, "device-b": -20})
    assert store.get("device-a") == 10
    assert store.get("device-b") == -20
    assert store.get("device-c") is None
