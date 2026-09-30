"""Timestamp and clock-quality profiler for Stage 0 message envelopes.

Ticket: P0-07 ("Timestamp and clock-quality profiler").

Scope note: real Supercharger telemetry access is still gated on later tickets (see
pipeline/stage0_landing/capture.py's module docstring), so this profiler is exercised here
against the synthetic fixture generator (tests/fixtures/generators/supercharger.py), which
deliberately models the same kinds of clock-quality issues this profiler exists to measure on
real data (see that module's docstring, "timestamp/clock quality"). The numbers this produces
against the generator describe the *generator's* modeled behavior, not confirmed production
device behavior - see docs/profiling/P0-07-timestamp-clock-quality.md for the actual report and
its scope note.

CRITICAL: this module must compute everything from fields a *real* Stage 0 message would
contain - `device_ts_ms` per reading, and `arrival_ts_ms`/`sent_ts_ms`/`device_id`/
`firmware_version`/`device_class` on the envelope. It deliberately never reads
`_debug_injected_issues`: that field is generator-only test metadata (see the fixture module's
docstring, "a real device would never send them, and parsers should ignore any _-prefixed
key") - a profiler that secretly relied on it to know which readings were corrupted would be
useless once real telemetry replaces the generator, because a real device never self-reports
"I have a clock bug". tests/unit/test_clock_quality_profiler.py is the one place in this
ticket allowed to read that field, and only as independent ground truth to sanity-check this
module's own (independently computed) detection rates - never as an input to the profiling
logic itself.

What "clock quality" means here, per (device_class, firmware_version):

1. Missing-timestamp rate: fraction of readings with `device_ts_ms is None` - the device sent
   a reading but didn't (or couldn't) stamp it.
2. Epoch-default rate: fraction of readings with `device_ts_ms == 0` - a classic uninitialized-
   RTC symptom (the device hasn't acquired real time yet, e.g. no GPS/NTP fix since boot, and
   reports the epoch instead of refusing to report).
3. Future-timestamp rate: fraction of readings whose `device_ts_ms` is implausibly far *ahead*
   of that message's `arrival_ts_ms`. Threshold: FUTURE_THRESHOLD_MS, documented below.
4. Clock drift: for each device, a per-device systematic skew estimate (median of
   arrival_ts_ms-of-message minus device_ts_ms-of-reading, across that device's non-corrupted
   readings), then the distribution (min/median/p90/max) of those per-device estimates,
   grouped by firmware_version. This is explicitly an *indirect* estimate - see
   `estimate_clock_drift`'s docstring - not a measurement of true clock error.

Threshold choice (future-timestamp): a device's clock can legitimately run a bit fast (NTP
drift, no sync since boot, timezone/DST handling bugs) and still land readings that arrive
*after* the device thinks they happened by a few minutes to a few hours - normal network/queue
jitter plus clock skew. A reading whose device_ts is hours-to-days *ahead of* the message's own
arrival time is a different, categorical problem: the device's clock is simply wrong, not
merely fast. FUTURE_THRESHOLD_MS = 6 hours splits these: comfortably above plausible clock skew
combined with the batch/late-arrival jitter this generator also models (late_delay_max_s
defaults to 4h - see GeneratorConfig), while comfortably below the generator's injected future
offsets (future_offset_min_s defaults to 1h but future_offset_max_s defaults to 14 days, so
most injected future timestamps land far past 6h). Revisit this threshold once real telemetry
is available (see docs/profiling/P0-07-timestamp-clock-quality.md's scope note) - it is a
judgment call tuned against this synthetic generator's parameters, not a measured production
cutoff.
"""
from __future__ import annotations

import argparse
import dataclasses
import statistics
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

MS_PER_S = 1000
MS_PER_HOUR = 3600 * MS_PER_S

# See module docstring's "Threshold choice" section for the reasoning behind 6 hours.
FUTURE_THRESHOLD_MS = 6 * MS_PER_HOUR


@dataclasses.dataclass(frozen=True)
class FirmwareGroupKey:
    """Groups readings by (device_class, firmware_version), matching the generator's own
    grouping (see supercharger.py's `messages_by_group`) and this ticket's "per firmware"
    "done when" requirement."""

    device_class: str
    firmware_version: str


@dataclasses.dataclass
class _GroupAccumulator:
    readings_total: int = 0
    missing_count: int = 0
    epoch_default_count: int = 0
    future_count: int = 0
    # device_id -> list of (arrival_ts_ms - device_ts_ms) for that device's clean readings.
    drift_samples_by_device: dict[str, list[int]] = dataclasses.field(
        default_factory=lambda: defaultdict(list)
    )


@dataclasses.dataclass(frozen=True)
class DriftDistribution:
    """Distribution (across devices) of each device's own drift estimate, in seconds.

    Positive means the device's clock runs *behind* arrival time on average (device_ts_ms is
    smaller than arrival_ts_ms beyond what queueing delay alone would explain); negative means
    it runs ahead. See `estimate_clock_drift` for why this is indirect.
    """

    device_count: int
    min_s: float | None
    median_s: float | None
    p90_s: float | None
    max_s: float | None


@dataclasses.dataclass(frozen=True)
class ClockQualityReport:
    """One group's (device_class, firmware_version) clock-quality profile."""

    key: FirmwareGroupKey
    readings_total: int
    missing_timestamp_rate: float
    epoch_default_rate: float
    future_timestamp_rate: float
    drift: DriftDistribution


def _is_missing(device_ts_ms: Any) -> bool:
    return device_ts_ms is None


def _is_epoch_default(device_ts_ms: Any) -> bool:
    return device_ts_ms == 0


def _is_future(device_ts_ms: Any, arrival_ts_ms: Any) -> bool:
    if device_ts_ms is None or arrival_ts_ms is None:
        return False
    return (device_ts_ms - arrival_ts_ms) > FUTURE_THRESHOLD_MS


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list. pct in [0, 100]."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100 * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def estimate_clock_drift(drift_samples_by_device: dict[str, list[int]]) -> DriftDistribution:
    """Reduce per-device drift *samples* (arrival_ts_ms - device_ts_ms, one per clean reading)
    to a distribution of per-device drift *estimates*.

    This is an indirect estimate of clock skew, not a direct measurement: arrival_ts_ms is
    ingest-side wall-clock receipt time, which includes network transit and any local
    buffering/batching delay on the device (see the fixture generator's `network_jitter_max_s`
    and outage-buffering behavior) on top of whatever the device's clock itself is off by.
    Taking the *median* of many (arrival - device_ts) samples per device is meant to average
    out that per-message queueing noise (assumed roughly symmetric and much smaller than a
    systematic clock offset) and recover the systematic skew, but a device whose queueing delay
    itself has a systematic component (e.g. always batches for exactly N minutes) would bias
    this estimate. Treat these numbers as "clock quality is worth a closer look for this
    firmware", not as an exact clock-offset measurement - see
    docs/profiling/P0-07-timestamp-clock-quality.md's scope note.

    Known limitation, discovered empirically against this repo's fixture generator (see
    docs/profiling/P0-07-timestamp-clock-quality.md): this generator sets a batch's
    `arrival_ts_ms` from the *device's own* last reading's (already clock-skewed) `device_ts_ms`
    plus a few seconds of jitter (see supercharger.py's `_emit_device_messages`, `base_arrival_ms
    = last_ts_ms`), rather than from an independent simulated network-arrival clock. That means
    the generator's injected per-device `clock_drift_max_s` mostly cancels out of
    `arrival_ts_ms - device_ts_ms` (it appears in both terms), so against *this* generator this
    estimate mostly measures a reading's position within its batch, not the injected skew - a
    measured, near-zero correlation, not a guess. This is a property of the generator's arrival
    simulation, not a flaw in the estimation method itself: on real telemetry, arrival_ts_ms is
    an independent ingest-side wall-clock receipt time (see pipeline/stage0_landing/capture.py),
    so it doesn't share the device's clock error the way this generator's does. Fixing the
    generator to model arrival independently is out of scope for this ticket.
    """
    per_device_estimates: list[float] = []
    for samples_ms in drift_samples_by_device.values():
        if not samples_ms:
            continue
        per_device_estimates.append(statistics.median(samples_ms) / MS_PER_S)

    if not per_device_estimates:
        return DriftDistribution(device_count=0, min_s=None, median_s=None, p90_s=None, max_s=None)

    per_device_estimates.sort()
    return DriftDistribution(
        device_count=len(per_device_estimates),
        min_s=per_device_estimates[0],
        median_s=statistics.median(per_device_estimates),
        p90_s=_percentile(per_device_estimates, 90),
        max_s=per_device_estimates[-1],
    )


def profile_messages(messages: Iterable[dict[str, Any]]) -> dict[FirmwareGroupKey, ClockQualityReport]:
    """Compute per-(device_class, firmware_version) timestamp/clock-quality rates.

    Only reads envelope fields a real Stage 0 message would carry: `device_class`,
    `firmware_version`, `device_id`, `arrival_ts_ms`, and each reading's `device_ts_ms` - see
    the module docstring's CRITICAL note on why `_debug_injected_issues` is never read here.
    """
    accumulators: dict[FirmwareGroupKey, _GroupAccumulator] = defaultdict(_GroupAccumulator)

    for message in messages:
        key = FirmwareGroupKey(
            device_class=message["device_class"], firmware_version=message["firmware_version"]
        )
        acc = accumulators[key]
        arrival_ts_ms = message.get("arrival_ts_ms")
        device_id = message.get("device_id")

        for reading in message.get("readings", []):
            device_ts_ms = reading.get("device_ts_ms")
            acc.readings_total += 1

            if _is_missing(device_ts_ms):
                acc.missing_count += 1
                continue
            if _is_epoch_default(device_ts_ms):
                acc.epoch_default_count += 1
                continue
            if _is_future(device_ts_ms, arrival_ts_ms):
                acc.future_count += 1
                continue

            # A "clean" reading (not missing/epoch/future) contributes one drift sample for
            # its device, per the ticket's "excluding readings already flagged" instruction.
            if arrival_ts_ms is not None and device_id is not None:
                acc.drift_samples_by_device[device_id].append(arrival_ts_ms - device_ts_ms)

    reports: dict[FirmwareGroupKey, ClockQualityReport] = {}
    for key, acc in accumulators.items():
        total = acc.readings_total
        reports[key] = ClockQualityReport(
            key=key,
            readings_total=total,
            missing_timestamp_rate=acc.missing_count / total if total else 0.0,
            epoch_default_rate=acc.epoch_default_count / total if total else 0.0,
            future_timestamp_rate=acc.future_count / total if total else 0.0,
            drift=estimate_clock_drift(acc.drift_samples_by_device),
        )
    return reports


def _format_report(reports: dict[FirmwareGroupKey, ClockQualityReport]) -> str:
    lines = []
    for key in sorted(reports, key=lambda k: (k.device_class, k.firmware_version)):
        r = reports[key]
        d = r.drift
        drift_str = (
            f"min={d.min_s:.1f}s median={d.median_s:.1f}s p90={d.p90_s:.1f}s max={d.max_s:.1f}s "
            f"(n_devices={d.device_count})"
            if d.device_count
            else "n/a (no clean readings)"
        )
        lines.append(
            f"{key.device_class}/{key.firmware_version}: "
            f"readings={r.readings_total} "
            f"missing={r.missing_timestamp_rate:.4f} "
            f"epoch_default={r.epoch_default_rate:.4f} "
            f"future={r.future_timestamp_rate:.4f} "
            f"drift[{drift_str}]"
        )
    return "\n".join(lines)


def _cli() -> None:
    """Run the profiler against the synthetic Supercharger fixture generator.

    Mirrors pipeline/stage0_landing/capture.py's `_cli`: until real Supercharger telemetry
    access exists (see that module's docstring), this is what "profile Stage 0 clock quality"
    means in dev - see this module's docstring and docs/profiling/P0-07-timestamp-clock-quality.md.
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
    reports = profile_messages(fixtures.all_messages())
    print(_format_report(reports))


if __name__ == "__main__":
    _cli()
