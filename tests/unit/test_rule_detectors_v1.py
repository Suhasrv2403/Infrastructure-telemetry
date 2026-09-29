"""Tests for the four stall-class rule detectors (P2-08: "Rule detectors v1").

Covers, per detector: a case where it correctly fires, a clean hour where it correctly does
NOT fire, and at least one rule-specific edge case (see each section below for which). Also
covers running all four through `run_active_detectors` against the real
`detectors/detectors.yaml` config + the real registry + a real `FindingStore`, confirming the
run reconciles and every emitted finding is PROVISIONAL (CLAUDE.md invariant 6 - these
detectors never emit FINAL; finalization is P2-11's job, not built here).

Fixture style mirrors test_detector_framework.py: hand-built input objects (there, GridBucket;
here, CanonicalRow), not the synthetic message generator - the generator
(tests/fixtures/generators/supercharger.py) doesn't model derates at all and doesn't give
per-reading control fine enough to construct rule-specific edge cases (an exact plateaued
derate run, a session ending mid-fault, etc), so hand-built CanonicalRow rows are the more
direct and more legible fixture for this ticket's own judgment calls.
"""
from __future__ import annotations

from detectors.framework.config import active_detectors, load_detector_config

# Importing this (not otherwise used directly in this file) registers "all_gap_hour" in the
# global registry - detectors/detectors.yaml declares it enabled, and active_detectors() raises
# loudly on an enabled-but-unregistered name (see config.py). test_detector_framework.py already
# imports this for its own tests, so in a full test-suite run this import is redundant but
# harmless; it's needed here so this file's end-to-end test also passes in isolation.
from detectors.framework.demo_all_gap_detector import (
    DETECTOR_NAME as _ALL_GAP_HOUR_NAME,  # noqa: F401
)
from detectors.framework.dispatch import run_active_detectors
from detectors.framework.findings import FindingStatus, FindingStore
from detectors.framework.registry import DetectorContext

# Importing this module triggers its four @register_detector(...) decorators, populating the
# framework's global registry - exactly like test_detector_framework.py's import of
# demo_all_gap_detector does for all_gap_hour.
from detectors.rules.charger_faults_v1 import (
    DERATE_DETECTOR_NAME,
    MODULE_FAULT_DETECTOR_NAME,
    SESSION_FAILURE_DETECTOR_NAME,
    STALL_DEVICE_CLASS,
    THERMAL_EVENT_DETECTOR_NAME,
    detect_derate_v1,
    detect_module_fault_v1,
    detect_session_failure_v1,
    detect_thermal_event_v1,
)
from pipeline.stage2_canonical.canonicalize import CanonicalRow

SUBJECT = ("stall-A", "2026-06-01T09")
BASE_TS_MS = 1_780_400_000_000
INTERVAL_MS = 15_000  # matches the stall fixture generator's reading_interval_s default


def _row(
    *,
    device_id: str = "stall-A",
    device_ts_ms: int,
    session_id: str = "sess-1",
    session_state: str = "charging",
    output_power_kw: float = 50.0,
    connector_temp_c: float = 35.0,
    stall_fault_code: int = 0,
    device_class: str = STALL_DEVICE_CLASS,
) -> CanonicalRow:
    """A hand-built Stage 2 canonical row for one supercharger_stall reading - only the fields
    these detectors actually read are given meaningful values; the rest are filled with
    plausible constants."""
    return CanonicalRow(
        fields={
            "device_id": device_id,
            "device_class": device_class,
            "firmware_version": "2.1.4",
            "device_ts_ms": device_ts_ms,
            "payload_hash": f"sha1:fake-{device_id}-{device_ts_ms}",
            "session_id": session_id,
            "session_state": session_state,
            "output_voltage_v": 400.0,
            "output_current_a": output_power_kw,
            "output_power_kw": output_power_kw,
            "connector_temp_c": connector_temp_c,
            "energy_delivered_kwh": 0.0,
            "stall_fault_code": stall_fault_code,
        },
        units={},
        dropped_fields=(),
    )


def _context(rows: tuple[CanonicalRow, ...], *, subject=SUBJECT, params=None) -> DetectorContext:
    return DetectorContext(subject=subject, payload=rows, params=params or {})


# ---------------------------------------------------------------------------------------------
# module_fault_v1
# ---------------------------------------------------------------------------------------------


def test_module_fault_v1_fires_on_a_fault_code_1001_reading():
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_id="sess-1", stall_fault_code=0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, session_id="sess-1", stall_fault_code=1001),
    )
    outcomes = detect_module_fault_v1(_context(rows))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is FindingStatus.PROVISIONAL
    assert outcome.evidence["count"] == 1
    assert outcome.evidence["session_ids"] == ("sess-1",)
    assert outcome.evidence["first_device_ts_ms"] == BASE_TS_MS + INTERVAL_MS
    assert outcome.evidence["last_device_ts_ms"] == BASE_TS_MS + INTERVAL_MS
    assert outcome.quality.measured_buckets == 1
    assert outcome.quality.total_buckets == 2


def test_module_fault_v1_does_not_fire_on_a_clean_hour():
    rows = tuple(
        _row(device_ts_ms=BASE_TS_MS + i * INTERVAL_MS, stall_fault_code=0) for i in range(4)
    )
    assert detect_module_fault_v1(_context(rows)) == []


def test_module_fault_v1_does_not_fire_on_a_different_fault_code():
    """Edge case: a real fault code (1042, thermal) must not be mistaken for 1001 - the two
    fault classes are distinct, not "any nonzero code counts"."""
    rows = (_row(device_ts_ms=BASE_TS_MS, stall_fault_code=1042),)
    assert detect_module_fault_v1(_context(rows)) == []


# ---------------------------------------------------------------------------------------------
# thermal_event_v1
# ---------------------------------------------------------------------------------------------


def test_thermal_event_v1_fires_on_fault_code_1042():
    rows = (
        _row(device_ts_ms=BASE_TS_MS, connector_temp_c=35.0, stall_fault_code=0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, connector_temp_c=40.0, stall_fault_code=1042),
    )
    outcomes = detect_thermal_event_v1(_context(rows))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is FindingStatus.PROVISIONAL
    assert outcome.evidence["fault_code_hits"] == 1
    assert outcome.evidence["readings_over_threshold"] == 0
    assert outcome.evidence["max_connector_temp_c"] == 40.0
    assert outcome.quality.label == "high"


def test_thermal_event_v1_fires_on_connector_temp_over_alarm_threshold():
    rows = (
        _row(device_ts_ms=BASE_TS_MS, connector_temp_c=35.0, stall_fault_code=0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, connector_temp_c=50.0, stall_fault_code=0),
    )
    outcomes = detect_thermal_event_v1(_context(rows))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.evidence["readings_over_threshold"] == 1
    assert outcome.evidence["fault_code_hits"] == 0
    assert outcome.evidence["max_connector_temp_c"] == 50.0
    assert outcome.quality.label == "medium"  # inferred from a threshold, not a firmware code


def test_thermal_event_v1_does_not_fire_on_a_clean_hour_at_the_catalog_max():
    """Edge case: readings right up to catalog/signals.yaml's stated connector_temp_c
    valid_range max (43.7C) must NOT fire - the alarm threshold (44.0C) was deliberately set
    just above that ceiling, precisely so ordinary full-power sessions don't trip it (see
    charger_faults_v1.py's DEFAULT_THERMAL_ALARM_C reasoning)."""
    rows = tuple(
        _row(device_ts_ms=BASE_TS_MS + i * INTERVAL_MS, connector_temp_c=43.7, stall_fault_code=0)
        for i in range(3)
    )
    assert detect_thermal_event_v1(_context(rows)) == []


# ---------------------------------------------------------------------------------------------
# session_failure_v1
# ---------------------------------------------------------------------------------------------


def test_session_failure_v1_fires_on_a_session_that_ends_the_hour_still_in_fault():
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_id="sess-1", session_state="plugged_in"),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, session_id="sess-1", session_state="charging"),
        _row(
            device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS,
            session_id="sess-1",
            session_state="fault",
            stall_fault_code=1001,
        ),
    )
    outcomes = detect_session_failure_v1(_context(rows))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is FindingStatus.PROVISIONAL
    failed = outcome.evidence["failed_sessions"]
    assert len(failed) == 1
    assert failed[0]["session_id"] == "sess-1"
    assert failed[0]["last_session_state"] == "fault"
    assert failed[0]["readings_after_fault"] == 0


def test_session_failure_v1_does_not_fire_on_a_clean_hour_of_completed_sessions():
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_id="sess-1", session_state="plugged_in"),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, session_id="sess-1", session_state="charging"),
        _row(
            device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS,
            session_id="sess-1",
            session_state="session_complete",
        ),
    )
    assert detect_session_failure_v1(_context(rows)) == []


def test_session_failure_v1_does_not_fire_on_a_session_still_charging_at_hour_end():
    """Edge case: an ordinary session that simply hasn't finished yet by the hour boundary (no
    fault reading anywhere) must NOT be flagged as a failure - this is the false-positive the
    ticket brief calls out by name."""
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_id="sess-1", session_state="plugged_in"),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, session_id="sess-1", session_state="charging"),
        _row(device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS, session_id="sess-1", session_state="charging"),
    )
    assert detect_session_failure_v1(_context(rows)) == []


def test_session_failure_v1_does_not_fire_on_a_session_that_recovers_within_the_hour():
    """Edge case: fault followed by session_complete within the same hour is read as
    resolution, not as "ignore the fault ever happened" - see the function's own docstring on
    this judgment call."""
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_id="sess-1", session_state="charging"),
        _row(
            device_ts_ms=BASE_TS_MS + INTERVAL_MS,
            session_id="sess-1",
            session_state="fault",
            stall_fault_code=2010,
        ),
        _row(
            device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS,
            session_id="sess-1",
            session_state="session_complete",
        ),
    )
    assert detect_session_failure_v1(_context(rows)) == []


# ---------------------------------------------------------------------------------------------
# derate_v1
# ---------------------------------------------------------------------------------------------


def _derate_run_rows(session_id: str = "sess-1") -> tuple[CanonicalRow, ...]:
    """A session that ramps to a peak (~82kW), then abruptly drops to and HOLDS around 30kW for
    4 consecutive readings - a plateaued drop, the shape this detector's default params
    (peak_drop_fraction=0.6, min_consecutive_readings=3, plateau_tolerance_kw=5.0) are meant to
    confirm as a genuine derate."""
    powers = [80.0, 82.0, 81.0, 30.0, 31.0, 29.0, 28.5]
    return tuple(
        _row(
            device_ts_ms=BASE_TS_MS + i * INTERVAL_MS,
            session_id=session_id,
            session_state="charging",
            output_power_kw=p,
        )
        for i, p in enumerate(powers)
    )


def test_derate_v1_fires_on_a_sustained_plateaued_power_drop():
    outcomes = detect_derate_v1(_context(_derate_run_rows()))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is FindingStatus.PROVISIONAL
    runs = outcome.evidence["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["session_id"] == "sess-1"
    assert run["run_peak_kw"] == 82.0
    assert run["reading_count"] == 4  # the four consecutive ~30kW readings
    assert run["run_max_power_kw"] - run["run_min_power_kw"] <= 5.0
    assert outcome.quality.label == "medium"


def test_derate_v1_does_not_fire_on_a_clean_stable_session():
    powers = [78.0, 80.0, 82.0, 81.0, 79.0, 80.0]
    rows = tuple(
        _row(
            device_ts_ms=BASE_TS_MS + i * INTERVAL_MS,
            session_state="charging",
            output_power_kw=p,
        )
        for i, p in enumerate(powers)
    )
    assert detect_derate_v1(_context(rows)) == []


def test_derate_v1_does_not_fire_on_a_natural_taper_near_completion():
    """Edge case: a session winding down naturally (power continuously declining, never
    plateauing - modeling the fixture generator's own CC/CV taper shape) must NOT be flagged as
    a derate, even though several consecutive readings do drop below 60% of the session's peak.
    The distinguishing signal this detector uses is "drop AND HOLD" vs "keep declining" - see
    detect_derate_v1's own docstring."""
    powers = [90.0, 85.0, 78.0, 70.0, 60.0, 50.0, 42.0, 35.0, 28.0, 20.0]
    rows = tuple(
        _row(
            device_ts_ms=BASE_TS_MS + i * INTERVAL_MS,
            session_state="charging",
            output_power_kw=p,
        )
        for i, p in enumerate(powers)
    )
    assert detect_derate_v1(_context(rows)) == []


def test_derate_v1_ignores_readings_outside_the_charging_state():
    """A low output_power_kw reading while plugged_in (not yet charging) or in fault (forced to
    0 by the generator's own model) must not itself be treated as a derate candidate - only
    `charging`-state readings are compared against the session's running peak."""
    rows = (
        _row(device_ts_ms=BASE_TS_MS, session_state="plugged_in", output_power_kw=0.0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, session_state="charging", output_power_kw=80.0),
        _row(device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS, session_state="charging", output_power_kw=82.0),
        _row(
            device_ts_ms=BASE_TS_MS + 3 * INTERVAL_MS,
            session_state="fault",
            output_power_kw=0.0,
            stall_fault_code=1001,
        ),
    )
    assert detect_derate_v1(_context(rows)) == []


# ---------------------------------------------------------------------------------------------
# Stall-class-only guard, shared by all four detectors via _stall_rows_sorted
# ---------------------------------------------------------------------------------------------


def test_detectors_reject_non_stall_rows_loudly():
    cabinet_row = _row(device_ts_ms=BASE_TS_MS, device_class="supercharger_cabinet")
    for detect in (
        detect_module_fault_v1,
        detect_thermal_event_v1,
        detect_session_failure_v1,
        detect_derate_v1,
    ):
        try:
            detect(_context((cabinet_row,)))
            raised = False
        except ValueError:
            raised = True
        assert raised, f"{detect.__name__} must reject a non-stall-class row loudly"


# ---------------------------------------------------------------------------------------------
# End to end: real detectors.yaml config + real registry + real FindingStore
# ---------------------------------------------------------------------------------------------


def test_run_active_detectors_end_to_end_with_all_four_via_real_config():
    """Resolves the real detectors/detectors.yaml against the real global registry (proving
    these four are actually declared and registered, not just importable), runs them via
    run_active_detectors against one device-hour built to trip all four rules at once plus one
    clean device-hour, and confirms the run reconciles with every emitted finding PROVISIONAL.

    Filters the resolved active detectors down to just this ticket's four names before running:
    `all_gap_hour` (P2-06's demo detector) expects a tuple[GridBucket, ...] payload, a different
    contract than the tuple[CanonicalRow, ...] these four detectors and this test build - mixing
    the two payload shapes in one dispatch call would just be testing that a foreign-payload
    detector degrades to a recorded failure (already covered by test_detector_framework.py),
    not this ticket's own detectors.
    """
    declarations = load_detector_config()
    resolved = active_detectors(declarations)
    our_names = {
        MODULE_FAULT_DETECTOR_NAME,
        THERMAL_EVENT_DETECTOR_NAME,
        SESSION_FAILURE_DETECTOR_NAME,
        DERATE_DETECTOR_NAME,
    }
    active = tuple(d for d in resolved if d.name in our_names)
    assert {d.name for d in active} == our_names, "all four P2-08 detectors must be enabled in detectors.yaml"

    rich_subject = ("stall-RICH", "2026-06-01T09")
    rich_rows = (
        _row(device_ts_ms=BASE_TS_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=80.0, connector_temp_c=35.0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=82.0, connector_temp_c=36.0),
        _row(device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=81.0, connector_temp_c=36.0),
        _row(device_ts_ms=BASE_TS_MS + 3 * INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=30.0, connector_temp_c=36.0),
        _row(device_ts_ms=BASE_TS_MS + 4 * INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=31.0, connector_temp_c=36.0),
        _row(device_ts_ms=BASE_TS_MS + 5 * INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="charging", output_power_kw=29.0, connector_temp_c=50.0),
        _row(device_ts_ms=BASE_TS_MS + 6 * INTERVAL_MS, device_id="stall-RICH", session_id="sess-rich", session_state="fault", output_power_kw=0.0, connector_temp_c=40.0, stall_fault_code=1001),
    )
    clean_subject = ("stall-CLEAN", "2026-06-01T09")
    clean_rows = (
        _row(device_ts_ms=BASE_TS_MS, device_id="stall-CLEAN", session_id="sess-clean", session_state="plugged_in", output_power_kw=0.0, connector_temp_c=30.0),
        _row(device_ts_ms=BASE_TS_MS + INTERVAL_MS, device_id="stall-CLEAN", session_id="sess-clean", session_state="charging", output_power_kw=78.0, connector_temp_c=34.0),
        _row(device_ts_ms=BASE_TS_MS + 2 * INTERVAL_MS, device_id="stall-CLEAN", session_id="sess-clean", session_state="charging", output_power_kw=80.0, connector_temp_c=35.0),
        _row(device_ts_ms=BASE_TS_MS + 3 * INTERVAL_MS, device_id="stall-CLEAN", session_id="sess-clean", session_state="session_complete", output_power_kw=79.0, connector_temp_c=35.0),
    )

    contexts = [
        DetectorContext(subject=rich_subject, payload=rich_rows, params={}),
        DetectorContext(subject=clean_subject, payload=clean_rows, params={}),
    ]
    store = FindingStore(clock=iter(range(1000, 100_000, 1000)).__next__)

    result = run_active_detectors(active, contexts, store)

    assert result.reconciles()
    assert result.runs_seen == len(active) * len(contexts)
    assert result.failures == ()
    assert len(result.findings) == 4  # one finding per detector, all from the rich device-hour

    for finding in result.findings:
        assert finding.status is FindingStatus.PROVISIONAL
        assert finding.subject == rich_subject

    for name in our_names:
        assert store.latest(name, rich_subject) is not None
        assert store.latest(name, clean_subject) is None
