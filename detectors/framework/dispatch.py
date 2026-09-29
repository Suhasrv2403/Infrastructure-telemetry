"""Dispatch: run a resolved set of active detectors against a batch of DetectorContexts and
emit findings into a FindingStore.

Mirrors parsers/framework.py's parse_messages()/ParseResult/reconcile() shape: a dispatch loop
over (plugin, input) pairs, a frozen result dataclass with per-call counters and a
reconciles()-style check, and "a plugin bug degrades that one (plugin, input) pair rather than
crashing the whole batch" - here, a detector that raises while evaluating one context is
recorded as a DetectorFailure and the run continues, instead of losing every other context's
findings.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from detectors.framework.config import ActiveDetector
from detectors.framework.findings import Finding, FindingStore
from detectors.framework.registry import DetectorContext, DetectorFrameworkError


class DetectorReconciliationError(DetectorFrameworkError):
    """Raised when a DetectorRunResult's counts don't reconcile - mirrors
    parsers/framework.py's ReconciliationError. Every (detector, context) pair attempted must
    be accounted for exactly once, never both succeeded and failed, never neither.
    """


@dataclasses.dataclass(frozen=True)
class DetectorFailure:
    """One (detector, context) pair whose detector function raised while evaluating it - the
    original exception's message is preserved as `reason` so a caller can see why without the
    dispatch loop itself crashing (the same "degrade to a recorded failure, don't lose the rest
    of the batch" stance parsers/framework.py's parse_messages() takes for a parser bug).
    """

    detector_name: str
    subject: object  # merge.DeviceHourKey
    reason: str


@dataclasses.dataclass(frozen=True)
class DetectorRunResult:
    """Summary of one run_active_detectors() call, mirroring ParseResult's/MergeResult's
    counter + reconciles() style."""

    runs_seen: int
    runs_succeeded: int
    findings: tuple[Finding, ...]
    failures: tuple[DetectorFailure, ...]

    def reconciles(self) -> bool:
        """True iff every (detector, context) pair attempted this run either succeeded (0 or
        more findings emitted - a detector finding nothing is a valid success, not a failure)
        or was recorded as a failure - never both, never neither."""
        return self.runs_seen == self.runs_succeeded + len(self.failures)


def run_active_detectors(
    active: Iterable[ActiveDetector],
    contexts: Iterable[DetectorContext],
    store: FindingStore,
) -> DetectorRunResult:
    """Run every active detector against every context (the full cross product - a caller
    that only wants a specific detector's contexts should pre-filter `contexts` or `active`),
    emitting each resulting DetectorOutcome into `store` via FindingStore.emit() (which itself
    stamps version/content_hash - see findings.py).

    `contexts` is consumed once via a materialized list, so a generator can be passed safely
    even though it's iterated once per active detector.
    """
    context_list = list(contexts)

    runs_seen = 0
    runs_succeeded = 0
    findings: list[Finding] = []
    failures: list[DetectorFailure] = []

    for detector in active:
        for context in context_list:
            runs_seen += 1
            try:
                outcomes = list(detector.func(context))
            except Exception as exc:  # noqa: BLE001 - a detector bug must degrade to a
                # recorded failure for this one (detector, context) pair, not crash the batch
                # or lose every other context's findings (see module docstring).
                failures.append(
                    DetectorFailure(
                        detector_name=detector.name,
                        subject=context.subject,
                        reason=f"detector_error: {exc}",
                    )
                )
                continue

            for outcome in outcomes:
                finding = store.emit(
                    detector_name=detector.name,
                    subject=context.subject,
                    status=outcome.status,
                    summary=outcome.summary,
                    evidence=outcome.evidence,
                    quality=outcome.quality,
                    detector_version=detector.detector_version,
                )
                findings.append(finding)
            runs_succeeded += 1

    return DetectorRunResult(
        runs_seen=runs_seen,
        runs_succeeded=runs_succeeded,
        findings=tuple(findings),
        failures=tuple(failures),
    )


def reconcile(result: DetectorRunResult) -> None:
    """Raise DetectorReconciliationError unless every (detector, context) pair attempted this
    run was accounted for exactly once."""
    if not result.reconciles():
        raise DetectorReconciliationError(
            f"{result.runs_seen} runs seen but {result.runs_succeeded} succeeded + "
            f"{len(result.failures)} failed = "
            f"{result.runs_succeeded + len(result.failures)} accounted for - every "
            "(detector, context) pair must be either succeeded or failed, never both, never "
            "neither"
        )
