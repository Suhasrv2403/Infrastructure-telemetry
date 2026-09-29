"""Tests for the detector plugin framework and findings schema (P2-06: "Detector plugin
framework and findings schema").

Covers:
- registry.py: @register_detector populates the registry; duplicate registration under the
  same name fails loudly.
- config.py: a config declaration naming a real registered detector resolves; a config
  declaration naming an unregistered detector raises loudly (never a silent skip).
- demo_all_gap_detector.py: the one demonstration detector runs against constructed Stage 3a
  grid input and produces a Finding carrying real evidence (and produces nothing when the grid
  isn't all-gap).
- findings.py: CLAUDE.md invariant 6 ("Findings are versioned, never overwritten") - re-running
  a detector on the same subject with CHANGED underlying data produces a NEW versioned finding
  while the OLD finding stays retrievable/intact; an unchanged re-run does NOT mint a spurious
  new version; a finding's provisional/final status round-trips through the store as a new
  appended version, never a mutation.
- dispatch.py: run_active_detectors()'s reconciles()-checkable result, including a detector
  that raises degrading to a recorded failure rather than crashing the batch.
"""
from __future__ import annotations

import pytest

from detectors.framework.config import (
    DetectorConfigError,
    DetectorDeclaration,
    UnregisteredDetectorError,
    active_detectors,
    load_detector_config,
)

# Importing the demo detector module triggers its @register_detector("all_gap_hour", ...)
# decorator, populating the framework's global registry - exactly like any other caller of
# active_detectors()/run_active_detectors() would rely on.
from detectors.framework.demo_all_gap_detector import (
    DETECTOR_NAME,
    detect_all_gap_hour,
)
from detectors.framework.dispatch import DetectorFailure, run_active_detectors
from detectors.framework.findings import (
    FindingQuality,
    FindingStateError,
    FindingStatus,
    FindingStore,
)
from detectors.framework.registry import (
    DetectorContext,
    DetectorOutcome,
    DuplicateDetectorError,
    Registry,
    get_registered_detector,
    register_detector,
    registered_detectors,
)
from pipeline.stage3_enrich.time_grid import GAP, MEASURED, GridBucket

SUBJECT = ("stall-A", "2026-06-01T09")


def _gap_bucket(bucket_start_ms: int) -> GridBucket:
    return GridBucket(
        device_id="stall-A",
        device_class="supercharger_stall",
        bucket_start_ms=bucket_start_ms,
        coverage=GAP,
        mode=None,
        reading_count=0,
    )


def _measured_bucket(bucket_start_ms: int) -> GridBucket:
    return GridBucket(
        device_id="stall-A",
        device_class="supercharger_stall",
        bucket_start_ms=bucket_start_ms,
        coverage=MEASURED,
        mode="charging",
        reading_count=1,
    )


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------


def test_register_detector_populates_registry():
    local_registry: Registry = {}
    register_detector("fake_detector", detector_version="1.0.0", registry=local_registry)(
        lambda context: []
    )

    assert "fake_detector" in registered_detectors(registry=local_registry)
    entry = get_registered_detector("fake_detector", registry=local_registry)
    assert entry is not None
    assert entry.name == "fake_detector"
    assert entry.detector_version == "1.0.0"


def test_register_detector_rejects_duplicate_name():
    local_registry: Registry = {}
    register_detector("dup", detector_version="1.0.0", registry=local_registry)(
        lambda context: []
    )
    with pytest.raises(DuplicateDetectorError):
        register_detector("dup", detector_version="2.0.0", registry=local_registry)(
            lambda context: []
        )


def test_demo_detector_is_registered_in_the_global_registry():
    """Importing demo_all_gap_detector must have registered it globally - the real "declared
    in config, resolvable by name" path this ticket's config.py depends on."""
    assert DETECTOR_NAME in registered_detectors()
    entry = get_registered_detector(DETECTOR_NAME)
    assert entry is not None
    assert entry.func is detect_all_gap_hour


# ---------------------------------------------------------------------------
# config.py - declared-in-config resolution against the registry
# ---------------------------------------------------------------------------


def test_active_detectors_resolves_a_real_registered_name():
    local_registry: Registry = {}
    register_detector("resolvable", detector_version="1.0.0", registry=local_registry)(
        lambda context: []
    )
    declarations = (
        DetectorDeclaration(name="resolvable", enabled=True, params={"threshold": 3}),
    )

    resolved = active_detectors(declarations, registry=local_registry)

    assert len(resolved) == 1
    assert resolved[0].name == "resolvable"
    assert resolved[0].detector_version == "1.0.0"
    assert resolved[0].params == {"threshold": 3}


def test_active_detectors_raises_loudly_for_unregistered_name():
    """The core "configuration error, not silently ignored" requirement: a config entry naming
    a detector nothing ever registered must fail the resolve, not be dropped quietly."""
    declarations = (DetectorDeclaration(name="never_registered", enabled=True, params={}),)

    with pytest.raises(UnregisteredDetectorError) as excinfo:
        active_detectors(declarations, registry={})

    assert "never_registered" in str(excinfo.value)


def test_active_detectors_ignores_disabled_unregistered_entries():
    """A disabled entry naming an unregistered detector is not an error - an operator may
    pre-stage config for code that hasn't shipped yet, as long as it's off."""
    declarations = (DetectorDeclaration(name="not_yet_built", enabled=False, params={}),)
    resolved = active_detectors(declarations, registry={})
    assert resolved == ()


def test_load_detector_config_reads_the_real_shipped_config_and_resolves():
    """The actual detectors/detectors.yaml this ticket ships must load and resolve cleanly
    against the actual global registry - the end-to-end "declared in config" proof."""
    declarations = load_detector_config()
    assert any(d.name == DETECTOR_NAME and d.enabled for d in declarations)

    resolved = active_detectors(declarations)
    assert any(d.name == DETECTOR_NAME for d in resolved)


def test_load_detector_config_rejects_malformed_yaml(tmp_path):
    bad = tmp_path / "bad_detectors.yaml"
    bad.write_text("not_a_detectors_key: true\n")
    with pytest.raises(DetectorConfigError):
        load_detector_config(bad)

    missing_fields = tmp_path / "missing_fields.yaml"
    missing_fields.write_text("detectors:\n  - name: foo\n")  # no 'enabled'
    with pytest.raises(DetectorConfigError):
        load_detector_config(missing_fields)


# ---------------------------------------------------------------------------
# demo_all_gap_detector.py - the framework's one demonstration detector
# ---------------------------------------------------------------------------


def test_demo_detector_fires_on_all_gap_hour_with_real_evidence():
    buckets = tuple(_gap_bucket(i * 60_000) for i in range(3))
    context = DetectorContext(subject=SUBJECT, payload=buckets, params={"min_gap_buckets": 1})

    outcomes = detect_all_gap_hour(context)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, DetectorOutcome)
    assert outcome.status is FindingStatus.PROVISIONAL
    assert "stall-A" in outcome.summary
    assert outcome.evidence["bucket_start_ms"] == tuple(b.bucket_start_ms for b in buckets)
    assert outcome.evidence["reading_counts"] == (0, 0, 0)
    assert outcome.quality.measured_buckets == 0
    assert outcome.quality.total_buckets == 3


def test_demo_detector_does_not_fire_when_any_bucket_is_measured():
    buckets = (_gap_bucket(0), _measured_bucket(60_000), _gap_bucket(120_000))
    context = DetectorContext(subject=SUBJECT, payload=buckets, params={})
    assert detect_all_gap_hour(context) == []


def test_demo_detector_does_not_fire_below_min_gap_buckets_threshold():
    buckets = (_gap_bucket(0),)
    context = DetectorContext(subject=SUBJECT, payload=buckets, params={"min_gap_buckets": 5})
    assert detect_all_gap_hour(context) == []


# ---------------------------------------------------------------------------
# findings.py - invariant 6: versioned, never overwritten
# ---------------------------------------------------------------------------


def _quality(measured: int, total: int) -> FindingQuality:
    return FindingQuality(label="low" if measured == 0 else "medium", measured_buckets=measured,
                           total_buckets=total)


def test_rerun_with_changed_data_produces_new_version_and_old_version_stays_intact():
    """The core invariant-6 proof: emit v1, then emit different content for the same
    (detector, subject) - v2 must be a NEW Finding, and v1 must remain retrievable, unchanged,
    byte-for-byte, from the store."""
    store = FindingStore(clock=iter([1000, 2000]).__next__)

    v1 = store.emit(
        detector_name="all_gap_hour",
        subject=SUBJECT,
        status=FindingStatus.PROVISIONAL,
        summary="3 gap buckets",
        evidence={"bucket_start_ms": (0, 60_000, 120_000)},
        quality=_quality(0, 3),
        detector_version="0.1.0",
    )
    assert v1.version == 1
    assert v1.supersedes is None
    assert v1.emitted_at_ms == 1000

    # Underlying data changed (a 4th gap bucket showed up on re-run - e.g. late data widened
    # the dirty hour's grid) - a genuinely new fact for the same subject.
    v2 = store.emit(
        detector_name="all_gap_hour",
        subject=SUBJECT,
        status=FindingStatus.PROVISIONAL,
        summary="4 gap buckets",
        evidence={"bucket_start_ms": (0, 60_000, 120_000, 180_000)},
        quality=_quality(0, 4),
        detector_version="0.1.0",
    )
    assert v2.version == 2
    assert v2.supersedes == 1
    assert v2.emitted_at_ms == 2000
    assert v2.content_hash != v1.content_hash

    # The old version is still retrievable, intact, exactly as it was emitted.
    fetched_v1 = store.get("all_gap_hour", SUBJECT, 1)
    assert fetched_v1 == v1
    assert fetched_v1.summary == "3 gap buckets"
    assert fetched_v1.evidence["bucket_start_ms"] == (0, 60_000, 120_000)

    # And the full history holds both, in order - nothing dropped, nothing mutated.
    history = store.history("all_gap_hour", SUBJECT)
    assert history == (v1, v2)
    assert store.latest("all_gap_hour", SUBJECT) == v2


def test_identical_rerun_does_not_mint_a_spurious_new_version():
    """A re-run that reaches the exact same conclusion from unchanged data must be a no-op -
    idempotent, mirroring Stage1MergeStore's natural-key dedup - not version spam."""
    store = FindingStore(clock=iter([1000, 2000, 3000]).__next__)
    kwargs = {
        "detector_name": "all_gap_hour",
        "subject": SUBJECT,
        "status": FindingStatus.PROVISIONAL,
        "summary": "3 gap buckets",
        "evidence": {"bucket_start_ms": (0, 60_000, 120_000)},
        "quality": _quality(0, 3),
        "detector_version": "0.1.0",
    }

    v1 = store.emit(**kwargs)
    v1_again = store.emit(**kwargs)

    assert v1_again is v1
    assert v1_again.version == 1
    assert store.history("all_gap_hour", SUBJECT) == (v1,)


def test_finding_evidence_is_defensively_copied_and_immutable():
    """Mutating the evidence dict a caller passed in after emit() must not silently rewrite a
    stored finding - invariant 6's "never overwritten" applies to evidence too, not just the
    version counter."""
    mutable_evidence = {"bucket_start_ms": [0, 60_000]}
    store = FindingStore(clock=iter([1000]).__next__)
    finding = store.emit(
        detector_name="all_gap_hour",
        subject=SUBJECT,
        status=FindingStatus.PROVISIONAL,
        summary="2 gap buckets",
        evidence=mutable_evidence,
        quality=_quality(0, 2),
        detector_version="0.1.0",
    )

    mutable_evidence["bucket_start_ms"].append(999_999)  # mutate the caller's own dict

    assert finding.evidence["bucket_start_ms"] == [0, 60_000], (
        "stored finding must be immune to the caller's dict being mutated afterward"
    )
    with pytest.raises(TypeError):
        finding.evidence["bucket_start_ms"] = "overwritten"


def test_provisional_to_final_status_round_trips_as_a_new_appended_version():
    store = FindingStore(clock=iter([1000, 2000]).__next__)
    provisional = store.emit(
        detector_name="all_gap_hour",
        subject=SUBJECT,
        status=FindingStatus.PROVISIONAL,
        summary="3 gap buckets",
        evidence={"bucket_start_ms": (0, 60_000, 120_000)},
        quality=_quality(0, 3),
        detector_version="0.1.0",
    )
    assert provisional.status is FindingStatus.PROVISIONAL

    final = store.finalize(
        detector_name="all_gap_hour", subject=SUBJECT, version=provisional.version
    )

    assert final.status is FindingStatus.FINAL
    assert final.version == provisional.version + 1
    assert final.supersedes == provisional.version
    assert final.summary == provisional.summary
    assert final.evidence == provisional.evidence

    # The provisional version is untouched, still retrievable at its original version/status -
    # finalizing must be an append, never a mutation.
    fetched_provisional = store.get("all_gap_hour", SUBJECT, provisional.version)
    assert fetched_provisional.status is FindingStatus.PROVISIONAL
    assert fetched_provisional == provisional

    assert store.latest("all_gap_hour", SUBJECT) == final
    assert store.history("all_gap_hour", SUBJECT) == (provisional, final)


def test_finalize_rejects_non_latest_version():
    store = FindingStore(clock=iter([1000, 2000, 3000]).__next__)
    v1 = store.emit(
        detector_name="all_gap_hour", subject=SUBJECT, status=FindingStatus.PROVISIONAL,
        summary="a", evidence={"x": 1}, quality=_quality(0, 1), detector_version="0.1.0",
    )
    store.emit(
        detector_name="all_gap_hour", subject=SUBJECT, status=FindingStatus.PROVISIONAL,
        summary="b", evidence={"x": 2}, quality=_quality(0, 1), detector_version="0.1.0",
    )

    with pytest.raises(FindingStateError):
        store.finalize(detector_name="all_gap_hour", subject=SUBJECT, version=v1.version)


def test_finalize_rejects_already_final_version():
    store = FindingStore(clock=iter([1000, 2000, 3000]).__next__)
    v1 = store.emit(
        detector_name="all_gap_hour", subject=SUBJECT, status=FindingStatus.PROVISIONAL,
        summary="a", evidence={"x": 1}, quality=_quality(0, 1), detector_version="0.1.0",
    )
    final = store.finalize(detector_name="all_gap_hour", subject=SUBJECT, version=v1.version)

    with pytest.raises(FindingStateError):
        store.finalize(detector_name="all_gap_hour", subject=SUBJECT, version=final.version)


def test_finalize_on_unknown_subject_raises():
    store = FindingStore()
    with pytest.raises(FindingStateError):
        store.finalize(detector_name="all_gap_hour", subject=("no-such-device", "2026-01-01T00"),
                        version=1)


# ---------------------------------------------------------------------------
# dispatch.py - run_active_detectors() end to end
# ---------------------------------------------------------------------------


def test_run_active_detectors_emits_findings_and_reconciles():
    local_registry: Registry = {}
    register_detector("all_gap_hour", detector_version="0.1.0", registry=local_registry)(
        detect_all_gap_hour
    )
    from detectors.framework.config import ActiveDetector

    active = (
        ActiveDetector(
            name="all_gap_hour", detector_version="0.1.0", func=detect_all_gap_hour,
            params={"min_gap_buckets": 1},
        ),
    )
    gap_context = DetectorContext(
        subject=SUBJECT, payload=tuple(_gap_bucket(i * 60_000) for i in range(2)), params={}
    )
    clean_context = DetectorContext(
        subject=("stall-B", "2026-06-01T09"),
        payload=(_measured_bucket(0),),
        params={},
    )
    store = FindingStore(clock=iter([1000, 2000]).__next__)

    result = run_active_detectors(active, [gap_context, clean_context], store)

    assert result.runs_seen == 2
    assert result.runs_succeeded == 2
    assert result.failures == ()
    assert result.reconciles()
    assert len(result.findings) == 1
    assert result.findings[0].subject == SUBJECT
    assert store.latest("all_gap_hour", SUBJECT) is not None
    assert store.latest("all_gap_hour", ("stall-B", "2026-06-01T09")) is None


def test_run_active_detectors_degrades_a_raising_detector_to_a_recorded_failure():
    from detectors.framework.config import ActiveDetector

    def _raising_detector(context: DetectorContext):
        raise ValueError("deliberate test failure")

    active = (
        ActiveDetector(name="broken", detector_version="0.0.1", func=_raising_detector, params={}),
    )
    context = DetectorContext(subject=SUBJECT, payload=(), params={})
    store = FindingStore()

    result = run_active_detectors(active, [context], store)

    assert result.runs_seen == 1
    assert result.runs_succeeded == 0
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert isinstance(failure, DetectorFailure)
    assert failure.detector_name == "broken"
    assert failure.reason == "detector_error: deliberate test failure"
    assert result.reconciles()
    assert result.findings == ()
