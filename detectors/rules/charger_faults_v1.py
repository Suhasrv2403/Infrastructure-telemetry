"""Rule-based detectors v1: stall-class charger faults, thermal events, session failures and
derates.

Ticket: P2-08 ("Rule detectors v1"). Build backlog.md's literal "done when" for this ticket is
"Rules live with thresholds reviewed by charger SMEs" - no charger SME exists in this sandbox
to review anything, so that half of the done-when cannot be satisfied here. What follows is the
other half: real rules, with real (documented, not hand-waved) reasoning behind every threshold,
each one tagged below with its provenance - taken directly from catalog/signals.yaml's stated
valid_range, or this module's own judgment call. Nothing here should be read as SME-reviewed;
see each function's docstring for exactly which numbers are whose.

Scope: stall-class only (module 1)
------------------------------------
Only `supercharger_stall` canonical rows are read - `session_id`, `session_state`,
`output_power_kw`, `connector_temp_c`, `stall_fault_code`, etc (catalog/signals.yaml). Cabinet-
class detection (`grid_voltage_v`, `transformer_temp_c`, `cabinet_fault_code`) is plausible
future work but explicitly OUT of scope for this ticket - see detectors/README.md, which lists
`rules/` as covering "session failures, derates, module faults, thermal" without naming the
cabinet signal set, and the ticket brief's own "Scope" section. `_stall_rows_sorted` below
raises loudly (ValueError) if it's ever handed a non-stall row, rather than silently ignoring
cabinet data that wandered in.

Payload contract (all four detectors below share this)
-----------------------------------------------------------
`DetectorContext.payload` must be a `tuple[CanonicalRow, ...]` (or any iterable of
`pipeline.stage2_canonical.canonicalize.CanonicalRow`) - every Stage 2 canonical row for ONE
`supercharger_stall` device in ONE event-hour (`context.subject`, a `DeviceHourKey`). This is
the same grain and the same row TYPE `pipeline/stage2_canonical/targeted_recompute.py`'s
`rows_for_device_hour` already produces (grouped by `device_hour_key`) and
`pipeline/stage3_enrich/time_grid.py`'s `build_grid` already consumes - it is not a new shape
invented here, just this ticket's own choice of which existing Stage 2 output to build on
(registry.py's module docstring explicitly leaves this open: "P2-08/.../detectors want to
consume Stage 2 canonical rows ... instead of a Stage 3a grid"). A caller wires this up by
canonicalizing one device-hour's Stage 1 rows (`canonicalize_rows`) and passing the resulting
tuple straight through; these detectors do not themselves canonicalize or filter by device/hour
- that is the caller's job, same as `demo_all_gap_detector.py` does not build its own grid.

Rows need not arrive pre-sorted; every detector below sorts by `device_ts_ms` itself (a `None`
device_ts_ms - see signals.yaml's own note that it "can be null (missing)" - sorts first,
deterministically, rather than crashing a `None`-vs-`int` comparison).

One finding per device-hour, not per session/reading
-----------------------------------------------------------
Each detector below returns at most ONE `DetectorOutcome` per call, aggregating every
qualifying session/reading it found in the hour into that one outcome's evidence, rather than
one outcome per session. This is a deliberate choice, not an oversight: `FindingStore.emit()`
is keyed by `(detector_name, subject)` where `subject` is the device-hour, and a detector that
returned N outcomes for N failing sessions in the same hour would make `dispatch.py` call
`store.emit()` N times in a row for that same subject - each call's differing content would mint
a new Finding VERSION that supersedes the previous one (see findings.py's versioning design),
so only the LAST session's finding would show up as "latest", silently burying the others in
history rather than losing them outright. Aggregating into one outcome keeps "the current state
of this device-hour" complete and avoids that footgun entirely.

Quality labels, honestly explained
--------------------------------------
`FindingQuality.label` here is a plain, coarse confidence tag: "high" when a finding rests on an
explicit, firmware-reported enum/code (`stall_fault_code`, `session_state == "fault"` - the
device itself said so, nothing inferred), "medium" when a finding rests (even partly) on one of
this module's own numeric thresholds (a connector-temp crossing with no accompanying fault code,
or any derate run - see below). `measured_buckets`/`total_buckets` count the readings that
actually justified the finding vs every reading available in the hour, the same "measured vs
total, one grain up from Stage 3a's per-minute coverage" idea `FindingQuality`'s own docstring
describes.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from detectors.framework.findings import FindingQuality, FindingStatus
from detectors.framework.registry import DetectorContext, DetectorOutcome, register_detector
from pipeline.stage2_canonical.canonicalize import CanonicalRow

STALL_DEVICE_CLASS = "supercharger_stall"

# catalog/signals.yaml's stall_fault_code enum_values, with the generator author's own
# commentary labels (signals.yaml: "These labels are the generator author's commentary, not a
# real Supercharger fault-code registry; real meanings and real code values need firmware SME
# confirmation" - carried forward here unchanged, not re-validated by this ticket).
FAULT_CODE_NONE = 0
FAULT_CODE_MODULE = 1001
FAULT_CODE_THERMAL = 1042
FAULT_CODE_COMMS = 2010  # not read by any detector in this module; listed for completeness.

DETECTOR_VERSION = "0.1.0"

MODULE_FAULT_DETECTOR_NAME = "module_fault_v1"
THERMAL_EVENT_DETECTOR_NAME = "thermal_event_v1"
SESSION_FAILURE_DETECTOR_NAME = "session_failure_v1"
DERATE_DETECTOR_NAME = "derate_v1"


def _stall_rows_sorted(payload: Iterable[CanonicalRow]) -> tuple[CanonicalRow, ...]:
    """Validate `payload` is stall-class-only and return it sorted by `device_ts_ms`.

    Raises ValueError (loud failure, not a silent filter) if any row's `device_class` isn't
    `supercharger_stall` - see module docstring's scope note. A missing/None `device_ts_ms`
    (signals.yaml documents this as a real possibility) sorts first rather than raising, since
    Python cannot compare `None < int` directly.
    """
    rows = tuple(payload)
    for row in rows:
        device_class = row.fields.get("device_class")
        if device_class != STALL_DEVICE_CLASS:
            raise ValueError(
                "detectors/rules/charger_faults_v1.py detectors are stall-class only "
                f"(device_class={STALL_DEVICE_CLASS!r}); got a row with "
                f"device_class={device_class!r} - cabinet-class detection is explicitly out of "
                "scope for P2-08 (see this module's docstring)"
            )
    def _sort_key(r: CanonicalRow) -> tuple[bool, int]:
        ts = r.fields.get("device_ts_ms")
        return (ts is None, ts or 0)

    return tuple(sorted(rows, key=_sort_key))


def _group_by_session(rows: tuple[CanonicalRow, ...]) -> dict[str, list[CanonicalRow]]:
    """Group already-sorted rows by `session_id`, preserving each session's device_ts_ms order."""
    sessions: dict[str, list[CanonicalRow]] = defaultdict(list)
    for row in rows:
        sessions[row.fields.get("session_id")].append(row)
    return sessions


# ---------------------------------------------------------------------------------------------
# module_fault_v1
# ---------------------------------------------------------------------------------------------


@register_detector(MODULE_FAULT_DETECTOR_NAME, detector_version=DETECTOR_VERSION)
def detect_module_fault_v1(context: DetectorContext) -> list[DetectorOutcome]:
    """Fire (PROVISIONAL) if any reading in this device-hour reports `stall_fault_code == 1001`
    ("module fault", per the generator's own commentary label in catalog/signals.yaml - the
    catalog itself says that label is not SME-confirmed, and this detector trusts it as-is,
    exactly like `catalog/signals.yaml`'s own stated caveat).

    Threshold provenance: the fault CODE (1001, `params["fault_code"]`, default) is taken
    directly from the catalog's `enum_values` for `stall_fault_code` - not this module's own
    judgment. There is no numeric threshold to tune here (unlike thermal_event_v1/derate_v1):
    an explicit, discrete, firmware-reported fault code is either present or it isn't, so a
    single occurrence is evidence enough to flag - no "sustained N readings" requirement, since
    averaging/requiring repetition would only suppress a real, already-unambiguous signal.

    Evidence: how many such readings, which session(s) they belong to, and the first/last
    `device_ts_ms` among them - so a reviewer can see the fault's extent within the hour without
    re-deriving it from raw rows.
    """
    rows = _stall_rows_sorted(context.payload)
    if not rows:
        return []

    fault_code = context.params.get("fault_code", FAULT_CODE_MODULE)
    fault_rows = [r for r in rows if r.fields.get("stall_fault_code") == fault_code]
    if not fault_rows:
        return []

    device_id, event_hour = context.subject
    session_ids = tuple(sorted({r.fields.get("session_id") for r in fault_rows}))
    evidence = {
        "fault_code": fault_code,
        "count": len(fault_rows),
        "session_ids": session_ids,
        "first_device_ts_ms": fault_rows[0].fields.get("device_ts_ms"),
        "last_device_ts_ms": fault_rows[-1].fields.get("device_ts_ms"),
    }
    quality = FindingQuality(
        label="high", measured_buckets=len(fault_rows), total_buckets=len(rows)
    )
    summary = (
        f"device {device_id} reported {len(fault_rows)} module-fault (code {fault_code}) "
        f"reading(s) across {len(session_ids)} session(s) in hour {event_hour}"
    )
    return [
        DetectorOutcome(
            status=FindingStatus.PROVISIONAL, summary=summary, evidence=evidence, quality=quality
        )
    ]


# ---------------------------------------------------------------------------------------------
# thermal_event_v1
# ---------------------------------------------------------------------------------------------

# Threshold provenance - this module's OWN judgment call, not SME-reviewed, not a catalog value:
# catalog/signals.yaml states connector_temp_c's valid_range as [29.0, 43.7], but that range is
# itself derived purely from reading the SYNTHETIC fixture generator's own noise formula
# (round(30 + power_kw * 0.12 + rng.uniform(-1, 1), 1), with power_kw capped at its own
# output_power_kw valid_range max of 105.6) - i.e. 43.7 is the ceiling of what the GENERATOR
# happens to produce under ordinary full-power charging, not a spec sheet's alarm setpoint or
# any firmware-confirmed safety limit. Alarming AT or BELOW that ceiling would mean flagging
# perfectly ordinary heavy-but-healthy sessions as soon as they approach full power - a charger
# SME would reject that as noise on day one. This detector therefore alarms slightly ABOVE the
# catalog's stated max (44.0C, `params["connector_temp_alarm_c"]`, default) rather than at or
# below it: a reading exceeding even the generator's own modeled full-power/full-noise ceiling
# is outside everything the current (unreviewed) catalog claims is achievable in normal
# operation, which is a defensible bar for "worth a human's attention" while this stays
# unreviewed. This is explicitly a stand-in, not a real thermal alarm setpoint - a firmware SME
# with the connector's actual thermal spec should replace it.
DEFAULT_THERMAL_ALARM_C = 44.0


@register_detector(THERMAL_EVENT_DETECTOR_NAME, detector_version=DETECTOR_VERSION)
def detect_thermal_event_v1(context: DetectorContext) -> list[DetectorOutcome]:
    """Fire (PROVISIONAL) if any reading in this device-hour reports `stall_fault_code == 1042`
    ("thermal", catalog commentary label) OR `connector_temp_c` exceeds
    `params["connector_temp_alarm_c"]` (default `DEFAULT_THERMAL_ALARM_C` - see the module-level
    comment above this function for that number's full reasoning). Either condition alone is
    enough to fire; a single reading over threshold fires, with no "sustained N readings"
    requirement - a firmware-explicit thermal fault code is unambiguous by construction, and the
    threshold was deliberately set above the catalog's own full-power ceiling precisely so that
    a single crossing is already noteworthy (see the reasoning above), unlike derate_v1's
    percentage-of-peak threshold, which genuinely does need sustained confirmation to avoid
    tripping on ordinary CC/CV noise.

    Evidence: the max connector temp seen anywhere in the hour (context, even if it didn't cross
    threshold), how many readings crossed `connector_temp_alarm_c`, and how many carried the
    explicit thermal fault code.
    """
    rows = _stall_rows_sorted(context.payload)
    if not rows:
        return []

    fault_code = context.params.get("fault_code", FAULT_CODE_THERMAL)
    temp_alarm_c = context.params.get("connector_temp_alarm_c", DEFAULT_THERMAL_ALARM_C)

    fault_hit_idx: set[int] = set()
    temp_hit_idx: set[int] = set()
    temps: list[float] = []
    for i, row in enumerate(rows):
        if row.fields.get("stall_fault_code") == fault_code:
            fault_hit_idx.add(i)
        temp = row.fields.get("connector_temp_c")
        if isinstance(temp, (int, float)):
            temps.append(temp)
            if temp > temp_alarm_c:
                temp_hit_idx.add(i)

    if not fault_hit_idx and not temp_hit_idx:
        return []

    device_id, event_hour = context.subject
    contributing = fault_hit_idx | temp_hit_idx
    evidence = {
        "fault_code": fault_code,
        "connector_temp_alarm_c": temp_alarm_c,
        "max_connector_temp_c": max(temps) if temps else None,
        "readings_over_threshold": len(temp_hit_idx),
        "fault_code_hits": len(fault_hit_idx),
    }
    quality = FindingQuality(
        label="high" if fault_hit_idx else "medium",
        measured_buckets=len(contributing),
        total_buckets=len(rows),
    )
    summary = (
        f"device {device_id} had {len(fault_hit_idx)} thermal-fault-code reading(s) and "
        f"{len(temp_hit_idx)} reading(s) over {temp_alarm_c}C (max seen "
        f"{evidence['max_connector_temp_c']}C) in hour {event_hour}"
    )
    return [
        DetectorOutcome(
            status=FindingStatus.PROVISIONAL, summary=summary, evidence=evidence, quality=quality
        )
    ]


# ---------------------------------------------------------------------------------------------
# session_failure_v1
# ---------------------------------------------------------------------------------------------


@register_detector(SESSION_FAILURE_DETECTOR_NAME, detector_version=DETECTOR_VERSION)
def detect_session_failure_v1(context: DetectorContext) -> list[DetectorOutcome]:
    """Fire (PROVISIONAL) for each session (grouped by `session_id`) in this device-hour that
    reaches `session_state == "fault"` (`params["fault_state"]`, default - taken directly from
    catalog/signals.yaml's `session_state` enum, not this module's invention) without a LATER
    reading (by `device_ts_ms`, within this same device-hour's data) reaching
    `session_state == "session_complete"` (`params["complete_state"]`, default, same enum) for
    that session. All qualifying sessions in the hour are combined into one finding (see module
    docstring's "one finding per device-hour" section).

    What "failure" means here, and the judgment call behind it (own reasoning, not SME-reviewed)
    ---------------------------------------------------------------------------------------------
    A session merely still `charging` (or `plugged_in`) at the hour boundary, with NO `fault`
    reading anywhere, does NOT fire - that is an ordinary in-progress session that happens to
    straddle an hour boundary, not a failure, and flagging it would be a false positive on
    perfectly healthy charging (this repo's own fixture generator models multi-hour sessions
    routinely). A session that reaches `fault` and is later followed (within this same hour's
    data) by `session_complete` is treated as RECOVERED, not a failure - the generator's own
    session model normally ends a session the moment it faults with no further readings, so a
    `fault` followed by `session_complete` is a real charger continuing on afterward, which this
    detector reads as resolution, not as "ignore the fault" - the fault event itself is still
    visible in `session_state` history for a human/other detector to inspect if desired; this
    detector's job is specifically "is this session STILL broken", not "did a fault ever occur".
    A session that reaches `fault` and has no later `session_complete` in this hour's data -
    whether because the hour simply ends there, or because later readings (if any, within the
    hour) never reach `session_complete` - counts as an unresolved failure.

    Known limitation, stated plainly: this detector sees only ONE device-hour at a time (the
    grain every detector in this framework operates at - see registry.py). A session that faults
    near an hour boundary and recovers via `session_complete` in the NEXT hour's data will still
    fire here, because that recovery is invisible to this call. A real system wanting to avoid
    that would need to either widen this detector's input window past a single hour or treat a
    provisional finding here as exactly that - provisional, subject to revision once later data
    is available (which invariant 6 / the framework's PROVISIONAL status already accounts for;
    this detector deliberately never emits FINAL - see registry.py's DetectorOutcome and the
    module docstring).
    """
    rows = _stall_rows_sorted(context.payload)
    if not rows:
        return []

    fault_state = context.params.get("fault_state", "fault")
    complete_state = context.params.get("complete_state", "session_complete")

    failed_sessions: list[dict] = []
    for session_id, session_rows in _group_by_session(rows).items():
        fault_indices = [
            i for i, r in enumerate(session_rows) if r.fields.get("session_state") == fault_state
        ]
        if not fault_indices:
            continue
        first_fault_idx = fault_indices[0]
        later_rows = session_rows[first_fault_idx + 1 :]
        resolved = any(r.fields.get("session_state") == complete_state for r in later_rows)
        if resolved:
            continue

        fault_row = session_rows[first_fault_idx]
        last_row = session_rows[-1]
        failed_sessions.append(
            {
                "session_id": session_id,
                "fault_device_ts_ms": fault_row.fields.get("device_ts_ms"),
                "stall_fault_code": fault_row.fields.get("stall_fault_code"),
                "last_session_state": last_row.fields.get("session_state"),
                "last_device_ts_ms": last_row.fields.get("device_ts_ms"),
                "readings_after_fault": len(later_rows),
                "readings_in_session": len(session_rows),
            }
        )

    if not failed_sessions:
        return []

    device_id, event_hour = context.subject
    measured = sum(f["readings_in_session"] for f in failed_sessions)
    quality = FindingQuality(
        label="high", measured_buckets=min(measured, len(rows)), total_buckets=len(rows)
    )
    session_ids = ", ".join(f["session_id"] for f in failed_sessions)
    summary = (
        f"device {device_id} had {len(failed_sessions)} unresolved-fault session(s) in hour "
        f"{event_hour}: {session_ids}"
    )
    return [
        DetectorOutcome(
            status=FindingStatus.PROVISIONAL,
            summary=summary,
            evidence={"failed_sessions": tuple(failed_sessions)},
            quality=quality,
        )
    ]


# ---------------------------------------------------------------------------------------------
# derate_v1 - the most judgment-heavy of the four; every constant below is this module's own
# reasoning, explicitly NOT SME-reviewed, NOT derived from any catalog value, and NOT proven
# against real charger behavior (the fixture generator does not model derates at all - see
# tests/fixtures/generators/supercharger.py - so nothing in this repo's synthetic data can
# validate these numbers either; they are a defensible starting point for a human to correct).
# ---------------------------------------------------------------------------------------------

# A reading counts as "derated" if its output_power_kw has dropped to at most this fraction of
# the session's running peak power (established from strictly earlier readings only - see
# _detect_derate_runs). 0.6 (a >=40% instantaneous drop) is chosen to sit well outside normal
# CC/CV ramp noise (the generator's own ramp model varies current by only +/-3% -
# `rng.uniform(0.97, 1.03)` in _simulate_stall_session) and outside the *start* of the modeled
# natural taper (which only begins at 75% of session duration and declines linearly, not in a
# sudden step) - a genuine derate (a charger clamping output) is modeled here as an abrupt,
# large drop, which a >=40% single-step threshold is meant to catch without ordinary noise
# tripping it.
DEFAULT_PEAK_DROP_FRACTION = 0.6

# How many CONSECUTIVE derated readings must appear before a run counts as sustained rather than
# a single noisy sample. At the stall fixture's 15s reading interval (GeneratorConfig.
# reading_interval_s), 3 consecutive readings is 30-45s of continuously reduced output -
# long enough that a single bad sample or brief communication glitch is very unlikely to explain
# it, short enough that a real derate is still caught promptly rather than needing minutes to
# confirm.
DEFAULT_MIN_CONSECUTIVE_DERATED_READINGS = 3

# A session's running peak must reach at least this many kW before ANY derate comparison is
# made against it. Without this floor, the first couple of readings during ramp-up (power still
# climbing from near 0) would produce a tiny, not-yet-meaningful "peak" that almost any later
# reading could trivially look like a ">=40% drop" from, even though nothing has actually been
# derated - the session simply hasn't ramped up yet. 20.0 kW is comfortably below this fixture's
# typical full-session peak (peak_current_a is rng.uniform(120, 250) at ~390-410V, i.e.
# roughly 47-103 kW peak) but well above where ramp-up noise could plausibly sit.
DEFAULT_MIN_PEAK_KW_FLOOR = 20.0

# A candidate run of consecutive "derated" readings is only CONFIRMED as a genuine derate if the
# readings within that run vary by no more than this many kW from each other (i.e. the power has
# dropped AND HELD roughly steady, rather than continuing to decline). This is the core
# heuristic this detector uses to tell a real derate apart from the fixture's modeled natural
# taper: the generator's taper is a continuous, monotonically-declining ramp all the way toward
# session end (`ramp = max(0.05, 1.0 - (frac - 0.75) / 0.25)`, still falling reading-to-reading),
# whereas a charger genuinely limiting/clamping its output for a real reason (thermal, grid,
# module fault-adjacent) is expected to plateau at the reduced level rather than keep decaying
# toward zero. A run that is still declining by more than this tolerance across its own span is
# treated as an ongoing taper and NOT confirmed - see _close_derate_run.
#
# Known limitation, stated plainly: this is a real, debatable judgment call with no ground truth
# to validate it against (the fixture generator never models a derate). A derate that itself
# ramps down gradually before plateauing, or a taper that happens to plateau briefly before
# resuming its decline, are both plausible real-world shapes this simple rule does not handle
# well. A charger SME with real derate telemetry to look at should revisit this constant (and
# arguably this whole plateau-vs-decline approach) before it is trusted operationally.
DEFAULT_PLATEAU_TOLERANCE_KW = 5.0

# session_state value treated as "actively charging" for derate purposes - taken from the
# catalog's session_state enum, configurable in case that enum is revised.
DEFAULT_CHARGING_STATE = "charging"


def _close_derate_run(
    current_run: list[dict], session_id: str, min_consecutive: int, plateau_tolerance_kw: float
) -> list[dict]:
    """Evaluate one candidate run of consecutive "derated" readings for one session; return a
    list with one confirmed-run evidence dict, or an empty list if the run doesn't qualify (too
    short, or still declining rather than plateaued - see DEFAULT_PLATEAU_TOLERANCE_KW above).
    """
    if len(current_run) < min_consecutive:
        return []
    powers = [entry["power"] for entry in current_run]
    if max(powers) - min(powers) > plateau_tolerance_kw:
        return []
    return [
        {
            "session_id": session_id,
            "run_peak_kw": current_run[0]["peak_at_time"],
            "run_min_power_kw": min(powers),
            "run_max_power_kw": max(powers),
            "start_device_ts_ms": current_run[0]["row"].fields.get("device_ts_ms"),
            "end_device_ts_ms": current_run[-1]["row"].fields.get("device_ts_ms"),
            "reading_count": len(current_run),
        }
    ]


def _detect_derate_runs(
    charging_rows: list[CanonicalRow],
    session_id: str,
    *,
    peak_drop_fraction: float,
    min_consecutive: int,
    min_peak_kw_floor: float,
    plateau_tolerance_kw: float,
) -> list[dict]:
    """Scan one session's `charging`-state readings (already sorted by device_ts_ms) for
    confirmed derate runs - see the module-level constants above for what each threshold means
    and why. The running peak used for each reading's comparison is established from strictly
    EARLIER readings only (never the current reading), matching the ticket's "own earlier
    readings established its running peak" framing - a reading is never compared against a peak
    that includes itself.
    """
    confirmed: list[dict] = []
    peak: float | None = None
    current_run: list[dict] = []

    for row in charging_rows:
        power = row.fields.get("output_power_kw")
        if not isinstance(power, (int, float)):
            # A non-numeric/missing power reading can't be evaluated - close whatever run was
            # in progress (same as a reading that plainly isn't derated) and move on, rather
            # than crashing the whole detector over one bad row.
            confirmed.extend(
                _close_derate_run(current_run, session_id, min_consecutive, plateau_tolerance_kw)
            )
            current_run = []
            continue

        if peak is not None and peak >= min_peak_kw_floor and power <= peak * peak_drop_fraction:
            current_run.append({"row": row, "power": power, "peak_at_time": peak})
        else:
            confirmed.extend(
                _close_derate_run(current_run, session_id, min_consecutive, plateau_tolerance_kw)
            )
            current_run = []

        if peak is None or power > peak:
            peak = power

    confirmed.extend(
        _close_derate_run(current_run, session_id, min_consecutive, plateau_tolerance_kw)
    )
    return confirmed


@register_detector(DERATE_DETECTOR_NAME, detector_version=DETECTOR_VERSION)
def detect_derate_v1(context: DetectorContext) -> list[DetectorOutcome]:
    """Fire (PROVISIONAL) if any `charging`-state session in this device-hour shows a sustained
    power derate: a run of `params["min_consecutive_readings"]` (default
    DEFAULT_MIN_CONSECUTIVE_DERATED_READINGS) or more CONSECUTIVE readings whose
    `output_power_kw` has dropped to at most `params["peak_drop_fraction"]` (default
    DEFAULT_PEAK_DROP_FRACTION) of the session's own running peak power up to that point, AND
    which then holds roughly steady (within `params["plateau_tolerance_kw"]`, default
    DEFAULT_PLATEAU_TOLERANCE_KW) rather than continuing to decline - see the module-level
    constants above this function for the full reasoning behind each threshold, and this
    function's own docstring below for why the plateau check exists.

    Why "drop and plateau", not just "drop" - avoiding the natural-taper false positive
    -------------------------------------------------------------------------------------------
    The fixture generator models an ordinary session winding down near completion as a
    CONTINUOUS, monotonically-declining ramp over its final 25% of duration (down to as low as
    5% of peak) - not a real derate, just CC/CV tapering as a session approaches full charge. A
    naive "power dropped below X% of peak" rule alone would false-positive on every such
    session. This detector's distinguishing signal is that a genuine derate (a charger clamping
    its output) is expected to PLATEAU at the reduced level, whereas a natural taper keeps
    falling reading over reading - see DEFAULT_PLATEAU_TOLERANCE_KW's comment for the concrete
    mechanism and its stated limitations. This is this module's own statistical judgment call,
    unvalidated against any real derate data (none exists in this repo's fixtures or elsewhere
    in this sandbox) - flagged here as plainly as the ticket brief asked for.

    All confirmed derate runs across all sessions in the hour are combined into one finding (see
    module docstring's "one finding per device-hour" section).
    """
    rows = _stall_rows_sorted(context.payload)
    if not rows:
        return []

    peak_drop_fraction = context.params.get("peak_drop_fraction", DEFAULT_PEAK_DROP_FRACTION)
    min_consecutive = context.params.get(
        "min_consecutive_readings", DEFAULT_MIN_CONSECUTIVE_DERATED_READINGS
    )
    min_peak_kw_floor = context.params.get("min_peak_kw_floor", DEFAULT_MIN_PEAK_KW_FLOOR)
    plateau_tolerance_kw = context.params.get(
        "plateau_tolerance_kw", DEFAULT_PLATEAU_TOLERANCE_KW
    )
    charging_state = context.params.get("charging_state", DEFAULT_CHARGING_STATE)

    confirmed_runs: list[dict] = []
    for session_id, session_rows in _group_by_session(rows).items():
        charging_rows = [r for r in session_rows if r.fields.get("session_state") == charging_state]
        confirmed_runs.extend(
            _detect_derate_runs(
                charging_rows,
                session_id,
                peak_drop_fraction=peak_drop_fraction,
                min_consecutive=min_consecutive,
                min_peak_kw_floor=min_peak_kw_floor,
                plateau_tolerance_kw=plateau_tolerance_kw,
            )
        )

    if not confirmed_runs:
        return []

    device_id, event_hour = context.subject
    measured = sum(run["reading_count"] for run in confirmed_runs)
    session_count = len({run["session_id"] for run in confirmed_runs})
    quality = FindingQuality(
        label="medium", measured_buckets=min(measured, len(rows)), total_buckets=len(rows)
    )
    summary = (
        f"device {device_id} had {len(confirmed_runs)} sustained power-derate run(s) across "
        f"{session_count} session(s) in hour {event_hour}"
    )
    return [
        DetectorOutcome(
            status=FindingStatus.PROVISIONAL,
            summary=summary,
            evidence={"runs": tuple(confirmed_runs)},
            quality=quality,
        )
    ]
