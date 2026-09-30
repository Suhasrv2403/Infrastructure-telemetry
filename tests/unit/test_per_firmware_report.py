"""Tests for the P0-09 per-firmware synthetic-proxy behavior report.

Same honesty framing and ground-truth access rule as
`tests/unit/test_retry_behavior_profiler.py`: this file is allowed to read the fixture
generator's `_debug_injected_issues` field to check the report's numbers against known
synthetic ground truth (a real device would never send that field, and
`profiling/retry_behavior/profiler.py` never reads it - only this analysis/reporting layer
does). Nothing here validates or claims anything about real firmware; it validates that the
per-firmware grouping and reporting logic in
`profiling/retry_behavior/per_firmware_report.py` correctly reflects the generator's own
synthetic ground truth, broken out per firmware instead of pooled across the fleet.
"""
from __future__ import annotations

from profiling.retry_behavior.per_firmware_report import (
    compute_per_firmware_metrics,
    render_behavior_profile_paragraph,
    render_markdown_report,
)
from profiling.retry_behavior.profiler import detect_buffer_burst_events, group_messages_by_device
from tests.fixtures.generators.supercharger import (
    DEFAULT_FIRMWARE,
    FIRMWARE_QUIRK_MULTIPLIER,
    GeneratorConfig,
    generate,
)

# Small, fast, deterministic sample for pinning down grouping/reporting logic - not meant to
# produce stable recall/precision numbers (see test_retry_behavior_profiler.py's own notes on
# why devices_per_firmware=4 gives only illustrative accuracy numbers). This file checks
# *structure and consistency with ground truth*, not specific accuracy thresholds, except
# where a firmware-level pattern should hold up even at small scale.


def test_metrics_grouped_by_firmware_not_pooled():
    """Every configured firmware version gets its own entry, and device counts per firmware
    match devices_per_firmware exactly - i.e. this is a real per-firmware breakdown, not a
    relabeled pooled result."""
    config = GeneratorConfig(devices_per_firmware=5)
    fixtures = generate(config)
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    all_firmwares = {fw for fws in DEFAULT_FIRMWARE.values() for fw in fws}
    assert set(metrics_by_firmware.keys()) == all_firmwares

    for firmware_version, metrics in metrics_by_firmware.items():
        assert metrics.firmware_version == firmware_version
        assert metrics.device_count == config.devices_per_firmware
        assert metrics.quirk_multiplier == FIRMWARE_QUIRK_MULTIPLIER[firmware_version]
        # Every firmware in this generator belongs to exactly one device class.
        assert len(metrics.device_classes) == 1


def test_per_firmware_ground_truth_counts_match_generator_debug_field():
    """The report's true_burst_messages / outage_events / drop counts for each firmware must
    exactly match what a direct read of the generator's own ground truth for that firmware
    says - this is the same cross-check test_retry_behavior_profiler.py does fleet-wide,
    repeated per firmware to pin down the grouping logic specifically."""
    config = GeneratorConfig(devices_per_firmware=6)
    fixtures = generate(config)
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    for (device_class, firmware_version), messages in fixtures.messages_by_group.items():
        expected_true_burst = sum(
            1 for m in messages if "post_outage_burst" in m["_debug_injected_issues"]
        )
        expected_stats = fixtures.stats_by_group[(device_class, firmware_version)]

        m = metrics_by_firmware[firmware_version]
        assert m.true_burst_messages == expected_true_burst
        assert m.outage_events == expected_stats.get("outage_events", 0)
        assert m.outage_readings_dropped == expected_stats.get("outage_readings_dropped", 0)
        assert m.outage_readings_buffered == expected_stats.get("outage_readings_buffered", 0)


def test_per_firmware_recall_precision_match_direct_detector_run():
    """The report's recall/precision for one firmware must equal what you'd get running the
    unmodified detector directly against just that firmware's messages and scoring against
    ground truth - i.e. the report doesn't change or leak fleet-wide pooling into a
    supposedly per-firmware number."""
    config = GeneratorConfig(devices_per_firmware=6)
    fixtures = generate(config)
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    for (device_class, firmware_version), messages in fixtures.messages_by_group.items():
        true_burst_flagged = 0
        true_burst_total = 0
        total_flagged = 0
        total_correct = 0
        for device_messages in group_messages_by_device(messages).values():
            true_ids = {
                msg["message_id"]
                for msg in device_messages
                if "post_outage_burst" in msg["_debug_injected_issues"]
            }
            flagged_ids = {e.message_id for e in detect_buffer_burst_events(device_messages)}
            true_burst_total += len(true_ids)
            true_burst_flagged += len(true_ids & flagged_ids)
            total_flagged += len(flagged_ids)
            total_correct += len(flagged_ids & true_ids)

        m = metrics_by_firmware[firmware_version]
        assert m.true_burst_messages == true_burst_total
        assert m.true_burst_messages_flagged == true_burst_flagged
        assert m.total_flagged == total_flagged
        assert m.total_flagged_correct == total_correct

        expected_recall = true_burst_flagged / true_burst_total if true_burst_total else None
        expected_precision = total_correct / total_flagged if total_flagged else None
        assert m.recall == expected_recall
        assert m.precision == expected_precision


def test_higher_quirk_multiplier_means_more_outage_exposure_in_this_generator():
    """Sanity-check the generator's own documented assumption (not a claim about real
    firmware): a firmware with a higher FIRMWARE_QUIRK_MULTIPLIER should show a higher
    observed outage rate per device and a higher observed drop rate than one with a lower
    multiplier, at a large enough sample to smooth out per-device randomness. This pins down
    that the per-firmware breakdown actually surfaces the variation the generator encodes,
    rather than diluting it back toward a fleet-wide average."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=25))
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    # 3.0.1 (0.5x) is the mildest firmware; 2.1.4 and 1.8.2 (2.5x) are the harshest, one per
    # device class so this compares like with like within each class's own firmware family.
    mild_stall = metrics_by_firmware["3.0.1"]
    harsh_stall = metrics_by_firmware["2.1.4"]
    assert harsh_stall.observed_outage_rate_per_device > mild_stall.observed_outage_rate_per_device
    assert harsh_stall.observed_drop_rate > mild_stall.observed_drop_rate

    mild_cabinet = metrics_by_firmware["1.9.0"]
    harsh_cabinet = metrics_by_firmware["1.8.2"]
    assert (
        harsh_cabinet.observed_outage_rate_per_device
        > mild_cabinet.observed_outage_rate_per_device
    )
    assert harsh_cabinet.observed_drop_rate > mild_cabinet.observed_drop_rate


def test_clock_issue_rate_does_not_vary_by_quirk_multiplier():
    """Honest counterpart to the outage/drop-rate check above: this generator does NOT scale
    missing/epoch-default/future-timestamp rates by FIRMWARE_QUIRK_MULTIPLIER (only outage
    probability and drop rate are scaled - see `_apply_outage` vs
    `_apply_timestamp_corruption` in the generator). Clock-issue rate per reading should
    therefore be roughly the same regardless of quirk multiplier, unlike outage/drop rate
    above. This pins down that the report's own "what did NOT differ by firmware" callout
    reflects the generator's actual behavior, not a documentation-only claim."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=25))
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    rates = [
        m.clock_issue_rate_per_reading
        for m in metrics_by_firmware.values()
        if m.clock_issue_rate_per_reading is not None
    ]
    assert len(rates) == len(metrics_by_firmware)
    # All firmware clock-issue rates should sit close together (within a couple percentage
    # points), regardless of quirk multiplier - a wide spread here would mean the generator
    # (or this computation) does tie clock issues to firmware after all.
    assert max(rates) - min(rates) < 0.02, f"unexpectedly firmware-dependent clock rates: {rates}"


def test_behavior_profile_paragraph_is_labeled_as_synthetic_and_not_a_finding():
    """Every per-firmware paragraph must carry an explicit synthetic-proxy disclaimer and
    must never claim to be a confirmed finding about real firmware."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=5))
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)

    for firmware_version, metrics in metrics_by_firmware.items():
        paragraph = render_behavior_profile_paragraph(metrics)
        assert "SYNTHETIC-PROXY" in paragraph
        assert firmware_version in paragraph
        assert "not a finding about real firmware" in paragraph.lower() or "not a finding" in paragraph.lower()


def test_markdown_report_has_one_section_per_firmware_with_synthetic_callout():
    """The rendered report must contain a per-firmware section for every firmware version,
    each preceded by the prominent synthetic-proxy callout, and must cross-link to the
    existing real-hardware test plan document."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=4))
    metrics_by_firmware = compute_per_firmware_metrics(fixtures)
    report = render_markdown_report(fixtures, metrics_by_firmware)

    all_firmwares = {fw for fws in DEFAULT_FIRMWARE.values() for fw in fws}
    for firmware_version in all_firmwares:
        assert f"Firmware {firmware_version}" in report

    # One callout for the report-level banner plus one per firmware section.
    assert report.count("Synthetic-proxy substitute") == 1 + len(all_firmwares)
    assert "P0-09-retry-behavior-test-plan.md" in report
    assert 'stays "To do"' in report
