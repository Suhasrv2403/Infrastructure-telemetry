"""Per-firmware synthetic-proxy behavior report for ticket P0-09.

Ticket P0-09 ("Device retry-behavior test with fault injection") is done when "Retry, buffer
or drop behavior documented per firmware" - via real fault injection against physical
Supercharger hardware, which CLAUDE.md scopes as human/firmware-owned. This module is not
that. It is a further synthetic-proxy substitute, built on top of two things that already
exist in this repo:

- `profiling/retry_behavior/profiler.py`: a heuristic detector that flags candidate
  buffer-and-burst timing signatures from arrival data alone (never reads generator
  ground-truth).
- `tests/fixtures/generators/supercharger.py`: the fixture generator whose synthetic outage
  simulation gives the detector something to run against, and whose `_debug_injected_issues`
  field (a real device would never send this) gives us known ground truth to score the
  detector against - exactly as `tests/unit/test_retry_behavior_profiler.py` already does.

What's new here is *grouping*. The existing detector and its accuracy numbers
(docs/profiling/P0-09-retry-behavior-test-plan.md, Part A) are reported fleet-wide, pooled
across every firmware version. The ticket's literal "done when" wants behavior documented
PER FIRMWARE - which matters because the generator itself models firmware-dependent
variation (`FIRMWARE_QUIRK_MULTIPLIER`: older/cheaper firmware gets a higher simulated outage
probability and a higher simulated local-buffer drop rate once an outage happens). This module
breaks the same analysis down by `firmware_version` and reports, per firmware:

1. Detector recall/precision against that firmware's own synthetic ground truth (same
   methodology as the pooled test, just not pooled).
2. A plain-language "assumed behavior profile" paragraph built from the generator's own
   ground-truth counters (not the detector's output) - outage frequency, drop rate, etc. -
   explicitly labeled as a synthetic-proxy inference about the generator's assumptions, never
   as a finding about real firmware.

Honesty framing, worth repeating because it's the entire point of this module: nothing here
is a measurement of any real device or firmware. It is what a hypothetical fault-injection
test's *reporting layer* would look like, run against a simulation instead of real hardware,
so that downstream work (P1-01's buffer sizing, telemetry-health dropout detectors, P0-10's
signal catalog) has a concrete per-firmware shaped artifact to reference while the real test
in docs/profiling/P0-09-retry-behavior-test-plan.md (Part B) remains undone. This module,
like the rest of P0-09's synthetic work, does NOT close the ticket - see that document's
"Status of this document" banner.

Ground-truth access rule (unchanged from the existing detector/test split): this module is
analysis/reporting code, not the detector. It is allowed to read `_debug_injected_issues` to
score the detector and to build the per-firmware ground-truth profile (same as the unit
tests do) - `profiling/retry_behavior/profiler.py` itself must never do this.
"""
from __future__ import annotations

import argparse
import dataclasses
from collections import Counter, defaultdict
from pathlib import Path

from profiling.retry_behavior.profiler import (
    DEFAULT_BATCH_SIZE_RATIO_THRESHOLD,
    DEFAULT_GAP_RATIO_THRESHOLD,
    detect_buffer_burst_events,
    group_messages_by_device,
)
from tests.fixtures.generators.supercharger import (
    FIRMWARE_QUIRK_MULTIPLIER,
    GeneratedFixtures,
    GeneratorConfig,
    generate,
)

# Larger than profiler.py's / the generator's own default (4) and the unit test's default
# scale, because a per-firmware breakdown splits an already-small sample of true outage
# events across five firmware groups - a bigger sample makes each group's numbers less
# noisy. Still a small synthetic sample, not a scaled-down real fleet; treat the report's
# numbers as illustrative, not precise, especially for the firmware with the fewest true
# outage events (see each section's callout).
DEFAULT_REPORT_DEVICES_PER_FIRMWARE = 40

TEST_PLAN_DOC = "docs/profiling/P0-09-retry-behavior-test-plan.md"


@dataclasses.dataclass(frozen=True)
class FirmwareBehaviorMetrics:
    """Per-firmware rollup: generator ground truth + detector accuracy against it.

    Everything here is derived either from the generator's own `_debug_injected_issues` /
    stats counters (ground truth about what the *simulation* did) or from running the
    unmodified `profiler.py` detector against that firmware's messages and scoring it against
    that ground truth. None of it is a measurement of real device behavior.
    """

    firmware_version: str
    device_classes: tuple[str, ...]
    quirk_multiplier: float
    device_count: int
    messages_total: int
    readings_total: int
    outage_events: int
    outage_readings_buffered: int
    outage_readings_dropped: int
    post_outage_burst_messages: int
    late_arrival_messages: int
    duplicate_messages: int
    missing_timestamp_readings: int
    epoch_default_timestamp_readings: int
    future_timestamp_readings: int
    expected_outage_probability: float
    expected_drop_rate: float
    true_burst_messages: int
    true_burst_messages_flagged: int
    total_flagged: int
    total_flagged_correct: int
    devices_with_true_burst: int
    devices_detector_hit_true_burst: int

    @property
    def recall(self) -> float | None:
        """Fraction of this firmware's true synthetic burst messages the detector flagged."""
        if not self.true_burst_messages:
            return None
        return self.true_burst_messages_flagged / self.true_burst_messages

    @property
    def precision(self) -> float | None:
        """Fraction of this firmware's flagged candidates that were real synthetic bursts."""
        if not self.total_flagged:
            return None
        return self.total_flagged_correct / self.total_flagged

    @property
    def device_recall(self) -> float | None:
        """Fraction of devices with >=1 true burst whose burst message got flagged."""
        if not self.devices_with_true_burst:
            return None
        return self.devices_detector_hit_true_burst / self.devices_with_true_burst

    @property
    def observed_drop_rate(self) -> float | None:
        """Of readings caught in a simulated outage window, the fraction actually dropped."""
        denom = self.outage_readings_buffered + self.outage_readings_dropped
        if not denom:
            return None
        return self.outage_readings_dropped / denom

    @property
    def observed_outage_rate_per_device(self) -> float | None:
        """Fraction of this firmware's devices that hit a simulated outage at all."""
        if not self.device_count:
            return None
        return self.outage_events / self.device_count

    @property
    def clock_issue_rate_per_reading(self) -> float | None:
        """Combined missing/epoch-default/future-timestamp rate, for the "did firmware affect
        clock issues too" cross-check (see module docstring / report's cross-firmware note)."""
        if not self.readings_total:
            return None
        clock_issues = (
            self.missing_timestamp_readings
            + self.epoch_default_timestamp_readings
            + self.future_timestamp_readings
        )
        return clock_issues / self.readings_total


def compute_per_firmware_metrics(
    fixtures: GeneratedFixtures,
    *,
    gap_ratio_threshold: float = DEFAULT_GAP_RATIO_THRESHOLD,
    batch_size_ratio_threshold: float = DEFAULT_BATCH_SIZE_RATIO_THRESHOLD,
) -> dict[str, FirmwareBehaviorMetrics]:
    """Group the generator's output by `firmware_version` (not by device_class, and not
    pooled across the whole fleet) and compute detector accuracy + ground-truth behavior
    stats for each firmware.

    In today's generator, `firmware_version` strings don't overlap between device classes
    (stalls: 2.1.4/2.3.0/3.0.1, cabinets: 1.8.2/1.9.0), so this is equivalent to a straight
    per-(device_class, firmware) breakdown - but grouping by firmware_version directly means
    this keeps working correctly if that ever changes (e.g. a shared firmware family across
    device classes).

    Only this analysis layer reads `_debug_injected_issues` - `detect_buffer_burst_events`
    itself is called exactly as `profiler.py` calls it, with only envelope fields.
    """
    config = fixtures.config

    messages_by_firmware: dict[str, list[dict]] = defaultdict(list)
    device_classes_by_firmware: dict[str, set[str]] = defaultdict(set)
    stats_by_firmware: dict[str, Counter] = defaultdict(Counter)

    for (device_class, firmware_version), messages in fixtures.messages_by_group.items():
        messages_by_firmware[firmware_version].extend(messages)
        device_classes_by_firmware[firmware_version].add(device_class)
        stats_by_firmware[firmware_version].update(
            fixtures.stats_by_group[(device_class, firmware_version)]
        )

    metrics_by_firmware: dict[str, FirmwareBehaviorMetrics] = {}
    for firmware_version, messages in messages_by_firmware.items():
        stats = stats_by_firmware[firmware_version]

        device_count = 0
        true_burst_messages = 0
        true_burst_messages_flagged = 0
        total_flagged = 0
        total_flagged_correct = 0
        devices_with_true_burst = 0
        devices_detector_hit_true_burst = 0

        for device_messages in group_messages_by_device(messages).values():
            device_count += 1
            true_burst_ids = {
                m["message_id"]
                for m in device_messages
                if "post_outage_burst" in m["_debug_injected_issues"]
            }
            events = detect_buffer_burst_events(
                device_messages,
                gap_ratio_threshold=gap_ratio_threshold,
                batch_size_ratio_threshold=batch_size_ratio_threshold,
            )
            flagged_ids = {e.message_id for e in events}

            if true_burst_ids:
                devices_with_true_burst += 1
                if true_burst_ids & flagged_ids:
                    devices_detector_hit_true_burst += 1

            true_burst_messages += len(true_burst_ids)
            true_burst_messages_flagged += len(true_burst_ids & flagged_ids)
            total_flagged += len(flagged_ids)
            total_flagged_correct += len(flagged_ids & true_burst_ids)

        multiplier = FIRMWARE_QUIRK_MULTIPLIER.get(firmware_version, 1.0)
        metrics_by_firmware[firmware_version] = FirmwareBehaviorMetrics(
            firmware_version=firmware_version,
            device_classes=tuple(sorted(device_classes_by_firmware[firmware_version])),
            quirk_multiplier=multiplier,
            device_count=device_count,
            messages_total=stats.get("messages_total", 0),
            readings_total=stats.get("readings_total", 0),
            outage_events=stats.get("outage_events", 0),
            outage_readings_buffered=stats.get("outage_readings_buffered", 0),
            outage_readings_dropped=stats.get("outage_readings_dropped", 0),
            post_outage_burst_messages=stats.get("post_outage_burst_messages", 0),
            late_arrival_messages=stats.get("late_arrival", 0),
            duplicate_messages=stats.get("duplicate_message", 0),
            missing_timestamp_readings=stats.get("missing_timestamp", 0),
            epoch_default_timestamp_readings=stats.get("epoch_default_timestamp", 0),
            future_timestamp_readings=stats.get("future_timestamp", 0),
            # Mirrors the formula `_apply_outage` in the generator actually uses, so this is
            # the theoretical rate the generator was configured to apply for this firmware -
            # not a re-measurement. If that formula ever changes, this drifts with it; the
            # `observed_*` properties above are the actual counts from this run and are the
            # more trustworthy number to quote.
            expected_outage_probability=min(0.95, config.outage_probability_per_device * multiplier),
            expected_drop_rate=min(0.9, config.outage_drop_rate * multiplier),
            true_burst_messages=true_burst_messages,
            true_burst_messages_flagged=true_burst_messages_flagged,
            total_flagged=total_flagged,
            total_flagged_correct=total_flagged_correct,
            devices_with_true_burst=devices_with_true_burst,
            devices_detector_hit_true_burst=devices_detector_hit_true_burst,
        )

    return metrics_by_firmware


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def render_behavior_profile_paragraph(metrics: FirmwareBehaviorMetrics) -> str:
    """One paragraph of plain-language "assumed behavior profile" for one firmware.

    Built entirely from the generator's own ground-truth counters and the quirk multiplier
    that drove them - never from the detector's flagged events, which get their own
    recall/precision line instead. Every sentence here describes the *synthetic model's*
    assumptions and this run's outcome under them, not real firmware behavior.
    """
    m = metrics
    class_label = " / ".join(m.device_classes) if m.device_classes else "unknown device class"

    if m.quirk_multiplier >= 2.0:
        quirk_desc = "the generator's harshest quirk multiplier (modeled as older/buggier)"
    elif m.quirk_multiplier > 1.0:
        quirk_desc = "a moderate quirk multiplier"
    elif m.quirk_multiplier < 1.0:
        quirk_desc = "the generator's mildest quirk multiplier (modeled as newer/more capable)"
    else:
        quirk_desc = "the generator's baseline (1.0x) quirk multiplier"

    sample_caveat = ""
    if m.true_burst_messages < 5:
        sample_caveat = (
            f" With only {m.true_burst_messages} true synthetic burst event(s) in this sample, "
            "treat this firmware's recall/precision numbers as especially noisy - a difference "
            "of one flagged or missed event swings the percentage a lot."
        )

    return (
        f"SYNTHETIC-PROXY INFERENCE about the fixture generator's model, not a finding about "
        f"real firmware {m.firmware_version}. In this generator, firmware {m.firmware_version} "
        f"({class_label}) is assigned {quirk_desc} (FIRMWARE_QUIRK_MULTIPLIER = "
        f"{m.quirk_multiplier:g}x), which the generator's `_apply_outage` step turns into a "
        f"configured ~{_pct(m.expected_outage_probability)} chance of a simulated connectivity "
        f"outage per device and a configured ~{_pct(m.expected_drop_rate)} drop rate for "
        f"readings caught in one - never into a different rate of clock-quality issues "
        f"(missing/epoch-default/future timestamps are injected at the same rate for every "
        f"firmware in this generator; see the report's cross-firmware note). Observed in this "
        f"run: {m.outage_events} simulated outages across {m.device_count} devices "
        f"({_pct(m.observed_outage_rate_per_device)} of devices affected), with "
        f"{_pct(m.observed_drop_rate)} of readings caught in those outage windows dropped "
        f"rather than buffered-and-flushed. If real firmware {m.firmware_version} resembles "
        f"this synthetic stand-in at all, the resulting hypothesis for Part B of the real test "
        f"plan ({TEST_PLAN_DOC}) is: a smaller effective local buffer and a higher share of "
        f"readings silently dropped (rather than buffered and flushed on reconnect) during a "
        f"real connectivity fault, relative to firmware with a lower quirk multiplier. The "
        f"arrival-timing detector recovered {_pct(m.recall)} of this firmware's synthetic burst "
        f"events (device-level recall {_pct(m.device_recall)}) at {_pct(m.precision, 1)} "
        f"precision - most flagged candidates are false alarms, mainly from independently-late "
        f"messages that look identical to a true outage from arrival timing alone (see "
        f"profiler.py's docstring).{sample_caveat}"
    )


_SYNTHETIC_CALLOUT = (
    "> **Synthetic-proxy substitute - not a real fault-injection result.** Everything in this "
    f"section is inferred from the fixture generator's synthetic simulation "
    "(`tests/fixtures/generators/supercharger.py`) and a heuristic detector's accuracy against "
    "that simulation's own known ground truth. No real Supercharger device or firmware was "
    "involved. It exists so downstream work has a concrete per-firmware-shaped artifact while "
    f"the real test in [`{TEST_PLAN_DOC}`](../../{TEST_PLAN_DOC}) (Part B) remains undone. "
    "P0-09 stays \"To do\" until that real test happens."
)


def render_markdown_report(
    fixtures: GeneratedFixtures, metrics_by_firmware: dict[str, FirmwareBehaviorMetrics]
) -> str:
    """Render the full per-firmware markdown report."""
    config = fixtures.config
    lines: list[str] = []
    lines.append("# P0-09: Per-firmware retry/buffer/drop behavior report (synthetic proxy)")
    lines.append("")
    lines.append('**Ticket:** P0-09 ("Device retry-behavior test with fault injection")')
    lines.append('**Done when:** Retry, buffer or drop behavior documented per firmware')
    lines.append(
        "**Status of this document:** a further synthetic-proxy deliverable, generated by "
        "`profiling/retry_behavior/per_firmware_report.py` against the synthetic fixture "
        "generator. It breaks the fleet-wide synthetic analysis in "
        f"[`{TEST_PLAN_DOC}`](../../{TEST_PLAN_DOC}) down per firmware, which is closer to "
        "the ticket's literal wording, but it is **not** the real per-firmware fault-injection "
        "result that ticket asks for, and per an explicit product-owner decision this ticket "
        "stays \"To do\" in the backlog regardless. See that document's own \"Status of this "
        "document\" banner and Part B for the real test plan this would need to be replaced "
        "with."
    )
    lines.append("")
    lines.append(_SYNTHETIC_CALLOUT)
    lines.append("")
    lines.append(
        f"Generated against `tests/fixtures/generators/supercharger.py` with "
        f"`devices_per_firmware={config.devices_per_firmware}`, `seed={config.seed}`. Detector "
        "thresholds: gap_ratio_threshold=3.0, batch_size_ratio_threshold=0.5 (profiler.py "
        "defaults, unmodified)."
    )
    lines.append("")
    lines.append("## Cross-firmware summary")
    lines.append("")
    lines.append(
        "| Firmware | Device class(es) | Quirk x | Devices | Outage rate | Drop rate "
        "(observed) | Detector recall | Detector precision |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for firmware_version in sorted(metrics_by_firmware, key=lambda fw: (metrics_by_firmware[fw].device_classes, fw)):
        m = metrics_by_firmware[firmware_version]
        lines.append(
            f"| {m.firmware_version} | {' / '.join(m.device_classes)} | {m.quirk_multiplier:g}x "
            f"| {m.device_count} | {_pct(m.observed_outage_rate_per_device)} | "
            f"{_pct(m.observed_drop_rate)} | {_pct(m.recall)} | {_pct(m.precision, 1)} |"
        )
    lines.append("")
    lines.append(
        "**What did NOT differ by firmware in this generator:** clock-quality issue rates. "
        "`FIRMWARE_QUIRK_MULTIPLIER`'s module comment describes older firmware as having "
        "\"more clock issues\" as well as smaller buffers, but the generator's actual "
        "`_apply_timestamp_corruption` step applies the same missing/epoch-default/future-"
        "timestamp rates to every firmware regardless of multiplier - only simulated outage "
        "probability and drop rate are actually scaled by it. The table below is the honest "
        "cross-check: rates should look flat across firmware."
    )
    lines.append("")
    lines.append("| Firmware | Clock-issue rate per reading (missing+epoch-default+future) |")
    lines.append("|---|---|")
    for firmware_version in sorted(metrics_by_firmware, key=lambda fw: (metrics_by_firmware[fw].device_classes, fw)):
        m = metrics_by_firmware[firmware_version]
        lines.append(f"| {m.firmware_version} | {_pct(m.clock_issue_rate_per_reading, 1)} |")
    lines.append("")
    lines.append(
        "This is itself a small, honest finding worth carrying into Part B: this generator "
        "does not actually model a per-firmware clock-quality difference, despite its own "
        "comment suggesting it should - so no hypothesis about firmware-dependent clock "
        "behavior can be drawn from it. That remains an open question for the real hardware "
        "test, not something this synthetic model can even pretend to answer."
    )
    lines.append("")
    lines.append("## Per-firmware sections")
    lines.append("")

    for firmware_version in sorted(metrics_by_firmware, key=lambda fw: (metrics_by_firmware[fw].device_classes, fw)):
        m = metrics_by_firmware[firmware_version]
        lines.append(f"### Firmware {m.firmware_version} ({' / '.join(m.device_classes)})")
        lines.append("")
        lines.append(_SYNTHETIC_CALLOUT)
        lines.append("")
        lines.append(render_behavior_profile_paragraph(m))
        lines.append("")
        lines.append("Raw counters for this firmware (from the generator's own ground truth):")
        lines.append("")
        lines.append(
            f"- Devices: {m.device_count}, messages: {m.messages_total}, readings: "
            f"{m.readings_total}"
        )
        lines.append(
            f"- Simulated outage events: {m.outage_events} "
            f"({_pct(m.observed_outage_rate_per_device)} of devices)"
        )
        lines.append(
            f"- Outage-window readings buffered-and-survived: {m.outage_readings_buffered}, "
            f"dropped: {m.outage_readings_dropped} (observed drop rate "
            f"{_pct(m.observed_drop_rate)}, generator was configured for "
            f"~{_pct(m.expected_drop_rate)})"
        )
        lines.append(
            f"- Detector: {m.true_burst_messages} true synthetic burst messages, "
            f"{m.true_burst_messages_flagged} flagged correctly, {m.total_flagged} total "
            f"flagged candidates ({m.total_flagged_correct} correct) -> recall "
            f"{_pct(m.recall)}, device-level recall {_pct(m.device_recall)}, precision "
            f"{_pct(m.precision, 1)}"
        )
        lines.append("")

    lines.append("## See also")
    lines.append("")
    lines.append(
        f"- [`{TEST_PLAN_DOC}`](../../{TEST_PLAN_DOC}) - the fleet-wide (non-per-firmware) "
        "synthetic-proxy analysis and, in Part B, the actual real fault-injection test plan "
        "this report is a stand-in for."
    )
    lines.append(
        "- `profiling/retry_behavior/profiler.py` - the detector this report runs, unmodified, "
        "per firmware."
    )
    lines.append(
        "- `profiling/retry_behavior/per_firmware_report.py` - this report's generating code."
    )
    lines.append("")

    return "\n".join(lines)


def generate_report(
    *,
    seed: int = GeneratorConfig().seed,
    devices_per_firmware: int = DEFAULT_REPORT_DEVICES_PER_FIRMWARE,
    gap_ratio_threshold: float = DEFAULT_GAP_RATIO_THRESHOLD,
    batch_size_ratio_threshold: float = DEFAULT_BATCH_SIZE_RATIO_THRESHOLD,
) -> str:
    """Generate the full markdown report text for a given generator configuration."""
    config = GeneratorConfig(seed=seed, devices_per_firmware=devices_per_firmware)
    fixtures = generate(config)
    metrics_by_firmware = compute_per_firmware_metrics(
        fixtures,
        gap_ratio_threshold=gap_ratio_threshold,
        batch_size_ratio_threshold=batch_size_ratio_threshold,
    )
    return render_markdown_report(fixtures, metrics_by_firmware)


def _cli() -> None:
    """Generate the per-firmware synthetic-proxy report and write it to disk (or stdout)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument(
        "--devices-per-firmware", type=int, default=DEFAULT_REPORT_DEVICES_PER_FIRMWARE
    )
    parser.add_argument("--gap-ratio-threshold", type=float, default=DEFAULT_GAP_RATIO_THRESHOLD)
    parser.add_argument(
        "--batch-size-ratio-threshold", type=float, default=DEFAULT_BATCH_SIZE_RATIO_THRESHOLD
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/profiling/P0-09-per-firmware-behavior-report.md"),
        help="Output markdown path, or '-' for stdout.",
    )
    args = parser.parse_args()

    report = generate_report(
        seed=args.seed,
        devices_per_firmware=args.devices_per_firmware,
        gap_ratio_threshold=args.gap_ratio_threshold,
        batch_size_ratio_threshold=args.batch_size_ratio_threshold,
    )

    if str(args.out) == "-":
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"Wrote per-firmware synthetic-proxy report to {args.out}")


if __name__ == "__main__":
    _cli()
