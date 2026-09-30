"""Tests for pipeline/stage3_enrich/external_sources.py (P2-02: ingest dispatch, outages,
weather, RMA and tickets - "Daily loads with freshness checks").

These exercise the actual ingest *logic* against the synthetic fixture generators in
tests/fixtures/generators/access_requests/ (see that package's README and each generator's own
ASSUMED SCHEMA docstring). Three things matter here, matching the module's own docstring:

1. load_day() is idempotent - loading the same (source, date) twice never duplicates records.
2. load_day() called across several distinct simulated days never produces overlapping
   records, and check_freshness() tracks the latest day actually loaded.
3. check_freshness() correctly distinguishes never-loaded, fresh, and stale sources.

See external_sources.py's own module docstring (the "READ THIS" section) for why proving (1)
and (2) in a single test run is evidence the *logic* is correct when invoked daily, not
evidence that anything in this repo actually invokes it daily - no scheduler is under test
here, because none exists yet.
"""
from __future__ import annotations

import datetime as dt

import pytest

from pipeline.stage3_enrich.external_sources import (
    SOURCE_SPECS,
    ExternalSourceStore,
    UnknownSourceError,
    check_all_freshness,
    check_freshness,
    load_day,
)
from tests.fixtures.generators.access_requests import dispatch as dispatch_fixtures
from tests.fixtures.generators.access_requests import outage as outage_fixtures
from tests.fixtures.generators.access_requests import rma as rma_fixtures
from tests.fixtures.generators.access_requests import tickets as tickets_fixtures
from tests.fixtures.generators.access_requests import weather as weather_fixtures
from tests.fixtures.generators.access_requests._common import SYNTHETIC_NOW_MS

# Small, concentrated fixture configs so a handful of calendar days each have a healthy number
# of records. The default GeneratorConfig() for the event-style sources spreads ~100-200
# records over a 730-day lookback, so thinly that any single day is likely empty - that would
# make "assert records_seen > 0 for this day" flaky. Concentrating lookback_days keeps these
# tests fast, dense, and deterministic without changing anything about load_day itself.
FIXTURE_CONFIGS: dict[str, object] = {
    "rma": rma_fixtures.GeneratorConfig(seed=7, num_records=400, lookback_days=4),
    "tickets": tickets_fixtures.GeneratorConfig(seed=7, num_records=400, lookback_days=4),
    "dispatch": dispatch_fixtures.GeneratorConfig(seed=7, num_records=400, lookback_days=4),
    "outage": outage_fixtures.GeneratorConfig(seed=7, num_records=400, lookback_days=4),
    "weather": weather_fixtures.GeneratorConfig(seed=7, lookback_days=4),
}

SOURCES = sorted(SOURCE_SPECS)


def _dates_with_records(source: str) -> list[dt.date]:
    """The distinct UTC calendar dates FIXTURE_CONFIGS[source] actually produces records for,
    sorted ascending - computed from the real generator rather than hardcoded, so these tests
    keep working even if a generator's rng sequence or defaults change."""
    spec = SOURCE_SPECS[source]
    config = FIXTURE_CONFIGS[source]
    records = spec.module.generate(config).records
    dates = {
        dt.datetime.fromtimestamp(r[spec.date_field] / 1000, tz=dt.timezone.utc).date()
        for r in records
    }
    return sorted(dates)


@pytest.mark.parametrize("source", SOURCES)
def test_load_day_is_idempotent(source):
    store = ExternalSourceStore()
    dates = _dates_with_records(source)
    assert dates, f"fixture config for {source!r} produced no records at all"
    for_date = dates[len(dates) // 2]

    first = load_day(source, for_date, store=store, fixture_config=FIXTURE_CONFIGS[source])
    assert first.records_seen > 0
    assert first.inserted == first.records_seen
    assert first.updated == 0
    assert first.unchanged == 0

    count_after_first = store.record_count(source)

    second = load_day(source, for_date, store=store, fixture_config=FIXTURE_CONFIGS[source])
    assert second.records_seen == first.records_seen
    assert second.inserted == 0
    assert second.unchanged == second.records_seen
    assert store.record_count(source) == count_after_first, "re-loading a day duplicated records"


@pytest.mark.parametrize("source", SOURCES)
def test_daily_semantics_across_simulated_days(source):
    """The core 'daily loads' proof: call load_day for three sequential simulated days in one
    test run, the same technique this repo's P1-12 ticket ("recurring", 15-minute logic) used
    to test recurring behavior without a real deployed scheduler. Asserts no duplication
    across days, and that freshness tracks the latest loaded day at each step."""
    store = ExternalSourceStore()
    config = FIXTURE_CONFIGS[source]
    dates = _dates_with_records(source)
    assert len(dates) >= 3, f"need >= 3 distinct dates with records for {source!r}, got {dates}"
    day1, day2, day3 = dates[0], dates[1], dates[2]

    total_records = 0
    for day in (day1, day2, day3):
        result = load_day(source, day, store=store, fixture_config=config)
        total_records += result.records_seen

        status = check_freshness(source, as_of=day, store=store, max_staleness_days=0)
        assert status.latest_loaded_date == day
        assert status.is_fresh

    assert store.record_count(source) == total_records, (
        "loading three distinct simulated days produced overlapping/duplicated records"
    )

    # Re-loading an earlier day after later days were already loaded must still be a no-op,
    # and must not roll freshness backward.
    replay = load_day(source, day1, store=store, fixture_config=config)
    assert replay.inserted == 0
    assert replay.updated == 0
    assert store.record_count(source) == total_records

    status_after_replay = check_freshness(source, as_of=day3, store=store, max_staleness_days=0)
    assert status_after_replay.latest_loaded_date == day3


def test_different_sources_do_not_share_state():
    store = ExternalSourceStore()
    rma_dates = _dates_with_records("rma")
    outage_dates = _dates_with_records("outage")

    load_day("rma", rma_dates[0], store=store, fixture_config=FIXTURE_CONFIGS["rma"])
    load_day("outage", outage_dates[0], store=store, fixture_config=FIXTURE_CONFIGS["outage"])

    assert store.record_count("rma") > 0
    assert store.record_count("outage") > 0
    assert store.latest_loaded_date("tickets") is None  # never loaded
    assert store.latest_loaded_date("weather") is None  # never loaded


def test_load_day_empty_day_still_advances_freshness():
    """A calendar day with zero matching records is still a legitimate, reconciled load - it
    must not be treated as a missing/failed load for freshness purposes (an outage-free day
    for a real feed isn't "stale data", it's "nothing happened")."""
    store = ExternalSourceStore()
    config = rma_fixtures.GeneratorConfig(seed=7, num_records=5, lookback_days=1)
    now_date = dt.datetime.fromtimestamp(SYNTHETIC_NOW_MS / 1000, tz=dt.timezone.utc).date()
    far_future_day = now_date + dt.timedelta(days=100)  # well outside the 1-day lookback

    result = load_day("rma", far_future_day, store=store, fixture_config=config)
    assert result.records_seen == 0
    assert result.inserted == 0

    status = check_freshness("rma", as_of=far_future_day, store=store, max_staleness_days=0)
    assert status.latest_loaded_date == far_future_day
    assert status.is_fresh


def test_check_freshness_never_loaded_is_not_fresh():
    store = ExternalSourceStore()
    status = check_freshness("dispatch", as_of=dt.date(2026, 6, 1), store=store)
    assert status.latest_loaded_date is None
    assert status.staleness_days is None
    assert not status.is_fresh


def test_check_freshness_flags_stale_source():
    store = ExternalSourceStore()
    config = FIXTURE_CONFIGS["outage"]
    dates = _dates_with_records("outage")
    load_day("outage", dates[0], store=store, fixture_config=config)

    far_later = dates[0] + dt.timedelta(days=10)
    status = check_freshness("outage", as_of=far_later, store=store, max_staleness_days=1)
    assert status.staleness_days == 10
    assert not status.is_fresh


def test_check_freshness_within_window_is_fresh():
    store = ExternalSourceStore()
    config = FIXTURE_CONFIGS["tickets"]
    dates = _dates_with_records("tickets")
    load_day("tickets", dates[0], store=store, fixture_config=config)

    next_day = dates[0] + dt.timedelta(days=1)
    status = check_freshness("tickets", as_of=next_day, store=store, max_staleness_days=1)
    assert status.staleness_days == 1
    assert status.is_fresh


def test_check_all_freshness_covers_all_five_sources():
    store = ExternalSourceStore()
    statuses = check_all_freshness(dt.date(2026, 6, 1), store=store)
    assert set(statuses) == set(SOURCE_SPECS) == {
        "rma", "tickets", "dispatch", "outage", "weather",
    }
    for status in statuses.values():
        assert status.latest_loaded_date is None
        assert not status.is_fresh


def test_load_day_rejects_unknown_source():
    store = ExternalSourceStore()
    with pytest.raises(UnknownSourceError):
        load_day("carrier_pigeon", dt.date(2026, 6, 1), store=store)


def test_check_freshness_rejects_unknown_source():
    store = ExternalSourceStore()
    with pytest.raises(UnknownSourceError):
        check_freshness("carrier_pigeon", dt.date(2026, 6, 1), store=store)


def test_weather_natural_key_dedupes_by_site_and_hour():
    """Weather has no source-native record id (see weather.py's docstring) - the merge key is
    (site_id, observation_ts). Confirm a full day yields NUM_SITES * 24 records and that
    re-loading it doesn't duplicate any (site, hour) pair."""
    num_sites = weather_fixtures.NUM_SITES
    store = ExternalSourceStore()
    dates = _dates_with_records("weather")
    for_date = dates[1]

    result = load_day("weather", for_date, store=store, fixture_config=FIXTURE_CONFIGS["weather"])
    assert result.records_seen == num_sites * 24
    assert result.inserted == num_sites * 24

    again = load_day("weather", for_date, store=store, fixture_config=FIXTURE_CONFIGS["weather"])
    assert again.inserted == 0
    assert again.unchanged == num_sites * 24
    assert store.record_count("weather") == num_sites * 24
