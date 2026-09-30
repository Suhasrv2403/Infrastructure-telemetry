"""Completeness and lateness sidecar, device x hour.

Ticket: P1-11 ("Completeness and lateness sidecar: device x hour"). CLAUDE.md describes this
as a sidecar living alongside Stage 2 Canonical: "same grain, canonical signals/units,
corrected event time, quality flags. Sidecar: completeness and lateness per device x hour."

What this computes, per (device_id, event_hour)
-------------------------------------------------
- `expected`: how many readings a device SHOULD have produced in that hour.
- `on_time`: readings that arrived at or before an "on-time" lateness threshold.
- `late`: readings that arrived, but past that threshold.
- `missing`: `max(expected - (on_time + late), 0)` - readings that should have shown up but
  haven't (yet - see "What 'missing' really means" below).

Grain: reuses the (device_id, event_hour) bucketing this repo already settled on in
pipeline/stage1_parsed/merge.py's `device_hour_key()` (P1-07, "dirty-keys table"), not a new
one invented here. `event_hour` is always derived from a reading's own `device_ts_ms` - never
from `arrival_ts_ms` - per CLAUDE.md invariant 4 ("never partition by device_id alone; event
time for Stage 1+").

Lineage note - why `device_hour_key` is reimplemented here instead of imported
--------------------------------------------------------------------------------
This ticket's branch is based on P1-08's tip, which - per this repo's actual (not idealized)
branch graph - predates P1-06 ("idempotent merge") and P1-07 ("dirty-keys table"): neither has
been merged into this branch's lineage yet, so `pipeline/stage1_parsed/merge.py` does not exist
on disk here to import from (confirmed: `git show P1-11-completeness-lateness-sidecar:
pipeline/stage1_parsed/merge.py` finds nothing; the function only exists on branch
`P1-07-dirty-keys-table`). Rather than invent a different hour-bucketing rule, `_event_hour()`
below reproduces P1-07's `device_hour_key()` algorithm exactly (same source field, same UTC
`"YYYY-MM-DDTHH"` string format, same "fail loudly on an unusable device_ts_ms" behavior) so
this sidecar's grain matches P1-06/P1-07's once those branches land. This local copy should be
deleted and replaced with a real import as soon as this branch is rebased past (or merged with)
P1-07 - it is a stopgap for branch ordering, not a deliberate second implementation.

Input shape
-----------
This module operates on plain `dict` rows carrying `device_id`, `device_ts_ms` and
`arrival_ts_ms` - nothing else. Both Stage 1 rows and Stage 2 `CanonicalRow.fields` dicts
satisfy this: `arrival_ts_ms` and `device_ts_ms` are identity fields
(pipeline/stage2_canonical/canonicalize.py's `IDENTITY_FIELDS`) that pass through
canonicalization completely unchanged, so it makes no difference to this sidecar whether it
runs on raw Stage 1 rows or canonicalized Stage 2 rows. It is documented here as living
alongside Stage 2 output (CLAUDE.md), so the expected caller passes `CanonicalRow.fields`
dicts, but nothing in this module actually depends on canonicalization having happened.

Lateness definition (reused from P0-08, not reinvented)
---------------------------------------------------------
profiling/lateness_duplicates/profiler.py (P0-08, "Lateness and duplicate profiler") already
defines `lateness_s = (arrival_ts_ms - device_ts_ms) / 1000` with a basic sanity guard
(`device_ts_ms` must be a plausible number, i.e. not `None` and not `0`, so a missing or
epoch-default clock doesn't poison the distribution with a multi-decade outlier). This module
reuses that exact formula and that exact guard - adapted here to also require a plausible
`arrival_ts_ms` per reading (P0-08 only needed a plausible `arrival_ts_ms` per *message*; this
module works from flattened per-reading rows that each carry their own `arrival_ts_ms`, so the
same guard is applied to both fields). A reading whose `arrival_ts_ms` or `device_ts_ms` isn't
plausible is excluded from `on_time`/`late` (its lateness can't be computed) but its bucket
still exists in the output with whatever `on_time`/`late` count its other readings produced;
excluding it from both is conservative (it will show up as increased `missing` rather than
being silently counted as on-time). This is expected to be rare in practice, since Stage 1/2
row identity fields are expected to already carry a real arrival timestamp set at Stage 0
capture time.

P0-08's docstring also carries an important, honest caveat worth restating here verbatim in
spirit: the synthetic fixture generator (tests/fixtures/generators/supercharger.py) anchors a
batch's `arrival_ts_ms` to its own *last* reading's `device_ts_ms` plus jitter, so a lot of
"lateness" visible in generator output is just batching position, not real network/device
lateness. That caveat is exactly why this module does NOT treat "any positive lateness" as
`late` - see the on-time threshold discussion below.

The hard, honest part: what "expected" means here (read before trusting these counts)
-----------------------------------------------------------------------------------------
"How many readings should this device have produced this hour" is a question this codebase
cannot answer with any real confidence yet. A genuine per-device reporting-cadence/history
dimension - the only thing that could ground "expected" in that device's *actual* observed
behavior - is P2-01, explicit future work, not built. Anything this module does here is
therefore a **documented simplification**, not a measurement:

    expected = round(3600 / expected_interval_s)

`expected_interval_s` is a required, explicit, caller-supplied parameter - never a value this
module tries to infer or look up - specifically so nobody mistakes it for ground truth. It
ships with a default (`DEFAULT_EXPECTED_INTERVAL_S`, see below) only so the sidecar is usable
out of the box; a caller who has better information (e.g. a real per-device-class SLA once one
exists) should pass their own value, and a caller integrating this with P2-01's device-history
dimension later should replace the constant default with an actual per-device lookup entirely.

Default (`DEFAULT_EXPECTED_INTERVAL_S = 15`): chosen from
`tests/fixtures/generators/supercharger.py`'s `GeneratorConfig.reading_interval_s` default
(15 seconds), the only reading-cadence number this repo currently expresses anywhere - it is
the generator's *modeled* Supercharger stall cadence, standing in for real device behavior the
same way P0-08's own docstring describes its profiler's findings as "illustrative until real
(or real-shaped) Stage 0 objects exist," not a measured production SLA. At 15s/reading this
gives `expected = 240` readings/hour. Treat this default as a placeholder to be replaced by
P2-01, not as a claim about what any real device actually does.

On-time threshold (`DEFAULT_ON_TIME_THRESHOLD_S = 180`): chosen to sit clearly above the
batching-only lateness scale P0-08's docstring works out for exactly this generator
configuration - "a stall reporting every 15s in batches of up to 12 will show up to ~165s of
'lateness' on its earliest batched reading under completely normal operation, with no network
delay or retry involved" (11 gaps of 15s between the earliest and last reading in a max-size
batch, `network_jitter_max_s` adding up to another 5s on top, i.e. up to ~170s of pure
batching-position "lateness"). 180s (3 minutes) rounds that up with a small margin, so normal
batching under this generator's config is never misclassified as `late`. This is explicitly a
threshold reasoned from *this repo's* observable batching shape, not a real network/SLA
number; a deployment with different batch sizes or intervals should pass its own
`on_time_threshold_s`.

What "missing" really means (read before treating it as ground truth)
---------------------------------------------------------------------
`missing` here means "not yet seen by this sidecar run," and nothing more. It can never fully
distinguish a reading that is truly, permanently missing (the device never sent it - dead
battery, decommissioned unit, permanent network loss) from one that simply hasn't landed *yet*
as of whenever this sidecar happened to run (ordinary late arrival, an outage the device is
still retrying through, a batch still in flight). CLAUDE.md invariant 6 says exactly this kind
of thing: "windows are provisional until the lateness horizon passes." Reconciling "missing for
now" into "confirmed missing" vs. "arrived after all, just later than this run looked" is what
the lateness horizon and P1-13's targeted dirty-key recompute exist for - re-running this
sidecar against a (device, hour) key once new data lands and comparing its output before/after
is *how* that reconciliation should eventually happen. It is not something this module
attempts on its own; it has no memory of a previous run and no way to know one happened.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections import Counter
from collections.abc import Iterable
from typing import Any

# Same reading-cadence number tests/fixtures/generators/supercharger.py's GeneratorConfig uses
# by default (reading_interval_s=15) - see module docstring's "Default" discussion. This is a
# placeholder standing in for the real per-device cadence dimension P2-01 will eventually add,
# not a measured production SLA.
DEFAULT_EXPECTED_INTERVAL_S: float = 15.0

# See module docstring's "On-time threshold" discussion: comfortably above the ~165-170s of
# pure batching-position "lateness" P0-08's docstring works out for this generator's default
# batch size (12) and reading interval (15s), plus jitter (up to 5s).
DEFAULT_ON_TIME_THRESHOLD_S: float = 180.0

# The (device_id, event_hour) grain - mirrors pipeline/stage1_parsed/merge.py's
# DeviceHourKey/device_hour_key() (P1-07) exactly; see module docstring's lineage note on why
# it's reproduced here rather than imported.
DeviceHourKey = tuple[Any, str]


class CompletenessSidecarError(Exception):
    """Base class for errors raised by this module."""


class InvalidEventTimestampError(CompletenessSidecarError):
    """Raised by `_event_hour()` when a row's device_ts_ms isn't a usable epoch-ms int.

    Mirrors pipeline/stage1_parsed/merge.py's error of the same name/purpose (see module
    docstring's lineage note): timestamp sanity is P1-05's job, not this module's - a row
    reaching this sidecar is assumed to already carry a usable device_ts_ms, so one that
    doesn't fails loudly here rather than landing in a nonsense hour bucket
    (e.g. every zeroed-clock device silently piling into 1970-01-01T00).
    """


def _event_hour(row: dict[str, Any]) -> dt.datetime:
    """The row's event time, parsed from `device_ts_ms` - never from `arrival_ts_ms`
    (CLAUDE.md invariant 4). Matches pipeline/stage1_parsed/merge.py's `_event_time()`.
    """
    device_ts_ms = row.get("device_ts_ms")
    if not isinstance(device_ts_ms, int) or isinstance(device_ts_ms, bool):
        raise InvalidEventTimestampError(
            f"row for device_id={row.get('device_id')!r} has device_ts_ms={device_ts_ms!r}, "
            "not a usable epoch-ms int - timestamp sanity is P1-05's job, not this sidecar's"
        )
    return dt.datetime.fromtimestamp(device_ts_ms / 1000, tz=dt.timezone.utc)


def device_hour_key(row: dict[str, Any]) -> DeviceHourKey:
    """The (device_id, event_hour) key for one row.

    `event_hour` is formatted as "YYYY-MM-DDTHH" in UTC, derived from `device_ts_ms` - never
    `arrival_ts_ms` - identical to pipeline/stage1_parsed/merge.py's `device_hour_key()` (see
    module docstring's lineage note on why this is a local copy, not an import).

    Raises InvalidEventTimestampError under the same conditions as merge.py's version.
    """
    event_time = _event_hour(row)
    return (row["device_id"], f"{event_time:%Y-%m-%dT%H}")


def _is_plausible_ts(value: Any) -> bool:
    """Same sanity guard as profiling/lateness_duplicates/profiler.py's
    `_has_plausible_device_ts()`: excludes missing (`None`) and epoch-default (`0`) timestamps,
    which would otherwise poison a lateness computation with either a crash or a multi-decade
    outlier. See module docstring's "Lateness definition" section for why this is applied to
    both `device_ts_ms` and `arrival_ts_ms` here.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _lateness_s(row: dict[str, Any]) -> float | None:
    """(arrival_ts_ms - device_ts_ms) / 1000, or None if either timestamp isn't plausible.

    Reuses profiling/lateness_duplicates/profiler.py's (P0-08) exact lateness formula - see
    module docstring. A negative value (arrival before the device's own clock says the
    reading happened - clock skew, not something this module tries to correct; that's P1-09)
    is left as-is, same as P0-08: it's still "on time" by any reasonable threshold, not an
    error condition to filter out.
    """
    arrival_ts_ms = row.get("arrival_ts_ms")
    device_ts_ms = row.get("device_ts_ms")
    if not _is_plausible_ts(arrival_ts_ms) or not _is_plausible_ts(device_ts_ms):
        return None
    return (arrival_ts_ms - device_ts_ms) / 1000.0


@dataclasses.dataclass(frozen=True)
class CompletenessLateness:
    """Completeness/lateness counts for one (device_id, event_hour) bucket.

    See module docstring for what each field means and, importantly, what `expected` and
    `missing` do NOT claim to know for certain (this is a documented simplification pending
    P2-01, not a measurement).
    """

    device_id: Any
    event_hour: str
    expected: int
    on_time: int
    late: int
    missing: int

    def reconciles(self) -> bool:
        """True iff `missing` is exactly consistent with how this module derives it:
        `missing == max(expected - (on_time + late), 0)`.

        This is a definitional sanity check on the four counts' internal consistency (would
        catch a bug in how `missing` was computed) - it is deliberately the exact equality,
        not the weaker `on_time + late + missing >= expected`, because that inequality holds
        trivially for any `missing` computed via `max(..., 0)` and so wouldn't actually catch
        anything going wrong. It is not, and can't be, a check against ground truth - see the
        module docstring's "What 'missing' really means" section.
        """
        return self.missing == max(self.expected - (self.on_time + self.late), 0)


def compute_completeness_lateness(
    rows: Iterable[dict[str, Any]],
    *,
    expected_interval_s: float = DEFAULT_EXPECTED_INTERVAL_S,
    on_time_threshold_s: float = DEFAULT_ON_TIME_THRESHOLD_S,
) -> dict[DeviceHourKey, CompletenessLateness]:
    """Compute per-(device_id, event_hour) completeness/lateness counts over `rows`.

    `expected_interval_s`: the assumed seconds-between-readings used to derive `expected =
    round(3600 / expected_interval_s)`. This is an explicit, caller-supplied parameter, not
    something this function infers - see module docstring for why, and for the meaning of the
    default. Must be > 0.

    `on_time_threshold_s`: a reading with lateness_s <= this value counts as `on_time`;
    greater counts as `late`. See module docstring for how the default was chosen.

    A bucket only appears in the result if at least one row's device_hour_key() resolved to
    it - there is no way to enumerate "every device that should exist" without a real device
    registry (P2-01), so a device-hour with zero landed readings at all can't appear here as
    all-missing; it simply isn't represented. This mirrors this module's `missing` semantics:
    it can only describe gaps relative to readings it has actually seen.

    Raises InvalidEventTimestampError if any row's device_ts_ms isn't a usable epoch-ms int
    (see device_hour_key()). Raises ValueError if `expected_interval_s` isn't positive.
    """
    if expected_interval_s <= 0:
        raise ValueError(f"expected_interval_s must be > 0, got {expected_interval_s!r}")

    expected = round(3600 / expected_interval_s)

    on_time_counts: Counter[DeviceHourKey] = Counter()
    late_counts: Counter[DeviceHourKey] = Counter()
    keys_seen: set[DeviceHourKey] = set()

    for row in rows:
        key = device_hour_key(row)
        keys_seen.add(key)

        lateness_s = _lateness_s(row)
        if lateness_s is None:
            # Can't tell whether this reading was on time or late - excluded from both
            # rather than guessed at. See module docstring's "Lateness definition" section.
            continue
        if lateness_s <= on_time_threshold_s:
            on_time_counts[key] += 1
        else:
            late_counts[key] += 1

    results: dict[DeviceHourKey, CompletenessLateness] = {}
    for key in keys_seen:
        device_id, event_hour = key
        on_time = on_time_counts[key]
        late = late_counts[key]
        missing = max(expected - (on_time + late), 0)
        results[key] = CompletenessLateness(
            device_id=device_id,
            event_hour=event_hour,
            expected=expected,
            on_time=on_time,
            late=late,
            missing=missing,
        )
    return results
