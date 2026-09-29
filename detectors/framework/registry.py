"""Detector registry: the code-level plugin registration half of "declared in config" (see
detectors/framework/config.py for the other half - resolving detectors/detectors.yaml against
this registry).

Mirrors parsers/framework.py's shape deliberately (P1-03's register_parser/Registry/dispatch
family is this repo's one other "plugin registration + declared-in-config + versioned
dispatch" framework - see this ticket's own scope note in detectors/framework/__init__.py):
a module-level registry dict, a decorator that populates it, loud-not-silent duplicate
rejection. The registration key differs on purpose - parsers dispatch by
(device_class, firmware_version) because a message's shape IS that pair; a detector is a
named plugin invoked by whichever subjects its config/caller feeds it, so it registers under
a single detector `name` instead.

Detector input shape (DetectorContext.payload) is deliberately generic ("whatever the
detector consumes"), not hard-wired to pipeline/stage3_enrich/time_grid.GridBucket - this
package must stay usable once P2-08/P2-09/P2-10/P2-15 detectors want to consume Stage 2
canonical rows or Stage 3c device-day features instead of a Stage 3a grid. This ticket's own
demo detector (demo_all_gap_detector.py) is the one concrete example, and it documents its own
expected payload type itself.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from typing import Any

# The (device_id, event_hour) grain a Finding is about - reusing merge.py's own
# DeviceHourKey/device_hour_key() convention (P1-06/P1-07) rather than reinventing a subject
# identity. This is this ticket's documented choice of finding grain: device-hour, the same
# grain P1-07's dirty-keys tracking and P2-03's Stage 3a grid rebuild already use, so a
# detector's subject lines up exactly with "the unit of work a late-arriving message can make
# dirty and force a recompute of" (CLAUDE.md invariant 5).
from pipeline.stage1_parsed.merge import DeviceHourKey


class DetectorFrameworkError(Exception):
    """Base class for detector-framework errors."""


class DuplicateDetectorError(DetectorFrameworkError):
    """Raised when a detector `name` is registered more than once.

    Mirrors parsers/framework.py's DuplicateParserError: two detectors silently racing to
    register the same name (the second registration shadowing the first) is almost certainly a
    bug, so registration fails loudly instead.
    """


@dataclasses.dataclass(frozen=True)
class DetectorContext:
    """One unit of detector input: a subject to evaluate, the data to evaluate it against, and
    this detector's resolved per-detector config parameters (from detectors/detectors.yaml via
    config.py's active_detectors()).

    `payload` is intentionally untyped at this layer - see module docstring. `params` defaults
    to an empty mapping so a context can be built without a config round-trip in tests.
    """

    subject: DeviceHourKey
    payload: Any
    params: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class DetectorOutcome:
    """What one detector plugin call returns for one finding candidate - the pre-store half of
    a Finding (see findings.py). A detector decides WHAT it found (status/summary/evidence/
    quality); dispatch.py's run_active_detectors() decides HOW that becomes a versioned,
    stored Finding (detector_name/subject/version/content_hash are stamped by the store, not
    the detector) - the same "plugin returns raw content, framework owns identity/dedup
    bookkeeping" split parsers/framework.py keeps between a parser (returns rows) and
    pipeline/stage1_parsed/merge.py (owns the natural key).

    A detector that finds nothing for a context returns an empty iterable - not every
    DetectorContext must produce a DetectorOutcome.
    """

    status: Any  # findings.FindingStatus - typed Any here to avoid a findings<->registry import
    # cycle; dispatch.py imports both and does the real stamping. See findings.py for the enum.
    summary: str
    evidence: Mapping[str, Any]
    quality: Any  # findings.FindingQuality - see the status field's note above.


# A detector plugin: takes one DetectorContext, returns zero or more DetectorOutcomes. Mirrors
# parsers/framework.py's ParserFunc (one envelope in, zero or more rows out).
DetectorFunc = Callable[[DetectorContext], Iterable[DetectorOutcome]]


@dataclasses.dataclass(frozen=True)
class RegisteredDetector:
    """One entry in the detector registry: a plugin's identity plus its callable.

    `detector_version` tags the CODE that produced a finding (e.g. "0.1.0" for this ticket's
    demo detector) - a distinct axis from Finding.version in findings.py, which counts
    successive emitted findings for one (detector_name, subject) pair. Bumping
    `detector_version` (a new registration under the same `name` is rejected - see
    DuplicateDetectorError - so a real revision needs either a code change re-running under the
    same name, which is fine and expected, or a deliberately new `name`) lets a human reviewing
    a Finding tell "this came from a newer/older build of the detector" apart from "this is a
    later re-run's opinion for the same build".
    """

    name: str
    detector_version: str
    func: DetectorFunc


# name -> RegisteredDetector. Populated by @register_detector at import time - the same
# "decorate to register" shape parsers/framework.py's module-level _REGISTRY uses. Tests that
# need an isolated registry can pass their own `registry` dict to register_detector()/
# active_detectors()/run_active_detectors() instead of relying on this global one.
Registry = dict[str, RegisteredDetector]
_REGISTRY: Registry = {}


def register_detector(
    name: str, *, detector_version: str, registry: Registry | None = None
) -> Callable[[DetectorFunc], DetectorFunc]:
    """Decorator registering `func` as the detector plugin named `name`.

    `registry` defaults to this module's global registry; pass an explicit dict to register
    into an isolated registry instead (useful in tests, so a throwaway detector registered for
    a fake name doesn't leak into the shared global registry other code relies on) - mirrors
    parsers/framework.py's register_parser().
    """
    target = _REGISTRY if registry is None else registry

    def decorator(func: DetectorFunc) -> DetectorFunc:
        if name in target:
            raise DuplicateDetectorError(
                f"a detector is already registered under name={name!r}"
            )
        target[name] = RegisteredDetector(name=name, detector_version=detector_version, func=func)
        return func

    return decorator


def registered_detectors(*, registry: Registry | None = None) -> tuple[str, ...]:
    """The detector names currently registered, for introspection/tests."""
    target = _REGISTRY if registry is None else registry
    return tuple(target.keys())


def get_registered_detector(
    name: str, *, registry: Registry | None = None
) -> RegisteredDetector | None:
    """Look up one registered detector by name, or None if nothing is registered under it."""
    target = _REGISTRY if registry is None else registry
    return target.get(name)
