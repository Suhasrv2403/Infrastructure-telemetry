"""Synthetic fixture generators standing in for P0-11's real sample extracts.

See `README.md` in this directory for the full explanation. In short: P0-11 ("Access to RMA,
tickets, dispatch, outage, provisioning data") requires a human (the EM) to contact and get
sign-off from five external system owners before real access or real sample extracts exist -
see `docs/access-requests/P0-11-data-access-requests.md` for those drafted, still-unsent
requests. That can't happen in this environment, and P0-11 correctly stays "To do" in the
backlog: nothing here grants real access or should be read as claiming it does.

Per explicit project-owner direction, the five generators in this package (`rma`, `tickets`,
`dispatch`, `outage`, `provisioning`) are a synthetic substitute for the "sample extracts
loaded" half of P0-11's done-when, so Phase 2 tickets that depend on these sources (P2-01,
P2-02, and downstream P2-04/P2-12/P3-05) have something concrete to build and test against
instead of waiting indefinitely. Every schema here is ASSUMED, not observed - each module's
own docstring says so and flags its riskiest assumption. When a real sample extract eventually
arrives for any of these five sources, hand it to the senior DS to validate schema and
quality (per the P0-11 doc's own instruction), and expect this generator's assumed shape for
that source to need correction.

This package also holds a sixth module, `weather.py`, added for P2-02 ("ingest dispatch,
outages, weather, RMA and tickets") - weather is NOT one of the five P0-11 sources above and
has no corresponding section in the P0-11 access-request doc; see `weather.py`'s own ASSUMED
SCHEMA docstring for why (short version: it likely comes from a commercial/public API, not an
internal-system access request P0-11's template fits).
"""
