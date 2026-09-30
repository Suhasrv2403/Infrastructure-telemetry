"""Stage 3c device-day feature table: one row per (device_id, event_day) summarizing a
device's Stage 3a grid for that UTC day.

Ticket: P2-05 ("Device-day feature table"). CLAUDE.md: "Stage 3 Enrich: ... 3c device x day."
docs/decisions/0001-object-store-layout.md already names the eventual warehouse table this is
headed toward: `stage3_device_day`.

Built from Stage 3a grid buckets, not raw rows directly
--------------------------------------------------------
CLAUDE.md's own stage ordering (3a grid, then 3b/3c) says a device-day feature is a summary of
a device's grid, not a re-derivation from Stage 1/2 rows. Every feature this module computes is
a plain aggregate over a device's `GridBucket` sequence for one day (time_grid.py, P2-03):
`compute_device_day_features` takes the 1440 one-minute buckets `build_grid()` produces for a
full UTC day and reduces them to one `DeviceDayFeatures` row. It never reads a CanonicalRow
field directly - if a feature isn't derivable from GridBucket's five fields (device_id,
device_class, bucket_start_ms, coverage, mode, reading_count), it isn't built here. That is a
deliberate scope fence, not an oversight: e.g. "average output power" would need real signal
values the grid doesn't carry, and is exactly the kind of feature this ticket's brief says not
to fabricate.

Device-class scope note (inherited from time_grid.py, not re-decided here)
----------------------------------------------------------------------------
This module only ever sees grid buckets, and time_grid.py's own SUPPORTED_DEVICE_CLASSES is
{"supercharger_stall", "supercharger_cabinet"} - the only classes with real fixtures/catalog
coverage in this repo (see time_grid.py's module docstring). This module has no separate
device-class allowlist of its own: it inherits whatever build_grid() already accepted.

Chosen features and why each is honestly grid-derived
---------------------------------------------------------
- `coverage_ratio` (measured_bucket_count / bucket_count): the "how complete was this device's
  data today" signal the ticket brief names directly - a straight read of GridBucket.coverage.
- `measured_bucket_count` / `gap_bucket_count` / `bucket_count`: the raw counts coverage_ratio
  is built from, kept alongside it so a consumer isn't forced to reverse a ratio to get whole
  numbers (e.g. for weighting an aggregate across many days).
- `longest_gap_minutes`: the longest run of consecutive GAP-coverage buckets. Each bucket is
  exactly BUCKET_MS (one minute) wide - the only bucket width time_grid.py's supported device
  classes ever use - so a run of N gap buckets is honestly N minutes; this field's name would
  need revisiting if a 5-minute grid (Powerwall/Powerpack) is ever added here, which is real,
  described, out-of-scope future work, the same way time_grid.py itself flags it.
- `reading_count_total`: sum of GridBucket.reading_count across the day - how many actual
  readings arrived, independent of how many buckets they landed in (a device reporting in
  bursts vs. steadily can have the same coverage_ratio but very different reading_count_total).
- `minutes_by_mode`: minutes spent in each `mode` value (GridBucket.mode, itself grounded in
  supercharger_stall's session_state or supercharger_cabinet's derived contactor_closed state -
  see time_grid.py). THIS MODULE'S OWN JUDGMENT CALL, stated plainly: it counts every bucket's
  mode, including a GAP bucket's forward-filled mode (time_grid.py's own "last known state is
  still the best available guess" reasoning), not just MEASURED buckets. The alternative -
  counting only measured buckets - would undercount "time in a state" for any state that
  persists through a brief reporting gap (e.g. a stall sitting in `charging` through a 2-minute
  dropout really was charging for those 2 minutes, as best anyone can tell); forward-filled
  mode is exactly what a live status dashboard would show during that gap, the same intuition
  time_grid.py's own docstring uses to justify forward-fill in the first place. A bucket whose
  mode is `None` (a leading gap before any reading ever landed, or - the all-gap-day edge case
  this ticket's tests require documenting - a day with no readings at all) is counted under the
  literal `None` key, not silently dropped or relabeled `"unknown"`; see
  `compute_device_day_features`'s docstring for the exact all-gap-day output.

Dirty-day recompute design (the "recomputed on late data" half of this ticket's done-when)
----------------------------------------------------------------------------------------------
P1-07's DirtyKeysTable tracks dirty keys at (device_id, event_hour) grain - a device-DAY
feature spans 24 of those. This module's design choice, stated plainly because the ticket asks
for it to be: **whenever ANY of a device-day's 24 hours is dirty, the WHOLE day's features are
recomputed, never just the dirty hours in isolation.** Reasons:
  1. None of this module's features are cleanly decomposable per-hour. `longest_gap_minutes`
     in particular can span an hour boundary (a gap starting at 23:50 and ending at 00:15 the
     next day is a single 25-minute gap, not two independent per-hour gaps) - recomputing only
     the dirty hour's slice of buckets would silently truncate a gap at the hour boundary and
     under-report it. `minutes_by_mode`'s forward-fill has the same cross-hour dependency: a
     clean hour immediately after a dirty one needs to know the dirty hour's last mode to
     seed its own leading buckets correctly (mirrors time_grid.py's own forward-fill-does-not-
     cross-hour-boundaries note - but here, unlike a single-hour grid rebuild, we control the
     whole day's range in one build_grid() call, so forward-fill correctly flows across the
     hour boundaries INSIDE one day, it just still doesn't reach into the PREVIOUS day).
  2. `coverage_ratio` and `reading_count_total` are simple sums and technically could be
     patched incrementally per dirty hour, but doing that would mean carrying forward a
     separate persisted day-level accumulator this repo has no storage for (see point 3) and
     would only save recomputing at most 23 of 1440 buckets' worth of aggregation - a trivial
     amount of work, not worth a separate incremental code path with its own correctness
     surface.
  3. There is no persisted Stage 2/3 cache anywhere in this repo to read "the rest of the
     day, unchanged" from - canonicalize.py and time_grid.py are pure functions with no store
     of their own (only Stage 1's Stage1MergeStore is an actual store). So "the already-clean
     hours of the day" are pulled directly from the SAME Stage 1 store those hours' rows have
     sat in untouched, and re-canonicalized fresh via P1-08's canonicalize_rows - which is
     idempotent (a pure function over data that, by definition, didn't change) and therefore
     produces byte-identical output to whatever an earlier pass would have computed for that
     hour. This is the "caller-supplied full day's row set, dirty subset freshly recomputed,
     rest taken as-is" design the ticket brief allows for - "as-is" here means "re-derived
     on-demand from the same untouched Stage 1 rows", not "read from a cache", because no cache
     exists in this codebase to read from (rebuild_dirty_grid_buckets has this exact same
     property one stage up: there is no separate Stage 3a store either).
  4. This recompute is still correctly SCOPED, not a CLAUDE.md-invariant-5 violation: it never
     touches another device's data, another day's data, or reprocesses a device-day that had no
     dirty hour at all. It touches at most 24 device-hour reads for exactly the device-days a
     late merge actually affected - a bounded, single-device-day recompute, not the
     "full-partition recompute" invariant 5 reserves for an explicit reviewed backfill job.

`rebuild_dirty_device_day_features` implements this: it calls P1-13's
`recompute_dirty_device_hours` exactly once (which snapshots and acknowledges every currently
dirty key across every device, the same "process the whole table each call" pattern
`rebuild_dirty_grid_buckets` already uses), groups the freshly-recomputed rows by
(device_id, event_day) via merge.py's own device_hour_key(), then for each affected day pulls
the day's remaining 24-minus-dirty hours' rows straight from `store` and canonicalizes them
fresh, builds one full-day grid via `build_grid`, and reduces it to one `DeviceDayFeatures` row
via `compute_device_day_features`.

What this module does NOT do (scope boundaries, stated per this ticket's brief)
-----------------------------------------------------------------------------------
- No Stage 3b (device x event) - that's P2-04, needs P2-02 (external event sources), neither
  built.
- No device history dimension (firmware/hardware-rev validity periods, as-of joins) - P2-01,
  needs P0-11 (real access), not built.
- No detector framework (P2-06) - a separate, sibling ticket.
- No 5-minute Powerwall/Powerpack grid support - inherited gap from time_grid.py, not
  re-decided or worked around here.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from pipeline.stage1_parsed.dirty_keys import DirtyKeysTable, DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import DeviceHourKey, Stage1MergeStore, device_hour_key
from pipeline.stage2_canonical.canonicalize import CanonicalRow, Catalog, canonicalize_rows
from pipeline.stage2_canonical.targeted_recompute import (
    TargetedRecomputeResult,
    recompute_dirty_device_hours,
    rows_for_device_hour,
)
from pipeline.stage3_enrich.time_grid import (
    BUCKET_MS,
    MEASURED,
    ONE_HOUR_MS,
    GridBucket,
    build_grid,
)

# The (device_id, event_day) key this module's features/rebuild are keyed on - event_day is a
# "YYYY-MM-DD" UTC date string, one grain coarser than merge.py's DeviceHourKey
# (device_id, "YYYY-MM-DDTHH").
DeviceDayKey = tuple[Any, str]

ONE_DAY_MS = 24 * ONE_HOUR_MS


class DeviceDayError(Exception):
    """Base class for Stage 3c device-day errors."""


def event_day_range_ms(event_day: str) -> tuple[int, int]:
    """[start_ms, end_ms) in epoch-ms for a "YYYY-MM-DD" UTC event_day string - exactly the
    24-hour window `hour_keys_for_day`'s 24 hour keys cover, mirroring time_grid.py's
    event_hour_range_ms one grain up.
    """
    day_start = dt.datetime.strptime(event_day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    start_ms = int(day_start.timestamp() * 1000)
    return start_ms, start_ms + ONE_DAY_MS


def hour_keys_for_day(device_id: Any, event_day: str) -> tuple[DeviceHourKey, ...]:
    """The 24 DeviceHourKey values (hours 00-23) covering one device's `event_day`, in the
    exact "YYYY-MM-DDTHH" string shape merge.py's device_hour_key() produces (zero-padded via
    strftime's own %H), so a key built here compares equal to one built from a real row.
    """
    return tuple((device_id, f"{event_day}T{hour:02d}") for hour in range(24))


@dataclasses.dataclass(frozen=True)
class DeviceDayFeatures:
    """One Stage 3c device-day feature row: a summary of one device's Stage 3a grid buckets for
    one UTC day. See module docstring for what each field means and why it's honestly derivable
    from GridBucket data alone.

    `minutes_by_mode` keys are GridBucket.mode values, including the literal `None` key for any
    bucket whose mode was never known (a leading gap before the day's first reading, or - on a
    day with no readings at all - every bucket; see compute_device_day_features's docstring).
    """

    device_id: Any
    device_class: str
    event_day: str
    bucket_count: int
    measured_bucket_count: int
    gap_bucket_count: int
    coverage_ratio: float
    longest_gap_minutes: int
    reading_count_total: int
    minutes_by_mode: dict[str | None, int]

    def reconciles(self) -> bool:
        """True iff the coverage counts and the mode-minute breakdown both account for exactly
        `bucket_count` buckets - never more, never fewer. Mirrors the reconciles() style
        CanonicalizeResult/MergeResult/TargetedRecomputeResult already use in this repo."""
        return (
            self.bucket_count == self.measured_bucket_count + self.gap_bucket_count
            and self.bucket_count == sum(self.minutes_by_mode.values())
        )


def compute_device_day_features(buckets: Sequence[GridBucket]) -> DeviceDayFeatures:
    """Reduce one device's contiguous GridBucket sequence for one day to a DeviceDayFeatures
    row.

    `buckets` must be non-empty, contiguous (each bucket_start_ms exactly one bucket-width after
    the previous - the width is inferred from the first two buckets, or assumed to be
    time_grid.BUCKET_MS for a single-bucket input), and all belong to the same device_id/
    device_class (a caller bug otherwise, and this raises ValueError - the same "fail loudly on
    misuse" stance build_grid() itself takes, rather than silently aggregating mismatched rows).
    `event_day` is read off the first bucket's bucket_start_ms UTC date - callers are expected
    to pass a day-aligned range (as `event_day_range_ms` produces), the same assumption
    time_grid.py's own event_hour_range_ms-driven callers make for hour alignment.

    All-gap day (device silent all day): every bucket has coverage=GAP and mode=None (nothing
    was ever measured to forward-fill from - see time_grid.py's forward-fill section), so this
    returns measured_bucket_count=0, coverage_ratio=0.0, longest_gap_minutes=bucket_count,
    reading_count_total=0, and minutes_by_mode={None: bucket_count}. This is a real, sensible
    (not crashing, not fabricated) result: a fully-silent device-day is exactly "0% coverage,
    one gap as long as the day, no known mode."
    """
    if not buckets:
        raise ValueError("cannot compute device-day features from zero buckets")

    device_id = buckets[0].device_id
    device_class = buckets[0].device_class
    event_day = dt.datetime.fromtimestamp(
        buckets[0].bucket_start_ms / 1000, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d")
    bucket_width_ms = (
        buckets[1].bucket_start_ms - buckets[0].bucket_start_ms if len(buckets) > 1 else BUCKET_MS
    )

    measured_count = 0
    gap_count = 0
    reading_count_total = 0
    longest_gap = 0
    current_gap = 0
    minutes_by_mode: dict[str | None, int] = {}
    prev_bucket_start_ms: int | None = None

    for bucket in buckets:
        if bucket.device_id != device_id or bucket.device_class != device_class:
            raise ValueError(
                "compute_device_day_features() received buckets for more than one "
                f"device_id/device_class: ({device_id!r}, {device_class!r}) vs. "
                f"({bucket.device_id!r}, {bucket.device_class!r}) - callers must pre-filter to "
                "one device's one day"
            )
        if prev_bucket_start_ms is not None and (
            bucket.bucket_start_ms != prev_bucket_start_ms + bucket_width_ms
        ):
            raise ValueError(
                "compute_device_day_features() requires a contiguous bucket sequence - gap "
                f"between bucket_start_ms={prev_bucket_start_ms} and {bucket.bucket_start_ms}"
            )
        prev_bucket_start_ms = bucket.bucket_start_ms

        if bucket.coverage == MEASURED:
            measured_count += 1
            current_gap = 0
        else:
            gap_count += 1
            current_gap += 1
            longest_gap = max(longest_gap, current_gap)

        reading_count_total += bucket.reading_count
        minutes_by_mode[bucket.mode] = minutes_by_mode.get(bucket.mode, 0) + 1

    bucket_count = len(buckets)

    return DeviceDayFeatures(
        device_id=device_id,
        device_class=device_class,
        event_day=event_day,
        bucket_count=bucket_count,
        measured_bucket_count=measured_count,
        gap_bucket_count=gap_count,
        coverage_ratio=measured_count / bucket_count,
        longest_gap_minutes=longest_gap,
        reading_count_total=reading_count_total,
        minutes_by_mode=minutes_by_mode,
    )


@dataclasses.dataclass(frozen=True)
class DeviceDayRebuildResult:
    """Per-(device_id, event_day) outcome of one rebuild_dirty_device_day_features() call.

    `dirty_hour_keys` is the subset of this day's 24 hour keys that were actually dirty going
    into this call (the other 24-minus-len(dirty_hour_keys) were re-derived from the store as
    "already clean" - see module docstring). `features` is None only when NOT ONE of the day's
    24 hours (dirty or clean) yielded a single successfully-canonicalized row - e.g. every dirty
    hour's rows all failed Stage 2 cast and the device otherwise has no data that day - mirroring
    rebuild_dirty_grid_buckets's own device_class=None/buckets=() edge case one level up.
    """

    day_key: DeviceDayKey
    device_class: str | None
    features: DeviceDayFeatures | None
    buckets: tuple[GridBucket, ...]
    dirty_hour_keys: frozenset[DeviceHourKey]
    rows_seen: int
    rows_failed: int


@dataclasses.dataclass(frozen=True)
class DeviceDayFeaturesRebuildResult:
    """Summary of one rebuild_dirty_device_day_features() call, mirroring GridRebuildResult's/
    TargetedRecomputeResult's counter style.

    `recompute` is the single underlying P1-13 TargetedRecomputeResult this call drove (for
    every currently-dirty hour across every device, not scoped to one day) - kept alongside so a
    caller can still see per-hour cast failures directly, the same reason GridRebuildResult
    keeps it.
    """

    device_days_seen: int
    device_days_rebuilt: int
    per_day: dict[DeviceDayKey, DeviceDayRebuildResult]
    recompute: TargetedRecomputeResult

    def reconciles(self) -> bool:
        """True iff every device-day this pass identified as affected got exactly one rebuild
        attempt. Mirrors GridRebuildResult.reconciles()."""
        return self.device_days_seen == self.device_days_rebuilt


def rebuild_dirty_device_day_features(
    store: Stage1MergeStore | DirtyKeyTrackingStore,
    dirty_keys: DirtyKeysTable,
    catalog: Catalog,
) -> DeviceDayFeaturesRebuildResult:
    """Recompute Stage 3c device-day features for every device-day touched by a currently-dirty
    hour in `dirty_keys` - the WHOLE day for each, never just its dirty hours in isolation. See
    module docstring's "Dirty-day recompute design" section for why.

    Calls P1-13's `recompute_dirty_device_hours` exactly once, which - as it always does -
    snapshots and acknowledges every dirty key currently in `dirty_keys` (across every device,
    not just the ones this call happens to be asked about; there is no "ask about one device"
    entry point, matching rebuild_dirty_grid_buckets's own whole-table-per-call pattern). Rows
    for the dirty hours come from that call directly; rows for each affected day's remaining
    (already-clean) hours are pulled straight from `store` and canonicalized fresh (see module
    docstring point 3 on why "fresh" and "as-is" are the same thing here). Everything is then
    assembled into one full-day grid via `build_grid` and reduced via
    `compute_device_day_features`.

    Never raises for a per-row cast failure or an empty day (delegated the same way
    rebuild_dirty_grid_buckets already does); only a genuine store/catalog misuse propagates.
    """
    recompute_result = recompute_dirty_device_hours(store, dirty_keys, catalog)

    # Which device-days are affected, and which of each one's hour keys were the dirty ones -
    # iterate recompute_result.per_key (every dirty key this pass attempted), not just rows, so
    # a dirty hour with zero surviving rows still marks its day as affected.
    dirty_hours_by_day: dict[DeviceDayKey, set[DeviceHourKey]] = {}
    for hour_key in recompute_result.per_key:
        device_id, event_hour = hour_key
        dirty_hours_by_day.setdefault((device_id, event_hour[:10]), set()).add(hour_key)

    # Group the freshly-recomputed dirty-hour rows back by their own (device_id, event_hour)
    # key, reusing merge.py's device_hour_key() the same way rebuild_dirty_grid_buckets does -
    # a CanonicalRow's fields still carry device_id/device_ts_ms unchanged (identity fields).
    fresh_rows_by_hour: dict[DeviceHourKey, list[CanonicalRow]] = {}
    for row in recompute_result.rows:
        fresh_rows_by_hour.setdefault(device_hour_key(row.fields), []).append(row)

    per_day: dict[DeviceDayKey, DeviceDayRebuildResult] = {}

    for day_key, dirty_hour_keys in dirty_hours_by_day.items():
        device_id, event_day = day_key
        day_rows: list[CanonicalRow] = []
        rows_seen = 0
        rows_failed = 0

        for hour_key in hour_keys_for_day(device_id, event_day):
            if hour_key in dirty_hour_keys:
                rows = fresh_rows_by_hour.get(hour_key, [])
                day_rows.extend(rows)
                per_key_result = recompute_result.per_key[hour_key]
                rows_seen += per_key_result.rows_seen
                rows_failed += per_key_result.rows_failed
            else:
                # Not dirty this pass - re-derive from the store, which still holds this hour's
                # rows untouched (Stage1MergeStore never deletes). See module docstring point 3
                # on why re-canonicalizing here is correct, not wasted/incorrect work.
                raw_rows = rows_for_device_hour(store, hour_key)
                clean_result = canonicalize_rows(list(raw_rows), catalog)
                day_rows.extend(clean_result.rows)
                rows_seen += clean_result.rows_seen
                rows_failed += clean_result.rows_failed

        if day_rows:
            device_class = day_rows[0].fields["device_class"]
            range_start_ms, range_end_ms = event_day_range_ms(event_day)
            buckets = build_grid(
                day_rows,
                device_id=device_id,
                device_class=device_class,
                range_start_ms=range_start_ms,
                range_end_ms=range_end_ms,
            )
            features = compute_device_day_features(buckets)
        else:
            device_class = None
            buckets = ()
            features = None

        per_day[day_key] = DeviceDayRebuildResult(
            day_key=day_key,
            device_class=device_class,
            features=features,
            buckets=buckets,
            dirty_hour_keys=frozenset(dirty_hour_keys),
            rows_seen=rows_seen,
            rows_failed=rows_failed,
        )

    return DeviceDayFeaturesRebuildResult(
        device_days_seen=len(per_day),
        device_days_rebuilt=len(per_day),
        per_day=per_day,
        recompute=recompute_result,
    )
