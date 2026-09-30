"""Lateness distribution and duplicate-rate profiler for Stage 0 message envelopes.

Ticket: P0-08 ("Lateness and duplicate profiler").

Scope note: real Supercharger network access doesn't exist yet (gated on later tickets), so
this profiler is exercised against the synthetic fixture generator
(tests/fixtures/generators/supercharger.py) via P0-05's Stage 0 capture path in the meantime -
see that module's docstring for the "one generator run stands in for one region" scoping
decision. Findings this profiler produces against the generator describe the generator's
*modeled* lateness/duplicate behavior, not confirmed production reality; treat them as
illustrative until real (or real-shaped) Stage 0 objects exist.

Design constraint (read before touching this file): every metric here is computed only from
fields a real Stage 0 message would actually contain - `arrival_ts_ms` (envelope), and each
reading's `device_ts_ms`/`payload_hash`, plus the envelope's `device_id`/`device_class`. This
module never reads `_debug_injected_issues` (generator-only test provenance, see the
generator's module docstring: "a real device would never send them, and parsers should ignore
any _-prefixed key"). Cross-checking against that field is a test-only concern
(tests/unit/test_lateness_duplicates_profiler.py), never production logic.

Two metrics, per device_class:

1. Lateness distribution: for each reading with a plausible device_ts_ms, lateness_s =
   (envelope arrival_ts_ms - reading device_ts_ms) / 1000. "Plausible" is a basic sanity guard
   only (device_ts_ms is not None and > 0) - this ticket doesn't need P0-07's full clock-skew
   profiler, just enough to keep missing/epoch-default timestamps from poisoning the
   distribution with nonsense (multi-decade) lateness values.

   Important caveat, worth restating anywhere these numbers get quoted: the generator anchors
   each message's `arrival_ts_ms` to its *last* reading's `device_ts_ms` (see
   `_emit_device_messages` in the generator - `base_arrival_ms = last_ts_ms`), plus jitter.
   That means every reading in a batch *except* the last shows positive lateness purely from
   batching - a stall reporting every 15s in batches of up to 12 will show up to ~165s of
   "lateness" on its earliest batched reading under completely normal operation, with no
   network delay or retry involved. Batching lateness and *arrival* lateness (late_arrival /
   post_outage_burst in the generator's model) are both real contributors to what this
   profiler measures, but they are not the same phenomenon, and the distribution reported here
   is a mix of both. A per-batch-position breakdown would separate them; out of scope for this
   ticket (see docs/profiling/P0-08-lateness-duplicates.md's notes).

2. Duplicate rate: the merge key Stage 1 will use is (device_id, device_ts, payload_hash) -
   CLAUDE.md invariant 2. A reading-key that shows up on more than one message envelope is
   exactly the signal of a device retransmission Stage 1's MERGE exists to absorb, so this
   profiler is effectively a pre-check of how much dedup work Stage 1 will have to do on real
   traffic once P1-06 (idempotent merge) lands. Reported two ways: the fraction of distinct
   reading-keys that appear on more than one message, and (best-effort) the fraction of
   messages that are pure duplicates of an earlier message (every reading-key already seen)
   versus partial overlaps (some but not all reading-keys already seen). Message-level
   classification processes messages in `arrival_ts_ms` order (ties broken by message_id) -
   the profiler's best proxy for "processing order" absent a real ingest timestamp, matching
   how Stage 0 objects are keyed (docs/decisions/0001, arrival hour).
"""
from __future__ import annotations

import argparse
import dataclasses
import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

# A reading-key exactly as Stage 1 will merge on (CLAUDE.md invariant 2), scoped to one
# device_class's message set.
ReadingKey = tuple[Any, Any, Any]  # (device_id, device_ts_ms, payload_hash)


def _has_plausible_device_ts(device_ts_ms: Any) -> bool:
    """Basic sanity guard, not P0-07's full clock-skew profiler.

    Excludes missing (`None`) and epoch-default (`0`) timestamps, which would otherwise poison
    a lateness distribution with either a crash (None) or a multi-decade outlier (0). Anything
    else - including generator-injected future timestamps - is left in; a device_ts_ms in the
    future produces a *negative* lateness value, which is itself part of what's worth seeing in
    the distribution, not something to filter out.
    """
    return isinstance(device_ts_ms, (int, float)) and device_ts_ms > 0


def _reading_key(device_id: Any, reading: dict[str, Any]) -> ReadingKey:
    return (device_id, reading.get("device_ts_ms"), reading.get("payload_hash"))


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method).

    `sorted_values` must be non-empty and already sorted ascending.
    """
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[int(rank)]
    lo_weight = sorted_values[lo] * (hi - rank)
    hi_weight = sorted_values[hi] * (rank - lo)
    return lo_weight + hi_weight


@dataclasses.dataclass(frozen=True)
class LatenessStats:
    """Distribution of (arrival_ts_ms - device_ts_ms) in seconds, over readings with a
    plausible device_ts_ms only. All fields are `None` when there were zero such readings."""

    readings_considered: int
    readings_skipped_implausible_ts: int
    min_s: float | None
    median_s: float | None
    p90_s: float | None
    p99_s: float | None
    max_s: float | None

    @classmethod
    def from_values(cls, values: list[float], skipped: int) -> LatenessStats:
        if not values:
            return cls(
                readings_considered=0,
                readings_skipped_implausible_ts=skipped,
                min_s=None,
                median_s=None,
                p90_s=None,
                p99_s=None,
                max_s=None,
            )
        ordered = sorted(values)
        return cls(
            readings_considered=len(ordered),
            readings_skipped_implausible_ts=skipped,
            min_s=ordered[0],
            median_s=_percentile(ordered, 0.5),
            p90_s=_percentile(ordered, 0.9),
            p99_s=_percentile(ordered, 0.99),
            max_s=ordered[-1],
        )


@dataclasses.dataclass(frozen=True)
class DuplicateStats:
    """Duplicate-rate summary for one device_class's reading-keys and messages.

    Key-level: computed over every reading-key regardless of order. Message-level: computed by
    walking messages in arrival order and checking each message's reading-keys against the set
    of keys already observed on an earlier message (see module docstring).
    """

    total_reading_instances: int
    distinct_reading_keys: int
    duplicated_reading_keys: int  # distinct keys that appear on >1 message
    duplicate_key_rate: float  # duplicated_reading_keys / distinct_reading_keys
    excess_reading_instance_rate: float  # (total_instances - distinct_keys) / total_instances

    messages_total: int
    pure_duplicate_messages: int  # every reading-key already seen on an earlier message
    partial_duplicate_messages: int  # some, but not all, reading-keys already seen
    pure_duplicate_message_rate: float
    partial_duplicate_message_rate: float


@dataclasses.dataclass(frozen=True)
class ClassProfile:
    device_class: str
    message_count: int
    reading_count: int
    lateness: LatenessStats
    duplicates: DuplicateStats


def _duplicate_stats(class_messages: list[dict[str, Any]]) -> DuplicateStats:
    key_counts: Counter[ReadingKey] = Counter()
    total_reading_instances = 0
    for message in class_messages:
        device_id = message.get("device_id")
        for reading in message.get("readings", []):
            key_counts[_reading_key(device_id, reading)] += 1
            total_reading_instances += 1

    distinct_reading_keys = len(key_counts)
    duplicated_reading_keys = sum(1 for c in key_counts.values() if c > 1)
    duplicate_key_rate = (
        duplicated_reading_keys / distinct_reading_keys if distinct_reading_keys else 0.0
    )
    excess_reading_instance_rate = (
        (total_reading_instances - distinct_reading_keys) / total_reading_instances
        if total_reading_instances
        else 0.0
    )

    # Message-level pure/partial classification, in arrival order.
    ordered_messages = sorted(
        class_messages, key=lambda m: (m.get("arrival_ts_ms", 0), m.get("message_id", ""))
    )
    seen_keys: set[ReadingKey] = set()
    messages_with_readings = 0
    pure_duplicate_messages = 0
    partial_duplicate_messages = 0
    for message in ordered_messages:
        device_id = message.get("device_id")
        readings = message.get("readings", [])
        if not readings:
            continue
        messages_with_readings += 1
        message_keys = {_reading_key(device_id, r) for r in readings}
        already_seen = message_keys & seen_keys
        if already_seen == message_keys:
            pure_duplicate_messages += 1
        elif already_seen:
            partial_duplicate_messages += 1
        seen_keys |= message_keys

    return DuplicateStats(
        total_reading_instances=total_reading_instances,
        distinct_reading_keys=distinct_reading_keys,
        duplicated_reading_keys=duplicated_reading_keys,
        duplicate_key_rate=duplicate_key_rate,
        excess_reading_instance_rate=excess_reading_instance_rate,
        messages_total=messages_with_readings,
        pure_duplicate_messages=pure_duplicate_messages,
        partial_duplicate_messages=partial_duplicate_messages,
        pure_duplicate_message_rate=(
            pure_duplicate_messages / messages_with_readings if messages_with_readings else 0.0
        ),
        partial_duplicate_message_rate=(
            partial_duplicate_messages / messages_with_readings
            if messages_with_readings
            else 0.0
        ),
    )


def _lateness_stats(class_messages: list[dict[str, Any]]) -> LatenessStats:
    values: list[float] = []
    skipped = 0
    for message in class_messages:
        arrival_ts_ms = message.get("arrival_ts_ms")
        if not isinstance(arrival_ts_ms, (int, float)):
            skipped += len(message.get("readings", []))
            continue
        for reading in message.get("readings", []):
            device_ts_ms = reading.get("device_ts_ms")
            if not _has_plausible_device_ts(device_ts_ms):
                skipped += 1
                continue
            values.append((arrival_ts_ms - device_ts_ms) / 1000.0)
    return LatenessStats.from_values(values, skipped)


def profile_messages(messages: Iterable[dict[str, Any]]) -> dict[str, ClassProfile]:
    """Compute a ClassProfile per device_class found in `messages`.

    `messages` is any iterable of Stage 0 message envelopes (see module docstring for the
    schema this relies on). Order doesn't matter for the key-level duplicate metric; the
    message-level pure/partial classification re-sorts by arrival_ts_ms internally.
    """
    by_class: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        device_class = message.get("device_class", "unknown")
        by_class.setdefault(device_class, []).append(message)

    profiles: dict[str, ClassProfile] = {}
    for device_class, class_messages in by_class.items():
        reading_count = sum(len(m.get("readings", [])) for m in class_messages)
        profiles[device_class] = ClassProfile(
            device_class=device_class,
            message_count=len(class_messages),
            reading_count=reading_count,
            lateness=_lateness_stats(class_messages),
            duplicates=_duplicate_stats(class_messages),
        )
    return profiles


def _format_report(profiles: dict[str, ClassProfile]) -> str:
    lines = []
    for device_class in sorted(profiles):
        p = profiles[device_class]
        lines.append(f"== {device_class} ==")
        lines.append(f"  messages={p.message_count} readings={p.reading_count}")
        lat = p.lateness
        if lat.readings_considered:
            lines.append(
                f"  lateness_s (n={lat.readings_considered}, "
                f"skipped_implausible_ts={lat.readings_skipped_implausible_ts}): "
                f"min={lat.min_s:.1f} median={lat.median_s:.1f} p90={lat.p90_s:.1f} "
                f"p99={lat.p99_s:.1f} max={lat.max_s:.1f}"
            )
        else:
            lines.append("  lateness_s: no readings with a plausible device_ts_ms")
        dup = p.duplicates
        lines.append(
            f"  duplicate reading-keys: {dup.duplicated_reading_keys}/"
            f"{dup.distinct_reading_keys} distinct keys duplicated "
            f"({dup.duplicate_key_rate:.2%}); excess instance rate "
            f"{dup.excess_reading_instance_rate:.2%} "
            f"({dup.total_reading_instances} total reading instances)"
        )
        lines.append(
            f"  duplicate messages: pure={dup.pure_duplicate_messages} "
            f"({dup.pure_duplicate_message_rate:.2%}) "
            f"partial={dup.partial_duplicate_messages} "
            f"({dup.partial_duplicate_message_rate:.2%}) of {dup.messages_total} messages"
        )
    return "\n".join(lines)


def _cli() -> None:
    """Run the profiler against the synthetic Supercharger fixture generator.

    Mirrors pipeline/stage0_landing/capture.py's CLI shape: until real Supercharger telemetry
    access exists (see module docstring's scope note), this generator run is the stand-in
    input for "a batch of Stage 0 message envelopes."
    """
    from tests.fixtures.generators.supercharger import GeneratorConfig, generate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument(
        "--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the raw profile as JSON instead of text."
    )
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, devices_per_firmware=args.devices_per_firmware)
    fixtures = generate(config)
    profiles = profile_messages(fixtures.all_messages())

    if args.json:
        import json

        print(json.dumps({k: dataclasses.asdict(v) for k, v in profiles.items()}, indent=2))
    else:
        print(_format_report(profiles))


if __name__ == "__main__":
    _cli()
