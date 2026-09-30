"""External source ingest: dispatch, outage, weather, RMA and tickets. (P2-02)

Ticket: P2-02 ("Ingest external sources: dispatch, outages, weather, RMA and tickets").
Depends on P0-11 ("Access to RMA, tickets, dispatch, outage, provisioning data"), which is
still "To do" - see docs/access-requests/P0-11-data-access-requests.md. Per the project-owner
decision that also unblocked P2-01, this module is built against the five synthetic fixture
generators in tests/fixtures/generators/access_requests/ (rma, tickets, dispatch, outage, and
this ticket's own weather.py addition) rather than waiting on that real access indefinitely.
Nothing here reads or claims real RMA/ticket/dispatch/outage/weather data; see each fixture
module's own ASSUMED SCHEMA docstring for what's still unvalidated.

Why "pipeline/stage3_enrich/" rather than a new top-level "pipeline/external_ingest/"
--------------------------------------------------------------------------------------
These five sources aren't Stage 0/1/2 telemetry - they don't go through parsing, timestamp
sanity, or canonicalization. But CLAUDE.md's own stage description puts "device history
dimension" and (via P3-05, which explicitly depends on this ticket) "mode-aware expected
counts" under Stage 3 Enrich / telemetry health, and P2-04 (event reconciliation, also
downstream of this ticket) is explicitly an enrichment of detected events against these same
sources. Landing them under stage3_enrich keeps them next to their actual consumers instead of
inventing a second top-level ingest tree that would only ever have one ticket's worth of
content. If a sixth external source shows up later and this stops feeling like "enrichment
inputs" and starts feeling like its own subsystem, splitting out pipeline/external_ingest/ at
that point is a small, mechanical move - nothing here depends on the current location beyond
its own import path.

What "load_day" actually does
------------------------------
`load_day(source, for_date, store=...)` selects every record from that source's fixture
generator whose *entry timestamp* falls on the UTC calendar date `for_date`, and merges each
one into `store` keyed on a per-source natural key (see SOURCE_SPECS below) plus a content
hash - the same MERGE-not-append idempotency pattern CLAUDE.md invariant 2 and P1-06 apply to
Stage 1 telemetry (`(device_id, device_ts, payload_hash)`), adapted here to
`(source, natural_key, payload_hash)`. Calling `load_day` twice for the same source and date
is a no-op the second time (every record already present with an unchanged payload_hash);
calling it for several different dates never produces two rows for what MERGE considers "the
same" record. See `ExternalSourceStore.merge()` for the three-way insert/update/unchanged
outcome this mirrors from a real `MERGE INTO ... table`.

"Entry timestamp" is itself a modeling choice, and an important limitation to flag: each
fixture generator produces one flat, already-final record per case (an RMA's `resolution` and
`closed_ts`, if any, are baked in at generation time, not layered on later) - these generators
don't simulate a record being re-emitted with updated fields on a later day, the way a real
ticketing system's daily feed might re-send an already-open ticket once it closes. `load_day`
therefore only ever sees "insert" and "unchanged" outcomes against this synthetic data in
practice (the "update" branch in `ExternalSourceStore.merge()` exists for the general case -
a real record changing state between fixture-generation runs - but nothing here currently
exercises it, since the fixtures are deterministic and regenerated identically from the same
seed/config on every call). Each source's entry timestamp field is the one closest to "when
this record enters that source's daily/weekly feed" per the P0-11 doc's own request wording:
`opened_ts` for RMA and tickets, `scheduled_ts` for dispatch, `start_ts` for outage, and
`observation_ts` for weather (naturally already per-hour, not per-case).

Freshness checks
-----------------
`check_freshness(source, as_of, store=...)` reports whether `store`'s most recent
*successfully loaded* date for that source is within `max_staleness_days` of `as_of` -
analogous to how this repo's planned Stage 2 completeness/lateness sidecar (P1-11) and
telemetry-health last-seen snapshot (P1-12) report gaps and staleness without needing a real
scheduler to invoke them; both are still "To do" at the commit this ticket is built against
(this repo's pipeline/stage2_canonical/ and pipeline/telemetry_health/ are README-only stubs
here), so this module's freshness check is written against CLAUDE.md's stage descriptions and
the P1-11/P1-12 naming convention rather than against actual precedent code. A source that has
never been loaded is always reported not-fresh, regardless of `max_staleness_days`.

READ THIS: what "daily loads" does and does NOT mean here
------------------------------------------------------------
This module's logic can be invoked once per calendar day and will behave correctly when it
is - `test_daily_semantics_across_simulated_days` in the test suite proves that by calling
`load_day` for three sequential simulated days in one test run and checking both idempotency
and freshness after each call, the same technique this repo's P1-12 ticket used to test
"recurring, 15-minute" behavior without a real deployed scheduler. But "correct when invoked
daily" and "actually invoked daily" are different claims. Nothing in this module, this repo,
or this sandbox environment triggers `load_day`/`check_freshness` on any schedule - there is
no cron job, no Dagster schedule or sensor, and no deployed orchestrator wired to this code.
Standing this up for real would need, at minimum: a Dagster schedule (or sensor) analogous to
pipeline/stage0_landing/dagster_assets.py's partitioned asset, but running once daily against
whatever real system eventually replaces each fixture generator; real extracts (or an API
client) for each of the five sources, since everything here still reads from
tests/fixtures/generators/access_requests/; and a place to actually persist ExternalSourceStore
state across runs (it is in-memory only - see that class's own docstring) rather than
recomputing from scratch inside a single process/test. Until that scheduler exists, treat this
ticket's "Daily loads with freshness checks" done-when as satisfied for the *logic*, not for
the *deployment* - see docs/profiling/P2-02-external-source-ingest-notes.md for the fuller,
self-critical version of this same point, mirroring how P1-12's own docstring named its gap.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Callable
from typing import Any

from tests.fixtures.generators.access_requests import dispatch as _dispatch_fixtures
from tests.fixtures.generators.access_requests import outage as _outage_fixtures
from tests.fixtures.generators.access_requests import rma as _rma_fixtures
from tests.fixtures.generators.access_requests import tickets as _tickets_fixtures
from tests.fixtures.generators.access_requests import weather as _weather_fixtures


def _payload_hash(record: dict[str, Any]) -> str:
    """Content hash of one record, used as the second half of this module's MERGE key
    (source, natural_key, payload_hash) - mirrors Stage 1's payload_hash role (CLAUDE.md
    invariant 2) applied to these five sources instead of telemetry readings."""
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode("utf-8")).hexdigest()


def _date_of(ts_ms: int) -> dt.date:
    """The UTC calendar date a millisecond epoch timestamp falls on."""
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date()


def _key_rma(record: dict[str, Any]) -> str:
    return record["rma_id"]


def _key_tickets(record: dict[str, Any]) -> str:
    return record["ticket_id"]


def _key_dispatch(record: dict[str, Any]) -> str:
    return record["dispatch_id"]


def _key_outage(record: dict[str, Any]) -> str:
    return record["outage_id"]


def _key_weather(record: dict[str, Any]) -> str:
    # Weather has no per-record id of its own (see weather.py's docstring) - (site_id,
    # observation_ts) is the natural key: this generator's dense hourly grid guarantees at
    # most one record per site per hour, so that pair is always unique.
    return f"{record['site_id']}|{record['observation_ts']}"


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    """One external source this module knows how to load: which fixture-generator module
    stands in for it, which of its timestamp fields defines "the day a record belongs to" for
    load_day's purposes, and how to compute its MERGE natural key."""

    name: str
    module: Any
    date_field: str
    key_fn: Callable[[dict[str, Any]], str]


SOURCE_SPECS: dict[str, SourceSpec] = {
    "rma": SourceSpec("rma", _rma_fixtures, "opened_ts", _key_rma),
    "tickets": SourceSpec("tickets", _tickets_fixtures, "opened_ts", _key_tickets),
    "dispatch": SourceSpec("dispatch", _dispatch_fixtures, "scheduled_ts", _key_dispatch),
    "outage": SourceSpec("outage", _outage_fixtures, "start_ts", _key_outage),
    "weather": SourceSpec("weather", _weather_fixtures, "observation_ts", _key_weather),
}


class UnknownSourceError(ValueError):
    """Raised when a source name isn't one of SOURCE_SPECS' five keys."""


def _spec_for(source: str) -> SourceSpec:
    try:
        return SOURCE_SPECS[source]
    except KeyError:
        raise UnknownSourceError(
            f"unknown external source {source!r}; expected one of {sorted(SOURCE_SPECS)}"
        ) from None


class ExternalSourceStore:
    """In-memory MERGE-based landing store for the five external sources.

    Every write goes through `merge()`, keyed on (source, natural_key), comparing payload_hash
    to report the same three-way insert/update/unchanged outcome a real `MERGE INTO ...` would
    report via its row counts. This is an in-memory stand-in for what would be a warehouse
    table (Iceberg, matching this repo's own stack - see CLAUDE.md) in production, exactly as
    tests/unit/test_stage0_capture.py mocks S3 with moto rather than requiring LocalStack: what
    P2-02 needs proven is the merge/dedup/freshness *logic*, not a particular storage engine.
    Swapping this for a real table would keep the same public API used by load_day and
    check_freshness (merge, record_successful_load, latest_loaded_date, records_for).

    Also unlike a real table, this store's state lives only for the lifetime of the Python
    object holding it - nothing here persists across process runs. A production deployment
    would need that persistence (the real MERGE target table); see this module's own docstring
    and docs/profiling/P2-02-external-source-ingest-notes.md for what else "daily loads" would
    need beyond this class.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        self._loaded_dates: dict[str, set[dt.date]] = {}

    def merge(self, source: str, key: str, payload_hash: str, record: dict[str, Any]) -> str:
        """Insert, update, or no-op one record at (source, key), keyed additionally by
        payload_hash. Returns "inserted", "updated", or "unchanged"."""
        table = self._records.setdefault(source, {})
        existing = table.get(key)
        if existing is None:
            table[key] = (payload_hash, record)
            return "inserted"
        existing_hash, _existing_record = existing
        if existing_hash == payload_hash:
            return "unchanged"
        table[key] = (payload_hash, record)
        return "updated"

    def record_successful_load(self, source: str, for_date: dt.date) -> None:
        """Mark `for_date` as successfully loaded for `source`, even if zero records matched
        that date - an empty day is a legitimate, reconciled load outcome, not a failure, and
        must still advance freshness (a source with no dispatches yesterday isn't "stale")."""
        self._loaded_dates.setdefault(source, set()).add(for_date)

    def latest_loaded_date(self, source: str) -> dt.date | None:
        dates = self._loaded_dates.get(source)
        return max(dates) if dates else None

    def loaded_dates(self, source: str) -> frozenset[dt.date]:
        return frozenset(self._loaded_dates.get(source, ()))

    def records_for(self, source: str) -> list[dict[str, Any]]:
        return [record for _hash, record in self._records.get(source, {}).values()]

    def record_count(self, source: str) -> int:
        return len(self._records.get(source, {}))


@dataclasses.dataclass(frozen=True)
class LoadResult:
    """Summary of one load_day() call, used both to log and to assert against in tests."""

    source: str
    for_date: dt.date
    records_seen: int
    inserted: int
    updated: int
    unchanged: int


def load_day(
    source: str,
    for_date: dt.date,
    *,
    store: ExternalSourceStore,
    fixture_config: Any | None = None,
) -> LoadResult:
    """Load `source`'s records for the UTC calendar date `for_date` into `store`.

    `fixture_config`, when given, is passed straight to that source's fixture-generator
    `GeneratorConfig` (e.g. `rma.GeneratorConfig(seed=..., lookback_days=...)`) so callers -
    chiefly tests - can control record volume and date range; the default is that source's own
    `GeneratorConfig()` defaults. Idempotent: calling this twice for the same
    (source, for_date, fixture_config) merges the identical record set again, and every record
    reports "unchanged" the second time (see ExternalSourceStore.merge).

    Regenerates that source's full deterministic corpus on every call and filters it down to
    `for_date` - cheap here because these are synthetic in-memory generators, but a real
    implementation would instead query the real extract/API for just that date rather than
    regenerating everything, the same caveat pipeline/stage0_landing/dagster_assets.py's own
    docstring makes about its synthetic source and Dagster partition caching.
    """
    spec = _spec_for(source)
    config = fixture_config if fixture_config is not None else spec.module.GeneratorConfig()
    all_records = spec.module.generate(config).records

    day_records = [r for r in all_records if _date_of(r[spec.date_field]) == for_date]

    inserted = updated = unchanged = 0
    for record in day_records:
        key = spec.key_fn(record)
        outcome = store.merge(source, key, _payload_hash(record), record)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    store.record_successful_load(source, for_date)

    return LoadResult(
        source=source,
        for_date=for_date,
        records_seen=len(day_records),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
    )


@dataclasses.dataclass(frozen=True)
class FreshnessStatus:
    """Whether `source`'s most recent successfully loaded date is within its staleness
    budget, as of `as_of`. `latest_loaded_date` is None iff `source` has never been loaded
    into `store` at all, in which case `is_fresh` is always False regardless of
    `max_staleness_days` - "never loaded" is not a 0-day-stale special case, it's a distinct,
    worse condition."""

    source: str
    as_of: dt.date
    latest_loaded_date: dt.date | None
    staleness_days: int | None
    max_staleness_days: int
    is_fresh: bool


def check_freshness(
    source: str,
    as_of: dt.date,
    *,
    store: ExternalSourceStore,
    max_staleness_days: int = 1,
) -> FreshnessStatus:
    """Report freshness for one source. See FreshnessStatus for field meaning.

    `staleness_days` can come out negative only if `store` has a load recorded for a date
    after `as_of` (e.g. a test loaded a "future" day relative to the `as_of` it later checks) -
    that's trivially fresh (staleness_days <= max_staleness_days still holds), not an error;
    real usage always has `as_of` be "today" and never loads a day past it.
    """
    _spec_for(source)  # validates `source`, raising UnknownSourceError early like load_day
    latest = store.latest_loaded_date(source)
    if latest is None:
        return FreshnessStatus(
            source=source,
            as_of=as_of,
            latest_loaded_date=None,
            staleness_days=None,
            max_staleness_days=max_staleness_days,
            is_fresh=False,
        )
    staleness_days = (as_of - latest).days
    return FreshnessStatus(
        source=source,
        as_of=as_of,
        latest_loaded_date=latest,
        staleness_days=staleness_days,
        max_staleness_days=max_staleness_days,
        is_fresh=staleness_days <= max_staleness_days,
    )


def check_all_freshness(
    as_of: dt.date,
    *,
    store: ExternalSourceStore,
    max_staleness_days: int = 1,
) -> dict[str, FreshnessStatus]:
    """check_freshness() for all five known sources at once, keyed by source name."""
    return {
        name: check_freshness(name, as_of, store=store, max_staleness_days=max_staleness_days)
        for name in SOURCE_SPECS
    }
