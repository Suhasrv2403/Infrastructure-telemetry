"""Synthetic fixture generator for grid/site outage records.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
No real grid/outage system schema was consulted to build this generator: P0-11's access
request for outage data (`docs/access-requests/P0-11-data-access-requests.md`, section 4) has
never been sent, and no sample extract has ever been received. Every field below is this
project's own best guess, based only on that request document's field wishlist. The riskiest
assumption here is grain: the request document explicitly allows for outages to be reported at
site/circuit/feeder granularity OR mapped to specific device IDs, and this generator picks the
site-granularity form (`site_id` + `affected_device_count`) as the ASSUMED default, since a
"how many devices at this site were affected" count is what P3-05 (mode-aware expected counts)
actually needs to reconcile telemetry silence against a known outage - it does not attempt to
model a per-device outage mapping. Validate this grain choice against a real extract once
access is granted (per the P0-11 doc: "hand it to the senior DS to validate schema and
quality").

Why this exists
----------------
Per explicit project-owner direction, this generator is a synthetic substitute for P0-11's
real "sample extracts loaded" acceptance bar. It specifically unblocks:
- **P2-02** (ongoing outage ingestion as a dated feed).
- **P2-04** (event reconciliation) and **P3-05** (mode-aware expected counts) - both need to
  tell "device went quiet because of a known outage" apart from "device went quiet because it
  broke".

Rates below (cause mix, how often an outage is still ongoing, duration, affected-device count)
are invented for schema/shape testing, not fit to any real grid-reliability data.

Output shape
------------
One JSON object per outage: `outage_id`, `site_id`, `start_ts`, `end_ts` (``None`` = ongoing,
not yet restored), `cause_code`, `affected_device_count`. `*_ts` fields are epoch milliseconds
(int). `site_id` is drawn from the same scheme as
`tests/fixtures/generators/supercharger.py`'s fixtures (see `_common.py`), so an outage record
and a Supercharger telemetry fixture can be joined on `site_id`.
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
)

CAUSE_CODES = [
    "weather",
    "equipment_failure",
    "planned_maintenance",
    "grid_upstream",
    "unknown",
]

# Same site pool supercharger.py's `index % 6` scheme produces, independent of
# devices_per_firmware (an outage is a site-level event, not tied to one device's firmware).
NUM_SITES = 6


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    num_records: int = 80
    seed: int = 1337
    lookback_days: int = 730
    now_ms: int = SYNTHETIC_NOW_MS
    ongoing_rate: float = 0.04
    min_duration_minutes: int = 5
    max_duration_hours: int = 48
    min_affected_devices: int = 1
    max_affected_devices: int = 50


@dataclasses.dataclass
class GeneratedRecords:
    config: GeneratorConfig
    records: list[dict[str, Any]]
    stats: Counter


def generate(config: GeneratorConfig | None = None) -> GeneratedRecords:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)
    stats: Counter = Counter()

    earliest_ms = config.now_ms - config.lookback_days * S_PER_DAY * MS_PER_S

    records: list[dict[str, Any]] = []
    for i in range(config.num_records):
        site_id = f"site-{rng.randrange(NUM_SITES):03d}"
        start_ts = rng.randint(earliest_ms, config.now_ms)

        ongoing = rng.random() < config.ongoing_rate
        end_ts = None
        if not ongoing:
            duration_ms = rng.randint(
                config.min_duration_minutes * 60 * MS_PER_S,
                config.max_duration_hours * 3600 * MS_PER_S,
            )
            end_ts = min(start_ts + duration_ms, config.now_ms)
            if end_ts <= start_ts:
                end_ts = start_ts + MS_PER_S
            stats["restored"] += 1
        else:
            stats["ongoing"] += 1

        cause_code = rng.choice(CAUSE_CODES)
        affected_device_count = rng.randint(
            config.min_affected_devices, config.max_affected_devices
        )

        stats[f"cause_{cause_code}"] += 1
        stats["records_total"] += 1

        records.append(
            {
                "outage_id": f"outage-{i:06d}",
                "site_id": site_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "cause_code": cause_code,
                "affected_device_count": affected_device_count,
            }
        )

    records.sort(key=lambda r: r["start_ts"])
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
        "--out", type=Path, default=Path("tests/fixtures/access_requests/outage.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--num-records", type=int, default=GeneratorConfig().num_records)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, num_records=args.num_records)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} outage records to {out_path}")


if __name__ == "__main__":
    _cli()
