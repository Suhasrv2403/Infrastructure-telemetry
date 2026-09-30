# 0002: Residential data retention periods and device-history access tier

**Status:** accepted
**Date:** 2026-09-30
**Ticket:** P0-12 (informs P3-12)

## Context

P0-12's self-conducted synthetic review (`P0-12-privacy-security-review` branch, commit
`06ba566`, Part 3) proposed concrete retention periods and an access-tier design for
residential data, explicitly flagged as an agent's engineering-judgment defaults, not real
policy - no real privacy/security review has happened. P3-12 ("Privacy controls for
residential data," Gate 3, depends on P0-12) needs one specific number per data category to
build against, not a range or a placeholder.

This was raised as `docs/PENDING-DECISIONS.md` D1 and decided by the user on 2026-09-30
(Option A: adopt the agent's proposed defaults as-is). This ADR is that decision, made real
and citable the way CLAUDE.md's decision-log convention expects.

## Decision

The following are the retention/access defaults P3-12 implements, **as config values, not
hard-coded**, so a future real privacy/security review can correct any of them with a
one-line change:

| Data | Retention | Notes |
| --- | --- | --- |
| Stage 1-3b per-event/per-reading residential telemetry | 13 months at full per-device grain, then delete the row-level record | One full seasonal cycle plus a one-month buffer. |
| Stage 3c device x day feature table | 24 months at per-device grain, then aggregate/delete | Supports multi-year degradation analysis while staying bounded. |
| Device-history join (`device_id` to account/address) | Retain only while the device is on an active account, plus a 90-day post-closure grace period, then delete the linkage row (the bare `device_id` and its non-address technical history may persist) | The single highest-leverage control: deleting this linkage downgrades everything joined through it back toward Confidential. |
| Stage 4 residential-traceable findings (pre-aggregation) | Same as their source telemetry (13 months), then re-expressed in aggregate form or deleted | Retention clock tied to the source data's clock. |

**Aggregation floor:** no Stage 4 mart or dashboard cell is built from fewer than **20**
distinct households for a given cohort/period grouping; cells below that threshold are
suppressed or merged into a coarser cohort.

**Device-history join access tier:** the join lives behind a named, Restricted-tier role
(e.g. `residential_identity_restricted`), distinct from general analytics access. Access
requires an individual named grant with a stated business justification, every read of the
identity-bearing form is logged (who/when/justification), access is reviewed quarterly, and
any grant unused for 90 days is revoked and must be re-requested. No bulk export or ad hoc
notebook join against this table - downstream consumers get either a pre-aggregated result or
a server-side-mediated query path.

## Consequences

- P3-12 can implement a concrete purge job and access-control layer immediately instead of
  waiting on a real review that hasn't been booked (P0-12's Part 2 "done when" is still
  unmet).
- The purge job operates over Stage partitions by event time and filters/deletes by
  `device_id` within them, consistent with invariant 4 ("never partition by device_id").
- If a real privacy review later sets different numbers, only the config values change - no
  schema or job redesign, per the Non-Negotiable this decision was scored against.
- This does not close P0-12 or P3-12; both still require the real reviews P0-12's Part 2
  describes. This ADR only removes the "no numbers to build against" blocker for P3-12's
  design.

## Alternatives considered

- **Shorter, more conservative defaults (6mo/12mo/30d/n=30)** - scored lower on downstream
  unblock value in `docs/PENDING-DECISIONS.md` D1's option table; not chosen.
- **Block P3-12 entirely until a real review is booked and held** - safest against legal risk
  but leaves Phase 3's Gate 3 dependency stalled indefinitely with no booking date in sight;
  not chosen.

See `docs/PENDING-DECISIONS.md` (Resolved section) and
`docs/privacy/P0-12-privacy-security-review-brief.md` Part 3 for the full reasoning behind
each number.
