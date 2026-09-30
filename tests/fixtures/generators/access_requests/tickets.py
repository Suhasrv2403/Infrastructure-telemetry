"""Synthetic fixture generator for support/service ticket records.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
No real ticketing system schema was consulted to build this generator: P0-11's access request
for ticket data (`docs/access-requests/P0-11-data-access-requests.md`, section 2) has never
been sent, and no sample extract has ever been received. Every field below is this project's
own best guess, based only on that request document's field wishlist - it is not evidence of
any real system's actual schema. The riskiest assumption here is `device_id` nullability: the
draft request itself says a ticket is "preferred [to reference] over customer name/contact
info" but doesn't claim every ticket has one, and plenty of real support tickets (billing,
account access) have nothing to do with a specific device - this generator models that by
leaving `device_id` unset on a configurable fraction of tickets. When access is eventually
granted, validate this (and everything else here) per the P0-11 doc's own instruction: "hand
it to the senior DS to validate schema and quality."

Why this exists
----------------
Per explicit project-owner direction, this generator is a synthetic substitute for P0-11's
real "sample extracts loaded" acceptance bar. It specifically unblocks:
- **P2-02** (ongoing tickets ingestion as a dated feed).
- **P2-04** (event reconciliation) - a ticket with a non-null `device_id` is a candidate
  corroborating signal for a detected event.
- **P2-12** (detector backtest ground truth, alongside RMA).

Rates below (category/severity mix, how often a ticket references a device, how long a ticket
stays open) are invented for schema/shape testing, not fit to any real support-volume data.

Output shape
------------
One JSON object per ticket: `ticket_id`, `device_id` (``None`` for tickets not tied to a
device), `opened_ts`, `closed_ts` (``None`` while still open), `category`, `severity`,
`status` (`"open"`, `"pending"` or `"closed"` - always `"closed"` iff `closed_ts` is set).
`*_ts` fields are epoch milliseconds (int). `device_id` values, when present, are drawn from
the same scheme as `tests/fixtures/generators/supercharger.py`'s fixtures (see `_common.py`),
so a ticket and a Supercharger telemetry fixture can be joined on `device_id`.
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
    iter_supercharger_devices,
)

CATEGORIES = [
    "billing",
    "technical_fault",
    "installation",
    "outage_report",
    "app_or_account",
    "other",
]

# Categories where a ticket is very unlikely to reference a specific device (billing/account
# issues aren't device faults). Everything else defaults to device-attached most of the time.
DEVICE_UNLIKELY_CATEGORIES = {"billing", "app_or_account"}

SEVERITIES = ["low", "medium", "high", "critical"]

STATUSES_OPEN = ["open", "pending"]


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    num_records: int = 200
    seed: int = 1337
    devices_per_firmware: int = 4
    lookback_days: int = 730
    now_ms: int = SYNTHETIC_NOW_MS
    still_open_rate: float = 0.15
    min_open_duration_days: int = 0
    max_open_duration_days: int = 14
    # Chance a ticket has no device_id even when its category is one that usually does.
    device_unlikely_category_device_rate: float = 0.05
    device_likely_category_device_rate: float = 0.9


@dataclasses.dataclass
class GeneratedRecords:
    config: GeneratorConfig
    records: list[dict[str, Any]]
    stats: Counter


def generate(config: GeneratorConfig | None = None) -> GeneratedRecords:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)
    devices = iter_supercharger_devices(config.devices_per_firmware)
    stats: Counter = Counter()

    earliest_open_ms = config.now_ms - config.lookback_days * S_PER_DAY * MS_PER_S

    records: list[dict[str, Any]] = []
    for i in range(config.num_records):
        category = rng.choice(CATEGORIES)
        device_rate = (
            config.device_unlikely_category_device_rate
            if category in DEVICE_UNLIKELY_CATEGORIES
            else config.device_likely_category_device_rate
        )
        device_id = rng.choice(devices).device_id if rng.random() < device_rate else None
        if device_id is None:
            stats["no_device_id"] += 1

        opened_ts = rng.randint(earliest_open_ms, config.now_ms)
        severity = rng.choice(SEVERITIES)

        still_open = rng.random() < config.still_open_rate
        closed_ts = None
        if not still_open:
            duration_days = rng.randint(
                config.min_open_duration_days, config.max_open_duration_days
            )
            closed_ts = min(opened_ts + duration_days * S_PER_DAY * MS_PER_S, config.now_ms)
            if closed_ts <= opened_ts:
                closed_ts = opened_ts + MS_PER_S
            status = "closed"
        else:
            status = rng.choice(STATUSES_OPEN)

        stats[f"category_{category}"] += 1
        stats[f"severity_{severity}"] += 1
        stats[f"status_{status}"] += 1
        stats["records_total"] += 1

        records.append(
            {
                "ticket_id": f"ticket-{i:06d}",
                "device_id": device_id,
                "opened_ts": opened_ts,
                "closed_ts": closed_ts,
                "category": category,
                "severity": severity,
                "status": status,
            }
        )

    records.sort(key=lambda r: r["opened_ts"])
    return GeneratedRecords(config, records, stats)


def write(generated: GeneratedRecords, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for record in generated.records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    return out_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/access_requests/tickets.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--num-records", type=int, default=GeneratorConfig().num_records)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, num_records=args.num_records)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} ticket records to {out_path}")


if __name__ == "__main__":
    _cli()
