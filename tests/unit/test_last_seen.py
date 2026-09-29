"""Tests for the telemetry-health last-seen snapshot and silence-episode tracker (P1-12:
"Last-seen snapshot and silence-episode job").

Covers pipeline/telemetry_health/last_seen.py: `build_last_seen_snapshot()` and
`SilenceEpisodeTracker`. See that module's docstring for the silence-threshold reasoning and
the "cause is always unknown" scope note these tests exercise against.
"""
from __future__ import annotations

from pipeline.telemetry_health.last_seen import (
    CAUSE_UNKNOWN,
    LastSeenSnapshot,
    SilenceEpisodeTracker,
    build_last_seen_snapshot,
)

MIN_MS = 60 * 1000
T0 = 1_780_358_400_000  # arbitrary fixed epoch-ms base, matches the generator's SYNTHETIC_NOW_MS


def _row(device_id: str, device_ts_ms: int) -> dict:
    return {"device_id": device_id, "device_ts_ms": device_ts_ms}


def _snapshot(taken_at_ms: int, readings: dict) -> LastSeenSnapshot:
    """Build a LastSeenSnapshot directly from {device_id: last_seen_ts_ms}, bypassing
    build_last_seen_snapshot()'s row-scanning when a test just wants to hand-construct a
    snapshot at a given tick."""
    rows = [_row(device_id, ts) for device_id, ts in readings.items()]
    return build_last_seen_snapshot(rows, taken_at_ms=taken_at_ms)


# ---------------------------------------------------------------------------
# build_last_seen_snapshot
# ---------------------------------------------------------------------------


def test_last_seen_is_the_max_device_ts_ms_per_device():
    rows = [
        _row("stall-0001", T0),
        _row("stall-0001", T0 + 15_000),
        _row("stall-0001", T0 + 5_000),  # out of order, must not override the max
        _row("stall-0002", T0 + 1_000),
    ]
    snapshot = build_last_seen_snapshot(rows, taken_at_ms=T0 + 100_000)

    assert snapshot.last_seen("stall-0001") == T0 + 15_000
    assert snapshot.last_seen("stall-0002") == T0 + 1_000
    assert snapshot.devices() == frozenset({"stall-0001", "stall-0002"})
    assert snapshot.rows_seen == 4
    assert snapshot.rows_skipped_invalid == 0


def test_last_seen_unknown_device_returns_none():
    snapshot = build_last_seen_snapshot([_row("stall-0001", T0)], taken_at_ms=T0)
    assert snapshot.last_seen("does-not-exist") is None


def test_last_seen_skips_rows_with_invalid_device_ts_ms_without_raising():
    rows = [
        _row("stall-0001", T0),
        {"device_id": "stall-0002", "device_ts_ms": None},  # missing timestamp corruption
        {"device_id": "stall-0003", "device_ts_ms": "not-an-int"},
        {"device_id": None, "device_ts_ms": T0},  # missing device_id
        {"device_id": "", "device_ts_ms": T0},  # empty device_id
    ]
    snapshot = build_last_seen_snapshot(rows, taken_at_ms=T0)

    assert snapshot.devices() == frozenset({"stall-0001"})
    assert snapshot.rows_seen == 5
    assert snapshot.rows_skipped_invalid == 4


def test_last_seen_taken_at_ms_defaults_to_something_reasonable_when_omitted():
    snapshot = build_last_seen_snapshot([_row("stall-0001", T0)])
    # Just confirm it's a real, positive epoch-ms wall-clock value - not asserting an exact
    # "now", to avoid a flaky test tied to wall-clock timing.
    assert snapshot.taken_at_ms > 0


# ---------------------------------------------------------------------------
# SilenceEpisodeTracker: core state machine
# ---------------------------------------------------------------------------


def test_device_reporting_normally_never_opens_an_episode():
    """A device whose gap since last-seen never reaches the threshold should never open an
    episode, across many successive ticks."""
    tracker = SilenceEpisodeTracker()
    device_id = "stall-0001"
    ts = T0
    tick = T0

    for i in range(20):
        ts += 15_000  # normal 15s reading cadence
        tick += 15_000
        changed = tracker.process_snapshot(_snapshot(tick, {device_id: ts}))
        assert changed == []

    assert tracker.open_episodes() == ()
    assert tracker.closed_episodes() == ()


def test_device_going_silent_past_threshold_opens_exactly_one_episode():
    tracker = SilenceEpisodeTracker()
    device_id = "stall-0001"
    last_reading_ts = T0

    # First snapshot: device is seen for the first time - no episode.
    changed = tracker.process_snapshot(_snapshot(T0, {device_id: last_reading_ts}))
    assert changed == []
    assert tracker.open_episodes() == ()

    # Subsequent ticks with no new reading: gap grows. Below threshold -> still nothing.
    tick = T0 + 15 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(tick, {device_id: last_reading_ts}))
    assert changed == []
    assert tracker.open_episodes() == ()

    # Gap now exceeds SILENCE_THRESHOLD_MS (30 min) -> episode opens.
    tick = T0 + 31 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(tick, {device_id: last_reading_ts}))
    assert len(changed) == 1
    episode = changed[0]
    assert episode.device_id == device_id
    assert episode.is_open
    assert episode.last_seen_before_silence_ms == last_reading_ts
    assert episode.opened_at_ms == tick
    assert episode.cause == CAUSE_UNKNOWN
    assert len(tracker.open_episodes()) == 1

    # A further tick with the gap still open must NOT open a second episode for the same
    # device.
    tick += 15 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(tick, {device_id: last_reading_ts}))
    assert changed == []
    assert len(tracker.open_episodes()) == 1

    # Device reports again -> episode closes.
    reappearance_ts = tick + 5_000
    close_tick = tick + 15 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(close_tick, {device_id: reappearance_ts}))
    assert len(changed) == 1
    closed = changed[0]
    assert closed is episode  # same object, mutated in place, not a new one
    assert not closed.is_open
    assert closed.resumed_at_ms == reappearance_ts
    assert closed.closed_at_ms == close_tick

    # Sensible relative to the actual last-seen / reappearance times: the episode's event-time
    # span brackets the true gap, and the wall-clock span is >= the event-time span (detection
    # always lags or matches the real event).
    assert closed.last_seen_before_silence_ms < closed.resumed_at_ms
    assert closed.opened_at_ms >= closed.last_seen_before_silence_ms
    assert closed.closed_at_ms >= closed.resumed_at_ms

    assert tracker.open_episodes() == ()
    assert tracker.closed_episodes() == (episode,)


def test_brand_new_device_first_appearance_never_opens_an_episode():
    """A device appearing for the first time ever - even on a tracker that already has other
    devices with long-open episodes - must not spuriously open an episode for itself."""
    tracker = SilenceEpisodeTracker()

    # Seed an existing, long-silent device first.
    tracker.process_snapshot(_snapshot(T0, {"stall-old": T0}))
    tracker.process_snapshot(_snapshot(T0 + 60 * MIN_MS, {"stall-old": T0}))
    assert len(tracker.open_episodes()) == 1

    # A brand-new device shows up for the first time on a later tick.
    changed = tracker.process_snapshot(
        _snapshot(T0 + 61 * MIN_MS, {"stall-old": T0, "stall-new": T0 + 61 * MIN_MS})
    )
    # Nothing changed for stall-new; stall-old's already-open episode is a no-op too.
    assert changed == []
    assert {e.device_id for e in tracker.open_episodes()} == {"stall-old"}


def test_silent_then_back_then_silent_again_produces_two_separate_episodes():
    tracker = SilenceEpisodeTracker()
    device_id = "stall-0001"

    tracker.process_snapshot(_snapshot(T0, {device_id: T0}))

    # First silence: opens then closes.
    open_tick_1 = T0 + 31 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(open_tick_1, {device_id: T0}))
    assert len(changed) == 1
    episode_1 = changed[0]

    reappear_1 = open_tick_1 + 5_000
    close_tick_1 = open_tick_1 + 15 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(close_tick_1, {device_id: reappear_1}))
    assert changed == [episode_1]
    assert not episode_1.is_open

    # Device reports normally a bit longer, then goes silent again.
    tracker.process_snapshot(_snapshot(close_tick_1 + MIN_MS, {device_id: reappear_1 + MIN_MS}))
    last_reading_2 = reappear_1 + MIN_MS

    open_tick_2 = close_tick_1 + MIN_MS + 31 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(open_tick_2, {device_id: last_reading_2}))
    assert len(changed) == 1
    episode_2 = changed[0]

    reappear_2 = open_tick_2 + 5_000
    close_tick_2 = open_tick_2 + 15 * MIN_MS
    changed = tracker.process_snapshot(_snapshot(close_tick_2, {device_id: reappear_2}))
    assert changed == [episode_2]

    # Two genuinely separate episode objects, not one continuous episode or reused state.
    assert episode_1 is not episode_2
    assert episode_1.last_seen_before_silence_ms == T0
    assert episode_1.resumed_at_ms == reappear_1
    assert episode_2.last_seen_before_silence_ms == last_reading_2
    assert episode_2.resumed_at_ms == reappear_2
    assert tracker.closed_episodes() == (episode_1, episode_2)
    assert tracker.open_episodes() == ()


# ---------------------------------------------------------------------------
# Repeated incremental calls vs. one batched call
# ---------------------------------------------------------------------------


def test_repeated_process_snapshot_calls_equal_one_process_snapshots_batch_call():
    """Feeding the same ordered sequence of snapshots to a tracker one at a time (the real
    15-min-job usage pattern: each tick is a separate call against carried-over state) must
    produce the same end state and the same sequence of state changes as handing that same
    ordered sequence to a fresh tracker's process_snapshots() in one call. See
    SilenceEpisodeTracker.process_snapshots()'s docstring for why this equivalence holds (both
    walk the identical per-tick state machine in the identical order) and why it does NOT
    extend to collapsing the ticks into a single final snapshot - that is a different, and
    deliberately not claimed, equivalence.
    """
    device_id = "stall-0001"
    snapshots = [
        _snapshot(T0, {device_id: T0}),
        _snapshot(T0 + 15 * MIN_MS, {device_id: T0}),
        _snapshot(T0 + 31 * MIN_MS, {device_id: T0}),  # opens
        _snapshot(T0 + 46 * MIN_MS, {device_id: T0}),  # stays open
        _snapshot(T0 + 61 * MIN_MS, {device_id: T0 + 61 * MIN_MS + 1}),  # closes
        _snapshot(T0 + 76 * MIN_MS, {device_id: T0 + 61 * MIN_MS + 1}),
    ]

    tracker_incremental = SilenceEpisodeTracker()
    incremental_changes = []
    for snapshot in snapshots:
        incremental_changes.extend(tracker_incremental.process_snapshot(snapshot))

    tracker_batched = SilenceEpisodeTracker()
    batched_changes = tracker_batched.process_snapshots(snapshots)

    assert len(incremental_changes) == len(batched_changes) == 2  # one open, one close
    for inc, batch in zip(incremental_changes, batched_changes):
        assert dataclasses_equal(inc, batch)

    assert tracker_incremental.open_episodes() == tracker_batched.open_episodes() == ()
    assert len(tracker_incremental.closed_episodes()) == len(tracker_batched.closed_episodes()) == 1
    inc_closed = tracker_incremental.closed_episodes()[0]
    batch_closed = tracker_batched.closed_episodes()[0]
    assert dataclasses_equal(inc_closed, batch_closed)


def dataclasses_equal(a, b) -> bool:
    """SilenceEpisode is a plain (non-frozen) dataclass, so `==` already compares fields -
    this helper just makes the intent explicit at each call site above."""
    return a == b


def test_a_single_snapshot_built_from_all_rows_at_once_is_not_claimed_equivalent_to_ticks():
    """Documents (and pins down) the NOT-equivalent case named in process_snapshots()'s
    docstring: collapsing several ticks into one process_snapshot() call against the final
    row state can miss an episode that fully opened and closed within a skipped interval -
    unlike feeding the same ticks in one at a time, which catches it.
    """
    device_id = "stall-0001"
    # Ticks that DO observe an intermediate silence: open at T0+31min, close at T0+46min.
    ticks = [
        _snapshot(T0, {device_id: T0}),
        _snapshot(T0 + 31 * MIN_MS, {device_id: T0}),  # opens
        _snapshot(T0 + 46 * MIN_MS, {device_id: T0 + 46 * MIN_MS}),  # closes
    ]
    tracker_ticked = SilenceEpisodeTracker()
    ticked_changes = tracker_ticked.process_snapshots(ticks)
    assert len(ticked_changes) == 2  # open + close: the blip was observed

    # A single call using only the first and final snapshot (skipping the intermediate tick
    # that would have caught the gap) never sees a gap large enough to open anything, because
    # by the time it looks, the device has already reported again.
    tracker_collapsed = SilenceEpisodeTracker()
    tracker_collapsed.process_snapshot(_snapshot(T0, {device_id: T0}))
    changed = tracker_collapsed.process_snapshot(
        _snapshot(T0 + 46 * MIN_MS, {device_id: T0 + 46 * MIN_MS})
    )
    assert changed == []  # the silence episode is invisible when the tick is skipped
    assert tracker_collapsed.closed_episodes() == ()
