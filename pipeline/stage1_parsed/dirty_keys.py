"""Stage 1 dirty-keys tracking: record which (device_id, event_hour) keys a late merge touched.

Ticket: P1-07 ("Dirty-keys table from incremental changes").

CLAUDE.md invariant 5: "Late data recomputes only dirty (device, hour) keys. Full-partition
recompute only via an explicit, reviewed backfill job." This module is what gives a later,
targeted-recompute job (P1-13, not this ticket) a precise list of what actually needs
recomputing after late data lands, instead of recomputing an entire partition on every merge.

Design decision - wrapper around Stage1MergeStore, not a change to MergeResult
-------------------------------------------------------------------------------
P1-06's Stage1MergeStore.merge() already returns everything needed to compute dirty keys
(keys_inserted, itself made of (device_id, device_ts_ms, payload_hash) tuples) without needing
to touch merge.py's public return type. Rather than growing MergeResult with a `dirty_keys`
field - which would make every caller of merge() pay for dirty-key bookkeeping whether or not
it wants it, and would fork the "is this the right natural key logic" surface between two
modules - this module wraps a Stage1MergeStore with an observer: DirtyKeyTrackingStore.merge()
delegates to the wrapped store's merge() unchanged (same MergeResult comes back untouched) and
separately records which (device_id, event_hour) keys the call touched that were already
populated before this call. merge.py stays exactly as P1-06 left it, other than one small,
genuinely shared refactor (see below).

What "late" means here (see the ticket brief) - only this case is dirty
-------------------------------------------------------------------------
A merge call inserts new rows into a (device_id, event_hour) that already had at least one row
from an EARLIER merge call (any earlier call, not just the immediately preceding one - see
DirtyKeyTrackingStore.merge()'s docstring for how that's checked). That's "late" data landing
after something downstream may already have processed that device-hour once.

The reverse - a merge call inserts the very first rows ever seen for a (device_id, event_hour)
- is NOT late. Nothing downstream could have processed a device-hour that never had data before,
so there is nothing to mark dirty. A row that is already present (rows_already_present, a pure
idempotent replay) never marks anything dirty either - nothing actually changed.

Reuses merge.py's hour bucketing (invariant 4: event time, never arrival time)
---------------------------------------------------------------------------------
`event_hour` here is exactly merge.py's device_hour_key()'s event_hour - derived from a row's
device_ts_ms via merge.py's _event_time(), the same logic partition_key() uses. This module
never reimplements hour bucketing; see device_hour_key_from_natural_key() below, which builds
on merge.py's natural_key/device_hour_key rather than re-deriving event time itself.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pipeline.stage1_parsed.merge import (
    DeviceHourKey,
    MergeResult,
    NaturalKey,
    Stage1MergeStore,
    device_hour_key,
)


def device_hour_key_from_natural_key(key: NaturalKey) -> DeviceHourKey:
    """The (device_id, event_hour) key for a Stage 1 natural key (device_id, device_ts_ms,
    payload_hash) - e.g. one of MergeResult.keys_inserted - without needing the full row dict.

    device_ts_ms is already embedded in the natural key (index 1), so this builds the minimal
    pseudo-row merge.py's device_hour_key() needs and calls straight into it - reusing its
    event-time parsing rather than re-deriving an hour bucket here.
    """
    device_id, device_ts_ms, _payload_hash = key
    return device_hour_key({"device_id": device_id, "device_ts_ms": device_ts_ms})


class DirtyKeysTable:
    """An idempotent set of (device_id, event_hour) keys awaiting targeted downstream recompute.

    A set, not a list, matching Stage 1's own idempotency ethos (Stage1MergeStore holds at most
    one row per natural key): marking the same key dirty any number of times, from any number of
    separate late merges, leaves exactly one entry for it. Keys stay dirty until whatever
    consumes them (P1-13's targeted Stage 2 recompute, not this ticket) acknowledges them via
    clear() - this table only ever grows on mark() and shrinks on clear(), it never silently
    drops a key on its own.
    """

    def __init__(self) -> None:
        self._dirty: set[DeviceHourKey] = set()

    def __len__(self) -> int:
        return len(self._dirty)

    def __contains__(self, key: DeviceHourKey) -> bool:
        return key in self._dirty

    def mark(self, keys: Iterable[DeviceHourKey]) -> None:
        """Add `keys` to the dirty set. Idempotent - a key already dirty is a no-op for it."""
        self._dirty.update(keys)

    def keys(self) -> frozenset[DeviceHourKey]:
        """A snapshot of every currently-dirty (device_id, event_hour) key."""
        return frozenset(self._dirty)

    def clear(self, keys: Iterable[DeviceHourKey] | None = None) -> None:
        """Acknowledge `keys` as recomputed, removing them from the dirty set. With no `keys`
        (the default), acknowledges everything currently dirty. Clearing a key that isn't
        (currently, or never was) dirty is a no-op for it, not an error - a recompute job
        racing a fresh mark(), or acknowledging a key twice, must never raise."""
        if keys is None:
            self._dirty.clear()
        else:
            self._dirty.difference_update(keys)

    # Alias: P1-13 (or any other consumer) may prefer "acknowledge" as the verb for "I recomputed
    # this, you can stop tracking it" - same operation as clear(), kept as one implementation so
    # the two names can never drift apart.
    acknowledge = clear


class DirtyKeyTrackingStore:
    """Wraps a Stage1MergeStore, observing each merge() call to feed a DirtyKeysTable.

    Every method other than merge() delegates straight through to the wrapped store - this is
    an observer, not a reimplementation of Stage1MergeStore's storage or dedup semantics.
    """

    def __init__(
        self,
        store: Stage1MergeStore | None = None,
        dirty_keys: DirtyKeysTable | None = None,
    ) -> None:
        self.store = store if store is not None else Stage1MergeStore()
        self.dirty_keys = dirty_keys if dirty_keys is not None else DirtyKeysTable()

    def __len__(self) -> int:
        return len(self.store)

    def __contains__(self, key: NaturalKey) -> bool:
        return key in self.store

    def get(self, key: NaturalKey) -> dict[str, Any] | None:
        return self.store.get(key)

    def rows(self) -> tuple[dict[str, Any], ...]:
        return self.store.rows()

    def merge(self, rows: Iterable[dict[str, Any]]) -> MergeResult:
        """Merge `rows` into the wrapped store, then mark any (device_id, event_hour) key this
        call inserted new rows into dirty, IF that device-hour already had at least one row
        from an earlier call - the same store.rows() the wrapped store has accumulated since it
        was created, not just what the immediately preceding merge() call touched. That's what
        makes this correct across more than two calls: a device-hour first populated in call 1,
        left untouched by call 2, then added to again in call 3 is still recognized as "already
        populated" in call 3, because store.rows() still holds call 1's rows (Stage1MergeStore
        never deletes).

        Returns the wrapped store's own MergeResult, unchanged - dirty-key bookkeeping is
        observed alongside the merge, not folded into its return value.
        """
        rows = list(rows)

        # Snapshot of every (device_id, event_hour) that had at least one row BEFORE this call,
        # from the wrapped store's full history (every prior merge() call, not just the last
        # one) - see the docstring above.
        already_populated_hours = {device_hour_key(row) for row in self.store.rows()}

        result = self.store.merge(rows)

        newly_dirty = {
            device_hour_key_from_natural_key(key)
            for key in result.keys_inserted
            if device_hour_key_from_natural_key(key) in already_populated_hours
        }
        if newly_dirty:
            self.dirty_keys.mark(newly_dirty)

        return result
