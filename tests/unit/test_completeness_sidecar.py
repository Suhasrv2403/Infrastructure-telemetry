"""Tests for the completeness/lateness sidecar (P1-11: "Completeness and lateness sidecar,
device x hour").

Per the ticket's testing note (shared with other tickets in this session): for scenarios that
need EXACT known expected/on_time/late/missing counts, rows are constructed directly with
hand-picked device_ts_ms/arrival_ts_ms values rather than the realistic-looking fixture
generator - reasoning out "how many readings should this device produce" is much easier with
numbers picked by hand than reverse-engineered from generator output.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from pipeline.stage2_canonical.completeness_sidecar import (
    DEFAULT_ON_TIME_THRESHOLD_S,
    CompletenessLateness,
    InvalidEventTimestampError,
    compute_completeness_lateness,
    device_hour_key,
)

# A fixed hour start, well clear of any DST/epoch edge cases: 2024-03-05T10:00:00 UTC.
HOUR_START = dt.datetime(2024, 3, 5, 10, 0, 0, tzinfo=dt.timezone.utc)
HOUR_START_MS = int(HOUR_START.timestamp() * 1000)
DEVICE_ID = "supercharger_stall-2.1.4-0000"


def _row(device_ts_ms: int, arrival_ts_ms: int, *, device_id: str = DEVICE_ID) -> dict:
    return {
        "device_id": device_id,
        "device_ts_ms": device_ts_ms,
        "arrival_ts_ms": arrival_ts_ms,
    }


def _readings_at_interval_s(interval_s: int, count: int, *, lateness_s: int = 0) -> list[dict]:
    """`count` readings starting at HOUR_START_MS, `interval_s` apart, each arriving
    `lateness_s` after its own device_ts_ms."""
    rows = []
    for i in range(count):
        device_ts_ms = HOUR_START_MS + i * interval_s * 1000
        arrival_ts_ms = device_ts_ms + lateness_s * 1000
        rows.append(_row(device_ts_ms, arrival_ts_ms))
    return rows


def test_full_expected_count_all_on_time():
    """A device-hour with exactly the expected count, all on-time: 0 late, 0 missing."""
    rows = _readings_at_interval_s(15, 240, lateness_s=5)  # well under the on-time threshold

    result = compute_completeness_lateness(rows, expected_interval_s=15)
    key = (DEVICE_ID, "2024-03-05T10")

    assert result[key] == CompletenessLateness(
        device_id=DEVICE_ID,
        event_hour="2024-03-05T10",
        expected=240,
        on_time=240,
        late=0,
        missing=0,
    )
    assert result[key].reconciles()


def test_some_readings_arrive_past_on_time_threshold():
    """Some readings arrive past the on-time threshold: correct late count, 0 missing since
    the rest of the expected readings all showed up (on time or late)."""
    on_time_rows = _readings_at_interval_s(15, 200, lateness_s=5)
    late_delay_ms = int(DEFAULT_ON_TIME_THRESHOLD_S) * 1000 + 30_000
    late_rows = [
        _row(
            HOUR_START_MS + (200 + i) * 15 * 1000,
            HOUR_START_MS + (200 + i) * 15 * 1000 + late_delay_ms,
        )
        for i in range(40)
    ]
    rows = on_time_rows + late_rows

    result = compute_completeness_lateness(rows, expected_interval_s=15)
    bucket = result[(DEVICE_ID, "2024-03-05T10")]

    assert bucket.expected == 240
    assert bucket.on_time == 200
    assert bucket.late == 40
    assert bucket.missing == 0
    assert bucket.reconciles()


def test_fewer_readings_than_expected_none_late():
    """A device-hour with fewer readings than expected and none late: correct missing count."""
    rows = _readings_at_interval_s(15, 100, lateness_s=2)  # only 100 of the expected 240

    result = compute_completeness_lateness(rows, expected_interval_s=15)
    bucket = result[(DEVICE_ID, "2024-03-05T10")]

    assert bucket.expected == 240
    assert bucket.on_time == 100
    assert bucket.late == 0
    assert bucket.missing == 140
    assert bucket.reconciles()


def test_mix_of_on_time_late_and_missing_in_one_hour():
    """A mix of on-time/late/missing in a single hour."""
    on_time_rows = _readings_at_interval_s(15, 150, lateness_s=10)
    late_rows = [
        _row(
            HOUR_START_MS + (150 + i) * 15 * 1000,
            HOUR_START_MS + (150 + i) * 15 * 1000 + 400_000,
        )
        for i in range(30)
    ]
    # 150 on-time + 30 late = 180 landed readings, out of 240 expected -> 60 missing.
    rows = on_time_rows + late_rows

    result = compute_completeness_lateness(rows, expected_interval_s=15)
    bucket = result[(DEVICE_ID, "2024-03-05T10")]

    assert bucket.expected == 240
    assert bucket.on_time == 150
    assert bucket.late == 30
    assert bucket.missing == 60
    assert bucket.reconciles()


def test_hour_boundary_readings_land_in_correct_bucket():
    """A reading whose device_ts_ms falls just before vs. just after an hour boundary lands in
    the correct (device_id, event_hour) bucket, via device_hour_key()."""
    just_before = HOUR_START_MS - 100  # 2024-03-05T09:59:59.900Z
    just_after = HOUR_START_MS + 100  # 2024-03-05T10:00:00.100Z

    assert device_hour_key(_row(just_before, just_before)) == (DEVICE_ID, "2024-03-05T09")
    assert device_hour_key(_row(just_after, just_after)) == (DEVICE_ID, "2024-03-05T10")

    rows = [_row(just_before, just_before + 1000), _row(just_after, just_after + 1000)]
    result = compute_completeness_lateness(rows, expected_interval_s=15)

    assert (DEVICE_ID, "2024-03-05T09") in result
    assert (DEVICE_ID, "2024-03-05T10") in result
    assert result[(DEVICE_ID, "2024-03-05T09")].on_time == 1
    assert result[(DEVICE_ID, "2024-03-05T10")].on_time == 1


def test_invalid_device_ts_ms_raises():
    """device_hour_key() (and therefore compute_completeness_lateness()) fails loudly on a
    row without a usable device_ts_ms, matching pipeline/stage1_parsed/merge.py's behavior -
    timestamp sanity is P1-05's job, not this sidecar's."""
    with pytest.raises(InvalidEventTimestampError):
        device_hour_key(_row(None, HOUR_START_MS))

    with pytest.raises(InvalidEventTimestampError):
        compute_completeness_lateness([_row(None, HOUR_START_MS)], expected_interval_s=15)


def test_expected_interval_must_be_positive():
    with pytest.raises(ValueError):
        compute_completeness_lateness(
            [_row(HOUR_START_MS, HOUR_START_MS)], expected_interval_s=0
        )


def test_multiple_devices_and_hours_are_kept_separate():
    rows = [
        _row(HOUR_START_MS, HOUR_START_MS + 1000, device_id="device-a"),
        _row(HOUR_START_MS + 3600_000, HOUR_START_MS + 3600_000 + 1000, device_id="device-a"),
        _row(HOUR_START_MS, HOUR_START_MS + 1000, device_id="device-b"),
    ]
    result = compute_completeness_lateness(rows, expected_interval_s=15)

    assert set(result.keys()) == {
        ("device-a", "2024-03-05T10"),
        ("device-a", "2024-03-05T11"),
        ("device-b", "2024-03-05T10"),
    }
    for bucket in result.values():
        assert bucket.on_time == 1
        assert bucket.late == 0


@pytest.mark.parametrize(
    ("on_time", "late", "expected"),
    [
        (240, 0, 240),  # exactly full, none missing
        (100, 0, 240),  # under-full, some missing
        (150, 30, 240),  # mixed
        (0, 0, 240),  # nothing landed at all - fully missing
        (250, 0, 240),  # more landed than expected - missing floors at 0, never negative
    ],
)
def test_reconciles_invariant_holds_across_scenarios(on_time, late, expected):
    """CompletenessLateness.reconciles() - missing == max(expected - (on_time+late), 0) - holds
    for every combination this module can actually produce, and catches a wrong `missing`."""
    missing = max(expected - (on_time + late), 0)
    bucket = CompletenessLateness(
        device_id=DEVICE_ID,
        event_hour="2024-03-05T10",
        expected=expected,
        on_time=on_time,
        late=late,
        missing=missing,
    )
    assert bucket.reconciles()

    # A deliberately wrong `missing` should NOT reconcile.
    broken = dataclasses.replace(bucket, missing=missing + 1)
    assert not broken.reconciles()


def test_no_readings_produces_empty_result():
    """No input rows -> no buckets at all (nothing to key on) - see module docstring on why a
    device-hour with zero landed readings can't be represented as "fully missing"."""
    assert compute_completeness_lateness([], expected_interval_s=15) == {}
