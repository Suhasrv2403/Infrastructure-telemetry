"""Synthetic fixture generator for Supercharger stall and cabinet telemetry.

Why this exists
----------------
Real Supercharger telemetry doesn't land until P0-05 (Stage 0 capture for one region), but
parser development (P1-03/P1-04) and pipeline development benefit from having *something*
shaped like real payloads to build against well before that. This generator produces
synthetic message envelopes for both device classes, deliberately injecting the same kinds of
messiness the Phase 0 profilers exist to measure on real data:

- arrival shape: variable batch sizes, network jitter, late-arriving messages (P0-06)
- timestamp/clock quality: missing, epoch-default and future device timestamps, plus a
  systematic per-device clock skew (P0-07)
- lateness and duplicates: a fraction of messages arrive very late; a fraction are resent
  verbatim (P0-08)
- retry/buffer/drop behavior: each device can hit a simulated connectivity outage, during
  which it buffers readings locally and bursts them on reconnect, dropping some fraction
  entirely (P0-09)

These numbers are illustrative defaults, not measurements. Once P0-06..P0-09 produce real
profiling reports, prefer real (or real-shaped) fixtures over this generator's output for
anything that needs to match production reality; keep using this generator for cases that
specifically need controlled, reproducible messiness (e.g. testing that a dedup or
quarantine rule behaves correctly).

Output shape
------------
Each generated *message* is one line of JSONL, matching Stage 0's grain ("one row per message
as received"): an envelope with device/firmware/protocol metadata plus a batch of one or more
raw readings. Keys prefixed with ``_`` (e.g. ``_debug_injected_issues``) are generator
provenance for tests and profiling introspection only - a real device would never send them,
and parsers should ignore any ``_``-prefixed key.

Every reading carries a ``payload_hash`` - a hash of its own field values - because Stage 1's
merge key is ``(device_id, device_ts, payload_hash)`` (see CLAUDE.md invariant 2), and a
duplicate injected here reuses the *same* device_ts/payload_hash on purpose, exactly like a
real device retransmitting because it never saw an ack.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

DeviceClass = Literal["supercharger_stall", "supercharger_cabinet"]

DEFAULT_FIRMWARE: dict[DeviceClass, list[str]] = {
    "supercharger_stall": ["2.1.4", "2.3.0", "3.0.1"],
    "supercharger_cabinet": ["1.8.2", "1.9.0"],
}

# Older firmware is modeled as buggier: more clock issues, smaller local buffers (so outages
# drop more), chunkier retries. 1.0 is baseline; higher = worse.
FIRMWARE_QUIRK_MULTIPLIER: dict[str, float] = {
    "2.1.4": 2.5,
    "1.8.2": 2.5,
    "2.3.0": 1.3,
    "1.9.0": 1.3,
    "3.0.1": 0.5,
}

MS_PER_S = 1000
S_PER_DAY = 86_400
# Fixed synthetic "now" so output is fully deterministic given a seed, independent of wall
# clock. 2026-06-01T00:00:00Z, arbitrary.
SYNTHETIC_NOW_MS = 1_780_358_400_000


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    devices_per_firmware: int = 4
    sessions_per_stall: int = 3
    cabinet_window_hours: int = 24
    reading_interval_s: int = 15
    seed: int = 1337

    late_message_rate: float = 0.06
    late_delay_min_s: int = 300
    late_delay_max_s: int = 4 * 3600

    duplicate_message_rate: float = 0.03

    missing_timestamp_rate: float = 0.01
    epoch_default_rate: float = 0.015
    future_timestamp_rate: float = 0.01
    future_offset_min_s: int = 3600
    future_offset_max_s: int = 14 * S_PER_DAY

    clock_drift_max_s: int = 600  # systematic per-device skew, +/-

    outage_probability_per_device: float = 0.25
    outage_drop_rate: float = 0.1
    outage_min_s: int = 900
    outage_max_s: int = 3 * 3600

    max_batch_size: int = 12
    network_jitter_max_s: int = 5


@dataclasses.dataclass
class GeneratedFixtures:
    config: GeneratorConfig
    messages_by_group: dict[tuple[DeviceClass, str], list[dict[str, Any]]]
    stats_by_group: dict[tuple[DeviceClass, str], Counter]

    def all_messages(self) -> Iterable[dict[str, Any]]:
        for msgs in self.messages_by_group.values():
            yield from msgs


def _payload_hash(reading_fields: dict[str, Any]) -> str:
    canonical = json.dumps(reading_fields, sort_keys=True, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _device_id(device_class: DeviceClass, firmware: str, index: int) -> str:
    prefix = "stall" if device_class == "supercharger_stall" else "cab"
    fw_tag = firmware.replace(".", "")
    return f"{prefix}-{fw_tag}-{index:04d}"


def _apply_timestamp_corruption(
    rng: random.Random, true_ts_ms: int, config: GeneratorConfig
) -> tuple[int | None, str | None]:
    """Return (device_ts_ms, issue_label) for one reading's true timestamp."""
    roll = rng.random()
    if roll < config.missing_timestamp_rate:
        return None, "missing_timestamp"
    roll -= config.missing_timestamp_rate
    if roll < config.epoch_default_rate:
        return 0, "epoch_default_timestamp"
    roll -= config.epoch_default_rate
    if roll < config.future_timestamp_rate:
        offset_s = rng.randint(config.future_offset_min_s, config.future_offset_max_s)
        return true_ts_ms + offset_s * MS_PER_S, "future_timestamp"
    return true_ts_ms, None


def _batch(
    rng: random.Random, readings: list[dict[str, Any]], max_batch_size: int
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(readings):
        size = rng.randint(1, max_batch_size)
        batches.append(readings[i : i + size])
        i += size
    return batches


def _make_envelope(
    rng: random.Random,
    device_class: DeviceClass,
    firmware: str,
    device_id: str,
    site_id: str,
    reading_batch: list[dict[str, Any]],
    true_arrival_base_ms: int,
    config: GeneratorConfig,
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    jitter_s = rng.randint(0, config.network_jitter_max_s)
    arrival_ts_ms = true_arrival_base_ms + jitter_s * MS_PER_S

    if rng.random() < config.late_message_rate:
        delay_s = rng.randint(config.late_delay_min_s, config.late_delay_max_s)
        arrival_ts_ms += delay_s * MS_PER_S
        issues.append("late_arrival")

    protocol = "mqtt_batch" if device_class == "supercharger_stall" else "https_poll"
    envelope = {
        "message_id": f"msg-{device_id}-{true_arrival_base_ms}-{rng.randint(0, 999_999)}",
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": firmware,
        "site_id": site_id,
        "protocol": protocol,
        "sent_ts_ms": true_arrival_base_ms,
        "arrival_ts_ms": arrival_ts_ms,
        "readings": reading_batch,
        "_debug_injected_issues": issues,
    }
    return envelope, issues


def _maybe_duplicate(
    rng: random.Random, envelope: dict[str, Any], config: GeneratorConfig
) -> dict[str, Any] | None:
    if rng.random() >= config.duplicate_message_rate:
        return None
    dup = json.loads(json.dumps(envelope))  # deep copy
    dup["message_id"] = envelope["message_id"] + "-retry"
    dup["arrival_ts_ms"] = envelope["arrival_ts_ms"] + rng.randint(1, 30) * MS_PER_S
    dup["_debug_injected_issues"] = list(envelope["_debug_injected_issues"]) + [
        f"duplicate_of:{envelope['message_id']}"
    ]
    return dup


def _simulate_stall_session(
    rng: random.Random,
    device_id: str,
    firmware: str,
    session_index: int,
    session_start_ms: int,
    config: GeneratorConfig,
) -> list[dict[str, Any]]:
    duration_s = rng.randint(600, 3600)
    interval_s = config.reading_interval_s
    num_readings = max(1, duration_s // interval_s)
    peak_current_a = rng.uniform(120, 250)
    voltage_v = rng.uniform(390, 410)
    has_fault = rng.random() < 0.04
    fault_at = rng.randint(num_readings // 3, num_readings - 1) if has_fault else None

    readings = []
    energy_kwh = 0.0
    for i in range(num_readings):
        frac = i / max(1, num_readings - 1)
        # Rough CC/CV-shaped ramp: ramp up over first 10%, hold, taper over last 25%.
        if frac < 0.1:
            ramp = frac / 0.1
        elif frac < 0.75:
            ramp = 1.0
        else:
            ramp = max(0.05, 1.0 - (frac - 0.75) / 0.25)
        current_a = peak_current_a * ramp * rng.uniform(0.97, 1.03)
        power_kw = voltage_v * current_a / 1000
        energy_kwh += power_kw * interval_s / 3600
        state = "charging"
        fault_code = 0
        if fault_at is not None and i >= fault_at:
            state = "fault"
            fault_code = rng.choice([1001, 1042, 2010])  # module fault / thermal / comms
            current_a = 0.0
            power_kw = 0.0
        elif i == 0:
            state = "plugged_in"
        elif i == num_readings - 1 and fault_at is None:
            state = "session_complete"

        true_ts_ms = session_start_ms + i * interval_s * MS_PER_S
        fields = {
            "session_id": f"sess-{device_id}-{session_index}",
            "state": state,
            "output_voltage_v": round(voltage_v, 1),
            "output_current_a": round(current_a, 2),
            "output_power_kw": round(power_kw, 2),
            "connector_temp_c": round(30 + power_kw * 0.12 + rng.uniform(-1, 1), 1),
            "energy_delivered_kwh": round(energy_kwh, 3),
            "fault_code": fault_code,
        }
        readings.append({"__true_ts_ms": true_ts_ms, "fields": fields})
        if fault_code:
            break
    return readings


def _simulate_cabinet_stream(
    rng: random.Random,
    device_id: str,
    firmware: str,
    window_start_ms: int,
    config: GeneratorConfig,
) -> list[dict[str, Any]]:
    interval_s = config.reading_interval_s * 4  # cabinets report less often than stalls
    num_readings = max(1, (config.cabinet_window_hours * 3600) // interval_s)
    readings = []
    for i in range(num_readings):
        hour_of_day = ((window_start_ms // MS_PER_S) // 3600 + i * interval_s // 3600) % 24
        # Rough daily demand curve: low overnight, peak mid-afternoon/evening.
        demand_factor = 0.3 + 0.7 * max(0.0, 1 - abs(hour_of_day - 17) / 10)
        active_stalls = max(0, round(rng.uniform(0, 8) * demand_factor))
        aggregate_power_kw = round(active_stalls * rng.uniform(40, 60), 1)
        fault_code = 0 if rng.random() > 0.01 else rng.choice([3001, 3002])
        true_ts_ms = window_start_ms + i * interval_s * MS_PER_S
        fields = {
            "grid_voltage_v": round(rng.uniform(475, 485), 1),
            "grid_frequency_hz": round(rng.uniform(59.9, 60.1), 3),
            "transformer_temp_c": round(35 + aggregate_power_kw * 0.05 + rng.uniform(-1, 1), 1),
            "contactor_closed": fault_code == 0,
            "active_stall_count": active_stalls,
            "aggregate_power_kw": aggregate_power_kw,
            "fault_code": fault_code,
        }
        readings.append({"__true_ts_ms": true_ts_ms, "fields": fields})
    return readings


def _finalize_readings(
    rng: random.Random,
    raw_readings: list[dict[str, Any]],
    clock_drift_ms: int,
    config: GeneratorConfig,
    stats: Counter,
) -> list[dict[str, Any]]:
    finalized = []
    for raw in raw_readings:
        true_ts_ms = raw["__true_ts_ms"] + clock_drift_ms
        device_ts_ms, issue = _apply_timestamp_corruption(rng, true_ts_ms, config)
        fields = dict(raw["fields"])
        fields["device_ts_ms"] = device_ts_ms
        fields["payload_hash"] = _payload_hash(fields)
        # Ground-truth event time (no drift, no corruption) - simulation-internal bookkeeping
        # only, stripped in _emit_device_messages before the envelope is built. It exists so
        # arrival can be anchored to when the reading really happened, independent of whatever
        # is wrong with the device's own reported clock (see _emit_device_messages).
        fields["__true_ts_ms"] = raw["__true_ts_ms"]
        finalized.append(fields)
        stats["readings_total"] += 1
        if issue:
            stats[issue] += 1
    return finalized


def _apply_outage(
    rng: random.Random,
    readings: list[dict[str, Any]],
    firmware: str,
    config: GeneratorConfig,
    stats: Counter,
) -> tuple[list[dict[str, Any]], bool]:
    """With some probability, mark a contiguous span as buffered-then-dropped-partial.

    Returns (surviving_readings, outage_happened). Dropped readings are simply excluded, the
    same as a real device's local buffer overflowing during an outage.
    """
    multiplier = FIRMWARE_QUIRK_MULTIPLIER.get(firmware, 1.0)
    if rng.random() >= min(0.95, config.outage_probability_per_device * multiplier):
        return readings, False
    if len(readings) < 4:
        return readings, False

    start = rng.randint(0, len(readings) - 4)
    span = rng.randint(3, min(len(readings) - start, 20))
    outage_span = readings[start : start + span]

    drop_rate = min(0.9, config.outage_drop_rate * multiplier)
    survivors = [r for r in outage_span if rng.random() >= drop_rate]
    dropped_count = len(outage_span) - len(survivors)
    stats["outage_events"] += 1
    stats["outage_readings_dropped"] += dropped_count
    stats["outage_readings_buffered"] += len(survivors)

    return readings[:start] + survivors + readings[start + span :], True


def _emit_device_messages(
    rng: random.Random,
    device_class: DeviceClass,
    firmware: str,
    device_id: str,
    site_id: str,
    readings: list[dict[str, Any]],
    outage_happened: bool,
    config: GeneratorConfig,
    stats: Counter,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    batches = _batch(rng, readings, config.max_batch_size)
    # If an outage happened, the *last* batch containing the buffered burst arrives all at
    # once, well after its readings' device_ts - modeling "reconnect and flush the buffer".
    outage_burst_index = len(batches) - 1 if outage_happened and batches else None

    for idx, batch in enumerate(batches):
        if not batch:
            continue
        # Arrival is anchored on the last reading's *ground-truth* event time, not on
        # device_ts_ms: a device's self-reported clock can be missing, epoch-default, future-
        # corrupted, or systematically drifted (see _finalize_readings/_apply_timestamp_
        # corruption), none of which changes when the reading actually happened or when the
        # resulting message physically reaches the ingest system. Deriving arrival from
        # device_ts_ms previously did two things wrong: (a) a corrupted-to-0/missing last
        # reading silently snapped arrival to the fixed SYNTHETIC_NOW_MS fallback, making an
        # otherwise-ordinary batch look days late; (b) a future-corrupted last reading pushed
        # arrival inappropriately far forward too. Anchoring on __true_ts_ms fixes both, and
        # also means arrival_ts_ms - device_ts_ms now actually reflects the injected clock
        # drift (previously the drift canceled out of that subtraction, see
        # profiling/clock_quality/profiler.py's estimate_clock_drift docstring).
        base_arrival_ms = batch[-1]["__true_ts_ms"]
        clean_batch = [
            {key: value for key, value in reading.items() if key != "__true_ts_ms"}
            for reading in batch
        ]
        envelope, issues = _make_envelope(
            rng, device_class, firmware, device_id, site_id, clean_batch, base_arrival_ms, config
        )
        if idx == outage_burst_index:
            burst_delay_s = rng.randint(config.outage_min_s, config.outage_max_s)
            envelope["arrival_ts_ms"] += burst_delay_s * MS_PER_S
            envelope["_debug_injected_issues"].append("post_outage_burst")
            stats["post_outage_burst_messages"] += 1

        messages.append(envelope)
        stats["messages_total"] += 1
        stats["batch_size_" + str(min(len(batch), 20))] += 1
        for issue in issues:
            stats[issue] += 1

        dup = _maybe_duplicate(rng, envelope, config)
        if dup is not None:
            messages.append(dup)
            stats["messages_total"] += 1
            stats["duplicate_message"] += 1

    return messages


def _simulate_stall_device(
    rng: random.Random, firmware: str, index: int, config: GeneratorConfig, stats: Counter
) -> list[dict[str, Any]]:
    device_id = _device_id("supercharger_stall", firmware, index)
    site_id = f"site-{index % 6:03d}"
    clock_drift_ms = rng.randint(-config.clock_drift_max_s, config.clock_drift_max_s) * MS_PER_S

    raw_readings: list[dict[str, Any]] = []
    session_start = SYNTHETIC_NOW_MS - 7 * S_PER_DAY * MS_PER_S + index * 3600 * MS_PER_S
    for session_index in range(config.sessions_per_stall):
        session_readings = _simulate_stall_session(
            rng, device_id, firmware, session_index, session_start, config
        )
        raw_readings.extend(session_readings)
        if session_readings:
            last = session_readings[-1]["__true_ts_ms"]
            session_start = last + rng.randint(1800, 3 * 3600) * MS_PER_S

    finalized = _finalize_readings(rng, raw_readings, clock_drift_ms, config, stats)
    finalized, outage_happened = _apply_outage(rng, finalized, firmware, config, stats)
    return _emit_device_messages(
        rng, "supercharger_stall", firmware, device_id, site_id, finalized, outage_happened, config, stats
    )


def _simulate_cabinet_device(
    rng: random.Random, firmware: str, index: int, config: GeneratorConfig, stats: Counter
) -> list[dict[str, Any]]:
    device_id = _device_id("supercharger_cabinet", firmware, index)
    site_id = f"site-{index % 6:03d}"
    clock_drift_ms = rng.randint(-config.clock_drift_max_s, config.clock_drift_max_s) * MS_PER_S

    window_start = SYNTHETIC_NOW_MS - config.cabinet_window_hours * 3600 * MS_PER_S
    raw_readings = _simulate_cabinet_stream(rng, device_id, firmware, window_start, config)

    finalized = _finalize_readings(rng, raw_readings, clock_drift_ms, config, stats)
    finalized, outage_happened = _apply_outage(rng, finalized, firmware, config, stats)
    return _emit_device_messages(
        rng, "supercharger_cabinet", firmware, device_id, site_id, finalized, outage_happened, config, stats
    )


def generate(config: GeneratorConfig | None = None) -> GeneratedFixtures:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)

    messages_by_group: dict[tuple[DeviceClass, str], list[dict[str, Any]]] = defaultdict(list)
    stats_by_group: dict[tuple[DeviceClass, str], Counter] = defaultdict(Counter)

    for device_class, firmwares in DEFAULT_FIRMWARE.items():
        for firmware in firmwares:
            group = (device_class, firmware)
            for index in range(config.devices_per_firmware):
                if device_class == "supercharger_stall":
                    msgs = _simulate_stall_device(rng, firmware, index, config, stats_by_group[group])
                else:
                    msgs = _simulate_cabinet_device(rng, firmware, index, config, stats_by_group[group])
                messages_by_group[group].extend(msgs)
            messages_by_group[group].sort(key=lambda m: m["arrival_ts_ms"])

    return GeneratedFixtures(config, dict(messages_by_group), dict(stats_by_group))


def write(fixtures: GeneratedFixtures, root: Path) -> Path:
    """Write JSONL fixtures + a manifest.json under root/<device_class>/<firmware>/."""
    manifest: dict[str, Any] = {
        "seed": fixtures.config.seed,
        "config": dataclasses.asdict(fixtures.config),
        "groups": {},
    }
    for (device_class, firmware), messages in fixtures.messages_by_group.items():
        out_dir = root / device_class / firmware
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "messages.jsonl"
        with out_path.open("w") as f:
            for msg in messages:
                f.write(json.dumps(msg, sort_keys=True) + "\n")
        manifest["groups"][f"{device_class}/{firmware}"] = dict(
            sorted(fixtures.stats_by_group[(device_class, firmware)].items())
        )
    manifest_path = root / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, devices_per_firmware=args.devices_per_firmware)
    fixtures = generate(config)
    manifest_path = write(fixtures, args.out)
    total_messages = sum(len(v) for v in fixtures.messages_by_group.values())
    print(f"Wrote {total_messages} messages across {len(fixtures.messages_by_group)} groups.")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    _cli()
