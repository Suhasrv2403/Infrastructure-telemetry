# P2-02 — External source ingest: what's built vs. what "daily" still needs

**Ticket:** P2-02 · Ingest external sources: dispatch, outages, weather, RMA and tickets
**Done when (per the backlog):** "Daily loads with freshness checks."
**Depends on:** P0-11 (still "To do" — see `docs/access-requests/P0-11-data-access-requests.md`)

This note exists for the same reason P1-12's own docstring named its gap honestly instead of
letting a passing test suite imply more than it proves: "the logic is correct when invoked" and
"the thing actually runs on the schedule its name promises" are different claims, and this
ticket's own wording ("**Daily** loads") plausibly asks for the second, not just the first.

## What's built and tested

- `pipeline/stage3_enrich/external_sources.py`:
  - `load_day(source, for_date, store=...)` for all five sources (`rma`, `tickets`,
    `dispatch`, `outage`, `weather`), each filtered by that source's own entry timestamp
    field down to one UTC calendar date.
  - Idempotent MERGE-style loading (`ExternalSourceStore.merge`), keyed on
    `(source, natural_key, payload_hash)` — the same shape as this repo's Stage 1
    `(device_id, device_ts, payload_hash)` MERGE key (CLAUDE.md invariant 2), adapted to these
    five sources' own natural ids (`rma_id`, `ticket_id`, `dispatch_id`, `outage_id`, or
    `(site_id, observation_ts)` for weather, which has no id of its own).
  - `check_freshness(source, as_of, store=...)` / `check_all_freshness(...)`, reporting
    whether each source's most recently *loaded* date is within a configurable staleness
    budget of `as_of`, and treating "never loaded" as its own always-not-fresh state.
- `tests/fixtures/generators/access_requests/weather.py`: a new, sixth synthetic fixture
  generator (site/hour-keyed, unlike this package's other five, which are event-keyed) — see
  its own ASSUMED SCHEMA docstring for why weather has no P0-11 section of its own.
- `tests/unit/test_external_sources_ingest.py`: proves, for every source, that (a) loading the
  same day twice never duplicates records, and (b) loading three sequential *simulated* days
  in one test run — the same "advance a fake clock across several ticks inside one test"
  technique this repo's P1-12 ticket used to test 15-minute "recurring" logic without a real
  scheduler — never produces overlapping records and correctly advances freshness after each
  simulated day.

None of that is nothing: it's real, deterministic, reviewable proof that the ingestion and
freshness logic is *correct*, and it will behave the same way whether it's invoked by a human
running a script once, by a test, or by a real scheduler later.

## What "actually running daily" would still need

None of the following exists in this repo or this sandbox, and nothing above creates it:

1. **A scheduler.** No cron job, no Dagster `ScheduleDefinition`/sensor, no deployed
   orchestrator trigger calls `load_day`/`check_freshness` on any cadence. The closest real
   precedent in this repo, `pipeline/stage0_landing/dagster_assets.py`, wraps its capture logic
   in a *partitioned Dagster asset* — P2-02's equivalent would be a daily-partitioned asset (or
   schedule) per source, registered in `pipeline/definitions.py` the way `stage0_landing` is.
   That's a straightforward, mechanical next step given the API shape above, but it is not
   built here, and nothing in this ticket stood up Dagster's scheduler process itself.
2. **Real sources.** Every `load_day` call still reads from
   `tests/fixtures/generators/access_requests/`, i.e. synthetic data — P0-11 access hasn't been
   granted (still "To do"), and weather has no access request at all yet (see weather.py's
   docstring). "Daily loads" of a real feed needs that feed to exist first.
3. **Persistent state.** `ExternalSourceStore` is in-memory only (see its own docstring) — it
   holds no state across process runs. A real deployment needs its MERGE target to be an actual
   warehouse table (Iceberg, per CLAUDE.md's stack), not a Python dict that resets every time
   the process restarts. Freshness that resets to "never loaded" on every deploy isn't freshness.
4. **Monitoring/alerting on staleness.** `check_freshness` computes a status; nothing pages
   anyone or blocks a downstream job when a source comes back stale. P3-05 (mode-aware expected
   counts, downstream of this ticket) will consume this signal, but alerting on it is out of
   this ticket's scope as built.

## Honest bottom line

Mirroring how P1-12 ("last-seen snapshot... 15-min job") was judged in this backlog — logic
built and thoroughly tested, but marked **Human-owned/To do** because no real 15-minute
scheduler is deployed in this sandbox — the same standard should apply here. What's in this
commit satisfies "the ingestion and freshness logic works correctly, including across
repeated/sequential invocations, proven without a real scheduler." It does **not** satisfy "a
real system loads these five sources once a day in production," because no scheduler was
deployed, no real source was connected, and no state persists between runs. Whether "Daily
loads with freshness checks" is read as the narrower or the broader claim is the human
reviewer's call, consistent with how P1-12 was judged — but this document, and the module
docstring it mirrors, are written so that call is well-informed rather than defaulting to
"tests pass, so it's done."
