"""Synthetic fixture generator for site-level weather observations.

ASSUMED SCHEMA - READ THIS FIRST
---------------------------------
Unlike this package's other five generators (`rma`, `tickets`, `dispatch`, `outage`,
`provisioning`), weather has no corresponding section in
`docs/access-requests/P0-11-data-access-requests.md` - that document only ever named RMA,
tickets, dispatch, outage and provisioning as the five sources needing a human-sent access
request to an *internal* system owner. Weather is different: P2-02's own ticket wording
("dispatch, outages, weather, RMA and tickets") is the only place this repo names it, and the
most plausible real source for fleet-wide weather observations is a commercial or public
provider (e.g. a paid weather API, NOAA/NWS) reached via an API key and a data-sharing
agreement, not an internal-system access request that would fit the P0-11 doc's template. No
such request has been drafted, because that's a separate, un-scoped piece of work this ticket
does not attempt - this generator exists purely so P2-02's ingestion logic has a fifth source
to build and test against, per the same project-owner direction that produced the other four
P0-11 substitutes. Every field below is this project's own best guess at a minimal weather
schema, not an observed provider's actual response shape. The riskiest assumption here is
grain: real weather providers vary widely (point observations at a station, gridded
reanalysis, per-site forecasts), and this generator picks the simplest form P2-02 and P3-05
actually need - one row per site per hour, i.e. a dense hourly grid, not a sparse event feed
like the other four sources in this package. Validate this against whatever real provider is
eventually chosen; expect the grain, units, and field names to need correction.

Why this exists
----------------
Per the same explicit project-owner direction that produced this package's other four
generators, this is a synthetic substitute for weather's own "sample extract loaded"
acceptance bar. It specifically unblocks:
- **P2-02** (ongoing weather ingestion as a dated feed, alongside dispatch/outage/RMA/tickets).
- **P3-05** (mode-aware expected counts) - weather (rain, extreme wind/temperature) is a
  plausible corroborating signal for why a device's session counts or reporting behavior
  looked different on a given day, similar to how outage records serve that role.

Rates and formulas below (diurnal temperature swing, precipitation probability, wind noise)
are invented for schema/shape testing, not fit to any real climate data.

Output shape
------------
One JSON object per (site, hour): `site_id`, `observation_ts`, `temperature_c`,
`precipitation_mm`, `wind_speed_kmh`. `observation_ts` is epoch milliseconds (int), on an
hourly grid (see `GeneratorConfig.hourly_step_hours`). `temperature_c` and `wind_speed_kmh`
are floats rounded to 1 decimal place; `precipitation_mm` is a float rounded to 2 decimal
places, 0.0 on hours with no precipitation. `site_id` is drawn from the same site pool as
`tests/fixtures/generators/supercharger.py`'s fixtures (see `_common.py`) - weather is a
site-level phenomenon, so unlike `rma`/`tickets`/`dispatch` this generator has no `device_id`
field at all, only `site_id` (matching `outage.py`'s site-only grain).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from tests.fixtures.generators.access_requests._common import (
    MS_PER_S,
    S_PER_DAY,
    SYNTHETIC_NOW_MS,
)

# Same site pool supercharger.py's `index % 6` scheme produces, independent of
# devices_per_firmware (weather is a site-level phenomenon, not tied to one device's
# firmware) - mirrors outage.py's identical NUM_SITES convention.
NUM_SITES = 6

MS_PER_HOUR = 3600 * MS_PER_S


@dataclasses.dataclass
class GeneratorConfig:
    """Tunable rates. Defaults are illustrative, not measured (see module docstring)."""

    seed: int = 1337
    lookback_days: int = 14
    now_ms: int = SYNTHETIC_NOW_MS
    hourly_step_hours: int = 1
    base_temp_c: float = 18.0
    temp_diurnal_amplitude_c: float = 8.0
    temp_site_spread_c: float = 6.0
    temp_noise_c: float = 2.0
    precipitation_rate: float = 0.12
    max_precipitation_mm: float = 8.0
    wind_base_kmh: float = 10.0
    wind_noise_kmh: float = 8.0


@dataclasses.dataclass
class GeneratedRecords:
    config: GeneratorConfig
    records: list[dict[str, Any]]
    stats: Counter


def _temperature_c(rng: random.Random, config: GeneratorConfig, ts_ms: int, site_index: int) -> float:
    hour_of_day = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).hour
    # Diurnal cycle peaking mid-afternoon (~15:00 UTC), not a realistic seasonal model - just
    # enough shape for a plausible-looking synthetic series (see module docstring).
    diurnal = config.temp_diurnal_amplitude_c * math.sin(2 * math.pi * (hour_of_day - 9) / 24)
    site_offset = config.temp_site_spread_c * ((site_index / max(NUM_SITES - 1, 1)) - 0.5)
    noise = rng.uniform(-config.temp_noise_c, config.temp_noise_c)
    return round(config.base_temp_c + diurnal + site_offset + noise, 1)


def _precipitation_mm(rng: random.Random, config: GeneratorConfig) -> float:
    if rng.random() < config.precipitation_rate:
        return round(rng.uniform(0.1, config.max_precipitation_mm), 2)
    return 0.0


def _wind_speed_kmh(rng: random.Random, config: GeneratorConfig) -> float:
    return round(max(0.0, config.wind_base_kmh + rng.uniform(-config.wind_noise_kmh, config.wind_noise_kmh)), 1)


def generate(config: GeneratorConfig | None = None) -> GeneratedRecords:
    config = config or GeneratorConfig()
    rng = random.Random(config.seed)
    stats: Counter = Counter()

    earliest_ms = config.now_ms - config.lookback_days * S_PER_DAY * MS_PER_S
    step_ms = config.hourly_step_hours * MS_PER_HOUR
    num_steps = max(0, (config.now_ms - earliest_ms) // step_ms)

    records: list[dict[str, Any]] = []
    for step in range(num_steps):
        observation_ts = earliest_ms + step * step_ms
        for site_index in range(NUM_SITES):
            site_id = f"site-{site_index:03d}"
            precipitation_mm = _precipitation_mm(rng, config)
            record = {
                "site_id": site_id,
                "observation_ts": observation_ts,
                "temperature_c": _temperature_c(rng, config, observation_ts, site_index),
                "precipitation_mm": precipitation_mm,
                "wind_speed_kmh": _wind_speed_kmh(rng, config),
            }
            if precipitation_mm > 0:
                stats["hours_with_precipitation"] += 1
            stats["records_total"] += 1
            records.append(record)

    records.sort(key=lambda r: (r["observation_ts"], r["site_id"]))
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
        "--out", type=Path, default=Path("tests/fixtures/access_requests/weather.jsonl")
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--lookback-days", type=int, default=GeneratorConfig().lookback_days)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, lookback_days=args.lookback_days)
    generated = generate(config)
    out_path = write(generated, args.out)
    print(f"Wrote {len(generated.records)} weather records to {out_path}")


if __name__ == "__main__":
    _cli()
