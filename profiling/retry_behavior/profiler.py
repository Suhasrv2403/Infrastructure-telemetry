"""Synthetic-proxy detector for device retry/buffer/drop timing signatures (ticket P0-09).

Ticket: P0-09 ("Device retry-behavior test with fault injection"), owned by "Senior SRE +
firmware" and done when "Retry, buffer or drop behavior documented per firmware" - via
actually inducing connectivity faults on real Supercharger hardware and observing how each
firmware version behaves. CLAUDE.md is explicit that "firmware fault-injection tests" against
real hardware are human-owned, not something an agent can do.

What this module actually is
-----------------------------
This is NOT that test. It is a synthetic-proxy analysis: a detector that looks, in arrival
data, for the *timing signature* a buffer-then-burst episode would plausibly leave behind -
an unusually long quiet gap for a device followed by an unusually-sized batch - and a CLI to
run it against the fixture generator (tests/fixtures/generators/supercharger.py), which
*simulates* outage/buffer/drop behavior for testing purposes only (see that module's
docstring). Running this detector against the generator's synthetic output tells us how well
a gap+batch-size heuristic can recover a *known, synthetic* ground truth; it says nothing
about how any real device or firmware actually behaves during a real connectivity fault. The
real deliverable for P0-09 is docs/profiling/P0-09-retry-behavior-test-plan.md, a test plan
for the humans who will run that real test; this module's findings feed that plan as
hypotheses to validate, not as confirmed behavior.

This detector uses only fields a real device's message envelope could plausibly produce
(arrival_ts_ms, readings) - it never reads `_debug_injected_issues`, the generator's own
"here's what I actually did" ground-truth field, which a real device would never send and
which only tests/unit/test_retry_behavior_profiler.py is allowed to read (as a way to check
this detector's accuracy against known synthetic ground truth).

The heuristic and why
----------------------
For one device's messages, sorted by `arrival_ts_ms`:

1. Compute that device's own baseline cadence: the median inter-arrival gap and the median
   batch size (readings per message) across all of its messages. Per-device baselines matter
   because "normal" cadence and batch size vary a lot by device class/session activity - a
   fixed global threshold would be meaningless.
2. For each message after the first, flag it as a candidate "buffer-and-burst" event if BOTH:
   - `gap_ratio` (this message's arrival gap since the device's previous message / the
     device's median gap) exceeds `gap_ratio_threshold` (default 3.0x): the device went quiet
     for far longer than its own normal cadence - a necessary signature of a connectivity gap,
     buffered locally or not.
   - `batch_size_ratio` (this message's batch size / the device's median batch size) exceeds
     `batch_size_ratio_threshold` (default 0.5x): the message isn't a degenerate, trivially
     small one (e.g. a single stray reading), which is weak corroboration that some amount of
     backlog rode along with it, rather than this simply being one ordinary message that
     happened to be delayed.

Why these specific numbers, and an honest limitation up front: exploratory analysis against
this repo's fixture generator (devices_per_firmware=25, several seeds; see the profiling
notes in tests/unit/test_retry_behavior_profiler.py) found gap_ratio > 3 alone recovers
~90%+ of the generator's injected `post_outage_burst` messages, which is a strong signal on
its own. Batch size is a much weaker corroborating signal than the ticket's framing suggests:
in this generator, the message tagged as the post-outage burst is simply the *last* batch in
the device's stream, sized by the same random 1..max_batch_size draw as every other batch -
it is NOT reliably larger than the device's typical batch (median batch_size_ratio for true
burst events came out well under 1.0 in exploration). A literal "batch size > 2x median"
requirement, which is closer to what "unusually large" naturally suggests, drives recall
toward zero against this generator. So `batch_size_ratio_threshold=0.5` here is deliberately
weak - "not a suspiciously tiny leftover batch" rather than "unusually large" - chosen because
that is what the data actually supports, not because it is what the ticket's plain-language
framing implied. See the module docstring above and the test file for real precision/recall
numbers, including the honest bad news: gap-based flagging alone cannot cleanly separate a
true outage-buffer-burst from an ordinary independently-late message (this generator injects
`late_arrival` on ~6% of messages via unrelated logic), because both produce the same
observable "isolated long gap" signature from the arrival-time series alone. Precision at the
level of "is this exact flagged message a real burst" is therefore low; recall (fraction of
true burst events that get flagged) is the more informative number here, and is reported
honestly in the test file even where it's mediocre.
"""
from __future__ import annotations

import argparse
import dataclasses
import statistics
from collections import defaultdict
from typing import Any

# Need enough history to trust a per-device median baseline; below this we don't have enough
# of the device's own normal behavior to say what "unusual" means for it.
MIN_MESSAGES_FOR_BASELINE = 4

DEFAULT_GAP_RATIO_THRESHOLD = 3.0
DEFAULT_BATCH_SIZE_RATIO_THRESHOLD = 0.5


@dataclasses.dataclass(frozen=True)
class BufferBurstEvent:
    """One flagged candidate buffer-and-burst event: an observed timing signature, not a
    confirmed device behavior."""

    device_id: str
    message_id: str
    message_index: int
    arrival_ts_ms: int
    gap_ms: int
    gap_ratio: float
    batch_size: int
    batch_size_ratio: float
    baseline_median_gap_ms: float
    baseline_median_batch_size: float


def _sorted_by_arrival(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda m: m["arrival_ts_ms"])


def detect_buffer_burst_events(
    messages: list[dict[str, Any]],
    *,
    gap_ratio_threshold: float = DEFAULT_GAP_RATIO_THRESHOLD,
    batch_size_ratio_threshold: float = DEFAULT_BATCH_SIZE_RATIO_THRESHOLD,
) -> list[BufferBurstEvent]:
    """Flag candidate buffer-and-burst events in one device's messages.

    `messages` must all belong to a single device (mixing devices would make the baseline
    cadence meaningless) and each needs `arrival_ts_ms` and `readings`. Order doesn't matter -
    they're sorted here by `arrival_ts_ms`, matching how a real observer would only ever see
    messages in arrival order.

    Only observable envelope fields are read here - never `_debug_injected_issues`.
    """
    if len(messages) < MIN_MESSAGES_FOR_BASELINE:
        return []

    ordered = _sorted_by_arrival(messages)
    gaps_ms = [
        ordered[i]["arrival_ts_ms"] - ordered[i - 1]["arrival_ts_ms"] for i in range(1, len(ordered))
    ]
    batch_sizes = [len(m["readings"]) for m in ordered]

    median_gap_ms = statistics.median(gaps_ms)
    median_batch_size = statistics.median(batch_sizes)
    if median_gap_ms <= 0 or median_batch_size <= 0:
        # No usable baseline cadence (e.g. every message arrived at once) - nothing to compare
        # against, so nothing can be flagged as "unusual" relative to it.
        return []

    events: list[BufferBurstEvent] = []
    for i in range(1, len(ordered)):
        gap_ms = gaps_ms[i - 1]
        gap_ratio = gap_ms / median_gap_ms
        batch_size = batch_sizes[i]
        batch_size_ratio = batch_size / median_batch_size

        if gap_ratio > gap_ratio_threshold and batch_size_ratio > batch_size_ratio_threshold:
            msg = ordered[i]
            events.append(
                BufferBurstEvent(
                    device_id=msg.get("device_id", ""),
                    message_id=msg.get("message_id", ""),
                    message_index=i,
                    arrival_ts_ms=msg["arrival_ts_ms"],
                    gap_ms=gap_ms,
                    gap_ratio=gap_ratio,
                    batch_size=batch_size,
                    batch_size_ratio=batch_size_ratio,
                    baseline_median_gap_ms=median_gap_ms,
                    baseline_median_batch_size=median_batch_size,
                )
            )
    return events


def group_messages_by_device(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split a flat list of envelopes (e.g. one device_class/firmware group) by device_id."""
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        by_device[message["device_id"]].append(message)
    return dict(by_device)


@dataclasses.dataclass(frozen=True)
class GroupProfile:
    """Detector output for one (device_class, firmware) group."""

    device_class: str
    firmware_version: str
    device_count: int
    devices_with_events: int
    events: list[BufferBurstEvent]


def profile_messages(
    messages: list[dict[str, Any]],
    device_class: str,
    firmware_version: str,
    *,
    gap_ratio_threshold: float = DEFAULT_GAP_RATIO_THRESHOLD,
    batch_size_ratio_threshold: float = DEFAULT_BATCH_SIZE_RATIO_THRESHOLD,
) -> GroupProfile:
    """Run the detector across every device in one (device_class, firmware) message group."""
    by_device = group_messages_by_device(messages)
    all_events: list[BufferBurstEvent] = []
    devices_with_events = 0
    for device_messages in by_device.values():
        device_events = detect_buffer_burst_events(
            device_messages,
            gap_ratio_threshold=gap_ratio_threshold,
            batch_size_ratio_threshold=batch_size_ratio_threshold,
        )
        if device_events:
            devices_with_events += 1
        all_events.extend(device_events)
    return GroupProfile(
        device_class=device_class,
        firmware_version=firmware_version,
        device_count=len(by_device),
        devices_with_events=devices_with_events,
        events=all_events,
    )


def _cli() -> None:
    """Run the detector against the synthetic Supercharger fixture generator and print a
    per-(device_class, firmware) summary of flagged candidate events.

    This is exploratory/reporting tooling for the P0-09 synthetic-proxy analysis, in the same
    spirit as pipeline/stage0_landing/capture.py's `_cli()` running against the same
    generator. It never reads `_debug_injected_issues` - it has no way of knowing, and does
    not claim to know, which flagged events are real. See this module's docstring.
    """
    from tests.fixtures.generators.supercharger import GeneratorConfig, generate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument(
        "--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware
    )
    parser.add_argument("--gap-ratio-threshold", type=float, default=DEFAULT_GAP_RATIO_THRESHOLD)
    parser.add_argument(
        "--batch-size-ratio-threshold", type=float, default=DEFAULT_BATCH_SIZE_RATIO_THRESHOLD
    )
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, devices_per_firmware=args.devices_per_firmware)
    fixtures = generate(config)

    print(
        f"Synthetic-proxy retry-behavior detector (P0-09) - NOT a real-device finding, see "
        f"module docstring.\nseed={args.seed} devices_per_firmware={args.devices_per_firmware} "
        f"gap_ratio_threshold={args.gap_ratio_threshold} "
        f"batch_size_ratio_threshold={args.batch_size_ratio_threshold}\n"
    )
    total_events = 0
    for (device_class, firmware_version), messages in sorted(fixtures.messages_by_group.items()):
        profile = profile_messages(
            messages,
            device_class,
            firmware_version,
            gap_ratio_threshold=args.gap_ratio_threshold,
            batch_size_ratio_threshold=args.batch_size_ratio_threshold,
        )
        total_events += len(profile.events)
        print(
            f"{device_class}/{firmware_version}: {profile.device_count} devices, "
            f"{profile.devices_with_events} with >=1 flagged event, "
            f"{len(profile.events)} events total"
        )
    print(f"\n{total_events} candidate buffer-and-burst events flagged across all groups.")


if __name__ == "__main__":
    _cli()
