"""Stage 2 canonicalization: raw Stage 1 fields -> canonical signal names/types/units.

Ticket: P1-08 ("Stage 2 canonicalization via signal catalog"). Done when (Build backlog.md):
"Canonical names, types and units for all pilot signals."

Scope note - read this before touching row shape: CLAUDE.md describes Stage 2 as "same grain,
canonical signals/units, corrected event time, quality flags." This module is ONLY the
"canonical signals/units" part. Corrected event time is P1-09 (clock-offset correction);
row-level quality flags are P1-10. Neither is implemented here, and nothing in this module
should grow toward either - a Stage 2 row coming out of `canonicalize_row` still carries its
raw `device_ts_ms` unmodified and carries no quality-flag fields.

Catalog: `catalog/signals.yaml`, loaded via `load_catalog`/`CATALOG_PATH`. That file is the v0
DRAFT Supercharger catalog copied verbatim from branch `P0-10-signal-catalog-v0` (see
catalog/README.md and the PROVENANCE note at the top of signals.yaml) - real content, but NOT
yet firmware-SME-reviewed. This module trusts whatever `(device_class, firmware_version, raw
field)` entries the catalog happens to contain and makes no claim those entries are correct;
that review is P0-10's job, not this one's.

Behavior on catalog coverage gaps (an intentional, stated choice - not a silent gap):
  - A raw field with NO catalog entry for its row's (device_class, firmware_version) is DROPPED
    from the canonical output. It is not part of the canonical "pilot signals" this ticket
    covers. This is deliberately not quarantine (P1-05 already owns timestamp quarantine at
    Stage 1) and not a per-row quality flag (P1-10's job) - it is just omission from Stage 2's
    signal set, counted so catalog completeness gaps are visible (see CanonicalizeResult).
  - A raw field that HAS a catalog entry but whose value can't be cast to the catalog's
    declared `type` raises CanonicalizationError for that row (see `canonicalize_row`) /
    degrades that one row to a per-row failure counted in `rows_failed` (see
    `canonicalize_rows`) - never silently coerced into something nonsensical (e.g. never
    silently turning an uncastable string into 0.0).
  - Identity/structural fields (`device_id`, `device_class`, `firmware_version`, `device_ts_ms`,
    `payload_hash`, `arrival_ts_ms`) always pass through unchanged, regardless of whether the
    catalog happens to also list them (the catalog does list `device_ts_ms`/`payload_hash`,
    tagged `kind: envelope_adjacent`, precisely because they are not measured signals - see
    signals.yaml's own header comment). They are row identity/merge-key bookkeeping (CLAUDE.md
    invariant 2), not something to canonicalize.

Explicitly NOT covered, also worth stating plainly: `site_id`, emitted by every registered
Supercharger parser (parsers/supercharger_stall/_common.py, parsers/supercharger_cabinet/
_common.py) as row identity, is neither in the identity-field allowlist this ticket's brief
specifies nor a cataloged raw field in signals.yaml. It is therefore DROPPED from canonical
output by the same "no catalog entry" rule as any other uncataloged field. This is a real,
reportable gap, not an oversight papered over - see this ticket's final report.

No unit *conversion* happens here. `unit` is carried forward as informational metadata only
(see CanonicalRow.units) - the catalog skeleton this was built against does not model
conversion factors, and none is needed for pilot-signal canonical naming/typing/unit-labeling.
"""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import yaml

# Row-identity / structural fields that are never looked up in the catalog and always pass
# through unchanged - see module docstring. Deliberately does NOT include `site_id`; see the
# module docstring's "Explicitly NOT covered" section for why that's a stated, not silent, gap.
IDENTITY_FIELDS = frozenset(
    {
        "device_id",
        "device_class",
        "firmware_version",
        "device_ts_ms",
        "payload_hash",
        "arrival_ts_ms",
    }
)

# Catalog types this module knows how to cast. Anything else in a catalog entry's `type` is a
# catalog authoring bug, not a row-data problem - see load_catalog.
_KNOWN_TYPES = frozenset({"float", "int", "bool", "enum", "string"})

CATALOG_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "catalog" / "signals.yaml"


class CanonicalizationError(Exception):
    """Base class for Stage 2 canonicalization errors."""


class CatalogError(CanonicalizationError):
    """Raised when the loaded catalog itself is malformed (an authoring bug in signals.yaml,
    not a row-data problem)."""


class CastError(CanonicalizationError):
    """Raised when a raw field's value can't be cast to its catalog-declared type.

    Carries the raw field name and firmware pair so a caller (or `canonicalize_rows`, which
    catches this per-row) can report exactly what failed without re-deriving it.
    """

    def __init__(self, raw_field: str, declared_type: str, value: Any, reason: str):
        self.raw_field = raw_field
        self.declared_type = declared_type
        self.value = value
        super().__init__(
            f"field {raw_field!r} value {value!r} could not be cast to declared type "
            f"{declared_type!r}: {reason}"
        )


# catalog[(device_class, firmware_version)][raw_field_name] -> entry dict (canonical_name,
# unit, type, ...). Flattened from signals.yaml's nested device_class -> firmware_version ->
# raw_field shape once at load time so per-row lookups are O(1) dict access.
Catalog = dict[tuple[str, str], dict[str, dict[str, Any]]]


def load_catalog(path: pathlib.Path | str = CATALOG_PATH) -> Catalog:
    """Load and flatten catalog/signals.yaml into `catalog[(device_class, firmware)][raw_field]`.

    Raises CatalogError if a firmware block's entries aren't a mapping of raw_field -> entry
    dict, or an entry is missing `canonical_name`/`type` (the two fields canonicalize_row can't
    do without) - fail loudly at load time rather than raising a confusing KeyError per-row
    deep in canonicalize_row.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    catalog: Catalog = {}
    for key, value in raw.items():
        if key in ("catalog_status", "catalog_version", "source_generator") or not isinstance(
            value, dict
        ):
            # Top-level metadata keys (see signals.yaml's own header), not a device_class block.
            continue
        device_class = key
        for firmware_version, fields in value.items():
            if not isinstance(fields, dict):
                raise CatalogError(
                    f"{device_class!r}/{firmware_version!r} is not a mapping of raw_field -> "
                    "entry in signals.yaml"
                )
            for raw_field, entry in fields.items():
                if not isinstance(entry, dict) or "canonical_name" not in entry or "type" not in entry:
                    raise CatalogError(
                        f"{device_class!r}/{firmware_version!r}/{raw_field!r} is missing "
                        "canonical_name and/or type in signals.yaml"
                    )
                catalog.setdefault((device_class, str(firmware_version)), {})[raw_field] = entry

    return catalog


def _cast_value(raw_field: str, declared_type: str, value: Any) -> Any:
    """Cast `value` to `declared_type`, raising CastError rather than silently coercing.

    float/int: standard Python conversion, but explicitly rejects None and bool-as-numeric
    surprises are left as-is (bool IS an int subclass in Python; a catalog entry typed `int`
    genuinely accepting a bool value is fine - the catalog, not this function, defines
    semantics).
    bool: only accepts an already-bool value, or the literal strings "true"/"false"
    (case-insensitive) - NOT `bool(value)`, since `bool("false")` is True in Python and would
    silently coerce a value that looks like "no" into "yes".
    enum/string: str(value) - enum validation against `enum_values` is intentionally not
    enforced here (that's closer to P1-10 quality-flag territory); the catalog entry's
    enum_values remains available to a caller that wants to check it.
    """
    if value is None:
        raise CastError(raw_field, declared_type, value, "value is None, cannot cast")

    try:
        if declared_type == "float":
            return float(value)
        if declared_type == "int":
            if isinstance(value, str):
                return int(value)
            if isinstance(value, float) and not value.is_integer():
                raise CastError(
                    raw_field, declared_type, value, "non-integral float cannot cast to int"
                )
            return int(value)
        if declared_type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                return value.strip().lower() == "true"
            raise CastError(
                raw_field, declared_type, value, "not a bool or a recognizable bool string"
            )
        if declared_type in ("enum", "string"):
            return str(value)
    except CastError:
        raise
    except (TypeError, ValueError) as exc:
        raise CastError(raw_field, declared_type, value, str(exc)) from exc

    raise CatalogError(
        f"field {raw_field!r} declares unsupported catalog type {declared_type!r} "
        f"(known types: {sorted(_KNOWN_TYPES)})"
    )


@dataclasses.dataclass(frozen=True)
class CanonicalRow:
    """One canonicalized Stage 2 row.

    `fields` holds identity fields unchanged plus every successfully canonicalized signal,
    keyed by canonical_name. `units` is informational metadata only (canonical_name -> unit
    string), carried forward from the catalog with no conversion applied - see module
    docstring. `dropped_fields` lists the raw field names from the input row that had no
    catalog entry for this row's (device_class, firmware_version) and were therefore omitted.
    """

    fields: dict[str, Any]
    units: dict[str, str]
    dropped_fields: tuple[str, ...]


def canonicalize_row(row: dict[str, Any], catalog: Catalog) -> CanonicalRow:
    """Canonicalize one Stage 1 row using `catalog`.

    Raises CastError if a cataloged field's value can't be cast to its declared type (see
    module docstring - this is not silently coerced). Raises nothing for an uncataloged
    non-identity field; it is simply dropped and recorded in the result's `dropped_fields`.
    """
    device_class = row.get("device_class")
    firmware_version = row.get("firmware_version")
    firmware_catalog = catalog.get((device_class, firmware_version), {})

    fields: dict[str, Any] = {}
    units: dict[str, str] = {}
    dropped: list[str] = []

    for raw_field, value in row.items():
        if raw_field in IDENTITY_FIELDS:
            fields[raw_field] = value
            continue

        entry = firmware_catalog.get(raw_field)
        if entry is None:
            dropped.append(raw_field)
            continue

        canonical_name = entry["canonical_name"]
        declared_type = entry["type"]
        fields[canonical_name] = _cast_value(raw_field, declared_type, value)
        unit = entry.get("unit")
        if unit is not None:
            units[canonical_name] = unit

    return CanonicalRow(fields=fields, units=units, dropped_fields=tuple(dropped))


@dataclasses.dataclass(frozen=True)
class CanonicalizeResult:
    """Summary of one canonicalization run, mirroring ParseResult's/CaptureResult's style
    (see parsers/framework.py, pipeline/stage0_landing/capture.py): counters to log, plus the
    per-row canonical output and per-row cast failures.
    """

    rows_seen: int
    rows_canonicalized: int
    rows: tuple[CanonicalRow, ...]
    failed_rows: tuple[tuple[dict[str, Any], CastError], ...]

    @property
    def rows_failed(self) -> int:
        return len(self.failed_rows)

    @property
    def fields_dropped_total(self) -> int:
        """Total count, across all successfully canonicalized rows, of raw fields dropped for
        lacking a catalog entry - a coarse signal of catalog completeness gaps."""
        return sum(len(row.dropped_fields) for row in self.rows)

    def dropped_field_counts(self) -> dict[str, int]:
        """raw_field_name -> number of successfully-canonicalized rows it was dropped from.

        Useful, more actionable signal for whoever reviews catalog completeness than the flat
        total: which specific raw fields are missing catalog entries, and how often.
        """
        counts: dict[str, int] = {}
        for row in self.rows:
            for field in row.dropped_fields:
                counts[field] = counts.get(field, 0) + 1
        return counts

    def reconciles(self) -> bool:
        """True iff every row seen was either canonicalized or recorded as a cast failure -
        never both, never neither."""
        return self.rows_seen == self.rows_canonicalized + len(self.failed_rows)


def canonicalize_rows(rows: list[dict[str, Any]], catalog: Catalog) -> CanonicalizeResult:
    """Canonicalize a batch of Stage 1 rows.

    A row whose value can't be cast to its catalog-declared type degrades that single row to a
    recorded failure (`failed_rows`, pairing the original row with the CastError) rather than
    raising and losing the whole batch - the same "one bad item never crashes the batch"
    pattern parsers/framework.py's parse_messages uses for quarantine.
    """
    rows_seen = 0
    canonical_rows: list[CanonicalRow] = []
    failed_rows: list[tuple[dict[str, Any], CastError]] = []

    for row in rows:
        rows_seen += 1
        try:
            canonical_rows.append(canonicalize_row(row, catalog))
        except CastError as exc:
            failed_rows.append((row, exc))

    return CanonicalizeResult(
        rows_seen=rows_seen,
        rows_canonicalized=len(canonical_rows),
        rows=tuple(canonical_rows),
        failed_rows=tuple(failed_rows),
    )
