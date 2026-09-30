"""Stage 3a time grid: fixed-width per-device buckets with coverage and mode labels.

Ticket: P2-03 ("Stage 3a time grid (5-min / 1-min) with coverage and mode labels").

Scope note - read this before assuming this module handles Powerwall/Powerpack
--------------------------------------------------------------------------------
CLAUDE.md describes Stage 3a as "5-min Powerwall/Powerpack, 1-min others". As of this ticket,
no Powerwall or Powerpack fixtures, generators, parsers or catalog entries exist anywhere in
this repo (confirmed by inspection of tests/fixtures/generators/ and catalog/signals.yaml,
which only model Supercharger stall/cabinet - see catalog/signals.yaml's own header). There is
therefore nothing real for a 5-minute grid to be built or tested against yet. This module
implements ONLY the 1-minute grid for the device classes that actually exist in this repo today
- `supercharger_stall` and `supercharger_cabinet` (see SUPPORTED_DEVICE_CLASSES below). The
5-minute Powerwall/Powerpack grid is real, described, un-scoped future work, not something this
module silently omits without saying so - do not read the absence of a 5-min path here as an
oversight.

Built from Stage 2 canonical rows, not raw Stage 1 fields
------------------------------------------------------------
This grid is built from `pipeline.stage2_canonical.canonicalize.CanonicalRow` objects (P1-08),
never straight from Stage 1's raw firmware field names - `mode` below is defined in terms of
cataloged *canonical* signal names (`session_state`, `contactor_closed`), so a caller must
canonicalize first. `rebuild_dirty_grid_buckets` does that wiring for the dirty-key path by
composing with P1-13's `recompute_dirty_device_hours` directly.

Grid bucket definition
-------------------------
A grid bucket is keyed by (device_id, bucket_start_ms): a fixed BUCKET_MS-wide window of EVENT
time (a `CanonicalRow`'s `device_ts_ms` floored to the bucket width), never arrival/ingest time
(CLAUDE.md invariant 4 - "Never partition by device_id alone[; ...] Stage 1+ by event time").
BUCKET_MS is 60_000 (one minute), per CLAUDE.md's "1-min others" language and confirmed against
tests/fixtures/generators/supercharger.py's GeneratorConfig.reading_interval_s (15s for stalls,
60s for cabinets - see module-level comment on SUPPORTED_DEVICE_CLASSES) - both comfortably
finer-grained than a 1-minute bucket, so a stall's healthy bucket typically holds ~4 readings
and a cabinet's typically holds ~1.

Coverage
-----------
A bucket's `coverage` is `"measured"` if at least one canonical row's floored `device_ts_ms`
landed in it, else `"gap"` (see MEASURED / GAP below). This is a binary "did a real reading
land here" signal, deliberately not a finer completeness metric (that's closer to P1-11's
completeness/lateness sidecar's territory, at the device-hour grain, not this grid's per-minute
grain).

Multiple readings in one bucket - "latest wins", documented, not silently arbitrary
----------------------------------------------------------------------------------------
A device reporting every ~15s (stalls) or ~60s (cabinets) can occasionally land more than one
reading in the same 1-minute bucket (e.g. a stall's 4th reading of one minute and 1st reading
of the next can round to the same floor when intervals jitter). This module resolves that by
taking the LATEST reading in the bucket (max `device_ts_ms`) as the bucket's representative row
for `mode`, and records `reading_count` (how many readings actually landed) so a downstream
consumer can see the collapse happened rather than assuming exactly one reading per bucket.
"Latest wins" was chosen over averaging because `mode` is a categorical/derived state label,
not a physical quantity - there is no sensible "average" of `charging` and `session_complete` -
and the most recent reading is the best available estimate of the device's state as of the end
of that bucket, which is the same intuition a live status dashboard would use. Ties (two rows
sharing the exact same `device_ts_ms`) resolve to whichever row appears first in the input
sequence (Python's `max()` keeps the first-encountered maximum) - in practice this only matters
for a genuine same-millisecond distinct-payload duplicate, since same-payload retransmissions
already collapsed to one row at Stage 1's merge (invariant 2); this repo's fixtures don't
produce that case, so it is documented rather than specially handled.

Mode - grounded in real cataloged fields, one judgment call flagged as such
--------------------------------------------------------------------------------
`mode` is a device's operating-state label as of a bucket's representative reading:

- `supercharger_stall`: the catalog's `session_state` (catalog/signals.yaml, renamed from raw
  `state`) is a direct, already-enumerated fit - `plugged_in` / `charging` / `fault` /
  `session_complete` - and is used as-is. No judgment call needed here.
- `supercharger_cabinet`: the catalog has no directly equivalent state enum. The closest real
  cataloged field is `contactor_closed` (bool - "Whether the cabinet's main contactor is closed
  (delivering power) at the time of this reading", catalog/signals.yaml). This module derives
  `mode` from it as `"energized"` (contactor_closed=True) / `"de_energized"` (False). This is
  THIS MODULE'S OWN JUDGMENT CALL, not SME-confirmed - the same honesty standard signals.yaml's
  own "DRAFT, NOT REVIEWED" / "needs firmware SME confirmation" caveats hold their fault-code
  fields to. A firmware SME may well want a richer cabinet mode (e.g. distinguishing a
  contactor-open state caused by a fault from one caused by planned de-energization) once real
  cabinet firmware documentation exists; `cabinet_fault_code` is available on the same row for
  a future revision to fold in, but this ticket does not attempt that inference.

Mode on a gap bucket - forward-fill within the bucket's own hour, documented
----------------------------------------------------------------------------------
When no reading lands in a bucket, the device did not stop existing - its last known state is
still the best available guess, so a gap bucket's `mode` forward-fills the previous bucket's
mode (`GridBucket.mode` of the most recent `measured` bucket seen so far) rather than being left
null or `"unknown"` for the whole gap. The one exception: a gap bucket with NO measured bucket
before it yet (the range's first reading hasn't happened) has nothing to forward-fill from, so
its `mode` is `None`. This forward-fill deliberately does NOT cross a call's own
`range_start_ms`/`range_end_ms` boundary - `build_grid` never looks at rows outside the range it
was given. That matters for `rebuild_dirty_grid_buckets`, which builds one grid per dirty
device-HOUR (P1-07's own dirty-key grain, CLAUDE.md invariant 5): reaching into a previous,
non-dirty hour's last mode to seed forward-fill would mean touching data outside what's dirty,
which is exactly what invariant 5 says not to do. A gap at the very start of a rebuilt hour is
therefore `None`, even if the previous (untouched) hour ended in a known mode - a real system
wiring this up for continuous per-device dashboards would carry that state across hours by
threading the previous grid's last mode in explicitly (this module does not do that; out of
scope here, see build_grid's `initial_mode` parameter for the seam a caller could use to do it).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from typing import Any

from pipeline.stage1_parsed.dirty_keys import DirtyKeysTable, DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import (
    DeviceHourKey,
    Stage1MergeStore,
    device_hour_key,
)
from pipeline.stage2_canonical.canonicalize import CanonicalRow, Catalog
from pipeline.stage2_canonical.targeted_recompute import (
    TargetedRecomputeResult,
    recompute_dirty_device_hours,
)

# One minute, in milliseconds - the "1-min others" grid width from CLAUDE.md's Stage 3a
# description. See module docstring's scope note: this is the only bucket width this module
# implements; the "5-min Powerwall/Powerpack" half of that same sentence has no fixtures to
# build or test against in this repo yet.
BUCKET_MS = 60_000

ONE_HOUR_MS = 3_600_000

# Device classes this module actually has catalog coverage and real fixtures for - see the
# module docstring's scope note. Building a grid for anything else raises
# UnsupportedDeviceClassError rather than silently producing a mode-less/garbage grid.
SUPPORTED_DEVICE_CLASSES = frozenset({"supercharger_stall", "supercharger_cabinet"})

MEASURED = "measured"
GAP = "gap"


class TimeGridError(Exception):
    """Base class for Stage 3a time grid errors."""


class UnsupportedDeviceClassError(TimeGridError):
    """Raised when asked to build a grid for a device_class this module doesn't define a mode
    for - see the module docstring's scope note. Not a silent skip: CLAUDE.md names Powerwall/
    Powerpack as in-scope for Stage 3a eventually, so a caller that reaches this module with one
    needs to know plainly that no grid logic exists for it yet, rather than getting an empty or
    mode-less grid back.
    """


def bucket_start_ms(device_ts_ms: int, *, bucket_ms: int = BUCKET_MS) -> int:
    """Floor `device_ts_ms` (an event-time epoch-ms timestamp) to the start of its bucket.

    Always derived from event time, never arrival time (CLAUDE.md invariant 4) - callers pass
    in a CanonicalRow's `device_ts_ms`, which is Stage 1/2's event-time field, unchanged by
    Stage 2 canonicalization (see canonicalize.py's IDENTITY_FIELDS).
    """
    return (device_ts_ms // bucket_ms) * bucket_ms


def _mode_for_fields(fields: dict[str, Any]) -> str:
    """The mode label for one canonicalized row's `fields` - see module docstring's "Mode"
    section for the per-device-class definitions and the cabinet judgment call.

    Raises UnsupportedDeviceClassError for a device_class this module has no mode definition
    for, and KeyError if a supported device_class's row is missing the specific catalog field
    its mode depends on (a canonicalization or catalog bug upstream, not something to paper
    over here with a fallback value).
    """
    device_class = fields.get("device_class")
    if device_class == "supercharger_stall":
        return fields["session_state"]
    if device_class == "supercharger_cabinet":
        return "energized" if fields["contactor_closed"] else "de_energized"
    raise UnsupportedDeviceClassError(
        f"no mode definition for device_class={device_class!r} - see time_grid.py's module "
        "docstring scope note (only supercharger_stall/supercharger_cabinet are implemented; "
        "Powerwall/Powerpack have no fixtures in this repo yet)"
    )


@dataclasses.dataclass(frozen=True)
class GridBucket:
    """One Stage 3a grid bucket: a device's coverage/mode state over one BUCKET_MS-wide window
    of event time.

    `reading_count` is how many canonical rows actually landed in this bucket (0 for a `gap`
    bucket, 1 in the common case, >1 when the "multiple readings in one bucket" case in the
    module docstring applies). `mode` is `None` only for a `gap` bucket that precedes any
    `measured` bucket in the same build_grid() call - see module docstring's forward-fill
    section.
    """

    device_id: str
    device_class: str
    bucket_start_ms: int
    coverage: str  # MEASURED or GAP
    mode: str | None
    reading_count: int


def build_grid(
    rows: Iterable[CanonicalRow],
    *,
    device_id: str,
    device_class: str,
    range_start_ms: int,
    range_end_ms: int,
    bucket_ms: int = BUCKET_MS,
    initial_mode: str | None = None,
) -> tuple[GridBucket, ...]:
    """Build a Stage 3a 1-minute grid for one device over [range_start_ms, range_end_ms).

    `rows` must all belong to `device_id` (a row for a different device_id is a caller bug and
    raises ValueError - this function does not silently filter, the same "fail loudly on
    misuse" spirit as merge.py's InvalidEventTimestampError) and, in this repo as scoped, all
    share `device_class` (mismatches likewise raise ValueError). A row whose bucket falls
    outside [range_start_ms, range_end_ms) is ignored - `build_grid` only ever reports on the
    range it was asked for.

    `range_end_ms - range_start_ms` must be an exact multiple of `bucket_ms` (default: whole
    minutes) - a partial trailing bucket would silently misrepresent coverage for time that was
    never actually in scope.

    `initial_mode` seeds mode forward-fill for a gap bucket at the very start of the range (see
    module docstring's forward-fill section) - a caller stitching consecutive ranges together
    (e.g. across hour boundaries) may pass in the previous range's last known mode; the default
    `None` means "nothing known yet", producing `mode=None` for a leading gap.

    Raises UnsupportedDeviceClassError via `_mode_for_fields` if `device_class` isn't one this
    module defines a mode for (see SUPPORTED_DEVICE_CLASSES), even when `rows` is empty (an
    all-gap grid still needs to know how a real reading would be interpreted upstream, and a
    caller asking for a grid over an unsupported device_class needs to fail loudly rather than
    get back a mode-less/silently-ignorable grid).
    """
    if device_class not in SUPPORTED_DEVICE_CLASSES:
        raise UnsupportedDeviceClassError(
            f"no mode definition for device_class={device_class!r} - see time_grid.py's module "
            "docstring scope note"
        )
    if range_end_ms <= range_start_ms:
        raise ValueError(f"range_end_ms ({range_end_ms}) must be > range_start_ms ({range_start_ms})")
    if (range_end_ms - range_start_ms) % bucket_ms != 0:
        raise ValueError(
            f"range [{range_start_ms}, {range_end_ms}) is not a whole number of "
            f"{bucket_ms}ms buckets"
        )

    by_bucket: dict[int, list[CanonicalRow]] = {}
    for row in rows:
        row_device_id = row.fields.get("device_id")
        row_device_class = row.fields.get("device_class")
        if row_device_id != device_id:
            raise ValueError(
                f"build_grid() called for device_id={device_id!r} but received a row for "
                f"device_id={row_device_id!r} - callers must pre-filter rows to one device"
            )
        if row_device_class != device_class:
            raise ValueError(
                f"build_grid() called with device_class={device_class!r} but received a row "
                f"with device_class={row_device_class!r}"
            )

        b = bucket_start_ms(row.fields["device_ts_ms"], bucket_ms=bucket_ms)
        if not (range_start_ms <= b < range_end_ms):
            continue
        by_bucket.setdefault(b, []).append(row)

    buckets: list[GridBucket] = []
    last_known_mode = initial_mode
    for b in range(range_start_ms, range_end_ms, bucket_ms):
        bucket_rows = by_bucket.get(b)
        if not bucket_rows:
            buckets.append(GridBucket(device_id, device_class, b, GAP, last_known_mode, 0))
            continue

        # Multiple readings in one bucket: latest wins - see module docstring.
        latest_row = max(bucket_rows, key=lambda r: r.fields["device_ts_ms"])
        mode = _mode_for_fields(latest_row.fields)
        last_known_mode = mode
        buckets.append(
            GridBucket(device_id, device_class, b, MEASURED, mode, len(bucket_rows))
        )

    return tuple(buckets)


def event_hour_range_ms(event_hour: str) -> tuple[int, int]:
    """[start_ms, end_ms) in epoch-ms for a P1-07-style event_hour string ("YYYY-MM-DDTHH",
    UTC - see merge.py's device_hour_key()), i.e. exactly the one-hour window a dirty
    (device_id, event_hour) key covers.
    """
    hour_start = dt.datetime.strptime(event_hour, "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc)
    start_ms = int(hour_start.timestamp() * 1000)
    return start_ms, start_ms + ONE_HOUR_MS


@dataclasses.dataclass(frozen=True)
class DeviceHourGridResult:
    """Per-(device_id, event_hour) grid rebuilt by one rebuild_dirty_grid_buckets() call."""

    key: DeviceHourKey
    device_id: str
    device_class: str | None
    buckets: tuple[GridBucket, ...]
    rows_seen: int
    rows_failed: int


@dataclasses.dataclass(frozen=True)
class GridRebuildResult:
    """Summary of one rebuild_dirty_grid_buckets() call, mirroring TargetedRecomputeResult's /
    MergeResult's counter style.

    `recompute` is the underlying P1-13 TargetedRecomputeResult this call produced its grid
    from - kept alongside rather than flattened away, so a caller can still see per-row cast
    failures (rows that never made it into any bucket because Stage 2 canonicalization itself
    failed for them - see DeviceHourGridResult.rows_failed for the per-key count of the same
    thing).
    """

    device_hours_seen: int
    device_hours_rebuilt: int
    buckets: tuple[GridBucket, ...]
    per_key: dict[DeviceHourKey, DeviceHourGridResult]
    recompute: TargetedRecomputeResult

    def reconciles(self) -> bool:
        """True iff every dirty device-hour this pass saw got exactly one grid-rebuild
        attempt - never partial, never skipped. Mirrors TargetedRecomputeResult.reconciles()."""
        return self.device_hours_seen == self.device_hours_rebuilt


def rebuild_dirty_grid_buckets(
    store: Stage1MergeStore | DirtyKeyTrackingStore,
    dirty_keys: DirtyKeysTable,
    catalog: Catalog,
    *,
    bucket_ms: int = BUCKET_MS,
) -> GridRebuildResult:
    """Rebuild Stage 3a grid buckets ONLY for the currently-dirty (device_id, event_hour) keys
    in `dirty_keys` - the same targeted-only property CLAUDE.md invariant 5 requires of Stage 2
    (P1-13), one stage further.

    Composes directly with P1-13's `recompute_dirty_device_hours`: that call already does the
    dirty-key scoping (pull only each dirty key's rows out of `store`, canonicalize only those,
    acknowledge the keys once done - see targeted_recompute.py) and returns freshly-recomputed
    CanonicalRow objects for exactly the dirty hours, never the whole store. This function
    groups those rows back by (device_id, event_hour) (reusing merge.py's `device_hour_key`,
    applied to each CanonicalRow's `fields` - which still carries `device_id`/`device_ts_ms`
    unchanged as identity fields, see canonicalize.py) and builds one full-hour, 60-bucket grid
    per dirty key via `build_grid`, over exactly that key's [event_hour, event_hour + 1h) range.

    A dirty key whose rows ALL failed Stage 2 cast (rows_failed == rows_seen for that key, see
    DeviceHourRecomputeResult) has no successfully canonicalized row to read a device_class off
    of, so its grid is left empty (`buckets=()`, `device_class=None` in the per-key result)
    rather than guessed at - it is still counted as "seen"/"rebuilt" (a rebuild was attempted;
    P1-13 already acknowledged the dirty key), the same "attempted, not silently dropped" stance
    targeted_recompute.py takes for a cast failure.

    Returns a GridRebuildResult a caller can verify against; never raises for a per-row cast
    failure (delegated to `recompute_dirty_device_hours`, which already doesn't raise for one).
    """
    recompute_result = recompute_dirty_device_hours(store, dirty_keys, catalog)

    rows_by_key: dict[DeviceHourKey, list[CanonicalRow]] = {}
    for row in recompute_result.rows:
        key = device_hour_key(row.fields)
        rows_by_key.setdefault(key, []).append(row)

    per_key: dict[DeviceHourKey, DeviceHourGridResult] = {}
    all_buckets: list[GridBucket] = []

    # Iterate the keys recompute_dirty_device_hours() actually attempted (recompute_result.
    # per_key), not rows_by_key's keys - a key with zero successfully-canonicalized rows still
    # needs a (empty) result entry, and must never be silently absent from `per_key`.
    for key, recompute_per_key in recompute_result.per_key.items():
        device_id, event_hour = key
        rows_for_key = rows_by_key.get(key, ())

        if rows_for_key:
            device_class = rows_for_key[0].fields["device_class"]
            range_start_ms, range_end_ms = event_hour_range_ms(event_hour)
            buckets = build_grid(
                rows_for_key,
                device_id=device_id,
                device_class=device_class,
                range_start_ms=range_start_ms,
                range_end_ms=range_end_ms,
                bucket_ms=bucket_ms,
            )
        else:
            device_class = None
            buckets = ()

        per_key[key] = DeviceHourGridResult(
            key=key,
            device_id=device_id,
            device_class=device_class,
            buckets=buckets,
            rows_seen=recompute_per_key.rows_seen,
            rows_failed=recompute_per_key.rows_failed,
        )
        all_buckets.extend(buckets)

    return GridRebuildResult(
        device_hours_seen=recompute_result.device_hours_seen,
        device_hours_rebuilt=len(per_key),
        buckets=tuple(all_buckets),
        per_key=per_key,
        recompute=recompute_result,
    )
