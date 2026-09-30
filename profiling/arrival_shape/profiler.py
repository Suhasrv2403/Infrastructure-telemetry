"""Arrival-shape profiler: protocol, batching and message-size shape per class and firmware.

Ticket: P0-06 ("Arrival-shape profiler").

Scope note: real Supercharger network/protocol access is still gated on later tickets (real
device fleet access is not yet in place - see P0-05's module docstring in
pipeline/stage0_landing/capture.py). This profiler is written against the Stage 0 message
envelope schema itself, so it will work unchanged against real Stage 0 objects once they exist;
in the meantime it's exercised (by the CLI below, and by the report in
docs/profiling/P0-06-arrival-shape.md) against the synthetic Supercharger fixture generator
(tests/fixtures/generators/supercharger.py), which is the only thing currently shaped like a
Stage 0 message. Findings produced this way describe the generator's modeled shape, not
confirmed production reality.

What this measures, given an iterable of Stage 0 message envelopes (dicts matching the schema
documented in tests/fixtures/generators/supercharger.py's module docstring and mirrored by
pipeline/stage0_landing/capture.py), broken down by (device_class, firmware_version):

1. Protocol distribution - which wire protocol(s) each group's messages used.
2. Batching shape - distribution of readings-per-message (batch size), plus a full histogram.
3. Message size - distribution of each envelope's `json.dumps(msg, sort_keys=True)` byte size,
   the same serialization pipeline.stage0_landing.capture.py's capture_messages() writes to the
   landing bucket, so this approximates real on-the-wire/on-disk envelope size.
4. Arrival cadence - distribution of inter-arrival gaps between consecutive messages from the
   same device (a stretch goal per the ticket; included here since it fell out of the same
   per-group message list at low extra cost).

CRITICAL: this module computes every statistic from fields a real Stage 0 message would
actually contain (message_id, device_id, device_class, firmware_version, site_id, protocol,
sent_ts_ms, arrival_ts_ms, readings). It deliberately never reads `_debug_injected_issues` -
that field is generator-only test/introspection metadata (see the generator's module
docstring: "a real device would never send them, and parsers should ignore any _-prefixed
key"). A profiler that secretly relied on it would report nothing once real telemetry replaces
the generator. `_debug_injected_issues` IS present in the envelope dicts this module measures
the byte-size of (see point 3 above) because that's what capture.py actually lands in Stage 0
today - excluding it from the size measurement would understate today's real landed object
size, even though it will shrink slightly once real (debug-field-free) messages replace
synthetic ones.
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

# Fixed byte-size bucket edges for the message-size histogram. Chosen to span "one tiny
# single-reading message" through "a full 12-reading batch" at roughly power-of-two
# granularity, not derived from any one run's data, so the same buckets are comparable across
# different GeneratorConfig runs (or, eventually, different real capture windows).
MESSAGE_SIZE_BUCKET_EDGES_BYTES: tuple[int, ...] = (256, 512, 1024, 2048, 4096, 8192)


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method).

    `values` need not be pre-sorted. pct is in [0, 100]. Used for min (pct=0), median (pct=50),
    p90 (pct=90) and max (pct=100) so all four numbers come from one consistent method rather
    than mixing statistics.median with a hand-rolled percentile.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


@dataclasses.dataclass(frozen=True)
class DistributionStats:
    """min/median/p90/max/mean over a batch of numeric samples."""

    count: int
    min: float
    median: float
    p90: float
    max: float
    mean: float

    @staticmethod
    def from_values(values: Sequence[float]) -> DistributionStats | None:
        """None (not zeros) when there are no samples, so callers can't mistake "no data" for
        a real all-zero distribution."""
        if not values:
            return None
        return DistributionStats(
            count=len(values),
            min=_percentile(values, 0),
            median=_percentile(values, 50),
            p90=_percentile(values, 90),
            max=_percentile(values, 100),
            mean=sum(values) / len(values),
        )


def _batch_size_histogram(batch_sizes: Sequence[int]) -> dict[int, int]:
    """Exact-size histogram from 1..max(batch_sizes), zero-filled in between.

    Batch sizes are a small bounded integer range (generator default max_batch_size=12), so an
    exact per-size count is more informative than lossy buckets, and cheap to compute.
    """
    if not batch_sizes:
        return {}
    counts = Counter(batch_sizes)
    return {size: counts.get(size, 0) for size in range(1, max(batch_sizes) + 1)}


def _size_bucket_label(
    size_bytes: int, edges: Sequence[int] = MESSAGE_SIZE_BUCKET_EDGES_BYTES
) -> str:
    prev = 0
    for edge in edges:
        if size_bytes < edge:
            return f"{prev}-{edge - 1}"
        prev = edge
    return f"{prev}+"


def _empty_size_histogram(edges: Sequence[int] = MESSAGE_SIZE_BUCKET_EDGES_BYTES) -> dict[str, int]:
    prev = 0
    labels = []
    for edge in edges:
        labels.append(f"{prev}-{edge - 1}")
        prev = edge
    labels.append(f"{prev}+")
    return {label: 0 for label in labels}


def _message_size_histogram(message_sizes: Sequence[int]) -> dict[str, int]:
    hist = _empty_size_histogram()
    for size in message_sizes:
        hist[_size_bucket_label(size)] += 1
    return hist


def _interarrival_gaps_s(msgs: Sequence[dict[str, Any]]) -> list[float]:
    """Gaps (seconds) between consecutive-by-arrival messages from the same device_id.

    Computed per device (a device's own message stream is what "cadence" means), then pooled
    across every device in the group into one distribution - a per-device breakdown would be
    far too granular for a summary report at this stage.
    """
    by_device: dict[str, list[int]] = defaultdict(list)
    for m in msgs:
        by_device[m["device_id"]].append(m["arrival_ts_ms"])

    gaps: list[float] = []
    for arrivals in by_device.values():
        arrivals.sort()
        for earlier, later in itertools.pairwise(arrivals):
            gaps.append((later - earlier) / 1000.0)
    return gaps


@dataclasses.dataclass(frozen=True)
class GroupReport:
    """Arrival-shape stats for one (device_class, firmware_version) group."""

    device_class: str
    firmware_version: str
    message_count: int
    reading_count: int
    protocol_counts: dict[str, int]
    batch_size: DistributionStats
    batch_size_histogram: dict[int, int]
    message_size_bytes: DistributionStats
    message_size_histogram: dict[str, int]
    interarrival_gap_s: DistributionStats | None


@dataclasses.dataclass(frozen=True)
class ArrivalShapeReport:
    """Top-level result: one GroupReport per (device_class, firmware_version)."""

    groups: dict[tuple[str, str], GroupReport]
    total_messages: int
    total_readings: int


def _profile_group(
    device_class: str, firmware_version: str, msgs: list[dict[str, Any]]
) -> GroupReport:
    protocol_counts = Counter(m["protocol"] for m in msgs)
    batch_sizes = [len(m["readings"]) for m in msgs]
    message_sizes = [len(json.dumps(m, sort_keys=True).encode("utf-8")) for m in msgs]
    gaps = _interarrival_gaps_s(msgs)

    return GroupReport(
        device_class=device_class,
        firmware_version=firmware_version,
        message_count=len(msgs),
        reading_count=sum(batch_sizes),
        protocol_counts=dict(sorted(protocol_counts.items())),
        batch_size=DistributionStats.from_values(batch_sizes),
        batch_size_histogram=_batch_size_histogram(batch_sizes),
        message_size_bytes=DistributionStats.from_values(message_sizes),
        message_size_histogram=_message_size_histogram(message_sizes),
        interarrival_gap_s=DistributionStats.from_values(gaps),
    )


def profile_arrival_shape(messages: Iterable[dict[str, Any]]) -> ArrivalShapeReport:
    """Compute the full arrival-shape report over an iterable of Stage 0 message envelopes.

    Groups by (device_class, firmware_version) - the same grouping P0-10's signal catalog and
    P0-07/P0-08's profilers use, since parser and quality behavior are both keyed on firmware,
    not just device class.
    """
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for msg in messages:
        by_group[(msg["device_class"], msg["firmware_version"])].append(msg)

    groups = {
        key: _profile_group(key[0], key[1], msgs) for key, msgs in by_group.items()
    }
    total_messages = sum(g.message_count for g in groups.values())
    total_readings = sum(g.reading_count for g in groups.values())
    return ArrivalShapeReport(
        groups=groups, total_messages=total_messages, total_readings=total_readings
    )


def _fmt_dist(d: DistributionStats | None, unit: str = "") -> str:
    if d is None:
        return "n/a (no samples)"
    return (
        f"min={d.min:.1f}{unit} median={d.median:.1f}{unit} p90={d.p90:.1f}{unit} "
        f"max={d.max:.1f}{unit} mean={d.mean:.1f}{unit} (n={d.count})"
    )


def format_report(report: ArrivalShapeReport) -> str:
    """Human-readable summary, one section per (device_class, firmware_version) group.

    Mirrors the plain print-style CLI output convention used by capture.py's _cli() and the
    fixture generator's own _cli(), rather than producing structured (e.g. JSON) output - this
    is meant to be read directly, or pasted into docs/profiling/P0-06-arrival-shape.md.
    """
    lines: list[str] = []
    lines.append(
        f"Arrival-shape report: {report.total_messages} messages, "
        f"{report.total_readings} readings, {len(report.groups)} (device_class, firmware) groups"
    )
    for (device_class, firmware), group in sorted(report.groups.items()):
        lines.append("")
        lines.append(f"== {device_class} / firmware {firmware} ==")
        lines.append(f"  messages={group.message_count} readings={group.reading_count}")
        lines.append(f"  protocol distribution: {group.protocol_counts}")
        lines.append(f"  batch size (readings/message): {_fmt_dist(group.batch_size)}")
        lines.append(f"  batch size histogram: {group.batch_size_histogram}")
        lines.append(f"  message size (bytes): {_fmt_dist(group.message_size_bytes, ' B')}")
        lines.append(f"  message size histogram: {group.message_size_histogram}")
        if group.interarrival_gap_s is not None:
            lines.append(f"  inter-arrival gap (s): {_fmt_dist(group.interarrival_gap_s, 's')}")
        else:
            lines.append(
                "  inter-arrival gap (s): n/a (every device in this group sent <=1 message)"
            )
    return "\n".join(lines)


def _cli() -> None:
    """Run the profiler against the synthetic Supercharger fixture generator and print a
    summary. There is no real Stage 0 capture window to point this at yet (see the module
    docstring's scope note), so - like capture.py's own CLI - this is what "run the profiler"
    means until real telemetry access exists.
    """
    from tests.fixtures.generators.supercharger import GeneratorConfig, generate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument(
        "--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware
    )
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, devices_per_firmware=args.devices_per_firmware)
    fixtures = generate(config)
    report = profile_arrival_shape(fixtures.all_messages())
    print(format_report(report))


if __name__ == "__main__":
    _cli()
