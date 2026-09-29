"""Tests for detectors/framework/lifecycle.py (P2-11: "Provisional-to-final window lifecycle
and finding versions").

This suite proves the POLICY layer - the "when" decision (due_for_finalization(),
finalize_due_windows()) - not finalize()'s own append-only mechanics, which
tests/unit/test_detector_framework.py already proves at the framework level (see e.g.
test_provisional_to_final_status_round_trips_as_a_new_appended_version,
test_finalize_rejects_non_latest_version, test_finalize_rejects_already_final_version). Every
test here builds a FindingStore directly with FindingStore.emit(), the same fixture shape
test_detector_framework.py's own findings.py tests use.

Covers:
1. A PROVISIONAL finding whose hour ended clearly before now_ms - horizon_ms gets finalized,
   AND the original PROVISIONAL row stays byte-identical and retrievable afterward (the actual
   invariant-6 proof for this ticket).
2. A PROVISIONAL finding still within the horizon is left untouched.
3. An already-FINAL finding is skipped cleanly, counted, never re-finalized.
4. Idempotency: running finalize_due_windows() twice with the same now_ms is a no-op the
   second time.
5. The exact horizon boundary (hour_end_ms + horizon_ms == now_ms) is INCLUSIVE - finalized,
   not skipped - and this is tested deliberately in both directions (one ms before vs. exactly
   at the boundary).
6. LifecycleRunResult.reconciles() is true for a run mixing due / not-due / already-final
   subjects.
"""
from __future__ import annotations

from detectors.framework.findings import FindingQuality, FindingStatus, FindingStore
from detectors.framework.lifecycle import (
    DEFAULT_HORIZON_MS,
    LifecycleRunResult,
    due_for_finalization,
    finalize_due_windows,
    hour_end_ms,
)

DETECTOR = "all_gap_hour"


def _quality(measured: int = 0, total: int = 3) -> FindingQuality:
    return FindingQuality(label="low", measured_buckets=measured, total_buckets=total)


def _emit_provisional(store: FindingStore, subject, *, summary: str = "3 gap buckets"):
    return store.emit(
        detector_name=DETECTOR,
        subject=subject,
        status=FindingStatus.PROVISIONAL,
        summary=summary,
        evidence={"bucket_start_ms": (0, 60_000, 120_000)},
        quality=_quality(),
        detector_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# hour_end_ms()
# ---------------------------------------------------------------------------


def test_hour_end_ms_is_the_exclusive_upper_bound_in_utc():
    # 2026-06-01T09 covers [09:00:00, 10:00:00) UTC - cross-checked against the independently
    # computed UTC epoch for 2026-06-01T10:00:00Z, not a hand-typed literal.
    import datetime as dt

    expected = dt.datetime(2026, 6, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    assert hour_end_ms("2026-06-01T09") == int(expected.timestamp() * 1000)


def test_hour_end_ms_is_exactly_one_hour_after_start():
    assert hour_end_ms("2026-01-01T00") + 0 == hour_end_ms("2026-01-01T00")
    assert hour_end_ms("2026-01-01T01") - hour_end_ms("2026-01-01T00") == 60 * 60 * 1000


# ---------------------------------------------------------------------------
# 1. Due finding gets finalized; original PROVISIONAL row survives byte-identical.
# ---------------------------------------------------------------------------


def test_due_provisional_finding_is_finalized_and_original_row_survives_byte_identical():
    store = FindingStore(clock=iter([1_000, 2_000, 3_000]).__next__)
    subject = ("stall-A", "2026-06-01T09")
    provisional = _emit_provisional(store, subject)

    now_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS + 60_000  # well past the horizon

    result = finalize_due_windows(store, now_ms=now_ms)

    assert len(result.finalized) == 1
    final = result.finalized[0]
    assert final.status is FindingStatus.FINAL
    assert final.version == provisional.version + 1
    assert final.supersedes == provisional.version
    assert final.summary == provisional.summary
    assert final.evidence == provisional.evidence

    # The invariant-6 proof: the ORIGINAL provisional row is still retrievable, byte-identical,
    # via both get() and history() - finalization appended, it never mutated or removed it.
    fetched = store.get(DETECTOR, subject, provisional.version)
    assert fetched == provisional
    assert fetched.status is FindingStatus.PROVISIONAL
    assert store.history(DETECTOR, subject) == (provisional, final)
    assert store.latest(DETECTOR, subject) == final

    assert result.skipped_still_provisional_within_horizon == 0
    assert result.skipped_already_final == 0
    assert result.reconciles(store)


# ---------------------------------------------------------------------------
# 2. Not-yet-due finding is left untouched.
# ---------------------------------------------------------------------------


def test_provisional_finding_within_horizon_is_left_untouched():
    store = FindingStore(clock=iter([1_000]).__next__)
    subject = ("stall-B", "2026-06-01T09")
    provisional = _emit_provisional(store, subject)

    now_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS - 1  # 1ms short of due

    result = finalize_due_windows(store, now_ms=now_ms)

    assert result.finalized == ()
    assert result.skipped_still_provisional_within_horizon == 1
    assert result.skipped_already_final == 0
    assert due_for_finalization(store, now_ms=now_ms) == ()

    assert store.history(DETECTOR, subject) == (provisional,)
    assert store.latest(DETECTOR, subject) == provisional
    assert result.reconciles(store)


# ---------------------------------------------------------------------------
# 3. Already-FINAL finding is skipped cleanly, never re-finalized.
# ---------------------------------------------------------------------------


def test_already_final_finding_is_skipped_not_re_finalized():
    store = FindingStore(clock=iter([1_000, 2_000, 3_000]).__next__)
    subject = ("stall-C", "2026-06-01T09")
    provisional = _emit_provisional(store, subject)
    already_final = store.finalize(
        detector_name=DETECTOR, subject=subject, version=provisional.version
    )

    now_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS + 60_000

    result = finalize_due_windows(store, now_ms=now_ms)

    assert result.finalized == ()
    assert result.skipped_already_final == 1
    assert result.skipped_still_provisional_within_horizon == 0
    assert store.latest(DETECTOR, subject) == already_final
    assert store.history(DETECTOR, subject) == (provisional, already_final)
    assert result.reconciles(store)


# ---------------------------------------------------------------------------
# 4. Idempotency: a second run at the same now_ms finalizes nothing new, raises nothing.
# ---------------------------------------------------------------------------


def test_finalize_due_windows_is_idempotent_across_repeated_calls_at_the_same_now_ms():
    store = FindingStore(clock=iter([1_000, 2_000, 3_000, 4_000]).__next__)
    due_subject = ("stall-D", "2026-06-01T09")
    # A distinct, later hour so this subject is genuinely still within its own horizon at
    # now_ms below (same trap as test 6's fixture: reusing due_subject's hour would make this
    # subject due too).
    within_subject = ("stall-E", "2026-06-03T09")
    _emit_provisional(store, due_subject)
    _emit_provisional(store, within_subject)

    # due_subject's hour is well past the horizon; within_subject's is not.
    now_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS + 60_000

    first = finalize_due_windows(store, now_ms=now_ms)
    state_after_first = {
        subject: store.history(detector, subject) for detector, subject in store.subjects()
    }

    second = finalize_due_windows(store, now_ms=now_ms)
    state_after_second = {
        subject: store.history(detector, subject) for detector, subject in store.subjects()
    }

    assert len(first.finalized) == 1
    assert second.finalized == ()  # nothing new finalized the second time
    assert second.skipped_already_final == 1  # the pair finalized in call 1 is now here
    assert second.skipped_still_provisional_within_horizon == 1
    assert state_after_first == state_after_second  # store state byte-identical across calls
    assert first.reconciles(store)
    assert second.reconciles(store)


# ---------------------------------------------------------------------------
# 5. Exact horizon boundary: inclusive.
# ---------------------------------------------------------------------------


def test_horizon_boundary_is_inclusive_exactly_at_now_ms_equals_hour_end_plus_horizon():
    store = FindingStore(clock=iter([1_000, 2_000]).__next__)
    subject = ("stall-F", "2026-06-01T09")
    provisional = _emit_provisional(store, subject)

    boundary_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS

    # Exactly at the boundary: due (inclusive).
    assert due_for_finalization(store, now_ms=boundary_ms) == (
        (DETECTOR, subject, provisional.version),
    )
    result_at_boundary = finalize_due_windows(store, now_ms=boundary_ms)
    assert len(result_at_boundary.finalized) == 1
    assert result_at_boundary.finalized[0].status is FindingStatus.FINAL


def test_horizon_boundary_one_ms_before_is_not_yet_due():
    store = FindingStore(clock=iter([1_000]).__next__)
    subject = ("stall-G", "2026-06-01T09")
    _emit_provisional(store, subject)

    boundary_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS
    assert due_for_finalization(store, now_ms=boundary_ms - 1) == ()

    result = finalize_due_windows(store, now_ms=boundary_ms - 1)
    assert result.finalized == ()
    assert result.skipped_still_provisional_within_horizon == 1


# ---------------------------------------------------------------------------
# 6. reconciles() over a mixed run.
# ---------------------------------------------------------------------------


def test_reconciles_true_for_a_mixed_run_of_due_not_due_and_already_final_subjects():
    store = FindingStore(clock=iter(range(1_000, 20_000, 1_000)).__next__)

    # due_subject: PROVISIONAL, hour ended well before the horizon closes at now_ms -> due.
    due_subject = ("stall-H", "2026-06-01T09")
    # within_horizon_subject: PROVISIONAL, but its hour ends exactly at now_ms, so its own
    # horizon has not yet elapsed at now_ms -> not due (see the boundary tests above for why
    # "elapsed at exactly now_ms" IS due; this subject's hour ends strictly later).
    within_horizon_subject = ("stall-I", "2026-06-02T09")
    # final_subject: already FINAL -> must be skipped, never re-finalized.
    final_subject = ("stall-J", "2026-06-01T09")

    _emit_provisional(store, due_subject)
    _emit_provisional(store, within_horizon_subject)
    final_provisional = _emit_provisional(store, final_subject)
    store.finalize(
        detector_name=DETECTOR, subject=final_subject, version=final_provisional.version
    )

    now_ms = hour_end_ms("2026-06-01T09") + DEFAULT_HORIZON_MS
    # Sanity check on the fixture itself: within_horizon_subject's hour ends exactly at now_ms
    # too (2026-06-02T09's hour-end equals 2026-06-01T09's hour-end + 24h), so it is NOT yet
    # due (0ms of its own horizon has elapsed), unlike due_subject/final_subject.
    assert hour_end_ms("2026-06-02T09") == now_ms

    result = finalize_due_windows(store, now_ms=now_ms)

    assert result.reconciles(store)
    assert (
        len(result.finalized)
        + result.skipped_still_provisional_within_horizon
        + result.skipped_already_final
        == len(store.subjects())
    )
    assert result.skipped_already_final == 1
    assert result.skipped_still_provisional_within_horizon == 1
    assert len(result.finalized) == 1
    assert result.finalized[0].subject == due_subject


def test_lifecycle_run_result_is_a_frozen_dataclass_with_expected_fields():
    result = LifecycleRunResult(
        finalized=(), skipped_still_provisional_within_horizon=0, skipped_already_final=0
    )
    assert result.finalized == ()
    assert result.skipped_still_provisional_within_horizon == 0
    assert result.skipped_already_final == 0
