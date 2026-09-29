"""Provisional-to-final window lifecycle: the lateness-horizon job/policy layer.

Ticket: P2-11 ("Provisional-to-final window lifecycle and finding versions"). Directly
implements the "when" half of CLAUDE.md invariant 6: "Windows are provisional until the
lateness horizon passes. Findings are versioned, never overwritten." The "never overwritten"
half, and the mechanics of the provisional -> final transition itself, are already built and
proven in detectors/framework/findings.py's FindingStore.finalize() (P2-06) - see that
module's docstring, which explicitly reserves "the lateness-horizon CLOCK - the scheduling
logic that decides *when* a window's data is late enough...to flip from provisional to final"
for this ticket. This module is that clock: it decides WHICH (detector, subject) pairs are due
for finalization at a given wall-clock instant, and drives FindingStore.finalize() over them.
It never appends, mutates, or otherwise touches Finding rows itself - finalize() already
guarantees the append-only/never-overwrite behavior on its own, and this module is not
re-implementing or re-proving that (see tests/unit/test_detector_framework.py's
test_provisional_to_final_status_round_trips_as_a_new_appended_version and the
test_finalize_rejects_* tests for that proof at the framework level).

Where the 24h default horizon comes from (read before changing it)
----------------------------------------------------------------------
`DEFAULT_HORIZON_MS` below is 24 hours, taken from docs/reports/P0-13-gate0-report.md
("P0-13: consolidate P0-06/07/08/09/10 findings into a Gate 0 prep report"), which states at
(as consolidated on trunk) line ~155:

    "Recommended lateness horizon: 24 hours, as a starting number for Gate 0 to confirm or
    override."

That report reasons the number from two things: (1) P0-08's measured on-time lateness
(p90 of ~605-946s, i.e. roughly 10-16 minutes) gives 24h well over 100x margin over normal
operation in the synthetic proxy fixtures, while explicitly warning NOT to size a horizon off
the p99/max tail, which the report traces to a specific generator bug
(`_emit_device_messages` snapping a corrupted batch's arrival anchor to "now", not real
lateness); and (2) the real open question a horizon should cover - outage-driven late arrival
on device reconnect - is bounded by P0-09's proposed fault-injection plan, which tests
blackout durations up to 4h with up to ~8h of recovery observation, comfortably inside 24h.

This is explicitly a *recommendation*, not yet a signed-off constant - the report itself says
Gate 0 must "confirm or override" it, and no Gate 0 sign-off exists in this repo yet. That is
exactly why `DEFAULT_HORIZON_MS` is a named, importable, overridable default parameter on every
function below rather than a bare literal buried in comparison logic: a caller with a
different (e.g. eventually Gate-0-approved) horizon passes `horizon_ms=` explicitly instead of
editing this module.

Lineage note - why this module cites the report by content, not by reading it from disk here
------------------------------------------------------------------------------------------------
This ticket's branch is based on P2-06's tip, which - per this repo's actual (not idealized)
branch graph - predates the "Consolidate scattered branch reports into docs/reports/" commit
that put P0-13-gate0-report.md at that path on trunk (`main`): that commit is not an ancestor
of this branch, so the file does not exist on disk in this worktree (confirmed: `git show
HEAD:docs/reports/P0-13-gate0-report.md` finds nothing here). The quote and line number above
were confirmed by reading the report's actual content via `git show
78ae654:docs/reports/P0-13-gate0-report.md` (its origin commit) rather than guessed - same
stopgap-for-branch-ordering situation, and same "read the real source, not the ticket
paraphrase" discipline, that pipeline/stage2_canonical/completeness_sidecar.py's P1-11
docstring describes for `device_hour_key()` under its own "Lineage note".

The policy this module implements
--------------------------------------
For each (detector_name, subject) pair a FindingStore has ever seen (`store.subjects()`):

- If that pair's LATEST finding is already FINAL, it is left alone (already done; counted as
  `skipped_already_final`).
- If it is PROVISIONAL, its `subject`'s event_hour end (`hour_end_ms()`) plus `horizon_ms` is
  compared against `now_ms`. Due (`hour_end_ms(...) + horizon_ms <= now_ms`, an inclusive
  boundary - see `due_for_finalization()`'s docstring) means it gets finalized; not yet due
  means it is left alone (counted as `skipped_still_provisional_within_horizon`).

Only the pair's LATEST version is ever considered, matching finalize()'s own "only the current
latest version may be finalized" rule - an older, superseded PROVISIONAL version is never a
finalization target even if it still exists in history, because it no longer represents this
subject's current state.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from detectors.framework.findings import Finding, FindingStatus, FindingStore
from pipeline.stage1_parsed.merge import DeviceHourKey

# See module docstring's "Where the 24h default horizon comes from". 24 hours in ms.
DEFAULT_HORIZON_MS: int = 24 * 60 * 60 * 1000

_EVENT_HOUR_FORMAT = "%Y-%m-%dT%H"


def hour_end_ms(event_hour: str) -> int:
    """The end (exclusive upper bound) of a DeviceHourKey's `event_hour` component, in epoch
    ms UTC.

    `event_hour` is the `"YYYY-MM-DDTHH"` UTC string
    pipeline/stage1_parsed/merge.py's `device_hour_key()` produces (confirmed by reading that
    function directly, not assumed - see its docstring: "formatted as 'YYYY-MM-DDTHH' in UTC
    so it sorts and compares as a plain string"). The hour it names covers
    `[start, start + 1h)`; this returns the exclusive upper bound `start + 1h`, i.e. the
    earliest instant at which every reading that could possibly belong to that hour has
    already happened (event-time-wise) - the natural zero point a lateness horizon counts
    forward from.
    """
    start = dt.datetime.strptime(event_hour, _EVENT_HOUR_FORMAT).replace(tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=1)
    return int(end.timestamp() * 1000)


@dataclasses.dataclass(frozen=True)
class LifecycleRunResult:
    """Summary of one finalize_due_windows() call, mirroring this repo's established
    counter + reconciles() result pattern (DetectorRunResult.reconciles(),
    MergeResult/CanonicalizeResult, TargetedRecomputeResult.reconciles(), etc. - see
    detectors/framework/dispatch.py and pipeline/stage1_parsed/merge.py)."""

    finalized: tuple[Finding, ...]
    skipped_still_provisional_within_horizon: int
    skipped_already_final: int

    def reconciles(self, store: FindingStore) -> bool:
        """True iff every (detector_name, subject) pair `store` currently has history for is
        accounted for exactly once by this run's outcome: finalized this run, still
        provisional and within the horizon, or already final - never both, never neither.

        Takes `store` explicitly (rather than caching a subject count at construction time)
        because finalize_due_windows() only ever finalizes EXISTING subjects - it never adds
        or removes a (detector_name, subject) pair from `store.subjects()` - so the count to
        reconcile against is exactly `len(store.subjects())` as of whenever the caller checks,
        which is always right after the run that produced this result.
        """
        accounted = (
            len(self.finalized)
            + self.skipped_still_provisional_within_horizon
            + self.skipped_already_final
        )
        return accounted == len(store.subjects())


def due_for_finalization(
    store: FindingStore, *, now_ms: int, horizon_ms: int = DEFAULT_HORIZON_MS
) -> tuple[tuple[str, DeviceHourKey, int], ...]:
    """Every (detector_name, subject, version) triple whose LATEST finding is PROVISIONAL and
    whose subject's hour has been over for at least `horizon_ms` as of `now_ms` - i.e. is due
    to be finalized right now.

    Boundary choice (deliberate, not an accident of `<=` vs `<`): a subject becomes due at the
    exact instant `hour_end_ms(event_hour) + horizon_ms == now_ms`, INCLUSIVE - the comparison
    is `<=`. The horizon is "the window has been open for at least this long", not "strictly
    longer than this long"; at the exact boundary instant the full horizon_ms has already
    elapsed (0ms is still >= 0ms remaining), so there is no principled reason to make a caller
    wait one more tick past a duration that has, by definition, already fully passed. This is
    tested explicitly (not left as an accident) in tests/unit/test_lifecycle.py.

    A subject whose latest finding is already FINAL is never included here, regardless of
    `now_ms`/`horizon_ms` - finalize() itself would reject re-finalizing it (see
    findings.py's FindingStateError), so this function filters it out up front rather than
    handing finalize_due_windows() something it can only discover is invalid by calling
    finalize() and catching an error.
    """
    due: list[tuple[str, DeviceHourKey, int]] = []
    for detector_name, subject in store.subjects():
        latest = store.latest(detector_name, subject)
        if latest is None or latest.status is FindingStatus.FINAL:
            continue
        _device_id, event_hour = subject
        if hour_end_ms(event_hour) + horizon_ms <= now_ms:
            due.append((detector_name, subject, latest.version))
    return tuple(due)


def finalize_due_windows(
    store: FindingStore,
    *,
    now_ms: int,
    horizon_ms: int = DEFAULT_HORIZON_MS,
    emitted_at_ms: int | None = None,
) -> LifecycleRunResult:
    """Finalize every (detector_name, subject) pair `due_for_finalization()` identifies as due
    as of `now_ms`, and report what happened to every pair the store has history for.

    Idempotent: calling this twice with the same `now_ms` (and the same or a later real clock
    for `store`'s own `emitted_at_ms` default, if left unset) finalizes nothing new on the
    second call and raises nothing. The first call's finalize() calls advance each due pair's
    latest version to FINAL; on the second call, due_for_finalization() no longer includes
    those pairs (their latest is now FINAL, not PROVISIONAL), so they fall out as
    `skipped_already_final` instead of being finalized again or raising
    FindingStateError - this module never calls finalize() on a pair it hasn't itself just
    determined is PROVISIONAL and due, so it never triggers finalize()'s "already FINAL"
    rejection under normal use.

    `emitted_at_ms` is passed straight through to every FindingStore.finalize() call (default:
    the store's own clock, exactly matching finalize()'s own default) - shared here as one
    value per run so every finalization this call performs is stamped with the same instant,
    rather than each drifting to whatever the store's real-wall-clock default returns call by
    call.
    """
    finalized: list[Finding] = []
    for detector_name, subject, version in due_for_finalization(
        store, now_ms=now_ms, horizon_ms=horizon_ms
    ):
        finalized.append(
            store.finalize(
                detector_name=detector_name,
                subject=subject,
                version=version,
                emitted_at_ms=emitted_at_ms,
            )
        )

    finalized_keys = {(f.detector_name, f.subject) for f in finalized}
    skipped_within_horizon = 0
    skipped_already_final = 0
    for detector_name, subject in store.subjects():
        if (detector_name, subject) in finalized_keys:
            continue
        latest = store.latest(detector_name, subject)
        if latest is not None and latest.status is FindingStatus.FINAL:
            skipped_already_final += 1
        else:
            skipped_within_horizon += 1

    return LifecycleRunResult(
        finalized=tuple(finalized),
        skipped_still_provisional_within_horizon=skipped_within_horizon,
        skipped_already_final=skipped_already_final,
    )
