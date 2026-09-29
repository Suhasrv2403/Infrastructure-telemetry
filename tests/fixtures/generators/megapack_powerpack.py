"""Synthetic fixture generator for Megapack and Powerpack pack-level and cell-level telemetry.

PREP WORK, not a ticket. This module exists to unblock P2-13 ("Megapack and Powerpack parsers,
incl. cell-level child table") and, indirectly, P2-14 (catalog sign-off), the same way
tests/fixtures/generators/supercharger.py unblocked P1-03/P1-04 before real Supercharger
telemetry landed. Nobody with real Megapack/Powerpack firmware or BMS knowledge has reviewed
anything in this file - every field name, unit, value range and cadence below is this generator
author's own reasonable-guess domain modeling (grid-scale lithium battery storage, informed by
publicly known Megapack/Powerpack product facts - large-format arrays, pack-level dispatch,
per-cell BMS monitoring - not a real spec sheet or firmware capture). Treat it exactly the way
catalog/signals.yaml already treats the Supercharger generator: a concrete, internally
consistent starting point for a firmware SME to correct, not ground truth.

Why one file for two products (a judgment call, documented per the prep instructions)
--------------------------------------------------------------------------------------
supercharger.py's one-file-per-firmware-*family* convention actually groups two distinct device
classes (supercharger_stall, supercharger_cabinet) in a single module because they share a
generation pipeline (envelope/batching/corruption helpers) and differ only in their per-reading
simulation function and numeric constants. Megapack and Powerpack are an even closer fit for
that pattern: they are the *same kind* of product (utility-scale grid battery + power
conversion system) at two different scales/generations, sharing every structural element -
pack-level dispatch simulation, a cell-level child stream, the same envelope/corruption/outage
machinery - and differing only in a handful of numeric constants (see PRODUCT_SPECS). Splitting
them into two files would mean copy-pasting the entire pack/cell simulation and shared-helper
logic twice for no benefit; one file parameterized by ProductSpec keeps the two in lockstep and
matches supercharger.py's own "shared machinery, per-class simulation function" shape.

The genuinely new structural element: a cell-level child stream
-----------------------------------------------------------------
Unlike Supercharger, Megapack/Powerpack are physically composed of many battery cells (grouped
into monitoring/reporting units by the BMS - see CELL COUNT MODELING below) that each report
their own voltage/temperature, at a much coarser cadence than pack-level telemetry (real BMS
systems summarize pack state frequently but sample individual cells far less often, to bound
telemetry volume across an array of hundreds-to-thousands of cells). This generator models that
as a genuinely separate message/record stream - not extra columns on the pack row:

  - megapack / powerpack:       one message envelope per (device_id, device_ts_ms) pack-level
                                 reading batch - the "parent" stream.
  - megapack_cell / powerpack_cell: one message envelope per batch of (device_id, cell_id,
                                 device_ts_ms) cell-level readings - the "child" stream, a
                                 distinct device_class/protocol/cadence, exactly what P2-13's
                                 done-when ("cell table partitioned and compacted") implies needs
                                 its own parser and its own Stage 1 table, not pack-row columns.

Cell child stream device_id / firmware_version: a cell-level message reuses the SAME device_id
and firmware_version as its parent pack device (it is the same physical unit's BMS emitting a
second stream), only device_class differs (<product>_cell instead of <product>). That is what
lets a downstream join reconnect cell rows to their parent pack device.

A flagged gap for whoever builds P2-13 for real (not something this prep ticket resolves):
CLAUDE.md invariant 2 states Stage 1's MERGE key is (device_id, device_ts, payload_hash) - it
says nothing about cell_id. In THIS generator's output that key still works "by accident": each
cell reading's payload_hash is computed over that cell's own fields (which include cell_id - see
_finalize_readings/_payload_hash), so two cells at the same device_ts_ms get different
payload_hash values and don't collide under the existing 3-part key. But the cell child table's
*intended*, human-legible key is (device_id, cell_id, device_ts_ms) - relying on payload_hash
uniqueness as an implicit stand-in for an explicit cell_id key component is fragile and worth a
real design decision (docs/decisions/) once P2-13 actually builds Stage 1 for this table.

Cell count modeling (a deliberate simplification, not a real cell count)
--------------------------------------------------------------------------
A real Megapack/Powerpack contains far more individual battery cells than this generator's
`cell_count` (thousands, arranged in series/parallel groups within modules); real BMS telemetry
realistically reports at a "cell group"/"monitoring channel" granularity anyway, not literal
per-cell, purely to bound reporting volume. `cell_id` below therefore models a BMS-reported
monitoring channel, not a literal battery cell. Megapack (current, larger-format product) is
modeled with more channels per unit than Powerpack (the earlier, smaller-format, being-phased-
out predecessor) - CELL_COUNT below is deliberately NOT scaled proportionally to either
product's real rated energy (which would put Megapack's channel count far higher and make
fixture generation/test runtime unpleasantly large for a prep artifact); the ratio chosen here
(MEGAPACK 64 : POWERPACK 24, see PRODUCT_SPECS) is picked only to preserve the *structural*
property P2-13 needs to exercise - "meaningfully more monitored cell groups on the larger
product" - at a tractable synthetic-fixture size, not to be read as a real spec.

Cell reporting cadence (the ratio is a documented guess, not a measurement)
------------------------------------------------------------------------------
Pack-level reading interval is chosen to match CLAUDE.md's own Stage 3a time-grid comment
("3a grid (5-min Powerwall/Powerpack, 1-min others)") rather than inventing a new number:
Megapack gets a 60s pack-level interval (grouped with "others"), Powerpack a 300s interval
(explicitly grouped with Powerwall in that comment) - the one piece of this generator's timing
model that is NOT an arbitrary guess, since it's already implied by existing repo design intent.
Cell-level cadence is then CELL_REPORTING_RATIO (12) times coarser than pack-level for both
products - a round multiplier reflecting that real BMS cell-detail sampling is commonly modeled
as roughly an order of magnitude (or a bit more) less frequent than pack-summary telemetry, to
bound data volume across many monitored cell groups; 12 was picked because it lands on tidy
cell intervals (Megapack: 12 min, Powerpack: 60 min/hourly) rather than for any deeper reason -
flag this for a firmware SME to correct with a real number.

Same messiness this generator's Supercharger sibling injects, and why
------------------------------------------------------------------------
Arrival shape (batching/jitter), timestamp/clock quality (missing/epoch-default/future,
systematic per-device drift), lateness/duplicates, and retry/buffer/drop behavior during a
simulated connectivity outage - the exact same categories supercharger.py models, using the
same GeneratorConfig field names/semantics so anything already written against one generator's
config shape (tests, docs) transfers with minimal surprise. Rates below are illustrative
defaults, not measurements, exactly like supercharger.py's own.

Output shape
------------
Identical envelope/reading shape to supercharger.py's output (see that module's own docstring):
one JSONL line per message envelope, `readings` is a batch of one or more flat reading dicts,
`_debug_injected_issues`-prefixed / `__`-prefixed keys are generator-only provenance a real
device would never send and a parser should ignore, `payload_hash` is a hash of a reading's own
field values used as part of Stage 1's merge key (CLAUDE.md invariant 2).
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

DeviceClass = Literal["megapack", "megapack_cell", "powerpack", "powerpack_cell"]
Product = Literal["megapack", "powerpack"]

# Pack-level firmware versions modeled per product. Two each - unlike supercharger.py's three,
# purely to keep this prep generator's default fixture volume manageable given the cell child
# stream multiplies row counts considerably (see module docstring); "top firmware versions" for
# P2-13's real done-when is a later, real decision, not made here.
DEFAULT_FIRMWARE: dict[Product, list[str]] = {
    "megapack": ["1.42.3", "1.44.0"],
    "powerpack": ["1.19.2", "1.21.0"],
}

CELL_DEVICE_CLASS: dict[Product, DeviceClass] = {
    "megapack": "megapack_cell",
    "powerpack": "powerpack_cell",
}

# Older firmware modeled as buggier, same convention as supercharger.py's
# FIRMWARE_QUIRK_MULTIPLIER (1.0 baseline, higher = worse). Powerpack (the older, predecessor
# product being phased out) is modeled with a higher baseline multiplier across both its
# firmware versions than Megapack, reflecting it being the less actively maintained product line
# - an editorial judgment call, not a measurement.
FIRMWARE_QUIRK_MULTIPLIER: dict[str, float] = {
    "1.42.3": 1.4,
    "1.44.0": 0.6,
    "1.19.2": 1.8,
    "1.21.0": 0.9,
}

MS_PER_S = 1000
S_PER_DAY = 86_400
# Fixed synthetic "now", matching supercharger.py's, so cross-generator fixtures line up on the
# same synthetic timeline if ever loaded together. 2026-06-01T00:00:00Z, arbitrary.
SYNTHETIC_NOW_MS = 1_780_358_400_000

# Shared, grid-side ranges (not device-specific): AC interconnection voltage/frequency should be
# a property of the grid a device is tied to, not of the device itself, so Megapack and Powerpack
# deliberately share these rather than inventing a per-product difference with no justification.
GRID_VOLTAGE_RANGE = (460.0, 500.0)
GRID_FREQUENCY_RANGE = (59.9, 60.1)
# Also shared: ambient temperature is a site/weather property, not a product property.
AMBIENT_TEMP_RANGE = (-20.0, 45.0)
CELL_TEMP_RANGE = (-10.0, 55.0)
# State-of-charge operating band shared across both products - real BMS systems rarely run a
# pack to literal 0/100% (reserve margin for cell longevity), modeled the same way
# supercharger_stall's fault/session mechanics leave headroom rather than hard 0/100 bounds.
SOC_RANGE = (5.0, 98.0)

INVERTER_STATUSES = ("standby", "charging", "discharging", "fault", "curtailed")


@dataclasses.dataclass(frozen=True)
class ProductSpec:
    """Per-product physical constants. Every number here is this generator author's own
    reasonable-guess judgment call (see module docstring), not sourced from a real datasheet."""

    product: Product
    device_class: DeviceClass
    cell_device_class: DeviceClass
    pack_reading_interval_s: int
    # Nominal usable energy capacity, used only to translate simulated power into a soc_pct
    # delta (delta_soc_pct = power_kw * hours / pack_energy_kwh * 100) - an internal-consistency
    # device, not a claimed real capacity figure.
    pack_energy_kwh: float
    pack_power_kw_max: float
    pack_voltage_range: tuple[float, float]
    cell_count: int
    cell_voltage_range: tuple[float, float]
    # index 0 is always "no fault" (0); the rest are this generator's own invented fault
    # category codes, deliberately in a per-product code space (see supercharger_stall vs
    # supercharger_cabinet's 1xxx/2xxx vs 3xxx precedent in catalog/signals.yaml) so a shared
    # canonical fault_code column never conflates two products' different code meanings.
    fault_codes: tuple[int, ...]


PRODUCT_SPECS: dict[Product, ProductSpec] = {
    "megapack": ProductSpec(
        product="megapack",
        device_class="megapack",
        cell_device_class="megapack_cell",
        pack_reading_interval_s=60,  # matches CLAUDE.md Stage 3a "1-min others" grouping
        pack_energy_kwh=3_900.0,  # loosely in line with public Megapack 2XL-scale figures
        pack_power_kw_max=1_900.0,
        pack_voltage_range=(600.0, 900.0),
        cell_count=64,  # BMS-reported monitoring channels, not literal cells - see docstring
        cell_voltage_range=(2.5, 3.65),  # LFP-chemistry-plausible per-cell range
        fault_codes=(0, 4101, 4220, 4355),
    ),
    "powerpack": ProductSpec(
        product="powerpack",
        device_class="powerpack",
        cell_device_class="powerpack_cell",
        pack_reading_interval_s=300,  # matches CLAUDE.md Stage 3a "5-min Powerwall/Powerpack"
        pack_energy_kwh=500.0,  # smaller predecessor product, roughly an order smaller
        pack_power_kw_max=250.0,
        pack_voltage_range=(380.0, 600.0),
        cell_count=24,  # fewer monitoring channels than Megapack - smaller, older product
        # Modeled with a higher per-cell voltage range than Megapack's LFP-plausible range,
        # reflecting Tesla's publicly known shift toward LFP chemistry in its newer grid-battery
        # product line - domain-plausible flavor, not a verified chemistry claim for either
        # product.
        cell_voltage_range=(3.0, 4.2),
        fault_codes=(0, 5101, 5220),
    ),
}


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring). Field
    names/semantics deliberately mirror supercharger.py's GeneratorConfig where the same concept
    applies, plus a handful of fields specific to the pack/cell split this generator adds."""

    devices_per_firmware: int = 3
    pack_window_hours: int = 24
    seed: int = 2026

    # Cell-level cadence, as a multiple of pack_reading_interval_s (see module docstring).
    cell_reporting_ratio: int = 12

    # How often a discharging pack gets a grid-curtailment order (utility/dispatcher-ordered
    # output reduction) that pulls actual delivered power well below the commanded
    # dispatch_setpoint_kw - a grid-interconnection behavior Supercharger has no analog for.
    curtailment_rate: float = 0.03

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

    pack_max_batch_size: int = 8
    cell_max_batch_size: int = 40
    network_jitter_max_s: int = 5


@dataclasses.dataclass
class GeneratedFixtures:
    config: GeneratorConfig
    messages_by_group: dict[tuple[DeviceClass, str], list[dict[str, Any]]]
    stats_by_group: dict[tuple[DeviceClass, str], Counter]

    def all_messages(self) -> Iterable[dict[str, Any]]:
        for msgs in self.messages_by_group.values():
            yield from msgs


# ---------------------------------------------------------------------------------------------
# Shared, generic helpers - deliberately duplicated from supercharger.py's own (rather than
# imported) so this module stays fully self-contained and supercharger.py stays untouched (see
# repo-wide "additive only, don't touch Supercharger files" constraint on this prep ticket).
# Logic below is intentionally the same shape as supercharger.py's equivalents.
# ---------------------------------------------------------------------------------------------


def _payload_hash(reading_fields: dict[str, Any]) -> str:
    canonical = json.dumps(reading_fields, sort_keys=True, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _device_id(product: Product, firmware: str, index: int) -> str:
    prefix = "mpk" if product == "megapack" else "ppk"
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
    protocol: str,
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

    envelope = {
        "message_id": f"msg-{device_id}-{device_class}-{true_arrival_base_ms}-{rng.randint(0, 999_999)}",
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
    protocol: str,
    readings: list[dict[str, Any]],
    outage_happened: bool,
    max_batch_size: int,
    config: GeneratorConfig,
    stats: Counter,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    batches = _batch(rng, readings, max_batch_size)
    # If an outage happened, the *last* batch containing the buffered burst arrives all at once,
    # well after its readings' device_ts - modeling "reconnect and flush the buffer".
    outage_burst_index = len(batches) - 1 if outage_happened and batches else None

    for idx, batch in enumerate(batches):
        if not batch:
            continue
        # `or SYNTHETIC_NOW_MS`, not just `.get(...)`, so a last reading with a missing OR
        # epoch-default (0) device_ts_ms - both falsy - falls back to a sane arrival base
        # instead of anchoring the envelope's arrival near 1970 (matches supercharger.py's own
        # `last_ts_ms or SYNTHETIC_NOW_MS` handling of the same corruption cases).
        last_ts_ms = batch[-1].get("device_ts_ms") or SYNTHETIC_NOW_MS
        base_arrival_ms = last_ts_ms if isinstance(last_ts_ms, int) else SYNTHETIC_NOW_MS
        envelope, issues = _make_envelope(
            rng, device_class, firmware, device_id, site_id, protocol, batch, base_arrival_ms, config
        )
        if idx == outage_burst_index:
            burst_delay_s = rng.randint(config.outage_min_s, config.outage_max_s)
            envelope["arrival_ts_ms"] += burst_delay_s * MS_PER_S
            envelope["_debug_injected_issues"].append("post_outage_burst")
            stats["post_outage_burst_messages"] += 1

        messages.append(envelope)
        stats["messages_total"] += 1
        stats["batch_size_" + str(min(len(batch), 40))] += 1
        for issue in issues:
            stats[issue] += 1

        dup = _maybe_duplicate(rng, envelope, config)
        if dup is not None:
            messages.append(dup)
            stats["messages_total"] += 1
            stats["duplicate_message"] += 1

    return messages


# ---------------------------------------------------------------------------------------------
# Pack-level simulation
# ---------------------------------------------------------------------------------------------


def _dispatch_profile(hour_of_day: float) -> tuple[str, float]:
    """Return (dispatch_state, magnitude_fraction in [0, 1]) for a simple peak-shaving dispatch
    cycle: charge off-peak overnight, discharge the evening peak, standby otherwise.

    Deliberately simplified - a real dispatch controller reacts to live price/grid signals a
    synthetic generator can't model; this exists only to give the simulated soc/power curve
    *some* plausible daily shape (a real, common grid-battery use case - peak shaving/arbitrage)
    instead of pure noise, not to model a real dispatch algorithm.
    """
    if 0 <= hour_of_day < 6:
        frac = 1 - abs(hour_of_day - 3) / 3
        return "charging", max(0.1, frac)
    if 16 <= hour_of_day < 21:
        frac = 1 - abs(hour_of_day - 18.5) / 2.5
        return "discharging", max(0.1, frac)
    return "standby", 0.0


def _simulate_pack_readings(
    rng: random.Random, spec: ProductSpec, window_start_ms: int, config: GeneratorConfig
) -> list[dict[str, Any]]:
    interval_s = spec.pack_reading_interval_s
    num_readings = max(1, (config.pack_window_hours * 3600) // interval_s)

    voltage_lo, voltage_hi = spec.pack_voltage_range
    voltage_span = voltage_hi - voltage_lo
    base_voltage = rng.uniform(voltage_lo, voltage_hi)
    ambient_base = rng.uniform(*AMBIENT_TEMP_RANGE)

    soc = rng.uniform(25.0, 75.0)
    energy_charged_kwh = 0.0
    energy_discharged_kwh = 0.0

    has_fault = rng.random() < 0.03
    fault_at = rng.randint(num_readings // 2, num_readings - 1) if has_fault and num_readings > 1 else None

    readings: list[dict[str, Any]] = []
    for i in range(num_readings):
        true_ts_ms = window_start_ms + i * interval_s * MS_PER_S
        hour_of_day = ((true_ts_ms // MS_PER_S) // 3600) % 24
        dispatch_state, frac = _dispatch_profile(hour_of_day)

        setpoint_kw = 0.0
        if dispatch_state == "charging":
            setpoint_kw = spec.pack_power_kw_max * frac * rng.uniform(0.9, 1.05)
        elif dispatch_state == "discharging":
            setpoint_kw = -spec.pack_power_kw_max * frac * rng.uniform(0.9, 1.05)

        inverter_status = dispatch_state
        actual_kw = setpoint_kw
        if dispatch_state == "discharging" and rng.random() < config.curtailment_rate:
            # Grid operator curtailment order: actual delivered power drops well below the
            # commanded setpoint, but the setpoint itself is unchanged (it's still the command,
            # just not fully honored) - a grid-interconnection behavior Supercharger has no
            # analog for.
            inverter_status = "curtailed"
            actual_kw = setpoint_kw * rng.uniform(0.0, 0.3)

        fault_code = 0
        if fault_at is not None and i >= fault_at:
            inverter_status = "fault"
            fault_code = rng.choice(spec.fault_codes[1:])
            actual_kw = 0.0
            setpoint_kw = 0.0

        delta_soc_pct = (actual_kw * interval_s / 3600) / spec.pack_energy_kwh * 100
        soc = min(SOC_RANGE[1], max(SOC_RANGE[0], soc + delta_soc_pct))
        if actual_kw > 0:
            energy_charged_kwh += actual_kw * interval_s / 3600
        elif actual_kw < 0:
            energy_discharged_kwh += -actual_kw * interval_s / 3600

        # Voltage loosely coupled to soc (higher soc -> slightly higher terminal voltage) plus
        # noise - not an independent physical simulation, same disclaimer style as
        # supercharger.py's connector_temp_c/transformer_temp_c.
        voltage_v = base_voltage + (soc - 50.0) / 50.0 * (voltage_span * 0.05) + rng.uniform(-1.5, 1.5)
        voltage_v = min(voltage_hi + 5.0, max(voltage_lo - 5.0, voltage_v))
        current_a = (actual_kw * 1000.0 / voltage_v) if voltage_v else 0.0

        ambient_temp_c = ambient_base + rng.uniform(-2.0, 2.0)
        load_frac = abs(actual_kw) / spec.pack_power_kw_max if spec.pack_power_kw_max else 0.0
        max_cell_temp_c = ambient_temp_c + load_frac * 15.0 + rng.uniform(0.0, 3.0)
        min_cell_temp_c = ambient_temp_c - rng.uniform(0.0, 2.0)

        fields = {
            "pack_soc_pct": round(soc, 2),
            "pack_voltage_v": round(voltage_v, 1),
            "pack_current_a": round(current_a, 2),
            "pack_power_kw": round(actual_kw, 2),
            "dispatch_setpoint_kw": round(setpoint_kw, 2),
            "inverter_status": inverter_status,
            "ambient_temp_c": round(ambient_temp_c, 1),
            "max_cell_temp_c": round(max_cell_temp_c, 1),
            "min_cell_temp_c": round(min_cell_temp_c, 1),
            "grid_voltage_v": round(rng.uniform(*GRID_VOLTAGE_RANGE), 1),
            "grid_frequency_hz": round(rng.uniform(*GRID_FREQUENCY_RANGE), 3),
            "energy_charged_kwh": round(energy_charged_kwh, 3),
            "energy_discharged_kwh": round(energy_discharged_kwh, 3),
            "fault_code": fault_code,
        }
        readings.append({"__true_ts_ms": true_ts_ms, "fields": fields})
        if fault_code:
            break
    return readings


# ---------------------------------------------------------------------------------------------
# Cell-level simulation
# ---------------------------------------------------------------------------------------------


def _simulate_cell_readings(
    rng: random.Random, spec: ProductSpec, window_start_ms: int, config: GeneratorConfig
) -> list[dict[str, Any]]:
    cell_interval_s = spec.pack_reading_interval_s * config.cell_reporting_ratio
    num_sweeps = max(1, (config.pack_window_hours * 3600) // cell_interval_s)

    v_lo, v_hi = spec.cell_voltage_range
    balance_threshold = v_lo + (v_hi - v_lo) * 0.85
    # Per-device, per-channel baseline voltage (manufacturing variation), stable across sweeps -
    # otherwise every cell would just be independent noise with no per-cell identity.
    baselines = [rng.uniform(v_lo + 0.05, v_hi - 0.05) for _ in range(spec.cell_count)]
    # One channel modeled as a mild, persistent low-voltage outlier some of the time - gives this
    # fixture some cell-imbalance-shaped signal for whoever eventually builds P2-15's cell-
    # imbalance detector to test against later; NOT required by this prep ticket, kept minimal.
    imbalanced_cell = rng.randrange(spec.cell_count) if rng.random() < 0.3 else None

    readings: list[dict[str, Any]] = []
    for sweep in range(num_sweeps):
        true_ts_ms = window_start_ms + sweep * cell_interval_s * MS_PER_S
        for cell_idx in range(spec.cell_count):
            voltage_v = baselines[cell_idx] + rng.uniform(-0.02, 0.02)
            if cell_idx == imbalanced_cell:
                voltage_v -= rng.uniform(0.05, 0.15)
            voltage_v = min(v_hi, max(v_lo, voltage_v))
            temp_c = rng.uniform(*CELL_TEMP_RANGE)
            balancing_active = voltage_v > balance_threshold and rng.random() < 0.3

            fields = {
                "cell_id": f"cg-{cell_idx:04d}",
                "cell_voltage_v": round(voltage_v, 3),
                "cell_temp_c": round(temp_c, 1),
                "balancing_active": balancing_active,
            }
            readings.append({"__true_ts_ms": true_ts_ms, "fields": fields})
    return readings


# ---------------------------------------------------------------------------------------------
# Per-device orchestration
# ---------------------------------------------------------------------------------------------


def _simulate_device(
    rng: random.Random,
    spec: ProductSpec,
    firmware: str,
    index: int,
    config: GeneratorConfig,
    pack_stats: Counter,
    cell_stats: Counter,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Simulate one physical device: its pack-level stream and its cell-level stream.

    Returns (pack_messages, cell_messages).
    """
    device_id = _device_id(spec.product, firmware, index)
    site_id = f"site-{index % 4:03d}"
    protocol_pack = "mqtt_batch"  # frequent, small pack updates - persistent-connection-shaped
    protocol_cell = "https_poll"  # bulkier periodic sweep - poll-shaped, same split rationale
    # supercharger.py uses for stall (mqtt_batch) vs cabinet (https_poll).

    clock_drift_ms = rng.randint(-config.clock_drift_max_s, config.clock_drift_max_s) * MS_PER_S
    window_start_ms = SYNTHETIC_NOW_MS - config.pack_window_hours * 3600 * MS_PER_S

    raw_pack = _simulate_pack_readings(rng, spec, window_start_ms, config)
    finalized_pack = _finalize_readings(rng, raw_pack, clock_drift_ms, config, pack_stats)
    finalized_pack, pack_outage = _apply_outage(rng, finalized_pack, firmware, config, pack_stats)
    pack_messages = _emit_device_messages(
        rng,
        spec.device_class,
        firmware,
        device_id,
        site_id,
        protocol_pack,
        finalized_pack,
        pack_outage,
        config.pack_max_batch_size,
        config,
        pack_stats,
    )

    raw_cell = _simulate_cell_readings(rng, spec, window_start_ms, config)
    finalized_cell = _finalize_readings(rng, raw_cell, clock_drift_ms, config, cell_stats)
    finalized_cell, cell_outage = _apply_outage(rng, finalized_cell, firmware, config, cell_stats)
    cell_messages = _emit_device_messages(
        rng,
        spec.cell_device_class,
        firmware,
        device_id,
        site_id,
        protocol_cell,
        finalized_cell,
        cell_outage,
        config.cell_max_batch_size,
        config,
        cell_stats,
    )

    return pack_messages, cell_messages


def generate(config: GeneratorConfig | None = None) -> GeneratedFixtures:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)

    messages_by_group: dict[tuple[DeviceClass, str], list[dict[str, Any]]] = defaultdict(list)
    stats_by_group: dict[tuple[DeviceClass, str], Counter] = defaultdict(Counter)

    for product, firmwares in DEFAULT_FIRMWARE.items():
        spec = PRODUCT_SPECS[product]
        for firmware in firmwares:
            pack_group = (spec.device_class, firmware)
            cell_group = (spec.cell_device_class, firmware)
            for index in range(config.devices_per_firmware):
                pack_msgs, cell_msgs = _simulate_device(
                    rng,
                    spec,
                    firmware,
                    index,
                    config,
                    stats_by_group[pack_group],
                    stats_by_group[cell_group],
                )
                messages_by_group[pack_group].extend(pack_msgs)
                messages_by_group[cell_group].extend(cell_msgs)
            messages_by_group[pack_group].sort(key=lambda m: m["arrival_ts_ms"])
            messages_by_group[cell_group].sort(key=lambda m: m["arrival_ts_ms"])

    return GeneratedFixtures(config, dict(messages_by_group), dict(stats_by_group))


def write(fixtures: GeneratedFixtures, root: Path) -> Path:
    """Write JSONL fixtures + a manifest.json under root/<device_class>/<firmware>/. Same shape
    as supercharger.py's write(), including for the two new *_cell device classes - a parser
    consuming both generators' output sees an identical directory/manifest convention."""
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
