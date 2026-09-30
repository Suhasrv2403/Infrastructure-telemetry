"""Stage 3 device history dimension: firmware/hardware/cell-lot/site (+ climate) with validity
periods, and the as-of join that reads it.

Ticket: P2-01 ("Device history dimension (firmware, hardware rev, cell lot, site, climate)").
Done when: "As-of joins return the firmware a device ran at any timestamp."

Depends: P0-11
-----------------
Real P0-11 access (RMA/tickets/dispatch/outage/provisioning extracts) still does not exist -
the access request in docs/access-requests/P0-11-data-access-requests.md has never been sent.
Per explicit project-owner direction (see P0-11's synthetic generator package,
tests/fixtures/generators/access_requests/, and its README), this ticket is built against that
package's synthetic provisioning-history substitute instead of waiting on real access. This
module's `ingest_provisioning_records()` consumes exactly the record shape
`tests/fixtures/generators/access_requests/provisioning.py` produces (`device_id`,
`effective_ts`, `firmware_version`, `hardware_rev`, `cell_lot`, `site_id`) - it does not import
that generator's private `current_as_of()` helper as production code; this module has its own
independent as-of implementation (`as_of_join()` below), and the generator's `current_as_of()`
is used only as a cross-check reference in tests, exactly as that module's own docstring
describes its purpose. If real P0-11 access lands and turns out to be snapshot-only rather than
a change-log (the P0-11 doc's own documented fallback), `ingest_provisioning_records()` would
need a snapshot-diffing front end feeding it the same DeviceHistoryRow shape - the versioned
dimension and as-of join below are agnostic to how the change-log rows were produced.

Versioned dimension shape
--------------------------
Each device's history is a chain of non-overlapping, contiguous `DeviceHistoryRow`s ordered by
`valid_from_ms`: row N's `valid_to_ms` equals row N+1's `valid_from_ms` exactly, and the last
(current) row's `valid_to_ms` is `None` (open-ended - "still in effect, no known end"). This is
the standard SCD2 (slowly-changing-dimension type 2) shape, built directly from the
provisioning change-log's `effective_ts` values: a device's Nth change event becomes valid at
its own `effective_ts` and remains valid until the next change event's `effective_ts` (or
forever, for the most recent change).

Boundary convention: half-open intervals `[valid_from_ms, valid_to_ms)`, closed on the left,
open on the right - same convention this repo already uses for windowed ranges (see
pipeline/stage3_enrich/time_grid.py's `build_grid()`, which takes `[range_start_ms,
range_end_ms)`). Concretely: a query `as_of_ts == some row's effective_ts` lands exactly on that
row's `valid_from_ms` and returns that (new) row, not the previous one - this is what the
ticket description calls out explicitly ("a change's own effective_ts should return the NEW
row, not the old one"). A query at `effective_ts - 1` (one ms before) falls in the *previous*
row's `[valid_from_ms, valid_to_ms)` interval and returns the old row. This matches
`provisioning.py`'s own reference `current_as_of()` docstring ("the row with the largest
effective_ts <= as_of_ts"), which is the same left-closed convention expressed as a max-filter
rather than an interval.

Any-timestamp scope (the ticket's literal "any timestamp")
------------------------------------------------------------
- Before a device's first (commissioning) record: no row is in effect yet. `as_of_join()`
  returns `None` ("unknown"), not the first row and not an exception - a device's provisioning
  state genuinely doesn't exist before it was commissioned.
- Between two records: returns the older of the two (half-open convention above).
- At or after the most recent record, including arbitrarily far in the future: returns that
  last (open-ended) row. This is a deliberate, documented choice - "as of now" or "as of any
  future time" is the same question as "what does this device currently run", since a device's
  state doesn't change again until a new change event is actually recorded, and this dimension
  has no way to know about a change that hasn't happened (or hasn't been ingested) yet. A
  caller that wants to distinguish "genuinely current" from "no data past some known-good
  ingestion horizon" needs to bring that horizon in separately - this module doesn't track an
  ingestion watermark, only the change-log's own timestamps.
- A device with no records at all (unknown device_id) returns `None`, same as "before first
  record" - both mean "this dimension has nothing to say about that device at that time".

Climate - NOT a real field, an assumed placeholder (read this before trusting climate_zone)
-----------------------------------------------------------------------------------------------
The ticket title names "climate" as one of the dimension's fields, but
`tests/fixtures/generators/access_requests/provisioning.py` does not carry any climate concept
at all (it only has firmware_version/hardware_rev/cell_lot/site_id - see that module's own
"Output shape" section), and the real P0-11 provisioning source this synthetic package
substitutes for has never been consulted either. Some other, unrelated branch in this repo
(not part of this branch's history - see `tests/fixtures/generators/powerwall.py`'s own
`CLIMATE_ZONES` constant on that branch) independently invented a per-device climate-zone
concept for Powerwall thermal simulation, but that generator and its fixtures do not exist on
this lineage (confirmed: this branch has no Powerwall/Megapack/Powerpack fixtures or code at
all, only Supercharger - same scope limitation time_grid.py documents for itself). There is
therefore no real or even generator-produced climate signal anywhere on this branch to ingest.

Rather than silently dropping "climate" from the dimension (the ticket names it explicitly) or
fabricating per-device sensor data, this module derives an ASSUMED `site_id -> climate_zone`
label from a small fixed table of zone names, keyed by a deterministic hash of `site_id`
(`_ASSUMED_CLIMATE_ZONES`, `assumed_climate_zone_for_site()` below). This is explicitly a
placeholder standing in for real site/location data that does not exist in this repo - it
carries no thermal baseline, no geographic meaning, and no correctness claim beyond "the same
site_id always maps to the same zone label". It exists so the dimension has *a* value in the
field the ticket names, clearly flagged as invented, rather than an unexplained gap. A real
climate assignment would come from actual site/location metadata (there is no such source
anywhere in this repo yet - see docs/access-requests/) and should replace this function
entirely, not extend it.

Because climate is derived from `site_id`, and `site_id` is itself a versioned field on this
dimension (site reassignment is one of the three change types the provisioning generator
models), `climate_zone` is computed per-row from that row's own `site_id` rather than stored
as a separate ingested field - a device's assumed climate changes exactly when its site does,
which is the only sensible behavior for a derived field over a validity-period dimension.
"""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
from collections.abc import Iterable
from typing import Any

# Own invented set of zone labels, in the same spirit (small fixed table, deterministically
# assigned, explicitly not real climatology) as the unrelated branch's CLIMATE_ZONES constant
# described in the module docstring above - but this repo's own lineage has no such table to
# import, so this is a fresh, independent placeholder, not a copy.
_ASSUMED_CLIMATE_ZONES: tuple[str, ...] = (
    "zone_a_hot_arid",
    "zone_b_mild_coastal",
    "zone_c_cold_continental",
    "zone_d_humid_subtropical",
    "zone_e_temperate_highland",
)


def assumed_climate_zone_for_site(site_id: str) -> str:
    """ASSUMED placeholder climate-zone label for `site_id` - see module docstring's "Climate"
    section. Deterministic (same site_id always yields the same zone) via a stable hash (never
    Python's salted `hash()`, which varies per process and would make this non-reproducible
    across runs/tests), and otherwise carries no real meaning."""
    digest = hashlib.sha256(site_id.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(_ASSUMED_CLIMATE_ZONES)
    return _ASSUMED_CLIMATE_ZONES[index]


@dataclasses.dataclass(frozen=True)
class DeviceHistoryRow:
    """One validity-period row in a device's provisioning history.

    `valid_to_ms` is `None` for the current (most recent) row - open-ended, still in effect.
    `climate_zone` is an ASSUMED placeholder derived from `site_id` - see module docstring.
    """

    device_id: str
    firmware_version: str
    hardware_rev: str
    cell_lot: str
    site_id: str
    climate_zone: str
    valid_from_ms: int
    valid_to_ms: int | None


class DeviceHistoryError(Exception):
    """Base class for device history dimension errors."""


class DuplicateEffectiveTimestampError(DeviceHistoryError):
    """Raised when a device's provisioning records contain two rows sharing the same
    `effective_ts` - the change-log invariant `current_as_of`/`as_of_join` both depend on
    ("the row that was current at time T is always well-defined") breaks if this happens, so
    ingestion fails loudly rather than silently picking one arbitrarily."""


def _history_row_from_record(record: dict[str, Any], valid_to_ms: int | None) -> DeviceHistoryRow:
    site_id = record["site_id"]
    return DeviceHistoryRow(
        device_id=record["device_id"],
        firmware_version=record["firmware_version"],
        hardware_rev=record["hardware_rev"],
        cell_lot=record["cell_lot"],
        site_id=site_id,
        climate_zone=assumed_climate_zone_for_site(site_id),
        valid_from_ms=record["effective_ts"],
        valid_to_ms=valid_to_ms,
    )


def ingest_provisioning_records(
    records: Iterable[dict[str, Any]],
) -> dict[str, tuple[DeviceHistoryRow, ...]]:
    """Build the versioned device-history dimension from provisioning change-log records shaped
    like `tests/fixtures/generators/access_requests/provisioning.py`'s output (one dict per
    change event: `device_id`, `effective_ts`, `firmware_version`, `hardware_rev`, `cell_lot`,
    `site_id`).

    Returns `{device_id: (DeviceHistoryRow, ...)}`, each device's rows sorted ascending by
    `valid_from_ms`, chained so row N's `valid_to_ms == row N+1's valid_from_ms` and the last
    row's `valid_to_ms is None` - see module docstring's "Versioned dimension shape".

    Raises `DuplicateEffectiveTimestampError` if any device has two records sharing an
    `effective_ts` (ambiguous ordering - see that error's docstring). Does not otherwise
    validate the record shape; a malformed record (missing key) raises a plain `KeyError` from
    the field access, consistent with this repo's "fail loudly on a caller/upstream bug rather
    than silently produce a garbage row" convention (see time_grid.py's build_grid()).
    """
    by_device: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_device.setdefault(record["device_id"], []).append(record)

    result: dict[str, tuple[DeviceHistoryRow, ...]] = {}
    for device_id, device_records in by_device.items():
        ordered = sorted(device_records, key=lambda r: r["effective_ts"])
        timestamps = [r["effective_ts"] for r in ordered]
        if len(timestamps) != len(set(timestamps)):
            raise DuplicateEffectiveTimestampError(
                f"device_id={device_id!r} has two provisioning records sharing an "
                "effective_ts - the as-of join is undefined without a strict ordering"
            )

        rows: list[DeviceHistoryRow] = []
        for i, record in enumerate(ordered):
            valid_to_ms = ordered[i + 1]["effective_ts"] if i + 1 < len(ordered) else None
            rows.append(_history_row_from_record(record, valid_to_ms))
        result[device_id] = tuple(rows)

    return result


def as_of_join(
    history: dict[str, tuple[DeviceHistoryRow, ...]],
    device_id: str,
    as_of_ts: int,
) -> DeviceHistoryRow | None:
    """The as-of join: the `DeviceHistoryRow` in effect for `device_id` at `as_of_ts`, i.e. the
    row whose half-open `[valid_from_ms, valid_to_ms)` interval contains `as_of_ts` (see module
    docstring's "Boundary convention" and "Any-timestamp scope" sections for the exact
    semantics at each boundary, including before-first-record, exactly-at-a-change, and
    far-future queries).

    Returns `None` if `device_id` is not in `history` at all, or if `as_of_ts` predates that
    device's earliest (commissioning) record - both mean "this dimension has nothing to say
    about that device at that time", not an error: querying an unknown timestamp is a normal,
    expected use of an as-of join, not caller misuse.
    """
    rows = history.get(device_id)
    if not rows:
        return None

    # `rows` is sorted ascending by valid_from_ms (ingest_provisioning_records()'s contract).
    # Binary search for the last row whose valid_from_ms <= as_of_ts; rows[0].valid_from_ms is
    # a device's commissioning timestamp, so as_of_ts below it means "before this device
    # existed" -> None, checked first as a fast path.
    if as_of_ts < rows[0].valid_from_ms:
        return None

    valid_from_values = [row.valid_from_ms for row in rows]
    # bisect_right gives the insertion point just past any exact match, so subtracting 1 lands
    # on the row whose valid_from_ms is <= as_of_ts and is the largest such value - exactly the
    # half-open-interval row containing as_of_ts, and exactly "largest effective_ts <= as_of_ts"
    # (provisioning.py's current_as_of() convention) when as_of_ts lands exactly on a
    # valid_from_ms.
    index = bisect.bisect_right(valid_from_values, as_of_ts) - 1
    return rows[index]
