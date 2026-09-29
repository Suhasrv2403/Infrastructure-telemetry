"""Telemetry health: last-seen snapshot and silence-episode tracking.

Ticket: P1-12 ("Last-seen snapshot and silence-episode job"). CLAUDE.md's "Telemetry health"
line: "last-seen snapshot (15-min job), silence episodes, dropout by cohort." This module
builds the first two. "Dropout by cohort" (correlated vs. isolated dropout across a fleet
cohort) is P2-10, explicitly out of scope here - see pipeline/telemetry_health/README.md.

What "last seen" means here
----------------------------
A device's last-seen time is the most recent `device_ts_ms` (EVENT time, never arrival time -
CLAUDE.md invariant 4) this pipeline has actually merged for it, exactly the same event-time
notion pipeline/stage1_parsed/merge.py's `device_hour_key()`/`partition_key()` use. The natural
source is `Stage1MergeStore.rows()` (or any iterable of dicts shaped like Stage 1 merged rows),
but `build_last_seen_snapshot()` only requires `device_id` and `device_ts_ms` per row, so it
works equally against Stage 1, Stage 2, or a synthetic stream of "this device reported at time
T" events - the ticket's own framing.

Not a scheduler
-----------------
CLAUDE.md frames the snapshot as a "15-min job." This module is the pure computation such a job
calls every 15 minutes - `build_last_seen_snapshot()` for the snapshot half,
`SilenceEpisodeTracker.process_snapshot()` for the episode half - not the job itself. Wiring an
actual cron/Dagster schedule around a call to these functions is thin orchestration glue, the
same separation pipeline/stage0_landing/capture.py and pipeline/stage1_parsed/merge.py keep
from their own Dagster asset wiring (see e.g. merge.py's module docstring), and is not built
here.

Silence threshold: why 30 minutes
-----------------------------------
`SILENCE_THRESHOLD_MS` (1_800_000 ms = 30 minutes) is how long a device's last-seen event time
must trail the current snapshot's `taken_at_ms` before an episode opens. Reasoning, from the
two numbers actually available to reason from (tests/fixtures/generators/supercharger.py's
`GeneratorConfig`):

- The job itself runs every 15 minutes. A gap shorter than one job cycle is not evidence of
  anything - the device may simply not have reported *yet* in this cycle. The threshold can't
  usefully be below the job cadence, so 15 minutes is a hard floor.
- `GeneratorConfig.outage_min_s = 900` (15 minutes) is the shortest connectivity outage the
  generator ever models; `outage_max_s = 3 * 3600` is the longest. A genuine outage is never
  shorter than 15 minutes by construction, so a threshold at exactly the 15-minute floor above
  would flag on the very first job tick after a device goes dark - too eager, and indistinguishable
  from ordinary job-cadence noise (a device whose reading landed 1 minute after this tick's
  cutoff looks identical to one that just went silent).
  Requiring the gap to survive TWO full job cycles (30 minutes = 2 x 15-minute cadence) before
  opening an episode is a deliberately simple debounce: it exceeds the generator's own
  `outage_min_s` floor without approaching `outage_max_s`, so a real short outage is still
  caught well before it resolves, while cutting down on job-cadence flapping.
- `reading_interval_s = 15` (stalls, in-session) and its 4x for cabinets are both far below the
  threshold and don't drive this number - see the honest limitation below.

Honest limitation this threshold does NOT solve: Supercharger stalls only report while a
charging session is active; `_simulate_stall_device` leaves 1800-10800s (30min-3h) gaps
*between* sessions, which is completely normal, expected silence, not device failure. A flat,
device-class-agnostic threshold like this one cannot tell "idle between sessions" apart from
"actually gone dark" - the generator's own gap can be up to 6x this threshold. Solving that
needs per-device/per-mode expected-cadence awareness (pipeline/telemetry_health/README.md's
"mode-aware expected counts," explicitly slated for P2-10/P3-05, not this ticket). This module
knowingly produces false-positive episodes for idle-between-sessions stalls until that lands;
said false positives are still real, honestly-labeled "we don't have a confirmed reading" gaps,
just not necessarily "device broken."

Why a brand-new device never spuriously opens an episode
------------------------------------------------------------
`SilenceEpisodeTracker` only evaluates the gap-since-last-seen for a device it has already
seen at least once (i.e. `prior_seen is not None`). A device's very first appearance just
records its last-seen time; there is no "silence" to have gone into, because there is no prior
confirmed reading to measure a gap against. Concretely: a fleet's first-ever snapshot, which by
definition has never seen any of its devices before, opens zero episodes no matter how large
`SILENCE_THRESHOLD_MS` is.

Episode `cause`
----------------
Every `SilenceEpisode.cause` is `CAUSE_UNKNOWN` ("unknown"). This ticket has no correlation
signal to tell a real device failure apart from a network outage, a pipeline-side processing
lag masking readings that already exist upstream, or (per the limitation above) a stall that is
simply between charging sessions - that correlation is exactly what "dropout by cohort" /
"mode-aware expected counts" (P2-10, P3-05) exist to build. `cause` is kept as a plain string
(not an enum) specifically so a future ticket can populate richer values (e.g.
"correlated_outage", "isolated_device_fault") without a schema migration; inventing fake-precise
causes here, with no signal to back them, would be worse than admitting we don't know yet.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from typing import Any

# See "Silence threshold: why 30 minutes" above.
SILENCE_THRESHOLD_MS = 30 * 60 * 1000

# See "Episode `cause`" above: the only honest value available to this ticket.
CAUSE_UNKNOWN = "unknown"


def _now_ms() -> int:
    return int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)


@dataclasses.dataclass(frozen=True)
class LastSeenSnapshot:
    """The most recent known `device_ts_ms` (event time) per device, as of `taken_at_ms`.

    `taken_at_ms` is wall-clock: when this snapshot was computed, not a device event time - it
    is what a 15-min job's successive calls advance, and what `SilenceEpisodeTracker` measures
    silence duration against.
    """

    taken_at_ms: int
    by_device: dict[str, int]
    rows_seen: int
    rows_skipped_invalid: int

    def last_seen(self, device_id: str) -> int | None:
        """The device's most recent known event time, or None if never seen."""
        return self.by_device.get(device_id)

    def devices(self) -> frozenset[str]:
        return frozenset(self.by_device)


def build_last_seen_snapshot(
    rows: Iterable[dict[str, Any]], *, taken_at_ms: int | None = None
) -> LastSeenSnapshot:
    """Compute a `LastSeenSnapshot` from `rows` - the pure computation a 15-min last-seen job
    calls each cycle. `rows` is any iterable of dicts carrying `device_id` and `device_ts_ms`
    (Stage 1 merged rows satisfy this; so does a hand-built stream of "device X reported at
    time T" test events).

    `taken_at_ms` defaults to "now" (wall-clock), matching how a real scheduled job would call
    this; tests pass an explicit value to make snapshots reproducible.

    A row missing `device_id`, or whose `device_ts_ms` isn't a usable epoch-ms int (mirroring
    pipeline/stage1_parsed/merge.py's `_event_time` validation), is skipped rather than raised
    on and counted in `rows_skipped_invalid` - unlike merge.py's `InvalidEventTimestampError`,
    a health snapshot's job is to report on the fleet's condition, and one malformed row should
    not take the whole snapshot down. Timestamp sanity itself remains P1-05's job upstream; this
    is defensive counting, not an attempt to repair bad rows.
    """
    taken_at_ms = _now_ms() if taken_at_ms is None else taken_at_ms

    by_device: dict[str, int] = {}
    rows_seen = 0
    rows_skipped_invalid = 0

    for row in rows:
        rows_seen += 1
        device_id = row.get("device_id")
        device_ts_ms = row.get("device_ts_ms")
        if (
            not isinstance(device_id, str)
            or not device_id
            or not isinstance(device_ts_ms, int)
            or isinstance(device_ts_ms, bool)
        ):
            rows_skipped_invalid += 1
            continue

        current = by_device.get(device_id)
        if current is None or device_ts_ms > current:
            by_device[device_id] = device_ts_ms

    return LastSeenSnapshot(
        taken_at_ms=taken_at_ms,
        by_device=by_device,
        rows_seen=rows_seen,
        rows_skipped_invalid=rows_skipped_invalid,
    )


@dataclasses.dataclass
class SilenceEpisode:
    """One silence episode for one device.

    Two independent pairs of timestamps, both meaningful and deliberately not conflated:
      - `last_seen_before_silence_ms` / `resumed_at_ms`: EVENT time (device_ts_ms) - the actual
        last confirmed reading before the gap, and the actual reading that ended it. This is
        the true duration of the reporting gap, in the device's own timeline.
      - `opened_at_ms` / `closed_at_ms`: snapshot wall-clock time - when the tracker, running on
        its 15-min cadence, actually noticed the gap cross the threshold / actually noticed a
        new reading. These lag the event-time pair by up to one job cycle (or, for opening,
        by however long the debounce in SILENCE_THRESHOLD_MS takes to trip) and are what a
        human paging on this data would see "now."
    """

    device_id: str
    last_seen_before_silence_ms: int
    opened_at_ms: int
    cause: str = CAUSE_UNKNOWN
    resumed_at_ms: int | None = None
    closed_at_ms: int | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at_ms is None


class SilenceEpisodeTracker:
    """Carries per-device state across successive `LastSeenSnapshot`s so a 15-min job can open
    and close silence episodes incrementally, without replaying the whole reading history on
    every call.

    Per-device state held between calls: the device's last known event time, and its currently
    open episode (if any). See `process_snapshot()` for the state machine.
    """

    def __init__(self, *, silence_threshold_ms: int = SILENCE_THRESHOLD_MS) -> None:
        self.silence_threshold_ms = silence_threshold_ms
        self._last_seen: dict[str, int] = {}
        self._open_episodes: dict[str, SilenceEpisode] = {}
        self._closed_episodes: list[SilenceEpisode] = []

    def open_episodes(self) -> tuple[SilenceEpisode, ...]:
        """Episodes currently open, one per device with an ongoing gap."""
        return tuple(self._open_episodes.values())

    def closed_episodes(self) -> tuple[SilenceEpisode, ...]:
        """Every episode this tracker has closed so far, in the order they closed."""
        return tuple(self._closed_episodes)

    def all_episodes(self) -> tuple[SilenceEpisode, ...]:
        """Every episode this tracker knows about - open and closed - in no particular
        cross-device order beyond closed-before-currently-open."""
        return self._closed_episodes + list(self._open_episodes.values())

    def process_snapshot(self, snapshot: LastSeenSnapshot) -> list[SilenceEpisode]:
        """Advance tracker state given one fresh `LastSeenSnapshot` (what one 15-min job tick
        would compute), returning the episodes that changed state (opened or closed) THIS call
        only - not the full open/closed set (use `open_episodes()`/`closed_episodes()` for
        that).

        State machine per device, evaluated over the union of devices this tracker has ever
        seen and devices present in `snapshot` (a device absent from one snapshot - e.g. a
        narrower row window than "everything ever seen" - is treated as "no new reading," not
        as having vanished):

        - Never seen before (`prior_seen is None`): record its last-seen time. Never opens an
          episode - see the module docstring on why a brand-new device can't be "silent."
        - Seen before, and `snapshot`'s value for it is newer than what we had: a fresh reading
          arrived. Advance last-seen. If an episode was open for this device, close it: the
          episode's event-time end is this new reading's own device_ts_ms, and its wall-clock
          close time is this snapshot's `taken_at_ms`.
        - Seen before, and no newer reading (value unchanged, or the device is missing from
          this snapshot): the gap since last-seen has grown to `taken_at_ms - last_seen`. If
          that gap has now reached `silence_threshold_ms` AND no episode is already open for
          this device, open one. If an episode is already open, this call is a no-op for that
          device (it stays open; it is not re-opened or duplicated).
        """
        changed: list[SilenceEpisode] = []
        taken_at_ms = snapshot.taken_at_ms
        device_ids = set(self._last_seen) | set(snapshot.by_device)

        for device_id in device_ids:
            prior_seen = self._last_seen.get(device_id)
            new_seen = snapshot.by_device.get(device_id, prior_seen)
            open_episode = self._open_episodes.get(device_id)

            if prior_seen is None:
                # Brand-new device: nothing to open against (no prior confirmed reading), and
                # by construction there can be no open episode for it yet either.
                self._last_seen[device_id] = new_seen
                continue

            if new_seen is not None and new_seen > prior_seen:
                self._last_seen[device_id] = new_seen
                if open_episode is not None:
                    open_episode.resumed_at_ms = new_seen
                    open_episode.closed_at_ms = taken_at_ms
                    del self._open_episodes[device_id]
                    self._closed_episodes.append(open_episode)
                    changed.append(open_episode)
                continue

            # No newer reading this tick: the gap has grown (or held steady) since last-seen.
            if open_episode is None:
                gap_ms = taken_at_ms - prior_seen
                if gap_ms >= self.silence_threshold_ms:
                    episode = SilenceEpisode(
                        device_id=device_id,
                        last_seen_before_silence_ms=prior_seen,
                        opened_at_ms=taken_at_ms,
                    )
                    self._open_episodes[device_id] = episode
                    changed.append(episode)
            # else: already open, no new reading - stays open, not re-opened.

        return changed

    def process_snapshots(self, snapshots: Iterable[LastSeenSnapshot]) -> list[SilenceEpisode]:
        """Convenience: `process_snapshot()` for each of `snapshots` in order, on this same
        tracker, returning every episode that changed state across all of them (in call order).

        This is deliberately just that loop - see tests/unit/test_last_seen.py's "repeated vs.
        batched" test for why a persistent tracker fed one snapshot at a time by an external
        caller (the real 15-min job's actual usage pattern, where each call may be a separate
        process invocation reloading the same carried state) is equivalent to feeding it the
        same ordered snapshots through this one call. Both walk the identical state machine in
        the identical order; nothing here special-cases "the last snapshot" or looks ahead.

        NOT equivalent to, and not a substitute for, a single `process_snapshot()` call made
        against a snapshot built from ALL rows at once (i.e. skipping the intermediate ticks
        entirely): `opened_at_ms`/`closed_at_ms` are wall-clock tick times, so skipping ticks
        changes which tick "detected" a state change, and a device that went silent and came
        back entirely within one skipped interval would be invisible to a single-final-snapshot
        call but visible here. That is an inherent property of a job that only observes the
        world at its own cadence, not a bug in this tracker - see the module docstring's
        threshold-vs.-cadence discussion.
        """
        changed: list[SilenceEpisode] = []
        for snapshot in snapshots:
            changed.extend(self.process_snapshot(snapshot))
        return changed
