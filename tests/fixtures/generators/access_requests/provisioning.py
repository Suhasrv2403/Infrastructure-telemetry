"""Synthetic fixture generator for device provisioning history records.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
No real provisioning/manufacturing/deployment system schema was consulted to build this
generator: P0-11's access request for provisioning data
(`docs/access-requests/P0-11-data-access-requests.md`, section 5) has never been sent, and no
sample extract has ever been received. Every field below is this project's own best guess,
based only on that request document's field wishlist. The single most important assumption -
called out explicitly in the P0-11 doc itself - is the one this generator is built entirely
around: **this must be a change-log/history table, not a current-state snapshot.** P2-01's
acceptance test is that an as-of join returns the firmware (etc.) a device ran at any past
timestamp, and a snapshot table cannot support that. If a real provisioning extract instead
turns out to be snapshot-only (the P0-11 doc's own fallback: "periodic full snapshots we can
diff ourselves"), this generator's shape (one row per change) is the wrong one to build
against and P2-01's ingestion would need to reconstruct history by diffing snapshots instead -
validate which form the real system actually provides once access is granted (per the P0-11
doc: "hand it to the senior DS to validate schema and quality").

Why this exists
----------------
Per explicit project-owner direction, this generator is a synthetic substitute for P0-11's
real "sample extracts loaded" acceptance bar. It specifically unblocks:
- **P2-01** (device history dimension) - the whole point of this generator. Every device gets
  a commissioning row plus zero or more later change rows (firmware upgrade, hardware swap,
  site reassignment), each with a strictly increasing `effective_ts` and no two rows for the
  same device sharing a timestamp, so "the row that was current at time T" is always
  well-defined: the row with the largest `effective_ts <= T` (see `current_as_of()` below).

Rates below (how many changes a device sees, how often each field changes, gap between
changes) are invented for schema/shape testing, not fit to any real deployment cadence.

Output shape
------------
One JSON object per provisioning change event: `device_id`, `effective_ts`, `firmware_version`,
`hardware_rev`, `cell_lot`, `site_id`. `effective_ts` is epoch milliseconds (int). Rows are a
full snapshot of the device's state *as of* `effective_ts`, not a diff - every field is present
on every row, even the ones that didn't change in that event (this matches how a real
change-history table is usually queried: "give me the whole record effective at time T", not
"give me just the delta"). `device_id`/`site_id` are drawn from the same scheme as
`tests/fixtures/generators/supercharger.py`'s fixtures (see `_common.py`).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from tests.fixtures.generators.access_requests._common import (
    MS_PER_S,
    S_PER_DAY,
    SYNTHETIC_NOW_MS,
    DeviceRef,
    iter_supercharger_devices,
)
from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE

HARDWARE_REVS = ["rev-a", "rev-b", "rev-c"]
CELL_LOT_YEARS = range(2019, 2026)


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    seed: int = 1337
    devices_per_firmware: int = 4
    now_ms: int = SYNTHETIC_NOW_MS
    # Commissioning happens sometime in this window before "now", so every device has at
    # least one change (a firmware upgrade, say) plausible before the current timestamp.
    min_commission_lookback_days: int = 60
    max_commission_lookback_days: int = 900
    min_changes_after_commission: int = 0
    max_changes_after_commission: int = 4
    min_gap_days: int = 20
    max_gap_days: int = 240
    firmware_upgrade_rate: float = 0.6
    hardware_swap_rate: float = 0.05
    site_reassignment_rate: float = 0.03


@dataclasses.dataclass
class GeneratedRecords:
    config: GeneratorConfig
    records: list[dict[str, Any]]
    stats: Counter


def _random_cell_lot(rng: random.Random) -> str:
    year = rng.choice(list(CELL_LOT_YEARS))
    month = rng.randint(1, 12)
    suffix = rng.choice("ABCDEFGH")
    batch = rng.randint(1, 9)
    return f"LOT-{year}{month:02d}-{suffix}{batch}"


def _device_history(
    rng: random.Random, device: DeviceRef, config: GeneratorConfig, stats: Counter
) -> list[dict[str, Any]]:
    firmware_options = DEFAULT_FIRMWARE[device.device_class]
    firmware_index = 0
    hardware_rev = rng.choice(HARDWARE_REVS)
    cell_lot = _random_cell_lot(rng)
    site_id = device.site_id

    commission_lookback_days = rng.randint(
        config.min_commission_lookback_days, config.max_commission_lookback_days
    )
    effective_ts = config.now_ms - commission_lookback_days * S_PER_DAY * MS_PER_S

    events = [
        {
            "device_id": device.device_id,
            "effective_ts": effective_ts,
            "firmware_version": firmware_options[firmware_index],
            "hardware_rev": hardware_rev,
            "cell_lot": cell_lot,
            "site_id": site_id,
        }
    ]
    stats["commission_events"] += 1

    num_changes = rng.randint(
        config.min_changes_after_commission, config.max_changes_after_commission
    )
    for _ in range(num_changes):
        gap_days = rng.randint(config.min_gap_days, config.max_gap_days)
        next_ts = effective_ts + gap_days * S_PER_DAY * MS_PER_S
        if next_ts >= config.now_ms:
            break

        changed = False
        if firmware_index < len(firmware_options) - 1 and rng.random() < config.firmware_upgrade_rate:
            firmware_index += 1
            changed = True
            stats["firmware_upgrade_events"] += 1
        if rng.random() < config.hardware_swap_rate:
            hardware_rev = rng.choice(HARDWARE_REVS)
            changed = True
            stats["hardware_swap_events"] += 1
        if rng.random() < config.site_reassignment_rate:
            site_id = f"site-{rng.randrange(6):03d}"
            changed = True
            stats["site_reassignment_events"] += 1

        effective_ts = next_ts
        if not changed:
            # A change log only logs actual changes - don't emit an identical duplicate row.
            continue

        events.append(
            {
                "device_id": device.device_id,
                "effective_ts": effective_ts,
                "firmware_version": firmware_options[firmware_index],
                "hardware_rev": hardware_rev,
                "cell_lot": cell_lot,
                "site_id": site_id,
            }
        )
        stats["change_events"] += 1

    return events


def generate(config: GeneratorConfig | None = None) -> GeneratedRecords:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)
    devices = iter_supercharger_devices(config.devices_per_firmware)
    stats: Counter = Counter()

    records: list[dict[str, Any]] = []
    for device in devices:
        records.extend(_device_history(rng, device, config, stats))
        stats["devices_total"] += 1

    stats["records_total"] = len(records)
    records.sort(key=lambda r: (r["device_id"], r["effective_ts"]))
    return GeneratedRecords(config, records, stats)


def current_as_of(
    records: list[dict[str, Any]], device_id: str, as_of_ts: int
) -> dict[str, Any] | None:
    """The provisioning row that was in effect for `device_id` at `as_of_ts`: the row with the
    largest `effective_ts <= as_of_ts`, or ``None`` if `as_of_ts` predates that device's
    commissioning row. This is the as-of join P2-01's acceptance test exercises, expressed
    directly rather than via a join engine, so it's usable both as a generator utility and as
    the reference implementation the unit tests check against."""
    candidates = [
        r for r in records if r["device_id"] == device_id and r["effective_ts"] <= as_of_ts
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["effective_ts"])


def write(generated: GeneratedRecords, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for record in generated.records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    return out_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/access_requests/provisioning.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} provisioning history records to {out_path}")


if __name__ == "__main__":
    _cli()
