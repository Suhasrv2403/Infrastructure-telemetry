"""Stage 2: per-device clock-offset estimation and corrected event time.

Ticket: P1-09 ("Per-device clock offset and corrected event time"). Done when (Build
backlog.md): "Offset estimates stored; corrected time within agreed tolerance on test set."

Scope note - read this before touching row shape: CLAUDE.md describes Stage 2 as "same grain,
canonical signals/units, corrected event time, quality flags." pipeline/stage2_canonical/
canonicalize.py (P1-08) is ONLY the "canonical signals/units" part and is explicit that a
`CanonicalRow`/raw Stage 1 row coming out of it still carries `device_ts_ms` unmodified. THIS
module is the "corrected event time" part. It composes with canonicalize.py's output (or with
raw Stage 1 rows - see below) rather than modifying it: nothing here mutates `device_ts_ms`,
and nothing here touches canonicalize.py. Row-level quality flags are a sibling ticket, P1-10,
being built independently in its own worktree right now; this module does not implement it and
stays out of its way (no flag fields, no "is this reading good" judgment beyond the narrow
"do we have an offset estimate for this device yet" case described below).

Input rows: dict[str, Any] with at least `device_id`, `device_ts_ms`, `arrival_ts_ms`.
Both a raw Stage 1 row and a `CanonicalRow.fields` dict (from canonicalize_row) satisfy this -
IDENTITY_FIELDS in canonicalize.py passes `device_id`/`device_ts_ms`/`arrival_ts_ms` through
unchanged, so this module works the same whether it runs before or after P1-08's
canonicalization. It is written against plain dicts (not `CanonicalRow`) so it doesn't need to
import canonicalize.py at all, keeping the two modules decoupled per the module boundary above.

Method - per-device offset estimate:
Same spirit as profiling/clock_quality/profiler.py's `estimate_clock_drift` (P0-07): the median
of `arrival_ts_ms - device_ts_ms` across a device's readings, which is meant to average out
per-message queueing/network jitter (assumed roughly symmetric) and recover a device's
systematic clock skew. The difference from P0-07's version: that one reduces *all* devices'
per-device medians into one firmware-level `DriftDistribution` for reporting. This module keeps
one estimate *per device_id* (a `dict[device_id, offset_ms]`, via `ClockOffsetStore`) because
applying a correction needs a single number per device, not a fleet-wide distribution. Same
sign convention as P0-07: a positive offset means the device's clock runs *behind* arrival time
(device_ts_ms is smaller than arrival_ts_ms beyond what transit/queueing delay alone would
explain); a negative offset means it runs ahead. Corrected event time is therefore
`device_ts_ms + offset_ms`.

Known limitation this inherits from P0-07 (read before trusting these numbers, and before
writing tests against the real fixture generator): tests/fixtures/generators/supercharger.py
derives a batch's `arrival_ts_ms` from the *device's own* last (already clock-skewed)
`device_ts_ms`, not from an independent arrival clock, so `arrival_ts_ms - device_ts_ms` mostly
measures a reading's position within its batch rather than the injected per-device skew (see
estimate_clock_drift's own docstring - a measured, near-zero correlation, not a guess). That
generator bug is fixed on a separate branch (`fix-generator-independent-arrival-clock`), which
this ticket's lineage (branched from P1-08) does not include, and fixing it is explicitly out
of scope here. Consequence: tests/unit/test_clock_offset.py does NOT run this module against
the real generator to check offset recovery - it constructs rows directly with hand-picked,
independent `device_ts_ms`/`arrival_ts_ms` values instead. See that test module's docstring.

Where estimates are stored, and when they're expected to be recomputed:
`ClockOffsetStore` is an in-memory `dict[device_id, offset_ms]`. In production this is meant to
be recomputed periodically (e.g. a scheduled Dagster asset re-running `estimate_offsets` over
each device's recent - not all-time - readings, so a device's estimate can track genuine clock
drift/resync events rather than being permanently anchored to its first-ever batch) and
persisted somewhere durable (a small keyed table, not specified by this ticket). Scheduling
that recomputation and choosing a durable store are explicitly out of scope for this module;
it only provides the estimator, the store shape, and the correction function that consumes it.
"""
from __future__ import annotations

import dataclasses
import statistics
from typing import Any

MS_PER_S = 1000


def estimate_offsets(readings_by_device: dict[str, list[tuple[int, int]]]) -> dict[str, int]:
    """Estimate one clock offset (in ms) per device from its (device_ts_ms, arrival_ts_ms) pairs.

    `readings_by_device` maps device_id -> list of (device_ts_ms, arrival_ts_ms) tuples for
    that device's readings (typically its recent "clean" readings - callers that already have
    P1-10-style quality judgments, or P0-07-style missing/epoch-default/future filtering, should
    filter before calling this; this function makes no such judgment itself, per the module
    docstring's scope note).

    For each device, offset_ms = median(arrival_ts_ms - device_ts_ms) across its readings -
    same reasoning as profiler.py's estimate_clock_drift: the median averages out per-message
    jitter under the assumption it's roughly symmetric and small next to a systematic skew. A
    device with zero readings in its list contributes no entry (nothing to estimate from); a
    device with exactly one reading gets that single sample as its "median" (see
    test_clock_offset.py's single-reading case).

    Pure function: deterministic given its input, so feeding the same readings twice yields the
    same result (idempotent) - see test_clock_offset.py's idempotency test.
    """
    offsets_ms: dict[str, int] = {}
    for device_id, pairs in readings_by_device.items():
        if not pairs:
            continue
        samples = [arrival_ts_ms - device_ts_ms for device_ts_ms, arrival_ts_ms in pairs]
        offsets_ms[device_id] = round(statistics.median(samples))
    return offsets_ms


@dataclasses.dataclass(frozen=True)
class CorrectedTime:
    """Result of applying a device's stored offset to one row's `device_ts_ms`.

    `corrected_event_ts_ms` is the new field this ticket adds - it is NEVER written back onto
    `device_ts_ms` itself (canonicalize.py's module docstring is explicit that device_ts_ms
    must stay unmodified through Stage 2; see this module's own docstring). `offset_ms` is the
    offset actually applied (0 when unestimated - see `is_estimated`). `is_estimated` is False
    when the device had no stored offset at correction time (first time seen, or not yet
    recomputed): in that case `corrected_event_ts_ms` passes `device_ts_ms` through unchanged
    (offset treated as 0) rather than silently claiming a precision we don't have. Downstream
    consumers that care about this distinction (e.g. a P1-10-style quality flag, or an analyst
    deciding whether to trust corrected_event_ts_ms) can branch on `is_estimated`; this module
    does not itself add a quality-flag field, per the P1-10 scope boundary in the module
    docstring.
    """

    device_ts_ms: int
    corrected_event_ts_ms: int
    offset_ms: int
    is_estimated: bool


class ClockOffsetStore:
    """Holds the current per-device clock-offset estimates: dict[device_id, offset_ms].

    This is intentionally the simplest thing that satisfies this ticket's "done when" ("offset
    estimates stored"): an in-memory mapping, not a durable table or a scheduled job. See the
    module docstring's "Where estimates are stored" section for how this is expected to be used
    in production (periodic recomputation from recent readings, persisted externally) and what
    is explicitly out of scope here (the scheduling and the persistence layer).
    """

    def __init__(self, offsets_ms: dict[str, int] | None = None) -> None:
        self._offsets_ms: dict[str, int] = dict(offsets_ms) if offsets_ms else {}

    def get(self, device_id: str) -> int | None:
        """Return the stored offset for `device_id` in ms, or None if never estimated."""
        return self._offsets_ms.get(device_id)

    def has(self, device_id: str) -> bool:
        return device_id in self._offsets_ms

    def update(self, offsets_ms: dict[str, int]) -> None:
        """Merge newly (re)computed offsets in, overwriting any prior estimate per device_id.

        This is the hook a periodic recomputation job would call with `estimate_offsets`'s
        output - see the module docstring; this method itself does no scheduling, it just
        applies whatever offsets it's given.
        """
        self._offsets_ms.update(offsets_ms)

    def as_dict(self) -> dict[str, int]:
        """A copy of the current device_id -> offset_ms mapping."""
        return dict(self._offsets_ms)

    def correct_row(self, row: dict[str, Any]) -> CorrectedTime:
        """Apply this store's offset for `row["device_id"]` to `row["device_ts_ms"]`.

        Returns a CorrectedTime with `corrected_event_ts_ms = device_ts_ms + offset_ms`, where
        offset_ms is 0 and `is_estimated` is False when this device has no stored estimate yet
        (see CorrectedTime's docstring for why that's not silently pretended away). `row` itself
        is never mutated and `device_ts_ms` is never touched - this only reads the row and
        returns a new, separate result; callers are responsible for attaching
        `corrected_event_ts_ms` onto whatever row representation they use (e.g. adding it to a
        CanonicalRow.fields dict), not this function.
        """
        device_id = row["device_id"]
        device_ts_ms = row["device_ts_ms"]
        offset_ms = self._offsets_ms.get(device_id)
        is_estimated = offset_ms is not None
        applied_offset_ms = offset_ms if is_estimated else 0
        return CorrectedTime(
            device_ts_ms=device_ts_ms,
            corrected_event_ts_ms=device_ts_ms + applied_offset_ms,
            offset_ms=applied_offset_ms,
            is_estimated=is_estimated,
        )
