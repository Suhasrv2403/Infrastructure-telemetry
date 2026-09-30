"""Synthetic fixture generator for RMA (return/warranty) records.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
No real RMA/warranty system schema was consulted to build this generator: P0-11's access
request for RMA data (`docs/access-requests/P0-11-data-access-requests.md`, section 1) has
never been sent, and no sample extract has ever been received. Every field below is this
project's own best guess, based only on that request document's field wishlist - not a real
system's actual schema, because no real system here has been observed. When access is
eventually granted, apply the P0-11 doc's own instruction: "When a sample extract arrives,
hand it to the senior DS to validate schema and quality" against this assumed shape, expecting
it to be wrong in some particulars (field names, extra fields, a `credited` resolution outcome
this generator deliberately excludes - see RESOLUTIONS below).

Why this exists
----------------
Per explicit project-owner direction, this generator is a synthetic substitute for P0-11's
real "sample extracts loaded" acceptance bar, so Phase 2 work doesn't wait indefinitely on a
human-only access request. It specifically unblocks:
- **P2-02** (ongoing RMA ingestion as a dated feed) - treat one `generate()` call as one
  ingest run's worth of records; vary `seed`/`config.now_ms` to simulate later runs arriving.
- **P2-12** (detector backtest ground truth) - `opened_ts` stands in for "when the fleet found
  out about a real failure"; a backtest can check whether a detector would have flagged
  `device_id` before that timestamp, and by how much lead time.

Rates below (reason-code mix, resolution mix, how long a case stays open) are invented for
schema/shape testing, not fit to any real failure distribution - do not use them as fleet
failure-rate assumptions anywhere outside a test.

Output shape
------------
One JSON object per RMA case: `rma_id`, `device_id`, `site_id`, `opened_ts`, `closed_ts`
(``None`` while still open), `reason_code`, `resolution` (``None`` while still open - a case
isn't resolved until it's closed). All `*_ts` fields are epoch milliseconds (int), matching
supercharger.py's timestamp convention even though the field names here follow the P0-11 doc's
wording (`opened_ts`, not `opened_ts_ms`). `device_id`/`site_id` are drawn from the same scheme
as `tests/fixtures/generators/supercharger.py`'s fixtures (see `_common.py`), so an RMA record
and a Supercharger telemetry fixture can be joined on `device_id` in cross-source tests.
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

REASON_CODES = [
    "cell_failure",
    "thermal_fault",
    "comms_failure",
    "physical_damage",
    "no_fault_found",
    "other",
]

# Matches the P0-11 draft request's wording ("replaced, repaired, credited, no fault found")
# minus "credited": that's a billing/finance outcome, not a device outcome, and the ticket's
# own field wishlist for this repo's consumers (P2-02/P2-12) only names
# repaired/replaced/no-fault-found. Left out of this ASSUMED schema on that basis.
RESOLUTIONS = ["repaired", "replaced", "no_fault_found"]


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    num_records: int = 150
    seed: int = 1337
    devices_per_firmware: int = 4
    lookback_days: int = 730
    now_ms: int = SYNTHETIC_NOW_MS
    still_open_rate: float = 0.05
    min_open_duration_days: int = 1
    max_open_duration_days: int = 45


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
        device = rng.choice(devices)
        opened_ts = rng.randint(earliest_open_ms, config.now_ms)

        still_open = rng.random() < config.still_open_rate
        closed_ts = None
        resolution = None
        if not still_open:
            duration_days = rng.randint(
                config.min_open_duration_days, config.max_open_duration_days
            )
            closed_ts = opened_ts + duration_days * S_PER_DAY * MS_PER_S
            closed_ts = min(closed_ts, config.now_ms)
            if closed_ts <= opened_ts:
                closed_ts = opened_ts + MS_PER_S
            resolution = rng.choice(RESOLUTIONS)
            stats[f"resolution_{resolution}"] += 1
        else:
            stats["still_open"] += 1

        reason_code = rng.choice(REASON_CODES)
        stats[f"reason_{reason_code}"] += 1
        stats["records_total"] += 1

        records.append(
            {
                "rma_id": f"rma-{i:06d}",
                "device_id": device.device_id,
                "site_id": device.site_id,
                "opened_ts": opened_ts,
                "closed_ts": closed_ts,
                "reason_code": reason_code,
                "resolution": resolution,
            }
        )

    records.sort(key=lambda r: r["opened_ts"])
    return GeneratedRecords(config, records, stats)


def write(generated: GeneratedRecords, out_path: Path) -> Path:
    """Write one JSON object per line (true JSONL, matching supercharger.py's convention)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for record in generated.records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    return out_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/access_requests/rma.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--num-records", type=int, default=GeneratorConfig().num_records)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, num_records=args.num_records)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} RMA records to {out_path}")


if __name__ == "__main__":
    _cli()
