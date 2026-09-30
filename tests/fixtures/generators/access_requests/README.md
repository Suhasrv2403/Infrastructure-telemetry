# tests/fixtures/generators/access_requests/

Synthetic substitute for P0-11's real "sample extracts loaded" — five fixture generators, one
per data source named in `docs/access-requests/P0-11-data-access-requests.md`.

## Why this exists

P0-11 ("Access to RMA, tickets, dispatch, outage, provisioning data") is "done when" read
access is granted and sample extracts are loaded — and that genuinely requires a human (the
EM) to identify the real external system owners, send the drafted requests in the doc above,
and follow up. None of that can happen from this environment, and **P0-11 stays "To do" in the
backlog** — nothing in this directory is evidence that real access exists, and it isn't meant
to close the ticket.

What it does instead, per explicit project-owner direction: this project already runs entirely
on synthetic/mock data pending real captures elsewhere (see `tests/fixtures/generators/
supercharger.py` for the Supercharger telemetry precedent), so the same approach applies here.
These five generators give Phase 2 tickets that are blocked on P0-11 — **P2-01** (device
history dimension) and **P2-02** (ingest dispatch/outage/RMA/tickets), and downstream **P2-04**,
**P2-12**, **P3-05** — something concrete to build and test against now, instead of waiting on
an access request with no committed timeline.

## What's here

| Module | Data source | Grain |
| --- | --- | --- |
| `rma.py` | RMA / warranty records | one row per RMA case |
| `tickets.py` | Support/service tickets | one row per ticket |
| `dispatch.py` | Field-service dispatch | one row per dispatch/site visit |
| `outage.py` | Grid/site outage records | one row per outage |
| `provisioning.py` | Device provisioning **history** | one row per provisioning *change event* — not a snapshot (see below) |
| `weather.py` | Site-level weather observations | one row per **(site, hour)** — a dense grid, not an event feed (see below) |

`_common.py` holds the constants and the device_id/site_id scheme shared by all five, copied
by hand from `tests/fixtures/generators/supercharger.py` so records here can be joined against
that generator's Supercharger fixtures in cross-source tests (same `device_id`/`site_id`
values, not a disjoint id space).

## Every schema here is ASSUMED

No real system was consulted for any of these five schemas — none exists in this repo, because
no access request has been sent. Every module's docstring says so explicitly and names its
single riskiest assumption. Treat every field name, enum value and nullability rule here as a
placeholder for "what a reasonable system might look like," not as a spec.

**When a real sample extract eventually arrives** for any of these five sources, the P0-11
doc's own instruction applies: hand it to the senior DS to validate schema and quality before
Phase 2 work depends on it. Expect this generator's assumed shape for that source to need
correction, and update (or retire) the corresponding module once real fixtures are available —
the same lifecycle `supercharger.py`'s own docstring describes for itself once P0-05 lands real
captures.

## The provisioning generator is a history table on purpose

P2-01's acceptance test is an as-of join: "what firmware was device X running at time T?" A
current-state snapshot can't answer that; only a change log can. `provisioning.py` generates
one row per device per *change event* (commissioning, firmware upgrade, hardware swap, site
reassignment), each with a strictly increasing `effective_ts` and no two rows for the same
device sharing a timestamp — so "the row current at time T" is always well-defined as the row
with the largest `effective_ts <= T`. `provisioning.current_as_of()` implements that lookup
directly, and `tests/unit/test_access_requests_provisioning_fixture_generator.py` tests it.

## `weather.py` is not one of the five P0-11 sources

Unlike the other five modules here, weather has no section in
`docs/access-requests/P0-11-data-access-requests.md` - that document only ever named RMA,
tickets, dispatch, outage and provisioning. Weather was added for **P2-02**'s own ticket
wording ("ingest dispatch, outages, **weather**, RMA and tickets"); the most plausible real
source is a commercial/public weather API rather than an internal system a P0-11-style access
request would fit, so no such request was drafted. It's a dense hourly grid (one row per site
per hour), not a sparse per-case event feed like this package's other five generators - see
`weather.py`'s own ASSUMED SCHEMA docstring.

## Running a generator standalone

Each module has a small CLI, matching `supercharger.py`'s convention, e.g.:

```
python -m tests.fixtures.generators.access_requests.rma --out tests/fixtures/access_requests/rma.jsonl
```
