"""Targeted Stage 2 recompute from Stage 1's dirty (device, hour) keys.

Ticket: P1-13 ("Targeted Stage 2 recompute from dirty keys"). CLAUDE.md invariant 5: "Late
data recomputes only dirty (device, hour) keys. Full-partition recompute only via an explicit,
reviewed backfill job." P1-07's dirty_keys.py already tells us *which* (device_id, event_hour)
keys need recompute after late Stage 1 data lands; P1-08's canonicalize.py already knows how to
turn raw Stage 1 rows into canonical Stage 2 rows. Neither module talks to the other. This
module is the missing wire between them: for each currently-dirty key, pull only that
device-hour's rows out of the Stage 1 store and canonicalize only those - never the whole
store - then acknowledge the keys it just processed so a caller can loop this without
reprocessing the same keys forever.

Cross-branch provenance note (read this before assuming canonicalize.py always lived here)
------------------------------------------------------------------------------------------
This worktree was branched from P1-07's tip (commit f6b7999), which predates P1-08 (Stage 2
canonicalization, built on a sibling branch). `pipeline/stage2_canonical/canonicalize.py`,
`catalog/signals.yaml` and `catalog/README.md` in this branch are therefore copied verbatim
from branch `P1-08-stage2-canonicalization` (commit ea98bf8), the same cross-lineage pattern
P1-08 itself used to pull P0-10's catalog, and P1-05 used to pull P0-07's profiler code. Nothing
in those three files was authored by this ticket; do not credit P1-13 with Stage 2
canonicalization itself.

What this module is NOT
------------------------
- Not a Dagster asset or any orchestration/scheduling wiring. Like merge.py and
  canonicalize.py before it, this is the storage-agnostic "pure core": a plain function other
  code will eventually call from a scheduled job. See merge.py's own module docstring for the
  fuller version of this argument.
- Not a change to Stage1MergeStore, DirtyKeysTable or DirtyKeyTrackingStore. Those are read via
  their existing public API only (`rows()`, `keys()`, `acknowledge()`/`clear()`).
- Not P1-09 (clock-offset correction), P1-10 (row-level quality flags) or P1-11 (completeness/
  lateness sidecar) - three sibling Stage 2 tickets built independently on their own branches.
  A row coming out of this module's recompute is exactly what `canonicalize_row` produces: raw
  `device_ts_ms`, no quality-flag fields, nothing sidecar-related.

How a device-hour's rows are found
-----------------------------------
Stage1MergeStore only exposes `rows()` (a snapshot of everything, keyed internally by natural
key), `get()` (by natural key) and `__contains__` (by natural key) - there is no index from a
(device_id, event_hour) key to "the rows in it". `rows_for_device_hour` below does an O(n) scan
of `store.rows()` per dirty key, filtering by `device_hour_key(row) == key`. That is an
acceptable in-process stand-in here, same spirit as Stage1MergeStore itself being a plain dict
rather than a real Iceberg table (see merge.py's module docstring) - a real implementation
against Iceberg would push this down to a partition/file-level predicate instead of scanning
every row in memory.

Acknowledging dirty keys
-------------------------
A dirty key is acknowledged (removed from the DirtyKeysTable) once this module has attempted to
recompute it, regardless of whether every row in it actually canonicalized. That mirrors
canonicalize_rows' own "one bad row degrades to a per-row failure, it never aborts the batch"
rule at the device-hour level: a device-hour with one row whose value won't cast to its
catalog type is not something re-running the exact same recompute will ever fix, so leaving it
dirty forever would loop without making progress. The per-row failure is still visible in the
result's `failed_rows` / per-key breakdown for whoever reviews recompute output - it is not
silently dropped, it is just not going to un-stick itself by staying dirty.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from pipeline.stage1_parsed.dirty_keys import DirtyKeysTable, DirtyKeyTrackingStore
from pipeline.stage1_parsed.merge import DeviceHourKey, Stage1MergeStore, device_hour_key
from pipeline.stage2_canonical.canonicalize import (
    CanonicalRow,
    CastError,
    Catalog,
    canonicalize_rows,
)


def rows_for_device_hour(
    store: Stage1MergeStore | DirtyKeyTrackingStore, key: DeviceHourKey
) -> tuple[dict[str, Any], ...]:
    """Every row currently in `store` belonging to the (device_id, event_hour) key `key`.

    O(n) scan of `store.rows()` - see the module docstring's "How a device-hour's rows are
    found" section for why that's an acceptable stand-in here. Accepts either a bare
    Stage1MergeStore or a DirtyKeyTrackingStore wrapping one; both expose `rows()`.
    """
    return tuple(row for row in store.rows() if device_hour_key(row) == key)


@dataclasses.dataclass(frozen=True)
class DeviceHourRecomputeResult:
    """Per-(device_id, event_hour) breakdown of one targeted recompute pass."""

    key: DeviceHourKey
    rows_seen: int
    rows_recomputed: int
    rows_failed: int

    def reconciles(self) -> bool:
        """True iff every row seen for this key was either recomputed or recorded as a cast
        failure - never both, never neither. Mirrors CanonicalizeResult.reconciles()."""
        return self.rows_seen == self.rows_recomputed + self.rows_failed


@dataclasses.dataclass(frozen=True)
class TargetedRecomputeResult:
    """Summary of one recompute_dirty_device_hours() call, mirroring MergeResult's /
    CanonicalizeResult's counter style.

    `device_hours_seen` and `device_hours_recomputed` are both the count of dirty keys this
    pass attempted - kept as two separate fields (rather than one) so a caller reads "how many
    device-hours were dirty going in" and "how many did the recompute pass actually process"
    as the same number by construction: this module never partially processes a dirty key's
    keys and never recomputes a key nobody asked for. Zero dirty keys in means zero out, and
    that equality holds either way.
    """

    device_hours_seen: int
    device_hours_recomputed: int
    rows_seen: int
    rows: tuple[CanonicalRow, ...]
    failed_rows: tuple[tuple[dict[str, Any], CastError], ...]
    per_key: dict[DeviceHourKey, DeviceHourRecomputeResult]

    @property
    def rows_recomputed(self) -> int:
        return len(self.rows)

    @property
    def rows_failed(self) -> int:
        return len(self.failed_rows)

    def reconciles(self) -> bool:
        """True iff every row seen across all dirty device-hours this pass touched was either
        successfully recomputed or recorded as a per-row cast failure - never both, never
        neither. Mirrors CanonicalizeResult.reconciles() / MergeResult.reconciles()."""
        return self.rows_seen == self.rows_recomputed + self.rows_failed


def recompute_dirty_device_hours(
    store: Stage1MergeStore | DirtyKeyTrackingStore,
    dirty_keys: DirtyKeysTable,
    catalog: Catalog,
) -> TargetedRecomputeResult:
    """Recompute Stage 2 canonical output ONLY for the rows belonging to the currently-dirty
    (device_id, event_hour) keys in `dirty_keys` - never the whole store (CLAUDE.md
    invariant 5).

    For each dirty key: pull that device-hour's rows out of `store` (ALL of them currently in
    the store for that key, not just whatever was newly inserted by the late merge that made it
    dirty - a real recompute of a device-hour redoes the whole hour, not just the delta) and run
    them through P1-08's `canonicalize_rows`. A row that fails to cast degrades to a per-row
    failure exactly as `canonicalize_rows` already does - it never aborts this pass, and never
    aborts processing of the other dirty keys in the same call.

    Snapshots `dirty_keys.keys()` once at the start and acknowledges exactly that snapshot when
    done - a key marked dirty by some concurrent merge() call *during* this pass is correctly
    left dirty for the next call, never lost.

    Returns a TargetedRecomputeResult a caller can log/verify against, and never raises for a
    per-row cast failure (see CastError) - only a genuinely unexpected error (e.g. store/catalog
    misuse) propagates.
    """
    keys = tuple(dirty_keys.keys())

    canonical_rows: list[CanonicalRow] = []
    failed_rows: list[tuple[dict[str, Any], CastError]] = []
    per_key: dict[DeviceHourKey, DeviceHourRecomputeResult] = {}
    rows_seen_total = 0

    for key in keys:
        rows = rows_for_device_hour(store, key)
        result = canonicalize_rows(list(rows), catalog)

        per_key[key] = DeviceHourRecomputeResult(
            key=key,
            rows_seen=result.rows_seen,
            rows_recomputed=result.rows_canonicalized,
            rows_failed=result.rows_failed,
        )
        rows_seen_total += result.rows_seen
        canonical_rows.extend(result.rows)
        failed_rows.extend(result.failed_rows)

    # Acknowledge exactly the keys this pass attempted - see module docstring on why this
    # happens even for a key with per-row cast failures.
    dirty_keys.acknowledge(keys)

    return TargetedRecomputeResult(
        device_hours_seen=len(keys),
        device_hours_recomputed=len(keys),
        rows_seen=rows_seen_total,
        rows=tuple(canonical_rows),
        failed_rows=tuple(failed_rows),
        per_key=per_key,
    )
