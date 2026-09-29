"""The findings schema and its append-only, versioned, never-overwritten store.

Ticket: P2-06 ("Detector plugin framework and findings schema"). Directly implements CLAUDE.md
invariant 6: "Windows are provisional until the lateness horizon passes. Findings are
versioned, never overwritten."

Scope note - what this module does NOT do
------------------------------------------
This module builds the SCHEMA and the STORE that can represent and enforce invariant 6's two
halves (provisional/final status; version, never overwrite). It does not build the
lateness-horizon CLOCK - the scheduling logic that decides *when* a window's data is late
enough, or safe enough, to flip from provisional to final. That's P2-11's job (see
detectors/README.md: "the provisional-to-final window lifecycle (P2-06, P2-11)" is explicitly
a shared effort between the two tickets). What this module guarantees is that WHATEVER drives
that transition can only do so by calling FindingStore.finalize(), which appends a new,
FINAL-status Finding rather than mutating the provisional one in place - the provisional
Finding stays exactly as it was, forever retrievable via FindingStore.history().

Finding grain: one (detector_name, subject) pair, where `subject` is a (device_id, event_hour)
DeviceHourKey - see registry.py's module docstring for why this ticket grounds a finding's
subject in the same device-hour grain P1-07's dirty-keys tracking and P2-03's Stage 3a grid
rebuild already use, rather than inventing a new one.

Versioning design (this ticket's own call, documented per the ticket brief)
-----------------------------------------------------------------------------
`Finding.version` is a plain integer, 1-based, PER (detector_name, subject) pair, assigned by
FindingStore.emit()/finalize() - never by the caller. It increments by exactly one each time a
new, distinct Finding is appended for that pair; the previous version's row is never touched.

"Distinct" is decided by content hash (Finding.content_hash, over detector_name/subject/status/
summary/evidence/quality - the fields that describe *what was found*, not bookkeeping like
emitted_at_ms or version itself). Two consecutive emit() calls for the same (detector, subject)
whose content hash matches produce NO new version - the existing latest Finding is returned
unchanged. This is a deliberate idempotency choice, not silent overwrite-avoidance: it mirrors
pipeline/stage1_parsed/merge.py's Stage1MergeStore, which treats two rows sharing a natural key
as true duplicates by construction and no-ops the second rather than inserting a second copy.
Here the "natural key" is (detector_name, subject, content_hash) - a detector re-run that
reaches the exact same conclusion from unchanged underlying data is the same fact restated, not
a new one, so it doesn't need a new version number to stay honest about invariant 6. A re-run
whose underlying data (and therefore evidence/summary/quality) actually changed DOES get a new
version, because its content hash differs - this is the core case P2-06's test suite proves
explicitly (see tests/unit/test_detector_framework.py).

Finalizing a provisional finding (FindingStore.finalize()) is implemented the same way: as an
APPEND, never a mutation. It copies the latest version's summary/evidence/quality/
detector_version forward into a brand new Finding with status=FINAL and a fresh version number,
recording `supersedes` = the provisional version it followed. The provisional Finding it
followed remains in the store, unchanged, at its original version - "never overwritten" is true
even across the provisional -> final transition, not just across detector re-runs.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import enum
import hashlib
import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from detectors.framework.registry import DetectorFrameworkError
from pipeline.stage1_parsed.merge import DeviceHourKey


class FindingStateError(DetectorFrameworkError):
    """Raised when a FindingStore call would violate the append-only/never-overwrite invariant
    or is otherwise nonsensical given a subject's current finding history (e.g. finalizing a
    version that isn't the latest, or finalizing an already-final finding) - fails loudly
    rather than silently no-op'ing or corrupting history, the same "fail loudly on misuse"
    stance pipeline/stage1_parsed/merge.py's InvalidEventTimestampError takes.
    """


class FindingStatus(enum.Enum):
    """A finding's lifecycle status (CLAUDE.md invariant 6). PROVISIONAL is what every finding
    starts as; FINAL is reached only via FindingStore.finalize() (see module docstring) - never
    by mutating a PROVISIONAL Finding's status field, which is impossible anyway since Finding
    is frozen.
    """

    PROVISIONAL = "provisional"
    FINAL = "final"


@dataclasses.dataclass(frozen=True)
class FindingQuality:
    """Data-quality/confidence context for one finding, grounded in the same coverage concept
    P2-03's Stage 3a grid already tracks (GridBucket.coverage - MEASURED vs GAP - see
    pipeline/stage3_enrich/time_grid.py) rather than an arbitrary confidence number invented
    here. `label` is a qualitative summary a human reviewing a finding can read at a glance;
    `measured_buckets`/`total_buckets` are the concrete counts a reviewer (or a future
    detector) can recompute the label from, so `label` is never a bare, unverifiable claim.

    This is deliberately not tied to time_grid.GridBucket at the type level (see registry.py's
    module docstring on why DetectorContext.payload stays generic) - a detector consuming a
    different Stage 3/4 input (Stage 2 canonical rows, Stage 3c device-day features) can still
    populate a FindingQuality by counting whatever it considers "measured" vs "total" for its
    own subject.
    """

    label: str
    measured_buckets: int
    total_buckets: int

    def __post_init__(self) -> None:
        if self.total_buckets < 0 or self.measured_buckets < 0:
            raise ValueError("measured_buckets/total_buckets must be >= 0")
        if self.measured_buckets > self.total_buckets:
            raise ValueError(
                f"measured_buckets ({self.measured_buckets}) cannot exceed total_buckets "
                f"({self.total_buckets})"
            )

    @property
    def coverage_fraction(self) -> float:
        """measured_buckets / total_buckets, or 0.0 for an empty (total_buckets == 0) subject -
        the same binary-coverage-rolled-up-to-a-fraction idea as GridBucket.coverage, one grain
        up."""
        if self.total_buckets == 0:
            return 0.0
        return self.measured_buckets / self.total_buckets


def _content_hash(
    *,
    detector_name: str,
    subject: DeviceHourKey,
    status: FindingStatus,
    summary: str,
    evidence: Mapping[str, Any],
    quality: FindingQuality,
) -> str:
    """A deterministic hash over everything that describes WHAT was found - used by
    FindingStore to decide whether a new emit()/finalize() call is a genuinely new fact (new
    version) or a restatement of the current latest version (no-op) - see module docstring.

    Deliberately excludes version/emitted_at_ms/supersedes/content_hash itself (pure
    bookkeeping the store assigns, not part of "what was found").
    """
    payload = {
        "detector_name": detector_name,
        "subject": list(subject),
        "status": status.value,
        "summary": summary,
        # dict(...) here (not the Mapping as-is) because `evidence` may already be a
        # Finding's read-only MappingProxyType (finalize() hashes latest.evidence, which
        # Finding.__post_init__ already wrapped) - json.dumps only fast-paths real dict
        # instances, and a plain dict keeps this hash's shape identical whether it's called
        # from emit() (a caller's raw dict) or finalize() (a stored Finding's wrapped one).
        "evidence": dict(evidence),
        "quality": dataclasses.asdict(quality),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class Finding:
    """One immutable, versioned detector finding about one (detector_name, subject) pair.

    Never constructed directly by detector or caller code outside this module - always via
    FindingStore.emit()/finalize(), which stamp `version`/`content_hash`/`supersedes` and
    enforce the never-overwrite invariant (see module docstring). A caller only ever supplies
    the "what was found" half (status/summary/evidence/quality/detector_version).

    `evidence` is defensively deep-copied and wrapped read-only at construction time (see
    __post_init__) - the concrete data that justified this finding (grid buckets, canonical
    row values, whatever the detector actually looked at) must stay exactly what it was at
    emit time, immune to a caller later mutating the dict object it originally passed in. A
    human reviewing a finding can inspect `evidence` to see *why* it fired, not just that some
    detector fired.
    """

    detector_name: str
    subject: DeviceHourKey
    version: int
    status: FindingStatus
    summary: str
    evidence: Mapping[str, Any]
    quality: FindingQuality
    detector_version: str
    emitted_at_ms: int
    supersedes: int | None
    content_hash: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"Finding.version must be >= 1, got {self.version}")
        # Immutable, defensively-copied evidence - see class docstring.
        object.__setattr__(
            self, "evidence", MappingProxyType(copy.deepcopy(dict(self.evidence)))
        )

    @property
    def key(self) -> tuple[str, DeviceHourKey, int]:
        """(detector_name, subject, version) - a Finding's full identity, unique across the
        whole store."""
        return (self.detector_name, self.subject, self.version)


def _default_clock() -> int:
    """Wall-clock epoch-ms, UTC - see the 3.10-compatibility note (never datetime.UTC)."""
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class FindingStore:
    """In-process, append-only store of Finding history, keyed by (detector_name, subject).

    Mirrors pipeline/stage1_parsed/merge.py's Stage1MergeStore in spirit - an in-process
    stand-in for what would eventually be a real versioned findings table (CLAUDE.md's Stage 4
    "versioned findings") - proving the append-only/never-overwrite SEMANTICS in isolation from
    any particular storage engine. Holds every version ever emitted for every (detector_name,
    subject) pair; nothing is ever deleted or mutated in place, only appended.

    `clock` defaults to real wall-clock UTC epoch-ms; tests pass a deterministic stand-in (e.g.
    an incrementing counter) so emitted_at_ms ordering can be asserted without depending on
    real time.
    """

    def __init__(self, *, clock: Callable[[], int] = _default_clock) -> None:
        self._clock = clock
        self._history: dict[tuple[str, DeviceHourKey], list[Finding]] = {}

    def history(self, detector_name: str, subject: DeviceHourKey) -> tuple[Finding, ...]:
        """Every version ever emitted for (detector_name, subject), oldest first. Always a
        fresh tuple snapshot - never the live internal list - so a caller can't mutate stored
        history through the returned object."""
        return tuple(self._history.get((detector_name, subject), ()))

    def latest(self, detector_name: str, subject: DeviceHourKey) -> Finding | None:
        """The highest-version Finding for (detector_name, subject), or None if none exists
        yet."""
        rows = self._history.get((detector_name, subject))
        return rows[-1] if rows else None

    def get(self, detector_name: str, subject: DeviceHourKey, version: int) -> Finding | None:
        """One specific historical version, or None if it doesn't exist - the "old finding is
        still retrievable/intact" check a re-run's new version must never break."""
        for finding in self._history.get((detector_name, subject), ()):
            if finding.version == version:
                return finding
        return None

    def subjects(self) -> tuple[tuple[str, DeviceHourKey], ...]:
        """Every (detector_name, subject) pair this store has any history for, for
        introspection/tests."""
        return tuple(self._history.keys())

    def emit(
        self,
        *,
        detector_name: str,
        subject: DeviceHourKey,
        status: FindingStatus,
        summary: str,
        evidence: Mapping[str, Any],
        quality: FindingQuality,
        detector_version: str,
        emitted_at_ms: int | None = None,
    ) -> Finding:
        """Append a new Finding version for (detector_name, subject), unless it is
        content-identical to the current latest version, in which case the existing latest
        Finding is returned unchanged (idempotent re-run - see module docstring). Never
        mutates or removes any prior version either way.
        """
        key = (detector_name, subject)
        history = self._history.setdefault(key, [])
        content_hash = _content_hash(
            detector_name=detector_name,
            subject=subject,
            status=status,
            summary=summary,
            evidence=evidence,
            quality=quality,
        )

        if history and history[-1].content_hash == content_hash:
            return history[-1]

        latest = history[-1] if history else None
        finding = Finding(
            detector_name=detector_name,
            subject=subject,
            version=(latest.version + 1) if latest else 1,
            status=status,
            summary=summary,
            evidence=evidence,
            quality=quality,
            detector_version=detector_version,
            emitted_at_ms=self._clock() if emitted_at_ms is None else emitted_at_ms,
            supersedes=latest.version if latest else None,
            content_hash=content_hash,
        )
        history.append(finding)
        return finding

    def finalize(
        self,
        *,
        detector_name: str,
        subject: DeviceHourKey,
        version: int,
        emitted_at_ms: int | None = None,
    ) -> Finding:
        """Transition the given PROVISIONAL version to FINAL - as a new appended version, never
        a mutation of the version being finalized (see module docstring). This is the seam a
        future lateness-horizon scheduler (P2-11) calls into once it decides a window's data is
        safe to finalize; this method does not itself decide *when* that is.

        Raises FindingStateError if `version` isn't the CURRENT LATEST version for
        (detector_name, subject) (finalizing a stale/superseded version would be finalizing a
        finding a later re-run has already moved past) or if it is already FINAL (finalizing
        twice is a caller bug, not a silent no-op - fail loudly).
        """
        key = (detector_name, subject)
        history = self._history.get(key)
        if not history:
            raise FindingStateError(
                f"no findings exist for detector_name={detector_name!r} subject={subject!r}"
            )
        latest = history[-1]
        if latest.version != version:
            raise FindingStateError(
                f"version {version} is not the latest ({latest.version}) for "
                f"detector_name={detector_name!r} subject={subject!r} - only the current "
                "latest version may be finalized"
            )
        if latest.status is FindingStatus.FINAL:
            raise FindingStateError(
                f"detector_name={detector_name!r} subject={subject!r} version={version} is "
                "already FINAL"
            )

        content_hash = _content_hash(
            detector_name=latest.detector_name,
            subject=latest.subject,
            status=FindingStatus.FINAL,
            summary=latest.summary,
            evidence=latest.evidence,
            quality=latest.quality,
        )
        finding = Finding(
            detector_name=latest.detector_name,
            subject=latest.subject,
            version=latest.version + 1,
            status=FindingStatus.FINAL,
            summary=latest.summary,
            evidence=latest.evidence,
            quality=latest.quality,
            detector_version=latest.detector_version,
            emitted_at_ms=self._clock() if emitted_at_ms is None else emitted_at_ms,
            supersedes=latest.version,
            content_hash=content_hash,
        )
        history.append(finding)
        return finding
