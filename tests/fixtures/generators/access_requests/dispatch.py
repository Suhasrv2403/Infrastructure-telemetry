"""Synthetic fixture generator for field-service dispatch records.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
No real dispatch/field-service system schema was consulted to build this generator: P0-11's
access request for dispatch data (`docs/access-requests/P0-11-data-access-requests.md`,
section 3) has never been sent, and no sample extract has ever been received. Every field
below is this project's own best guess, based only on that request document's field wishlist.
The riskiest assumption here is `ticket_id` linkage: the draft request asks for a "linked
ticket ID if available", implying the real system may or may not cross-reference tickets at
all. This generator models that as "sometimes present, sometimes null" but does NOT guarantee
those `ticket_id` values exist in any particular `tickets.py` generator run - the two
generators are independently seeded and not cross-referenced against each other's output. If a
downstream test needs a dispatch record and a ticket record that actually share a real
`ticket_id`, generate that linkage explicitly in the test rather than relying on these two
generators to agree by chance. Validate all of this against a real extract once access is
granted (per the P0-11 doc: "hand it to the senior DS to validate schema and quality").

Why this exists
----------------
Per explicit project-owner direction, this generator is a synthetic substitute for P0-11's
real "sample extracts loaded" acceptance bar. It specifically unblocks:
- **P2-02** (ongoing dispatch ingestion as a dated feed).
- **P2-04** (event reconciliation) - a completed dispatch is strong evidence a detected event
  actually happened at a device/site.

Rates below (how often a dispatch has completed, outcome mix, ticket linkage rate) are
invented for schema/shape testing, not fit to any real field-service data.

Output shape
------------
One JSON object per dispatch: `dispatch_id`, `device_id`, `site_id`, `scheduled_ts`,
`completed_ts` (``None`` while not yet completed), `technician_outcome` (``None`` while not
yet completed - there's no outcome before a technician visits), `ticket_id` (``None`` when not
linked to a ticket). `*_ts` fields are epoch milliseconds (int). `device_id`/`site_id` are
drawn from the same scheme as `tests/fixtures/generators/supercharger.py`'s fixtures (see
`_common.py`).
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

OUTCOMES = [
    "resolved_on_site",
    "part_replaced",
    "no_fault_found",
    "escalated",
    "rescheduled",
]


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    num_records: int = 120
    seed: int = 1337
    devices_per_firmware: int = 4
    lookback_days: int = 730
    now_ms: int = SYNTHETIC_NOW_MS
    not_yet_completed_rate: float = 0.08
    # Arrival-to-completion gap: a truck roll doesn't happen the instant it's scheduled.
    min_completion_gap_hours: int = 1
    max_completion_gap_hours: int = 5 * 24
    ticket_linked_rate: float = 0.55


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

    earliest_ms = config.now_ms - config.lookback_days * S_PER_DAY * MS_PER_S

    records: list[dict[str, Any]] = []
    for i in range(config.num_records):
        device = rng.choice(devices)
        scheduled_ts = rng.randint(earliest_ms, config.now_ms)

        not_completed = rng.random() < config.not_yet_completed_rate
        completed_ts = None
        technician_outcome = None
        if not not_completed:
            gap_hours = rng.randint(
                config.min_completion_gap_hours, config.max_completion_gap_hours
            )
            completed_ts = min(scheduled_ts + gap_hours * 3600 * MS_PER_S, config.now_ms)
            if completed_ts <= scheduled_ts:
                completed_ts = scheduled_ts + MS_PER_S
            technician_outcome = rng.choice(OUTCOMES)
            stats[f"outcome_{technician_outcome}"] += 1
        else:
            stats["not_yet_completed"] += 1

        ticket_id = None
        if rng.random() < config.ticket_linked_rate:
            # Plausible-format id, not guaranteed to exist in any particular tickets.py run -
            # see module docstring.
            ticket_id = f"ticket-{rng.randint(0, 999_999):06d}"
            stats["ticket_linked"] += 1

        stats["records_total"] += 1

        records.append(
            {
                "dispatch_id": f"dispatch-{i:06d}",
                "device_id": device.device_id,
                "site_id": device.site_id,
                "scheduled_ts": scheduled_ts,
                "completed_ts": completed_ts,
                "technician_outcome": technician_outcome,
                "ticket_id": ticket_id,
            }
        )

    records.sort(key=lambda r: r["scheduled_ts"])
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
        "--out", type=Path, default=Path("tests/fixtures/access_requests/dispatch.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--num-records", type=int, default=GeneratorConfig().num_records)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, num_records=args.num_records)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} dispatch records to {out_path}")


if __name__ == "__main__":
    _cli()
