"""Declarative detector configuration: detectors/detectors.yaml -> resolved, active detectors.

Ticket: P2-06's "done when" requires detectors "declared in config", not hardcoded into a
pipeline. Mirrors catalog/signals.yaml's precedent (pipeline/stage2_canonical/canonicalize.py's
load_catalog()) of a real, loaded YAML config file - not code-level registration alone - as the
place a human declares which detectors are active and with what parameters.

The split is deliberate: registry.py's @register_detector populates a CODE-level registry (a
detector exists and is runnable); this module resolves detectors/detectors.yaml (which
detectors are DECLARED active, and with what per-detector params) against that registry. A
config entry naming a detector that was never registered is a configuration error, raised
loudly here at load/resolve time - never a silent skip - mirroring parsers/framework.py's
"unregistered (device_class, firmware_version) -> quarantine, never silently drop" spirit,
adapted to "unregistered detector name in config -> fail the config resolve, don't silently
ignore it and pretend the detector ran".
"""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import yaml

from detectors.framework.registry import (
    DetectorFrameworkError,
    RegisteredDetector,
    Registry,
    get_registered_detector,
)

# detectors/detectors.yaml, alongside detectors/README.md - same "config file lives at the
# package root, loading code lives one level in" layout catalog/signals.yaml +
# pipeline/stage2_canonical/canonicalize.py's CATALOG_PATH use.
DETECTOR_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "detectors.yaml"
)


class DetectorConfigError(DetectorFrameworkError):
    """Raised when detectors/detectors.yaml itself is malformed (an authoring bug in the
    config file, not a registry mismatch - see UnregisteredDetectorError for that case)."""


class UnregisteredDetectorError(DetectorFrameworkError):
    """Raised when an ENABLED config entry names a detector that was never registered via
    @register_detector. This is a configuration error, surfaced loudly at resolve time - never
    a silent skip that would let a declared-active detector quietly not run. See module
    docstring.
    """


@dataclasses.dataclass(frozen=True)
class DetectorDeclaration:
    """One entry from detectors/detectors.yaml, as authored - before resolving `name` against
    the code registry. `enabled=False` entries are kept (not dropped) by load_detector_config()
    so a config listing can be introspected/tested in full; active_detectors() is what filters
    to enabled-and-resolved."""

    name: str
    enabled: bool
    params: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class ActiveDetector:
    """One declared-active detector, fully resolved: its registry entry plus its config
    params, ready to hand to dispatch.py's run_active_detectors()."""

    name: str
    detector_version: str
    func: Any  # registry.DetectorFunc
    params: dict[str, Any]


def load_detector_config(
    path: pathlib.Path | str = DETECTOR_CONFIG_PATH,
) -> tuple[DetectorDeclaration, ...]:
    """Load and validate detectors/detectors.yaml into a tuple of DetectorDeclaration.

    Expected top-level shape: a `detectors:` key holding a list of mappings, each with at
    least `name` (str) and `enabled` (bool); `params` (mapping) is optional and defaults to
    `{}`. Raises DetectorConfigError for anything else - a malformed config fails loudly at
    load time, the same "fail at load, not deep in a per-row lookup" stance
    canonicalize.py's load_catalog() takes for signals.yaml.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict) or "detectors" not in raw:
        raise DetectorConfigError(
            f"{path}: expected a top-level 'detectors:' list, got {raw!r}"
        )

    entries = raw["detectors"]
    if not isinstance(entries, list):
        raise DetectorConfigError(f"{path}: 'detectors:' must be a list, got {entries!r}")

    declarations: list[DetectorDeclaration] = []
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry or "enabled" not in entry:
            raise DetectorConfigError(
                f"{path}: each detectors[] entry needs 'name' and 'enabled', got {entry!r}"
            )
        name = entry["name"]
        enabled = entry["enabled"]
        if not isinstance(name, str) or not isinstance(enabled, bool):
            raise DetectorConfigError(
                f"{path}: entry 'name' must be a string and 'enabled' a bool, got {entry!r}"
            )
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise DetectorConfigError(
                f"{path}: entry {name!r}'s 'params' must be a mapping, got {params!r}"
            )
        declarations.append(DetectorDeclaration(name=name, enabled=enabled, params=dict(params)))

    return tuple(declarations)


def active_detectors(
    declarations: tuple[DetectorDeclaration, ...],
    *,
    registry: Registry | None = None,
) -> tuple[ActiveDetector, ...]:
    """Resolve `enabled=True` declarations against the detector registry.

    Raises UnregisteredDetectorError, naming every unregistered-but-enabled detector at once
    (not just the first one hit), if any enabled declaration names a detector that was never
    registered via @register_detector - a configuration error, never a silent skip (see module
    docstring). A `enabled=False` declaration for an unregistered name is NOT an error (an
    operator may pre-stage config for a detector whose code hasn't shipped/registered yet, as
    long as it's left off).
    """
    missing: list[str] = []
    resolved: list[ActiveDetector] = []

    for declaration in declarations:
        if not declaration.enabled:
            continue
        entry: RegisteredDetector | None = get_registered_detector(
            declaration.name, registry=registry
        )
        if entry is None:
            missing.append(declaration.name)
            continue
        resolved.append(
            ActiveDetector(
                name=entry.name,
                detector_version=entry.detector_version,
                func=entry.func,
                params=declaration.params,
            )
        )

    if missing:
        raise UnregisteredDetectorError(
            "detectors/detectors.yaml declares detector(s) with no @register_detector "
            f"registration: {sorted(missing)!r} - either register them in code or set "
            "enabled: false in config"
        )

    return tuple(resolved)
