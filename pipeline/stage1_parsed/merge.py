"""Stage 1 idempotent merge: dedup Stage 1 rows onto (device_id, device_ts_ms, payload_hash).

Ticket: P1-06 ("Idempotent merge on natural key into event-time partitions").

Scope note - read this before assuming this module talks to Iceberg
------------------------------------------------------------------
CLAUDE.md's "Stack" section names Iceberg-on-object-storage + Spark as the eventual real
Stage 1 store, confirmed at a later gate - it is not available in this sandbox, and building a
real Iceberg `MERGE INTO` is out of scope for what P1-06 can prove here. This module is the
storage-agnostic "pure core" of the merge, the same separation pipeline/stage0_landing/
capture.py and parsers/framework.py keep from their own orchestration/storage wiring: it
implements the actual merge *semantics* - is this natural key already present, and if not,
insert it - against an in-process stand-in store (Stage1MergeStore, a plain dict keyed by the
natural key). That proves the merge logic itself is correct and idempotent. Wiring this (or an
equivalent) into a real Iceberg table - via Spark, or via Dagster plus an object-store-backed
table format - is separate follow-up work, not this ticket as scoped here.

What this is NOT
-----------------
- Not a Dagster asset (no orchestration wiring here - see pipeline/stage0_landing/
  dagster_assets.py for the shape that would eventually wrap this).
- Not timestamp sanity (P1-05, a sibling ticket): this module assumes the rows it's given
  already carry a usable `device_ts_ms` (a real epoch-ms int). A row with a missing/epoch-
  default/otherwise-invalid device_ts_ms is P1-05's problem to fix or flag before it reaches
  the partition-key function here, not something this module tries to detect or repair.
- Not conflict resolution for "same natural key, different field values". The natural key
  includes `payload_hash`, itself derived from the row's own field values (see
  tests/fixtures/generators/supercharger.py's `_payload_hash`): two rows sharing a natural key
  are true duplicates by construction (the same payload, retransmitted), never a real conflict
  to reconcile. If that assumption is ever wrong, it's a bug upstream (a parser or hash
  computation), not something for this module to paper over.

Invariant 2 (CLAUDE.md): "Stage 1 writes are MERGE on (device_id, device_ts, payload_hash).
Never a plain append." Invariant 4: "Never partition by device_id. Stage 0 by arrival time,
Stage 1+ by event time." Both are what this module exists to satisfy.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections.abc import Iterable
from typing import Any

# Natural key for the Stage 1 merge (invariant 2): a duplicate reading (same device, same
# event timestamp, same payload) collapses to one row no matter how many times or in what
# order it's merged in.
NaturalKey = tuple[Any, Any, Any]

# Number of device_id hash buckets within a (device_class, event_date, hour) partition. 16 is
# a deliberately modest choice for this stand-in: Stage 1's per-partition volume in this
# sandbox (synthetic Supercharger fixtures, ~tens of devices per firmware) doesn't need more
# spread to avoid oversized buckets, and a small, fixed power-of-two bucket count is easy to
# reason about and cheap to rebalance later (a real Iceberg partition spec would pick this
# based on measured per-partition row counts, e.g. targeting ~100-500MB per file - that
# measurement doesn't exist yet, so 16 is a placeholder, not a load-bearing constant).
DEVICE_BUCKET_COUNT = 16


class MergeError(Exception):
    """Base class for Stage 1 merge errors."""


class InvalidEventTimestampError(MergeError):
    """Raised by partition_key() when a row's device_ts_ms isn't a usable epoch-ms int.

    Timestamp sanity (deciding what to do with a missing/epoch-default/future device_ts_ms) is
    P1-05's job, not this module's - a row should already have a sane device_ts_ms by the time
    it reaches partition_key(). This error exists so a row that slips through without one fails
    loudly here rather than silently landing in a nonsense partition (e.g. epoch_date=1970-01-01
    for every device with a zeroed clock).
    """


def device_bucket(device_id: str, *, bucket_count: int = DEVICE_BUCKET_COUNT) -> int:
    """Deterministic device_id -> bucket assignment, stable across processes and runs.

    Uses sha1 (not Python's built-in hash()) because hash() is salted per-process for
    non-numeric types (PYTHONHASHSEED) - the whole point of bucketing here is that the same
    device_id lands in the same bucket every time, including across separate "runs" (backfills,
    re-merges), so a process-randomized hash would silently break that. sha1 is not used for
    anything security-sensitive here, only for its stable, well-distributed digest.

    This buckets *within* a (device_class, event_date, hour) partition - it is not itself a
    partition key and must never be used as one without the device_class/event-time
    components alongside it (invariant 4: never partition by device_id alone).
    """
    digest = hashlib.sha1(device_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % bucket_count


def natural_key(row: dict[str, Any]) -> NaturalKey:
    """The Stage 1 merge key for one row: (device_id, device_ts_ms, payload_hash).

    Two rows with the same natural key are treated as true duplicates by construction - see
    the module docstring's "What this is NOT" section on conflict resolution.
    """
    return (row["device_id"], row["device_ts_ms"], row["payload_hash"])


def partition_key(row: dict[str, Any], *, bucket_count: int = DEVICE_BUCKET_COUNT) -> str:
    """The Stage 1 partition path for one row.

    Layout: stage1_parsed/device_class=<class>/event_date=<YYYY-MM-DD>/hour=<HH>/
    device_bucket=<NN>/

    Partitioned by device_class and EVENT time - derived from the row's own `device_ts_ms`,
    never from an arrival/ingest timestamp - then bucketed by a hash of device_id (never by
    device_id alone). This is the Stage 1+ half of invariant 4; contrast with
    pipeline/stage0_landing/capture.py's arrival_key(), which partitions Stage 0 by
    `arrival_ts_ms` and never by device_ts_ms or device_id.

    Raises InvalidEventTimestampError if device_ts_ms isn't a usable epoch-ms int - see that
    error's docstring for why this module doesn't try to sanitize it itself (P1-05's job).
    """
    device_ts_ms = row.get("device_ts_ms")
    if not isinstance(device_ts_ms, int) or isinstance(device_ts_ms, bool):
        raise InvalidEventTimestampError(
            f"row for device_id={row.get('device_id')!r} has device_ts_ms={device_ts_ms!r}, "
            "not a usable epoch-ms int - timestamp sanity is P1-05's job, not merge.py's"
        )

    event_time = dt.datetime.fromtimestamp(device_ts_ms / 1000, tz=dt.timezone.utc)
    bucket = device_bucket(row["device_id"], bucket_count=bucket_count)
    return (
        f"stage1_parsed/device_class={row['device_class']}/"
        f"event_date={event_time:%Y-%m-%d}/hour={event_time:%H}/"
        f"device_bucket={bucket:02d}/"
    )


@dataclasses.dataclass(frozen=True)
class MergeResult:
    """Summary of one merge() call, mirroring CaptureResult's counter style (see
    pipeline/stage0_landing/capture.py)."""

    rows_seen: int
    rows_inserted: int
    rows_already_present: int
    keys_inserted: tuple[NaturalKey, ...]

    @property
    def rows_merged(self) -> int:
        return self.rows_inserted + self.rows_already_present

    def reconciles(self) -> bool:
        """True iff every row seen this call resulted in exactly one merged outcome (newly
        inserted, or already present from this call or a prior one) - mirrors
        CaptureResult.reconciles() / ParseResult.reconciles()."""
        return self.rows_seen == self.rows_merged


class Stage1MergeStore:
    """In-process stand-in for a Stage 1 Iceberg table's merge semantics.

    Holds at most one row per natural key (device_id, device_ts_ms, payload_hash), regardless
    of how many times or in what order overlapping batches are merged into it - the mechanism
    behind invariant 2 ("MERGE ... never a plain append") and this ticket's "done when":
    replaying a message N times yields exactly one row.

    This is deliberately NOT an Iceberg table - see the module docstring's scope note. It
    exists to prove the merge logic (the decision of what to insert vs. skip) is correct in
    isolation from any particular storage engine, the same way capture.py's arrival_key() and
    "check before write" logic is storage-agnostic even though it's exercised here against S3.
    """

    def __init__(self) -> None:
        self._rows: dict[NaturalKey, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: NaturalKey) -> bool:
        return key in self._rows

    def get(self, key: NaturalKey) -> dict[str, Any] | None:
        return self._rows.get(key)

    def rows(self) -> tuple[dict[str, Any], ...]:
        """A snapshot of every row currently in the store, in insertion order."""
        return tuple(self._rows.values())

    def merge(self, rows: Iterable[dict[str, Any]]) -> MergeResult:
        """Merge `rows` into the store on their natural key.

        For each row: if its natural key isn't already present (in this store, from this call
        or any prior one), insert it. If the key IS already present, this is a no-op for that
        row - not an error, not a second copy - which is exactly what makes repeated calls
        (a replayed message, a re-run backfill, an overlapping late-arriving batch) converge to
        exactly one row per unique natural key no matter how many times or in what order they
        run.

        Two rows sharing a natural key are assumed to be true duplicates by construction (see
        module docstring) - the second one is dropped as a no-op without comparing field
        values.
        """
        rows_seen = 0
        rows_inserted = 0
        rows_already_present = 0
        keys_inserted: list[NaturalKey] = []

        for row in rows:
            rows_seen += 1
            key = natural_key(row)

            if key in self._rows:
                # Invariant 2: never a plain append - a natural key already present is a
                # no-op, not a duplicate row.
                rows_already_present += 1
                continue

            self._rows[key] = row
            rows_inserted += 1
            keys_inserted.append(key)

        return MergeResult(
            rows_seen=rows_seen,
            rows_inserted=rows_inserted,
            rows_already_present=rows_already_present,
            keys_inserted=tuple(keys_inserted),
        )
