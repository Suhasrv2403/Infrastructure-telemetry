"""Timestamp sanity checks and quarantine classification for Stage 1 rows.

Ticket: P1-05 ("Timestamp sanity checks and quarantine table").

Scope note: parsers/framework.py (P1-03) turns a landed Stage 0 message envelope into flat
per-reading Stage 1 rows. Before those rows reach the Stage 1 MERGE-on-(device_id, device_ts,
payload_hash) step (invariant 2, P1-06 - a sibling ticket this module does not build), this
module runs a row-level sanity pass on their timestamps and separates each row into either
"accepted" or "quarantined with a reason code" - never dropped (CLAUDE.md invariant 3:
"Cleaning flags bad values; it never deletes rows").

This module is pure Python operating on plain dicts, storage-agnostic like
pipeline/stage0_landing/capture.py's and parsers/framework.py's own "pure core" (no S3/
Iceberg/Dagster dependency here). Wiring this classification into an actual quarantine
table/Dagster asset (there is no real Iceberg/Spark available in this dev environment - see
CLAUDE.md's "Stack" section, which names the eventual real stack, not what's running here) is
separate follow-up work, out of scope for this ticket as scoped.

A Stage 1 row (as produced by a registered parser - see parsers/supercharger_stall/
firmware_2_1_4.py) is expected to carry at minimum `device_id`, `device_ts_ms`, `payload_hash`
and `arrival_ts_ms`. As of this ticket, the P1-03 demo parser does not yet thread
`arrival_ts_ms` from the envelope down into each row it emits - only the envelope (the Stage 0
message) carries it. Production wiring of this module therefore needs `arrival_ts_ms` threaded
through from the envelope into each row (either by a parser, or by whatever calls this module
between parsing and merge) before the future-timestamp check below is meaningful; this
module's own tests add it to their fixtures rather than waiting on that.

Reason codes (per the ticket's "done when": "Epoch, future and implausible times quarantined
with reason codes"):

- missing_timestamp: `device_ts_ms is None` - the device sent a reading but didn't (or
  couldn't) stamp it.
- epoch_default_timestamp: `device_ts_ms == 0` - a classic uninitialized-RTC symptom.
- future_timestamp: `device_ts_ms` more than FUTURE_THRESHOLD_MS ahead of that row's own
  `arrival_ts_ms`. See FUTURE_THRESHOLD_MS below for the threshold and its reasoning, reused
  verbatim from profiling/clock_quality/profiler.py (P0-07) rather than redefined here - this
  ticket acts on the same "implausible" signal that profiler measures, it does not get to
  redefine what "implausible" means.

A row missing `arrival_ts_ms` (`None`, or the key entirely absent) cannot be checked for
future_timestamp and is accepted on that check (matching profiler.py's own `_is_future`, which
treats a missing arrival_ts_ms as "not future" rather than guessing) - it may still be
quarantined for missing/epoch reasons independently.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

MS_PER_S = 1000
MS_PER_HOUR = 3600 * MS_PER_S

# Reused from profiling/clock_quality/profiler.py's FUTURE_THRESHOLD_MS (P0-07, commit fd385bd
# on branch P0-07-timestamp-clock-quality-profiler). Duplicated here rather than imported
# because that module lives on a branch not yet merged into this ticket's P1-03-derived
# lineage (profiling/clock_quality/ does not exist in this worktree) - see this repo's git
# history for that branch if it needs reconciling later. Same value, same reasoning: a
# device's clock can legitimately run a bit fast and still land readings that arrive after the
# device thinks they happened by a few minutes to a few hours (normal network/queue jitter
# plus clock skew - the synthetic generator's late_delay_max_s defaults to 4h). A reading whose
# device_ts is hours-to-days *ahead of* the message's own arrival time is a different,
# categorical problem: the device's clock is simply wrong, not merely fast.
# FUTURE_THRESHOLD_MS = 6 hours splits these: comfortably above plausible clock skew combined
# with late-arrival jitter, while comfortably below the generator's injected future offsets
# (future_offset_min_s defaults to 1h but future_offset_max_s defaults to 14 days, so most
# injected future timestamps land far past 6h). A judgment call tuned against the synthetic
# generator's parameters, not a measured production cutoff - revisit once real telemetry is
# available, same as profiler.py notes.
FUTURE_THRESHOLD_MS = 6 * MS_PER_HOUR

REASON_MISSING_TIMESTAMP = "missing_timestamp"
REASON_EPOCH_DEFAULT_TIMESTAMP = "epoch_default_timestamp"
REASON_FUTURE_TIMESTAMP = "future_timestamp"


class TimestampSanityError(Exception):
    """Base class for timestamp-sanity errors."""


class ReconciliationError(TimestampSanityError):
    """Raised when rows_total doesn't equal (accepted + quarantined).

    Mirrors pipeline/stage0_landing/capture.py's and parsers/framework.py's own
    ReconciliationError: every row must be accounted for exactly once, never silently vanish.
    """


@dataclasses.dataclass(frozen=True)
class QuarantinedRow:
    """One row quarantined for a bad timestamp.

    `row` is the original Stage 1 row, preserved verbatim (never mutated or dropped), per
    CLAUDE.md invariant 3.
    """

    row: dict[str, Any]
    reason: str


@dataclasses.dataclass(frozen=True)
class TimestampSanityResult:
    """Summary of one sanity-check run, mirroring CaptureResult/ParseResult's style (see
    pipeline/stage0_landing/capture.py and parsers/framework.py): counters to log, plus a
    reconciles()-style check that every row seen was accounted for exactly once."""

    rows_total: int
    accepted_rows: tuple[dict[str, Any], ...]
    quarantined_rows: tuple[QuarantinedRow, ...]
    missing_count: int
    epoch_default_count: int
    future_count: int

    def reconciles(self) -> bool:
        """True iff every row seen ended up either accepted or quarantined - never both,
        never neither."""
        return self.rows_total == len(self.accepted_rows) + len(self.quarantined_rows)


def _classify(row: dict[str, Any]) -> str | None:
    """Return the reason code for one row, or None if it's clean."""
    device_ts_ms = row.get("device_ts_ms")

    if device_ts_ms is None:
        return REASON_MISSING_TIMESTAMP
    if device_ts_ms == 0:
        return REASON_EPOCH_DEFAULT_TIMESTAMP

    arrival_ts_ms = row.get("arrival_ts_ms")
    if arrival_ts_ms is not None and (device_ts_ms - arrival_ts_ms) > FUTURE_THRESHOLD_MS:
        return REASON_FUTURE_TIMESTAMP

    return None


def check_timestamps(rows: Iterable[dict[str, Any]]) -> TimestampSanityResult:
    """Classify each Stage 1 row as accepted or quarantined-with-a-reason.

    A row is checked in order: missing, then epoch-default, then future (a row can only ever
    match one reason, since missing/epoch-default already short-circuit before the future
    check runs). The original row dict is never mutated, and is preserved verbatim in both the
    accepted list and any QuarantinedRow - per CLAUDE.md invariant 3, cleaning flags bad values,
    it never deletes rows.
    """
    rows_total = 0
    accepted: list[dict[str, Any]] = []
    quarantined: list[QuarantinedRow] = []
    missing_count = 0
    epoch_default_count = 0
    future_count = 0

    for row in rows:
        rows_total += 1
        reason = _classify(row)

        if reason is None:
            accepted.append(row)
            continue

        quarantined.append(QuarantinedRow(row=row, reason=reason))
        if reason == REASON_MISSING_TIMESTAMP:
            missing_count += 1
        elif reason == REASON_EPOCH_DEFAULT_TIMESTAMP:
            epoch_default_count += 1
        elif reason == REASON_FUTURE_TIMESTAMP:
            future_count += 1

    return TimestampSanityResult(
        rows_total=rows_total,
        accepted_rows=tuple(accepted),
        quarantined_rows=tuple(quarantined),
        missing_count=missing_count,
        epoch_default_count=epoch_default_count,
        future_count=future_count,
    )


def reconcile(result: TimestampSanityResult) -> None:
    """Raise ReconciliationError unless every row seen was accounted for exactly once."""
    if not result.reconciles():
        raise ReconciliationError(
            f"{result.rows_total} rows seen but {len(result.accepted_rows)} accepted + "
            f"{len(result.quarantined_rows)} quarantined = "
            f"{len(result.accepted_rows) + len(result.quarantined_rows)} accounted for - every "
            "row must be either accepted or quarantined, never both, never neither"
        )
