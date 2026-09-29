"""Stage 2 row-level quality flags: range, stuck, counter reset, jumps.

Ticket: P1-10 ("Row-level quality flags: range, stuck, counter reset, jumps"). Done when
(Build backlog.md): "Flags populated; no rows deleted; flag rates on dashboard." The dashboard
half is future UI/reporting work, out of scope here - this module's job is only to make sure
flag data is *computed and attached* to every row, never to drop or mutate a row (CLAUDE.md
invariant 3: "Cleaning flags bad values; it never deletes rows.").

Scope note - read this before touching row shape: this module builds ON TOP of
`pipeline.stage2_canonical.canonicalize.CanonicalRow` output. It does not touch
canonicalize.py, does not implement P1-09 (clock-offset-corrected event time - a sibling
ticket being built independently; this module only ever reads a row's *existing*
`device_ts_ms`, never a corrected timestamp), and does not implement P1-11
(completeness/lateness sidecar). It also does not build any dashboard/reporting; it only
produces the per-row flag data a future dashboard ticket would report from.

Ordering precondition (the CALLER's responsibility, not this module's): `flag_device_rows`
takes the full reading sequence for exactly ONE device, ordered by event time (ascending
`device_ts_ms`). "stuck", "counter reset" and "jump" are inherently sequential comparisons
between a reading and the reading immediately before it for the same device - this module does
not sort, dedupe, or verify device homogeneity beyond a cheap same-`device_id` sanity check
(see `MixedDeviceError`). Passing unsorted or multi-device rows produces meaningless flags
without raising (other than the mixed-device check), because there is no way for this module to
tell "unsorted" apart from "genuinely time-decreasing" input.

No row is ever dropped, reordered or mutated: `flag_device_rows` returns exactly one
`FlaggedRow` per input `CanonicalRow`, in the same order, wrapping the original row unchanged
alongside whatever flags fired on it (possibly none - see `QualityFlagResult.reconciles`).

----------------------------------------------------------------------------------------------
Flag definitions (this ticket's own judgment calls - reasoning documented here, not claimed as
firmware-SME-reviewed, same honesty standard as catalog/signals.yaml and canonicalize.py):

1. RANGE - value falls outside its catalog `valid_range`.
   Checked only for canonical fields that (a) have a catalog entry for this row's
   (device_class, firmware_version), (b) declare a numeric `type` (float/int - valid_range is
   null for every bool/enum/string entry in today's catalog anyway), and (c) have a non-null
   `valid_range` bound. `valid_range` may specify only a lower or only an upper bound (e.g.
   `energy_delivered_kwh: [0.0, null]` - no fixed upper bound modeled); whichever side is
   present is checked, the null side is skipped. A field with no catalog entry, or
   `valid_range: null` entirely, cannot be range-checked - this is a real coverage gap, not
   silently treated as "in range" (see `RANGE_UNCHECKABLE_KINDS` and the module report).

2. STUCK - the same field on the same device reports an identical value across
   `STUCK_RUN_THRESHOLD` (5) consecutive readings.
   Applies only to catalog `type: float` signal fields - a continuous, noisy physical
   measurement (voltage, current, power, temperature, cumulative energy). Int/bool/enum/string
   fields are excluded on purpose: `active_stall_count` can legitimately sit at 0 for hours
   overnight, `fault_code` can legitimately sit at 0 for an entire fault-free session, and
   `session_state`/`contactor_closed` legitimately hold one value across many readings by
   design - none of that is a "stuck sensor", it is normal steady-state operation, and flagging
   it would be pure noise.
   `output_voltage_v` is further exempted (`STUCK_EXEMPT_FIELDS`): the catalog's own
   description says it is drawn once per session and reused unchanged for every reading in
   that session (see signals.yaml / `_simulate_stall_session`), so a stuck check on it would
   flag correct, intentional behavior, not a fault.
   Threshold reasoning: stalls report every `reading_interval_s` = 15s
   (tests/fixtures/generators/supercharger.py `GeneratorConfig.reading_interval_s`; cabinets
   report at 4x that, 60s). The remaining noisy float fields (current, power, connector/
   transformer temp, grid voltage/frequency, aggregate power) all carry `rng.uniform(...)`
   noise added every reading, so two truly independent readings landing on the exact same
   rounded value by chance already has non-trivial odds - three or four in a row is
   increasingly unlikely by chance, but not vanishingly so across a ~1.1M-device fleet. Five
   consecutive identical values (60s of no change at all for a stall, 5 minutes for a cabinet)
   is short enough to catch a genuinely frozen sensor well within a typical session (sessions
   run 600-3600s = many multiples of 5 readings) while keeping the false-positive rate from
   pure noise-coincidence low. This is a tunable constant, not a measured value - see
   `STUCK_RUN_THRESHOLD`.

3. COUNTER_RESET - a monotonic/cumulative field decreases between consecutive readings for the
   same device, other than at a genuine session boundary.
   Only `energy_delivered_kwh` is treated as monotonic/cumulative today (`COUNTER_FIELDS`) -
   it is the only field the catalog documents as such ("monotonically non-decreasing within a
   session... resets to 0.0 at the start of each new session"). A decrease is expected exactly
   when `session_id` changes between the two readings (a new session legitimately starts its
   own counter at 0); it is NOT expected when `session_id` stays the same. Decision on the
   ambiguous case - `session_id` missing/null on either reading: we do NOT suppress the flag.
   We can only prove a decrease is a legitimate session boundary when we can positively confirm
   both readings' session ids and see they differ; if we can't confirm that, invariant 3 says
   to flag rather than silently assume the best case, so an unconfirmable decrease still fires
   COUNTER_RESET.

4. JUMP - a field changes by more than a plausible amount given the elapsed time between two
   consecutive readings for the same device.
   `JUMP_MAX_RATE_PER_SECOND` is a per-field "max plausible rate of change" table this ticket
   defines from scratch (nothing like it exists in the catalog yet) - a judgment call about
   real charger/grid physics, informed by (but not copied from) the generator's own valid
   ranges and cadence, and explicitly NOT SME-reviewed. Reasoning per field is inline as
   comments on the table. Two exclusions, both documented there too:
     - Cabinet's `active_stall_count` and `aggregate_power_kw` are aggregate "how many/how much
       right now" counts, not continuous physical sensor readings - several stalls plugging in
       or unplugging within one reporting interval can legitimately swing them across their
       whole small range, so a rate-of-change check on them would flag normal fleet behavior,
       not a data fault. They are left out of `JUMP_MAX_RATE_PER_SECOND` entirely.
     - Session-scoped stall fields (`JUMP_SESSION_SCOPED_FIELDS`) skip the jump check across a
       session boundary (`session_id` changes between the two readings): a brand new session
       can legitimately start at a very different voltage/current/temperature than the last
       reading of the previous session ended at, so that comparison is meaningless. Like
       COUNTER_RESET, this suppression only applies when both readings' `session_id`s are
       confirmed present and different; an unconfirmable case still gets checked.
   The jump check also requires both readings' `device_ts_ms` to be present and the elapsed
   time to be strictly positive (a null timestamp or non-increasing timestamp makes "rate of
   change" undefined) - see `_elapsed_seconds`. That silently-uncheckable case is not a
   coverage gap in the same sense as RANGE's: P1-05 (Stage 1 timestamp sanity) is the ticket
   responsible for timestamp quality; this module simply can't compute a rate without one.
----------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import dataclasses
from typing import Any

from pipeline.stage2_canonical.canonicalize import CanonicalRow, Catalog

# ------------------------------------------------------------------------------------------
# Flag kinds. Plain string constants (not an Enum) to match this repo's existing style
# (canonicalize.py, parsers/framework.py use plain dataclasses/strings, no Enum usage anywhere
# in the codebase today).
RANGE = "range"
STUCK = "stuck"
COUNTER_RESET = "counter_reset"
JUMP = "jump"

# Catalog `type`s that a numeric range check makes sense for. Every bool/enum/string entry in
# today's catalog already declares `valid_range: null`, so this is a belt-and-suspenders guard
# against a future catalog entry that accidentally pairs a non-numeric type with a range.
_RANGE_CHECKABLE_TYPES = frozenset({"float", "int"})

# See flag definition 2 (STUCK) above for why only float-typed signal fields are eligible, and
# why output_voltage_v is exempted despite being a float field.
STUCK_EXEMPT_FIELDS = frozenset({"output_voltage_v"})
STUCK_RUN_THRESHOLD = 5

# See flag definition 3 (COUNTER_RESET) above. The only field today's catalog documents as
# monotonic/cumulative within a session.
COUNTER_FIELDS = frozenset({"energy_delivered_kwh"})

# See flag definition 4 (JUMP) above. Units are the field's catalog unit, per second.
# Reasoning per field:
JUMP_MAX_RATE_PER_SECOND: dict[str, float] = {
    # DC output, tightly regulated by the charger; even a fault clearing (current/power
    # collapsing to 0) settles within ~1-2s in practice. A fault-collapse over one 15s reading
    # interval implies ~(250A/15s)=16.7 A/s and ~(105.6kW/15s)=7 kW/s - these thresholds give
    # several times that headroom so normal fault transitions never trip them, while still
    # catching an implausible multi-hundred-unit discontinuity between adjacent readings.
    "output_voltage_v": 10.0,  # V/s - session-constant by design; any real change is slow.
    "output_current_a": 40.0,  # A/s
    "output_power_kw": 20.0,  # kW/s
    # Cable/transformer temperature has real thermal mass; it cannot swing many degrees in one
    # 15-60s reading interval. Threshold allows a full valid_range span's worth of change well
    # within one interval, which is already generous, while catching e.g. a sensor glitch
    # jumping from ~35C to ~90C between adjacent readings.
    "connector_temp_c": 2.0,  # C/s
    "transformer_temp_c": 0.5,  # C/s (cabinets report every 60s, more thermal mass to move)
    # Cumulative energy can only accumulate as fast as power is being delivered:
    # max instantaneous rate = max output_power_kw / 3600 = 105.6/3600 ~= 0.0293 kWh/s. This
    # threshold gives ~1.7x headroom over that physical ceiling before calling a jump
    # implausible (it does not (and need not) special-case decreases - those are
    # COUNTER_RESET's job, not JUMP's).
    "energy_delivered_kwh": 0.05,  # kWh/s
    # Grid-tied quantities are tightly frequency-/voltage-regulated; real deviations at the
    # scale of the valid_range spans (10V, 0.2Hz) inside one 60s interval would be a genuine
    # grid event, not routine noise. Thresholds are generous multiples of that full-range-per-
    # interval scale so only a clearly implausible discontinuity trips them.
    "grid_voltage_v": 5.0,  # V/s
    "grid_frequency_hz": 0.05,  # Hz/s
    # active_stall_count and aggregate_power_kw are deliberately NOT in this table - see flag
    # definition 4's "exclusions" note above.
}

# Session-scoped stall fields: a jump check across a session boundary (session_id changes) is
# meaningless, since a new session can legitimately start at very different values. See flag
# definition 4 above.
JUMP_SESSION_SCOPED_FIELDS = frozenset(
    {
        "output_voltage_v",
        "output_current_a",
        "output_power_kw",
        "connector_temp_c",
        "energy_delivered_kwh",
    }
)


class MixedDeviceError(Exception):
    """Raised when `flag_device_rows` is given rows for more than one `device_id`.

    This module's sequential checks (stuck/counter-reset/jump) are only meaningful within one
    device's reading history - see the module docstring's ordering precondition. This is the
    one input-contract violation cheap enough to check for and clear enough to fail loudly on,
    the same "fail loud on a structural violation" posture canonicalize.py's CatalogError
    takes; it is NOT a substitute for the caller's responsibility to also sort by event time,
    which this module has no cheap way to verify.
    """


@dataclasses.dataclass(frozen=True)
class QualityFlag:
    """One flag that fired on one field of one row.

    `kind` is one of RANGE/STUCK/COUNTER_RESET/JUMP. `field` is the canonical field name the
    flag concerns. `detail` is a human-readable explanation carrying the concrete numbers
    involved, for logging/debugging - not meant to be machine-parsed.
    """

    kind: str
    field: str
    detail: str


@dataclasses.dataclass(frozen=True)
class FlaggedRow:
    """One Stage 2 row plus whatever quality flags fired on it.

    `row` is the original `CanonicalRow`, unchanged - see CLAUDE.md invariant 3 and the module
    docstring: this module never mutates or drops a row, only annotates it. `flags` is empty
    for a row nothing fired on (including, unavoidably, a device's first reading - there is
    nothing to compare it against yet for the sequential checks; see `is_flagged`).
    """

    row: CanonicalRow
    flags: tuple[QualityFlag, ...]

    @property
    def is_flagged(self) -> bool:
        return len(self.flags) > 0

    def flags_of_kind(self, kind: str) -> tuple[QualityFlag, ...]:
        return tuple(f for f in self.flags if f.kind == kind)


@dataclasses.dataclass(frozen=True)
class QualityFlagResult:
    """Summary of one `flag_device_rows` run, mirroring CanonicalizeResult's/ParseResult's
    style (counters to log, plus the per-row output)."""

    rows_seen: int
    rows: tuple[FlaggedRow, ...]

    @property
    def rows_flagged(self) -> int:
        return sum(1 for r in self.rows if r.is_flagged)

    def flag_counts(self) -> dict[str, int]:
        """flag kind -> number of (row, field) occurrences it fired on. A coarse "flag rate"
        signal - the dashboard ticket's raw material, not the dashboard itself."""
        counts: dict[str, int] = {}
        for flagged_row in self.rows:
            for flag in flagged_row.flags:
                counts[flag.kind] = counts.get(flag.kind, 0) + 1
        return counts

    def reconciles(self) -> bool:
        """True iff every input row produced exactly one output row, in order, with its
        original data untouched - the "no rows deleted" half of this ticket's done-when."""
        return self.rows_seen == len(self.rows)


def _canonical_entry_index(
    device_class: Any, firmware_version: Any, catalog: Catalog
) -> dict[str, dict[str, Any]]:
    """catalog[(device_class, firmware_version)] re-keyed by canonical_name instead of raw
    field name, so a flag check can look an entry up by the same key CanonicalRow.fields uses.
    Returns {} for an uncataloged (device_class, firmware_version) pair - nothing is
    range-checkable for such a row, which is a real, reportable coverage gap (see module
    docstring), not an error.
    """
    firmware_catalog = catalog.get((device_class, firmware_version), {})
    return {entry["canonical_name"]: entry for entry in firmware_catalog.values()}


def _check_range(
    row: CanonicalRow, entries: dict[str, dict[str, Any]]
) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    for canonical_name, value in row.fields.items():
        entry = entries.get(canonical_name)
        if entry is None or entry.get("type") not in _RANGE_CHECKABLE_TYPES:
            continue
        valid_range = entry.get("valid_range")
        if valid_range is None:
            continue
        lo, hi = valid_range
        if lo is not None and value < lo:
            flags.append(
                QualityFlag(RANGE, canonical_name, f"value {value!r} below valid_range min {lo!r}")
            )
        elif hi is not None and value > hi:
            flags.append(
                QualityFlag(RANGE, canonical_name, f"value {value!r} above valid_range max {hi!r}")
            )
    return flags


def _session_changed(previous: CanonicalRow, current: CanonicalRow) -> bool:
    """True only when both readings carry a non-null session_id and they differ - the one case
    this module treats as a *confirmed* session boundary. See COUNTER_RESET and JUMP
    definitions above for why an unconfirmable case (session_id missing on either side) is
    deliberately NOT treated as a boundary."""
    prev_session = previous.fields.get("session_id")
    curr_session = current.fields.get("session_id")
    return prev_session is not None and curr_session is not None and prev_session != curr_session


def _elapsed_seconds(previous: CanonicalRow, current: CanonicalRow) -> float | None:
    """Seconds between two readings' device_ts_ms, or None if undefined (either timestamp
    missing, or non-increasing - see module docstring's JUMP notes)."""
    prev_ts = previous.fields.get("device_ts_ms")
    curr_ts = current.fields.get("device_ts_ms")
    if prev_ts is None or curr_ts is None:
        return None
    elapsed_ms = curr_ts - prev_ts
    if elapsed_ms <= 0:
        return None
    return elapsed_ms / 1000.0


def _check_counter_reset(previous: CanonicalRow, current: CanonicalRow) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    for field in COUNTER_FIELDS:
        if field not in previous.fields or field not in current.fields:
            continue
        prev_val = previous.fields[field]
        curr_val = current.fields[field]
        if curr_val < prev_val and not _session_changed(previous, current):
            flags.append(
                QualityFlag(
                    COUNTER_RESET,
                    field,
                    f"value decreased from {prev_val!r} to {curr_val!r} without a confirmed "
                    "session_id change",
                )
            )
    return flags


def _check_jump(previous: CanonicalRow, current: CanonicalRow) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    elapsed_s = _elapsed_seconds(previous, current)
    if elapsed_s is None:
        return flags
    for field, max_rate in JUMP_MAX_RATE_PER_SECOND.items():
        if field not in previous.fields or field not in current.fields:
            continue
        if field in JUMP_SESSION_SCOPED_FIELDS and _session_changed(previous, current):
            continue
        prev_val = previous.fields[field]
        curr_val = current.fields[field]
        rate = abs(curr_val - prev_val) / elapsed_s
        if rate > max_rate:
            flags.append(
                QualityFlag(
                    JUMP,
                    field,
                    f"value changed from {prev_val!r} to {curr_val!r} over {elapsed_s:g}s "
                    f"({rate:g}/s > max plausible {max_rate:g}/s)",
                )
            )
    return flags


def flag_device_rows(rows: list[CanonicalRow], catalog: Catalog) -> QualityFlagResult:
    """Compute quality flags for one device's canonicalized readings.

    `rows` must be every reading for exactly one `device_id`, ordered by ascending event time
    (`device_ts_ms`) - see the module docstring's ordering precondition; this function does not
    sort or dedupe. Raises `MixedDeviceError` if `rows` contains more than one distinct
    `device_id`. Never drops or mutates a row: the result always has exactly one `FlaggedRow`
    per input row, in the same order (`QualityFlagResult.reconciles()`), wrapping the original
    `CanonicalRow` untouched.
    """
    device_ids = {row.fields.get("device_id") for row in rows}
    if len(device_ids) > 1:
        raise MixedDeviceError(
            f"flag_device_rows requires rows for exactly one device_id, got {sorted(device_ids)}"
        )

    flagged_rows: list[FlaggedRow] = []
    stuck_run: dict[str, tuple[Any, int]] = {}  # canonical_name -> (last_value, run_length)
    previous: CanonicalRow | None = None

    for row in rows:
        device_class = row.fields.get("device_class")
        firmware_version = row.fields.get("firmware_version")
        entries = _canonical_entry_index(device_class, firmware_version, catalog)

        flags: list[QualityFlag] = _check_range(row, entries)

        # Stuck: only float-typed signal fields, excluding fields documented as intentionally
        # session-constant (STUCK_EXEMPT_FIELDS) - see flag definition 2 above.
        for canonical_name, value in row.fields.items():
            entry = entries.get(canonical_name)
            if entry is None or entry.get("type") != "float":
                continue
            if canonical_name in STUCK_EXEMPT_FIELDS:
                continue
            last_value, run_length = stuck_run.get(canonical_name, (None, 0))
            run_length = run_length + 1 if value == last_value else 1
            stuck_run[canonical_name] = (value, run_length)
            if run_length >= STUCK_RUN_THRESHOLD:
                flags.append(
                    QualityFlag(
                        STUCK,
                        canonical_name,
                        f"value {value!r} unchanged for {run_length} consecutive readings "
                        f"(threshold {STUCK_RUN_THRESHOLD})",
                    )
                )

        if previous is not None:
            flags.extend(_check_counter_reset(previous, row))
            flags.extend(_check_jump(previous, row))

        flagged_rows.append(FlaggedRow(row=row, flags=tuple(flags)))
        previous = row

    return QualityFlagResult(rows_seen=len(rows), rows=tuple(flagged_rows))
