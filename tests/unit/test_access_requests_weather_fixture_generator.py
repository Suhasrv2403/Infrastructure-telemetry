"""Tests for the synthetic weather fixture generator (P2-02's own substitute source - see
weather.py's ASSUMED SCHEMA docstring for why it isn't part of the P0-11 access-request set).

These test the *generator*, not a real weather-provider schema - there is no real schema to
compare against. What matters here: deterministic output, the assumed dense hourly-grid shape
(one row per site per hour, not a sparse event feed like this package's other generators), and
that site_id values overlap with tests/fixtures/generators/supercharger.py's own scheme.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json

from tests.fixtures.generators.access_requests._common import (
    MS_PER_S,
    S_PER_DAY,
    SYNTHETIC_NOW_MS,
)
from tests.fixtures.generators.access_requests.weather import (
    NUM_SITES,
    GeneratorConfig,
    generate,
    write,
)


def test_generate_is_deterministic():
    a = generate(GeneratorConfig(seed=42, lookback_days=5))
    b = generate(GeneratorConfig(seed=42, lookback_days=5))
    assert json.dumps(a.records, sort_keys=True) == json.dumps(b.records, sort_keys=True)


def test_different_seed_changes_output():
    a = generate(GeneratorConfig(seed=1, lookback_days=5))
    b = generate(GeneratorConfig(seed=2, lookback_days=5))
    assert json.dumps(a.records, sort_keys=True) != json.dumps(b.records, sort_keys=True)


def test_schema_and_types():
    generated = generate(GeneratorConfig(lookback_days=3))
    required_keys = {
        "site_id",
        "observation_ts",
        "temperature_c",
        "precipitation_mm",
        "wind_speed_kmh",
    }
    for record in generated.records:
        assert required_keys == set(record.keys())
        assert isinstance(record["observation_ts"], int)
        assert isinstance(record["temperature_c"], float)
        assert isinstance(record["precipitation_mm"], float)
        assert isinstance(record["wind_speed_kmh"], float)
        assert record["precipitation_mm"] >= 0.0
        assert record["wind_speed_kmh"] >= 0.0


def test_one_row_per_site_per_hour_dense_grid():
    """Unlike this package's event-style generators (rma/tickets/dispatch/outage - one row per
    case/ticket/dispatch/outage), weather is a dense grid: every site gets exactly one
    observation every hour across the whole lookback window (see module docstring)."""
    config = GeneratorConfig(lookback_days=3)
    generated = generate(config)

    expected_hours = config.lookback_days * 24
    assert len(generated.records) == expected_hours * NUM_SITES

    by_site: dict[str, set[int]] = {}
    for record in generated.records:
        by_site.setdefault(record["site_id"], set()).add(record["observation_ts"])

    assert set(by_site.keys()) == {f"site-{i:03d}" for i in range(NUM_SITES)}
    for site_id, timestamps in by_site.items():
        assert len(timestamps) == expected_hours, f"{site_id} missing hourly observations"


def test_site_ids_overlap_with_supercharger_scheme():
    generated = generate(GeneratorConfig(lookback_days=2))
    valid_sites = {f"site-{i:03d}" for i in range(NUM_SITES)}
    for record in generated.records:
        assert record["site_id"] in valid_sites


def test_observations_are_hourly_and_within_lookback_window():
    config = GeneratorConfig(lookback_days=4, now_ms=SYNTHETIC_NOW_MS)
    generated = generate(config)
    earliest_ms = config.now_ms - config.lookback_days * S_PER_DAY * MS_PER_S

    timestamps = sorted({r["observation_ts"] for r in generated.records})
    assert timestamps[0] == earliest_ms
    assert timestamps[-1] < config.now_ms
    for a, b in itertools.pairwise(timestamps):
        assert b - a == 3600 * MS_PER_S


def test_records_sorted_by_observation_ts_then_site():
    generated = generate(GeneratorConfig(lookback_days=2))
    keys = [(r["observation_ts"], r["site_id"]) for r in generated.records]
    assert keys == sorted(keys)


def test_at_least_some_precipitation_over_a_long_enough_window():
    generated = generate(GeneratorConfig(lookback_days=14))
    assert any(r["precipitation_mm"] > 0 for r in generated.records)
    assert any(r["precipitation_mm"] == 0 for r in generated.records)


def test_write_roundtrip(tmp_path):
    generated = generate(GeneratorConfig(lookback_days=1))
    out_path = write(generated, tmp_path / "weather.jsonl")
    lines = out_path.read_text().splitlines()
    assert len(lines) == len(generated.records)
    for line in lines:
        json.loads(line)  # true JSONL, one object per line


def test_observation_ts_hour_matches_utc_datetime_decomposition():
    """Sanity check that observation_ts really is UTC epoch ms, matching this package's
    convention elsewhere (see _common.py's SYNTHETIC_NOW_MS comment)."""
    generated = generate(GeneratorConfig(lookback_days=1))
    for record in generated.records[:24]:
        as_dt = dt.datetime.fromtimestamp(record["observation_ts"] / 1000, tz=dt.timezone.utc)
        assert as_dt.minute == 0
        assert as_dt.second == 0
