"""Synthetic fixture generator for Powerwall (residential home battery) telemetry.

Why this exists
----------------
This is PREP work unblocking P3-03 ("Powerwall parsers and signal catalog"), not P3-03 itself.
No real Powerwall payload capture exists yet (same situation Supercharger was in before P0-05/
P1-04), and P3-03 cannot start writing real per-firmware parsers or getting its catalog
reviewed without *something* shaped like real payloads to build against. This generator plays
the same role tests/fixtures/generators/supercharger.py played for Supercharger: it produces
synthetic Stage 0 message envelopes for the `powerwall` device class, deliberately injecting
the same categories of messiness the Phase 0 profilers measure on real data (arrival shape,
timestamp/clock quality, lateness/duplicates, retry/buffer/drop behavior) - see that module's
own docstring for the full rationale, which applies unchanged here. All rates below are
illustrative defaults, not measurements, exactly like Supercharger's.

Scope, deliberately narrow (see this ticket's own scope note)
---------------------------------------------------------------
- Fixtures + catalog only. No parsers, no Stage 1/2 wiring - that is P3-03's job, on top of
  this. `parsers/powerwall/README.md` stays a placeholder; nothing here registers a parser.
- Pack/system-level telemetry only. Unlike Megapack/Powerpack (a sibling prep effort, see
  `.worktrees/prep-megapack-powerpack-fixtures`), P3-03's own "done when" text does not mention
  a cell-level child table for Powerwall, so this generator does not invent one - one reading
  per device per interval, no cell/module fan-out.

Domain judgment calls (own reasoning, not a Tesla spec sheet - flagged honestly, matching
catalog/signals.yaml's existing `v0_draft_unreviewed` caveat style)
---------------------------------------------------------------------
A Powerwall is a single-home battery (much smaller scale per-unit than Megapack/Powerpack),
normally paired with solar, used for backup power and self-consumption / time-of-use
optimization. The signals modeled below are my own best-effort guess at a plausible residential
battery telemetry schema, not sourced from a real spec sheet or API:

- `state_of_charge_pct`: usable state of charge, 0-100.
- `battery_power_kw`: signed instantaneous battery power. Sign convention chosen here:
  POSITIVE = discharging (battery supplying the home), NEGATIVE = charging (battery drawing
  power) - i.e. the same sign the battery's contribution takes in a site energy-balance
  equation (see `grid_power_kw` below). Capped at `+/-battery_power_max_kw` (default 5.0 kW),
  a rough stand-in for a continuous power rating; real hardware peak/continuous ratings differ
  and are not modeled.
- `solar_power_kw`: paired solar array's instantaneous production, >= 0. Per-device array size
  is drawn once from `[solar_array_min_kw, solar_array_max_kw]` (default 3-10 kW) to give a
  varied residential population instead of one fixed array size.
- `site_load_kw`: modeled home consumption, a smooth base-plus-morning-plus-evening-peak curve
  by hour-of-day, not a real appliance-level load model.
- `grid_power_kw`: derived, not independently modeled, from the site energy-balance identity
  `site_load_kw = solar_power_kw + battery_power_kw + grid_power_kw` (grid POSITIVE = import
  from the grid, NEGATIVE = export to it) so the four power signals are internally consistent
  by construction - see `test_powerwall_fixture_generator.py`.
- `backup_reserve_pct`: the user-configured minimum charge to preserve for backup, 5-100.
  Modeled as fixed for a device's whole synthetic stream (a real setting changes rarely), same
  pattern as Supercharger's per-session `output_voltage_v`.
- `grid_status`: `grid_connected` (normal), `islanded` (grid down, home running off
  battery/solar only), `transitioning` (the one reading immediately before/after an island
  event) - loosely inspired by real Powerwall app terminology, not a documented state machine.
- `operating_mode`: `self_powered` (use solar/battery to minimize grid draw), `backup_only`
  (stay near full charge, mostly idle otherwise), `time_based_control` (charge off-peak,
  discharge on-peak on a fixed daily schedule) - a plausible simplification of real Powerwall
  operating modes, not verified against the real product.
- `battery_temp_c` / `inverter_temp_c`: modeled off a per-device "climate zone" baseline (own
  invented set of 4 zones spanning roughly 4-27 C, meant to give the 1M-device fleet climate
  diversity - see below) plus diurnal variation, load-coupled heating and noise. Not a thermal
  simulation.
- `fault_code`: 0 = no fault; nonzero codes 4001-4004 are this generator's own invented
  categories (loosely: inverter fault / comms fault / over-temp shutdown / grid-disconnect
  fault), same "commentary, not a real registry" caveat Supercharger's fault codes carry.

Population generation at Powerwall's actual scale
----------------------------------------------------
This repo's Phase 3 is named "Powerwall at scale" for a reason: fleet size here is orders of
magnitude past Supercharger's regional pilot (a handful of stalls/cabinets). A few structural
choices below exist specifically because of that, extending (not replacing) Supercharger's
"loop over devices_per_firmware" pattern:

1. Each device's synthetic RNG is seeded independently and deterministically from
   `(config.seed, firmware, absolute_device_index)` (`_device_seed`), rather than one shared
   `random.Random` advanced sequentially across every device the way supercharger.py's
   `generate()` does. One device's output no longer depends on the generation order or on any
   other device's draws, which means a real 1M-device run can be sharded arbitrarily (any
   worker generating any contiguous or non-contiguous slice of device indices, in any order, in
   parallel) and still reproduce byte-identical output to a single-process run - useful once a
   1M-device fixture actually needs to be generated for real, not just a few dozen for tests.
   `GeneratorConfig.device_index_start` exists so a shard's absolute device indices don't
   collide with another shard's.
2. `iter_device_messages()` yields one device's messages at a time and holds nothing else in
   memory. `generate()` (used by tests and by default) consumes it eagerly into the same
   in-memory `GeneratedFixtures` shape supercharger.py's `generate()` returns, which is fine at
   test/dev scale (`devices_per_firmware` in the single/double digits) but would not be at 1M
   devices. `write_streaming()` consumes the same iterator directly and writes each device's
   messages to its firmware's JSONL file as it goes, never holding more than one device's
   messages in memory - the path intended for an eventual large-scale run. Its one deliberate
   trade-off versus `write()`: it does not do `write()`'s whole-group arrival-time sort (that
   would require buffering an entire group in memory again), so its JSONL is in per-device
   emission order, not global arrival order - arguably more realistic, since a real landing
   bucket isn't globally sorted either.
3. `site_id` is assigned modulo 5,000 (vs. Supercharger's modulo 6) and each device's climate
   zone is assigned deterministically (`absolute_index % len(CLIMATE_ZONES)`, not randomly) so
   climate assignment is reproducible and easy to reason about even at large device counts,
   giving the population some geographic/climate diversity without inventing new wire fields
   for it (climate zone itself is a generator-internal parameter, not something a real
   Powerwall would report, so it is not written into any reading and is not catalogued).

Output shape
------------
Identical envelope/reading shape to supercharger.py's JSONL output (see that module's
docstring) - one JSONL line per message: an envelope with device/firmware/site metadata plus a
batch of readings, each reading carrying `device_ts_ms` and `payload_hash` for Stage 1's merge
key (device_id, device_ts_ms, payload_hash) (CLAUDE.md invariant 2). `_`-prefixed keys are
generator-only provenance, never real device output.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

DEVICE_CLASS = "powerwall"

DEFAULT_FIRMWARE: list[str] = ["23.44.10", "24.4.2", "25.12.1"]

# Older firmware modeled as buggier, same convention as supercharger.py's
# FIRMWARE_QUIRK_MULTIPLIER: 1.0 is baseline, higher = worse (more clock issues, more outage
# drop). Own judgment about which firmware string is "oldest" (lexically/chronologically first
# in DEFAULT_FIRMWARE), not derived from anything real.
FIRMWARE_QUIRK_MULTIPLIER: dict[str, float] = {
    "23.44.10": 2.0,
    "24.4.2": 1.2,
    "25.12.1": 0.6,
}

OPERATING_MODES: tuple[str, ...] = ("self_powered", "backup_only", "time_based_control")
GRID_STATUSES: tuple[str, ...] = ("grid_connected", "transitioning", "islanded")
FAULT_CODES: tuple[int, ...] = (4001, 4002, 4003, 4004)

# (zone_name, baseline_ambient_temp_c) - own invented set spanning a plausible residential
# climate range, deterministically assigned per device (see module docstring). Not real
# climatology, just enough spread to make battery/inverter temps device-population-varied.
CLIMATE_ZONES: tuple[tuple[str, float], ...] = (
    ("hot_arid", 27.0),
    ("mild_coastal", 16.0),
    ("cold_continental", 4.0),
    ("humid_subtropical", 21.0),
)

# Fixed nameplate usable capacity assumed for every device, own rough judgment call (real
# Powerwall capacity/degradation is not modeled). Used only to convert battery_power_kw into a
# state_of_charge_pct delta.
NAMEPLATE_CAPACITY_KWH = 13.5

MS_PER_S = 1000
S_PER_DAY = 86_400
# Fixed synthetic "now", same arbitrary anchor date supercharger.py uses, kept identical so
# fixtures from both generators line up on a shared synthetic timeline if ever compared.
SYNTHETIC_NOW_MS = 1_780_358_400_000


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    devices_per_firmware: int = 5
    device_index_start: int = 0
    window_hours: int = 72
    reading_interval_s: int = 300
    seed: int = 2026

    late_message_rate: float = 0.05
    late_delay_min_s: int = 300
    late_delay_max_s: int = 4 * 3600

    duplicate_message_rate: float = 0.02

    missing_timestamp_rate: float = 0.01
    epoch_default_rate: float = 0.01
    future_timestamp_rate: float = 0.008
    future_offset_min_s: int = 3600
    future_offset_max_s: int = 14 * S_PER_DAY

    clock_drift_max_s: int = 600  # systematic per-device skew, +/-

    outage_probability_per_device: float = 0.2
    outage_drop_rate: float = 0.08
    outage_min_s: int = 900
    outage_max_s: int = 3 * 3600

    max_batch_size: int = 24
    network_jitter_max_s: int = 5

    # Powerwall-specific tunables (see module docstring's "domain judgment calls").
    solar_array_min_kw: float = 3.0
    solar_array_max_kw: float = 10.0
    battery_power_max_kw: float = 5.0
    backup_reserve_min_pct: float = 5.0
    backup_reserve_max_pct: float = 100.0
    island_event_probability_per_device: float = 0.15
    island_min_readings: int = 2
    island_max_readings: int = 12
    fault_probability_per_device: float = 0.03


@dataclasses.dataclass
class GeneratedFixtures:
    config: GeneratorConfig
    messages_by_group: dict[tuple[str, str], list[dict[str, Any]]]
    stats_by_group: dict[tuple[str, str], Counter]

    def all_messages(self) -> Iterable[dict[str, Any]]:
        for msgs in self.messages_by_group.values():
            yield from msgs


def _payload_hash(reading_fields: dict[str, Any]) -> str:
    canonical = json.dumps(reading_fields, sort_keys=True, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _device_id(firmware: str, index: int) -> str:
    fw_tag = firmware.replace(".", "")
    return f"pw-{fw_tag}-{index:06d}"


def _device_seed(seed: int, firmware: str, index: int) -> int:
    """Deterministic per-device seed, independent of every other device's draws or of
    generation order - see module docstring's "Population generation at Powerwall's actual
    scale" section for why this matters."""
    key = f"{seed}:{DEVICE_CLASS}:{firmware}:{index}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def _climate_zone_for_index(index: int) -> tuple[str, float]:
    return CLIMATE_ZONES[index % len(CLIMATE_ZONES)]


def _apply_timestamp_corruption(
    rng: random.Random, true_ts_ms: int, config: GeneratorConfig
) -> tuple[int | None, str | None]:
    """Return (device_ts_ms, issue_label) for one reading's true timestamp. Identical logic to
    supercharger.py's function of the same name (not imported from it, to keep this generator
    self-contained - see module docstring)."""
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

    # Home gateway reporting over the internet, batch-uploaded via HTTPS - same protocol
    # convention supercharger_cabinet uses for its own continuous (non-session) stream, not a
    # per-charge-session protocol like supercharger_stall's mqtt_batch (see
    # docs/reports/P0-06-arrival-shape.md).
    protocol = "https_poll"
    envelope = {
        "message_id": f"msg-{device_id}-{true_arrival_base_ms}-{rng.randint(0, 999_999)}",
        "device_id": device_id,
        "device_class": DEVICE_CLASS,
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


def _simulate_powerwall_stream(
    rng: random.Random,
    device_id: str,
    firmware: str,
    window_start_ms: int,
    climate_baseline_c: float,
    solar_capacity_kw: float,
    operating_mode: str,
    backup_reserve_pct: float,
    config: GeneratorConfig,
) -> list[dict[str, Any]]:
    """Simulate one device's raw (pre-corruption) reading stream over `config.window_hours`.

    See module docstring's "domain judgment calls" section for the reasoning behind every
    formula below; nothing here is derived from a real spec.
    """
    interval_s = config.reading_interval_s
    num_readings = max(1, (config.window_hours * 3600) // interval_s)

    soc = rng.uniform(30.0, 90.0)  # unrounded internal state, rounded only at output

    has_fault = rng.random() < config.fault_probability_per_device
    fault_at = rng.randint(0, num_readings - 1) if has_fault else None
    fault_span = rng.randint(1, 6) if has_fault else 0
    fault_code_value = rng.choice(FAULT_CODES) if has_fault else 0

    island_event: tuple[int, int] | None = None
    if rng.random() < config.island_event_probability_per_device and num_readings > 4:
        island_len = min(
            num_readings - 1, rng.randint(config.island_min_readings, config.island_max_readings)
        )
        island_start = rng.randint(0, max(0, num_readings - island_len - 1))
        island_event = (island_start, island_start + island_len)

    transition_indices: set[int] = set()
    if island_event is not None:
        if island_event[0] > 0:
            transition_indices.add(island_event[0] - 1)
        if island_event[1] < num_readings:
            transition_indices.add(island_event[1])

    readings: list[dict[str, Any]] = []
    for i in range(num_readings):
        true_ts_ms = window_start_ms + i * interval_s * MS_PER_S
        hour_of_day = ((true_ts_ms // MS_PER_S) // 3600) % 24

        # Solar: 0 outside roughly 6am-6pm, a smooth midday-peaked arc scaled by this device's
        # array size, plus weather-style noise (only ever attenuates, never boosts above the
        # array's capacity).
        if 6 <= hour_of_day <= 18:
            solar_factor = max(0.0, math.sin(math.pi * (hour_of_day - 6) / 12))
        else:
            solar_factor = 0.0
        solar_power_kw = (
            round(solar_capacity_kw * solar_factor * rng.uniform(0.6, 1.0), 2)
            if solar_factor > 0
            else 0.0
        )

        # Site load: a flat base plus a morning and a (larger) evening bump, each a Gaussian
        # in hour-of-day, times noise.
        morning_peak = 1.0 * math.exp(-((hour_of_day - 7) ** 2) / 4)
        evening_peak = 1.8 * math.exp(-((hour_of_day - 19) ** 2) / 6)
        site_load_kw = round(max(0.1, (0.5 + morning_peak + evening_peak) * rng.uniform(0.85, 1.15)), 2)

        if island_event is not None and island_event[0] <= i < island_event[1]:
            grid_status = "islanded"
        elif i in transition_indices:
            grid_status = "transitioning"
        else:
            grid_status = "grid_connected"

        if grid_status == "islanded":
            # Real islanding behavior always prioritizes keeping the home powered, regardless
            # of the configured operating_mode - own judgment call.
            deficit = site_load_kw - solar_power_kw
            if deficit > 0:
                battery_power_kw = min(config.battery_power_max_kw, deficit) if soc > 2.0 else 0.0
            else:
                battery_power_kw = -min(config.battery_power_max_kw, -deficit) if soc < 100.0 else 0.0
        elif grid_status == "transitioning":
            battery_power_kw = 0.0
        elif operating_mode == "backup_only":
            # Stay near full charge; only a small trickle once below ~99.5%, otherwise idle.
            battery_power_kw = -min(config.battery_power_max_kw, 1.0) if soc < 99.5 else 0.0
        elif operating_mode == "time_based_control":
            if hour_of_day < 6:
                battery_power_kw = -min(config.battery_power_max_kw, 3.0) if soc < 95.0 else 0.0
            elif 16 <= hour_of_day < 21:
                battery_power_kw = (
                    min(config.battery_power_max_kw, 3.0) if soc > backup_reserve_pct else 0.0
                )
            else:
                battery_power_kw = 0.0
        else:  # self_powered
            surplus = solar_power_kw - site_load_kw
            if surplus > 0 and soc < 100.0:
                battery_power_kw = -min(config.battery_power_max_kw, surplus)
            elif surplus < 0 and soc > backup_reserve_pct:
                battery_power_kw = min(config.battery_power_max_kw, -surplus)
            else:
                battery_power_kw = 0.0

        in_fault = has_fault and fault_at is not None and fault_at <= i < fault_at + fault_span
        if in_fault:
            battery_power_kw = 0.0
            fault_code = fault_code_value
        else:
            fault_code = 0

        if grid_status in ("islanded", "transitioning"):
            grid_power_kw = 0.0
        else:
            grid_power_kw = round(site_load_kw - solar_power_kw - battery_power_kw, 2)

        # positive battery_power_kw (discharge) reduces stored energy; negative (charge)
        # increases it.
        energy_delta_kwh = -battery_power_kw * interval_s / 3600
        soc = min(100.0, max(0.0, soc + energy_delta_kwh / NAMEPLATE_CAPACITY_KWH * 100))

        battery_temp_c = round(
            climate_baseline_c
            + 3 * math.sin(math.pi * (hour_of_day - 15) / 12)
            + abs(battery_power_kw) * 0.5
            + rng.uniform(-1, 1),
            1,
        )
        inverter_temp_c = round(
            battery_temp_c + 5 + abs(battery_power_kw) * 1.2 + rng.uniform(-1, 1), 1
        )

        fields = {
            "state_of_charge_pct": round(soc, 2),
            "battery_power_kw": round(battery_power_kw, 2),
            "solar_power_kw": solar_power_kw,
            "site_load_kw": site_load_kw,
            "grid_power_kw": grid_power_kw,
            "backup_reserve_pct": round(backup_reserve_pct, 1),
            "grid_status": grid_status,
            "operating_mode": operating_mode,
            "battery_temp_c": battery_temp_c,
            "inverter_temp_c": inverter_temp_c,
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
    """Connectivity outage (device buffers locally, then bursts/partially-drops on reconnect) -
    same modeling as supercharger.py's function of the same name. This is about the home
    gateway's internet connectivity, unrelated to `grid_status`/electrical islanding modeled in
    `_simulate_powerwall_stream`."""
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
    outage_burst_index = len(batches) - 1 if outage_happened and batches else None

    for idx, batch in enumerate(batches):
        if not batch:
            continue
        last_ts_ms = batch[-1].get("device_ts_ms")
        base_arrival_ms = last_ts_ms if isinstance(last_ts_ms, int) else SYNTHETIC_NOW_MS
        envelope, issues = _make_envelope(
            rng, firmware, device_id, site_id, batch, base_arrival_ms, config
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


def _simulate_device(
    rng: random.Random, firmware: str, absolute_index: int, config: GeneratorConfig
) -> tuple[list[dict[str, Any]], Counter]:
    stats: Counter = Counter()
    device_id = _device_id(firmware, absolute_index)
    site_id = f"site-{absolute_index % 5000:05d}"
    zone_name, climate_baseline_c = _climate_zone_for_index(absolute_index)
    stats["climate_zone_" + zone_name] += 1

    solar_capacity_kw = rng.uniform(config.solar_array_min_kw, config.solar_array_max_kw)
    operating_mode = rng.choice(OPERATING_MODES)
    stats["operating_mode_" + operating_mode] += 1
    backup_reserve_pct = rng.uniform(config.backup_reserve_min_pct, config.backup_reserve_max_pct)
    clock_drift_ms = rng.randint(-config.clock_drift_max_s, config.clock_drift_max_s) * MS_PER_S

    window_start_ms = SYNTHETIC_NOW_MS - config.window_hours * 3600 * MS_PER_S
    raw_readings = _simulate_powerwall_stream(
        rng,
        device_id,
        firmware,
        window_start_ms,
        climate_baseline_c,
        solar_capacity_kw,
        operating_mode,
        backup_reserve_pct,
        config,
    )

    finalized = _finalize_readings(rng, raw_readings, clock_drift_ms, config, stats)
    finalized, outage_happened = _apply_outage(rng, finalized, firmware, config, stats)
    messages = _emit_device_messages(
        rng, firmware, device_id, site_id, finalized, outage_happened, config, stats
    )
    return messages, stats


def iter_device_messages(
    config: GeneratorConfig,
) -> Iterator[tuple[str, str, list[dict[str, Any]], Counter]]:
    """Lazily yield (firmware_version, device_id, messages, stats) one device at a time.

    Holds nothing from other devices in memory - see module docstring's "Population generation
    at Powerwall's actual scale" section. `device_class` is always `DEVICE_CLASS` ("powerwall")
    and is not part of the yielded tuple.
    """
    for firmware in DEFAULT_FIRMWARE:
        for i in range(config.devices_per_firmware):
            absolute_index = config.device_index_start + i
            rng = random.Random(_device_seed(config.seed, firmware, absolute_index))
            messages, stats = _simulate_device(rng, firmware, absolute_index, config)
            device_id = _device_id(firmware, absolute_index)
            yield firmware, device_id, messages, stats


def generate(config: GeneratorConfig | None = None) -> GeneratedFixtures:
    config = config or GeneratorConfig()

    messages_by_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    stats_by_group: dict[tuple[str, str], Counter] = defaultdict(Counter)

    for firmware, _device_id_value, messages, stats in iter_device_messages(config):
        group = (DEVICE_CLASS, firmware)
        messages_by_group[group].extend(messages)
        stats_by_group[group].update(stats)

    for group, messages in messages_by_group.items():
        messages.sort(key=lambda m: m["arrival_ts_ms"])

    return GeneratedFixtures(config, dict(messages_by_group), dict(stats_by_group))


def write(fixtures: GeneratedFixtures, root: Path) -> Path:
    """Write JSONL fixtures + a manifest.json under root/powerwall/<firmware>/. Mirrors
    supercharger.py's write() exactly (same manifest shape, same "one line per message"
    convention)."""
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


def write_streaming(config: GeneratorConfig, root: Path) -> Path:
    """Write fixtures device-by-device, never holding more than one device's messages in
    memory - the path intended for an eventual real large (e.g. ~1M device) run. See module
    docstring's "Population generation at Powerwall's actual scale" point 2 for the one
    deliberate trade-off versus `write()` (no whole-group arrival-time sort)."""
    stats_by_group: dict[tuple[str, str], Counter] = defaultdict(Counter)
    counts_by_group: dict[tuple[str, str], int] = defaultdict(int)
    file_handles: dict[tuple[str, str], Any] = {}

    try:
        for firmware, _device_id_value, messages, stats in iter_device_messages(config):
            group = (DEVICE_CLASS, firmware)
            if group not in file_handles:
                out_dir = root / DEVICE_CLASS / firmware
                out_dir.mkdir(parents=True, exist_ok=True)
                file_handles[group] = (out_dir / "messages.jsonl").open("w")
            fh = file_handles[group]
            for msg in messages:
                fh.write(json.dumps(msg, sort_keys=True) + "\n")
            counts_by_group[group] += len(messages)
            stats_by_group[group].update(stats)
    finally:
        for fh in file_handles.values():
            fh.close()

    manifest: dict[str, Any] = {
        "seed": config.seed,
        "config": dataclasses.asdict(config),
        "streaming": True,
        "groups": {
            f"{dc}/{fw}": dict(sorted(stats_by_group[(dc, fw)].items()))
            for (dc, fw) in counts_by_group
        },
    }
    manifest_path = root / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument(
        "--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware
    )
    parser.add_argument(
        "--device-index-start", type=int, default=GeneratorConfig().device_index_start
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help=(
            "Write per-device without holding the whole run in memory or globally sorting - "
            "use for large (e.g. ~1M device) runs; see module docstring."
        ),
    )
    args = parser.parse_args()

    config = GeneratorConfig(
        seed=args.seed,
        devices_per_firmware=args.devices_per_firmware,
        device_index_start=args.device_index_start,
    )
    if args.streaming:
        manifest_path = write_streaming(config, args.out)
        print("Streamed fixtures (per-device emission order, no global arrival sort).")
        print(f"Manifest: {manifest_path}")
    else:
        fixtures = generate(config)
        manifest_path = write(fixtures, args.out)
        total_messages = sum(len(v) for v in fixtures.messages_by_group.values())
        print(f"Wrote {total_messages} messages across {len(fixtures.messages_by_group)} groups.")
        print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    _cli()
