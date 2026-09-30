# P0-12: Privacy and security review kickoff

**Status:** In progress — Part 1 complete, Part 2 is preparation only, Part 3 is a synthetic self-review (see banners below; none of this closes Part 2's "booked" requirement)
**Date:** 2026-09-28
**Ticket:** P0-12 (Build backlog.md, Phase 0, component GOV, owner EM)
**Depended on by:** P3-12 "Privacy controls for residential data" (Phase 3, Gate 3)

## Scope and honesty note

Build backlog.md's "done when" for P0-12 is: *"Data classification drafted; residential and
utility-site reviews booked."* This document delivers the first half in full (Part 1) and
prepares, but does not accomplish, the second half (Part 2). Booking a review means a named
human — the project EM — actually scheduling time with legal/privacy/security stakeholders who
are real people at this company. That action cannot be taken from this session. Part 2 is a
ready-to-send brief, not a record that the reviews happened. **P0-12 should not be marked "Done"
in Build backlog.md until an EM has actually booked both reviews (or confirmed they're on the
calendar) and updated this document's Part 2 status.**

---

# Part 1 — Data classification draft

## 1.1 Why a custom scheme, and how to read it

This draft adapts a standard four-tier scheme (Public / Internal / Confidential / Restricted)
rather than inventing a new one, because that vocabulary is what most legal/security reviewers
already use and will map onto their own org's DLP tooling and access-control policies without
translation. Two extra columns are added beyond the tier itself, because tier alone doesn't
answer the two questions a reviewer will actually ask:

- **Reversibility to a person/household** — can this data, alone or combined with other data
  this pipeline holds, be used to identify or infer something about a specific person, family,
  or address? This is the axis that separates *residential* risk from *utility-site* risk in
  this project, and it's called out per-row because it's the main thing that should change
  between the two planned reviews.
- **Plausible regulatory surface** — stated in general, hedged terms. This project does not
  currently have a stated legal jurisdiction, corporate entity, or in-house counsel position on
  file in this repo, so nothing below should be read as a legal determination. It is a flag for
  what privacy counsel should confirm or correct in the review, not a substitute for that review.

### Tier definitions used below

| Tier | Meaning here |
| --- | --- |
| **Public** | Safe to disclose externally as-is (e.g. published spec sheets, firmware version numbers in isolation). Nothing in this pipeline's data model is Public by default. |
| **Internal** | Business-operational data with no direct link to an individual or a specific real-world address; broad internal access is acceptable with standard employment-based access controls. |
| **Confidential** | Data that identifies or is closely tied to a specific device, site, or account, and whose exposure would cause customer, contractual, or competitive harm, but is not itself directly personal. Requires role-based access and audit logging. |
| **Restricted** | Data that identifies, locates, or reveals behavioral patterns about a specific person or household, or that combined with other Restricted/Confidential data in this pipeline would do so. Requires the strictest access tier, retention limits, and (where used outside a restricted analytics context) aggregation or de-identification before use. |

## 1.2 Classification table

| # | Data category | Example fields / stages | Tier | Reversible to a person/household? | Plausible regulatory surface (unconfirmed — see 1.1) | Handling this pipeline should already satisfy or must satisfy before Phase 1 production ingest |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Residential device telemetry — Powerwall | Stage 1–3 canonical readings (state of charge, power flow, charge/discharge cycles, fault codes) keyed by `device_id` at a residence | **Restricted** | Yes, directly — a Powerwall's `device_id` maps to one household's electrical behavior | Consumer/residential privacy law likely applies in most jurisdictions this company could operate in (e.g. general consumer-data or "personal information" statutes); which specific regime(s) apply depends on where customers and the company are located, which this repo does not state. Confirm with privacy counsel. | Already satisfied: invariant 8 (no real residential data outside prod; synthetic fixtures only in dev/test). To satisfy before Phase 1: role-based access limited to a named residential-data tier (not all-engineer default access), a defined retention/purge policy, and aggregation before residential data reaches any dashboard or export below Stage 3 — this is exactly P3-12's scope, and this classification is the input P3-12 needs. |
| 2 | Residential usage/energy behavior patterns | Charge/discharge timing, daily energy curves, presence-correlated signals (e.g. consumption dips suggesting occupancy) derived at Stage 3 device×day / Stage 4 | **Restricted** | Yes, and more sensitive than raw telemetry alone — timing patterns can reveal occupancy, routines, and lifestyle, which is a well-recognized higher-sensitivity category in smart-home/utility privacy discussions even where it isn't the raw identifier itself | Same hedge as row 1; some jurisdictions treat inferred behavioral/occupancy data as more sensitive than raw usage data specifically because of the inference risk. Flag this explicitly to counsel rather than assuming raw-data rules cover it. | Should not be exposed at device+day granularity outside a restricted analytics context; Stage 4 fleet marts (P3-07) and dashboards (P3-09) should consume only cohort/site-aggregated versions of this, never per-household curves, unless a specific reviewed access path exists. |
| 3 | Utility/commercial-site telemetry — Supercharger stalls & cabinets, Megapack, Powerpack (utility-scale) | Stage 1–3 readings keyed by stall/cabinet/unit `device_id` at a site the company or a commercial customer operates, not a private residence | **Confidential** | Generally no — a Supercharger site or a utility Megapack installation is not a private residence, and the data does not on its own identify a specific person. Exception: if a Powerpack is sited at a residential or small-business address in a way that makes the site identifiable as someone's home, treat that installation's data as row 1/2 instead. | Likely governed more by commercial/contractual terms with the site owner or utility, and in some cases by grid-security or critical-infrastructure considerations (e.g. real-time output data from grid-connected assets can be operationally sensitive even without being "personal" data) rather than consumer privacy law. This is a different regulatory shape than residential data, not a lighter version of it — flag to security review, not just privacy review. | Role-based access scoped to the operating team/site; no invariant currently requires synthetic-only fixtures for this category outside prod (unlike row 1), but production access should still be logged and scoped by site/role given the grid-security angle. |
| 4 | Device identifiers | `device_id`, serial numbers, firmware version, hardware revision | **Confidential** standalone; **Restricted** when joined to a residential `device_id` (row 1) | Only in combination — a bare `device_id` is a Confidential asset-tracking key; it becomes person-identifying the moment it's joined to a residential account or address (which CLAUDE.md's device-history dimension, Stage 3 "device history dimension with validity periods," is designed to do) | N/A directly, but the join is the mechanism by which rows 1–2's protections must extend to this table. | The device history dimension (Stage 3) is a natural chokepoint: access to the table that joins `device_id` → account/address should itself be Restricted-tier, since it's what turns every other table's `device_id` into a residential identifier. Worth flagging explicitly in the P3-12 access-tier design. |
| 5 | Site / location data | Site coordinates or address for Supercharger, Megapack, Powerpack installations; residence address/zip tied to a Powerwall account | **Restricted** for residential addresses; **Confidential** for utility/commercial site coordinates | Residential address: yes, directly, and is itself one of the most sensitive fields this pipeline could hold regardless of what telemetry it's attached to. Commercial site coordinates: generally no (these are typically public or semi-public, e.g. Supercharger locations are often publicly listed) | Residential address sits squarely in scope for whatever consumer/data-protection regime applies (see row 1's hedge). Commercial site location is lower risk but may still be commercially sensitive (competitive site-selection info) even if not legally "personal." | If this pipeline stores or joins to address-level data at all (vs. an opaque site/account ID), that should be an explicit, reviewed decision, ideally minimized to the coarsest granularity (e.g. cohort/region) actually needed downstream — flag as a question for the residential review. |
| 6 | RMA, tickets, dispatch, outage, provisioning data (P0-11) | Referenced by P0-11 ("Access to RMA, tickets, dispatch, outage, provisioning data"); not yet loaded into this pipeline | **Confidential**, likely **Restricted** for the residential subset | Depends on content — dispatch/provisioning records for a residence commonly include name, address, and contact info directly, which is more identifying than telemetry alone | Same hedge as row 1, likely with sharper implications since these records often contain direct identifiers (name, phone, address) rather than inferred ones. | Out of this pipeline's current scope (P0-11 is a separate, not-yet-done ticket) but flagged here because P0-11's sample extracts, once loaded, should be classified using this same table before they touch any shared environment. Recommend re-running this classification pass against P0-11's actual schema once samples land. |
| 7 | Detector findings / reliability outputs (Stage 4) | Versioned findings, device snapshots, fleet marts | **Confidential** if traceable to device/site; **Internal** once aggregated to cohort/fleet level with no residential join | Findings that name a specific residential `device_id` inherit row 1's classification by reference; cohort/fleet/day aggregates with k-anonymity-style minimum cohort sizes are lower risk | Same hedge as row 1 for the residential-traceable subset. | This is the intended output of P3-07's design goal ("dashboards never scan Stage 3") — classification here should reinforce that dashboards and marts are built from aggregates, not from anything that still carries a residential `device_id`. |
| 8 | Synthetic test fixtures (`tests/fixtures/`) | Generator output, e.g. `tests/fixtures/generators/supercharger.py` | **Public/Internal** (not real data at all) | No — by construction, per invariant 8, these are synthetic and contain no real residential data | None — not real personal or operational data. | Already satisfied by invariant 8 as written. Worth confirming in the residential review that the *generators* themselves (not just their output) don't accidentally encode real device IDs, addresses, or account patterns copied from production as templates. |

## 1.3 How this maps to P3-12

P3-12 ("Privacy controls for residential data," Gate 3, depends on P0-12) is scoped to build:
*access tiers, retention limits, and aggregation.* This table is designed to hand P3-12 a
starting input for each of those three things directly:

- **Access tiers** → the Tier column above, with row 4 (the device-history join) flagged as the
  key chokepoint to gate.
- **Retention limits** → not yet defined by this document; retention periods are a policy
  decision for the residential review (Part 2), not something this draft should presume. Flagged
  as an open decision for that meeting, not left silently unaddressed.
- **Aggregation** → rows 2 and 7 specifically call out the granularity at which residential data
  should stop flowing downstream as raw per-device rows and become cohort/site aggregates.

---

# Part 2 — Review-booking brief (ready for an EM to use)

> **No reviews are booked yet.** Everything below is a brief for the project EM to send to
> stakeholders in order to book two review meetings. Nothing in this section should be read as
> confirmation that a meeting exists on anyone's calendar.

## 2.1 Two reviews, not one

Per P0-12's scope note in `docs/KICKOFF.md` ("prepare the privacy and security review brief for
residential and utility-site data") and the classification in Part 1, residential (Powerwall)
and utility/commercial-site (Supercharger, Megapack, Powerpack) data have different sensitivity
profiles and likely different regulatory surfaces (Part 1, rows 1 vs. 3). They should be booked
as **two separate review sessions**, not one combined meeting, so each can go deep with the
right specialists and reach a decision instead of surfacing both topics shallowly.

### Review A — Residential (Powerwall) data handling

**Proposed attendees**
- Privacy counsel (or outside privacy counsel if none in-house yet)
- Security lead / security engineering rep
- Project EM (owns this ticket, convenes the meeting)
- Engineering lead(s) for Stage 3 device-history dimension and Stage 4 outputs (the components
  that join device telemetry to an account/address — see Part 1, row 4)
- Optional: a DS/analytics lead if retention/aggregation tradeoffs need to be argued technically

**Decisions this review needs to make**
1. Confirm or correct the regulatory surface flagged in Part 1 (which regime(s) actually apply,
   given the company's real jurisdictions and customer base).
2. Approve or amend the tier assignments for residential rows in Part 1 (rows 1, 2, 4-restricted,
   5-residential, 6-residential, 7-residential).
3. Set retention limits for residential-tier data (not yet defined in Part 1 — this is the
   review's job, not a pre-decided input).
4. Set the minimum aggregation/cohort-size rule before residential data can reach Stage 4 marts
   or dashboards.
5. Decide who is authorized to hold the device-history join (row 4) and what access-review
   cadence applies to it.
6. Confirm invariant 8 ("no real residential data outside prod") as currently written satisfies
   the review's requirements, or specify what it's missing.

**Materials to bring:** this document's Part 1 in full (particularly rows 1, 2, 4-6), `CLAUDE.md`
(invariant 8 and the stage/grain model), and P3-12's ticket description in Build backlog.md as
the downstream target this review's decisions feed into.

**Suggested timing:** before Phase 1 production ingest work begins. Phase 1's stated goal is
"Stages 0-2 in production for stalls and cabinets" — Supercharger, not Powerwall — so this
review is not strictly blocking on Phase 1's start date. However, P3-12 (which this review feeds)
sits in Phase 3, and the device-history dimension and account/address joins that make residential
data identifiable are architectural decisions made much earlier (Stage 3 design, Phase 1-2). This
review should happen **before those joins are designed and built**, not retrofitted after,
because it's much cheaper to build the access boundary in from the start than to add it later.
Practically: target this for completion by Gate 0 (end of Phase 0) if schedules allow, and no
later than early Phase 1.

### Review B — Utility/commercial-site (Supercharger, Megapack, Powerpack) data handling

**Proposed attendees**
- Security lead (primary — grid-security and critical-infrastructure angle per Part 1, row 3)
- Privacy counsel (secondary — mainly to confirm this category is *not* being over- or
  under-classified relative to residential data)
- Project EM
- Engineering lead(s) for ingest and Stage 1-2 (the components handling real production
  Supercharger data starting in Phase 1)
- Optional: whoever owns commercial/contractual relationships with site hosts or utility
  partners, since contractual terms may impose handling requirements independent of law

**Decisions this review needs to make**
1. Confirm the security/grid-sensitivity framing in Part 1 row 3 is right, or correct it — this
   determines whether this review is led by security or by legal/commercial.
2. Approve or amend the tier assignment for utility/commercial-site data (Part 1 row 3, and the
   commercial-site subset of rows 5 and 7).
3. Identify whether any specific installation (e.g. a Powerpack at a residential or small-business
   address) should be reclassified into the residential tier on a case-by-case basis, and set a
   rule for how that gets flagged.
4. Confirm whether any contractual data-handling terms with site hosts/utility partners impose
   requirements beyond what Part 1 assumes.

**Materials to bring:** this document's Part 1 (particularly row 3, and the commercial-site
entries in rows 5 and 7), and `README.md`'s device-class list for scope confirmation.

**Suggested timing:** before Phase 1 production ingest work begins. This is the one with a hard
deadline: Phase 1's explicit goal is "Stages 0-2 in production for **stalls and cabinets**" (i.e.
Supercharger, which is utility/commercial-site data), meaning real production data volume for
this category starts at the top of Phase 1, not later. **This review should be booked and
completed before Phase 1 start**, not treated as optional groundwork.

## 2.2 Sequencing note

Review B (utility/commercial-site) is more time-sensitive than Review A (residential) given
Phase 1's stated scope (Supercharger only). If both cannot be booked immediately, prioritize
booking Review B first, but do not let that become a reason to defer Review A indefinitely —
Phase 1-2 work on the device-history dimension (which is what eventually makes residential data
identifiable, per Part 1 row 4) will already be underway before Review A's nominal Phase 3
dependency (P3-12) comes due, so the underlying architecture benefits from Review A's input
earlier rather than later.

---

# Part 3 — Self-conducted assumption-based review (synthetic substitute, not a real review)

> **⚠️ NOT A REAL REVIEW — READ BEFORE USING ANYTHING BELOW ⚠️**
>
> No real privacy counsel, security lead, or EM has looked at this section. It was produced by
> an agent (this session), on explicit instruction from the project owner, as a synthetic
> stand-in for Review A and Review B (Part 2) — the same way the rest of this pipeline runs on
> synthetic fixtures instead of real residential data. It is one engineer's best-effort walk
> through the same open questions Review A and Review B were designed to answer, using only
> publicly-reasonable privacy/security judgment plus this repo's own stated facts (`CLAUDE.md`'s
> invariants, Part 1's classification table, Build backlog.md's ticket text). It is **not** legal
> advice, **not** a security assessment by anyone qualified to give one, and **not** a substitute
> for Review A or Review B actually happening with real people in the room.
>
> **P0-12 must not be marked "Done" in Build backlog.md on the basis of this section.** Part 2's
> "done when" — *residential and utility-site reviews booked* — is still unmet. This section only
> gives P3-12 (and anyone else blocked on P0-12) something concrete to build against in the
> meantime, with every assumption labeled as exactly that.

## 3.0 Method

For each open question raised by Review A's and Review B's agendas (Part 2, §2.1), this section
gives one of three answers:

- **Assumed answer** — a specific, reasoned proposal a real reviewer can accept, amend, or
  reject, with the reasoning stated so the reviewer can attack the reasoning rather than start
  from nothing.
- **Partial answer** — an engineering-judgment default that is safe to build against *now*, but
  paired with the specific legal/contractual/organizational fact that only a real reviewer can
  supply.
- **No responsible guess** — stated as such, with the reason a guess would be actively harmful
  (e.g. inventing a fake jurisdiction or a fake contract term) rather than merely unconfirmed.

## 3.1 Review A open questions (residential / Powerwall)

### 3.1.a Regulatory surface (Review A decision 1; Part 1 §1.1)

**No responsible guess.** Which consumer-privacy regime(s) apply (e.g. a US state consumer
privacy statute, GDPR, or something else) depends on facts this repo does not contain and this
session cannot obtain: the company's actual jurisdictions of incorporation and operation, where
its residential customers are located, and whether it currently has in-house or outside privacy
counsel. Guessing a specific regime here would be worse than saying nothing, because downstream
work could silently start assuming (for example) that GDPR-style rules apply when they don't, or
vice versa. **This stays fully blocked on real privacy counsel; no assumption substitutes.**

### 3.1.b Tier assignments for residential rows (Review A decision 2)

**Assumed answer:** keep Part 1's tier assignments as-is (rows 1, 2, 4-restricted, 5-residential,
6-residential, 7-residential all **Restricted**). Reasoning: all six rows share the property that
they identify, locate, or reveal behavior of a specific household, which is exactly Part 1 §1.1's
Restricted definition, and none of them have a stated business need for broad internal access
that would argue for loosening the tier. This is a low-risk assumption to build against — even a
reviewer who disagrees on regulatory framing is unlikely to disagree that this data should start
at the strictest internal tier and be *loosened* only with a documented reason, not the reverse.

### 3.1.c Retention limits for residential-tier data (Review A decision 3; Part 1 §1.3 explicitly left open)

**Assumed answer**, proposed as a concrete starting policy for P3-12 to implement and Review A to
confirm or override:

| Data | Proposed retention | Reasoning |
| --- | --- | --- |
| Stage 1–3b per-event / per-reading residential telemetry (row 1) | 13 months at full per-device grain, then delete the row-level record (an aggregate may be retained per 3.1.d) | 13 months (not 12) covers one full seasonal cycle plus a one-month buffer, which matches this pipeline's own reliability-analysis need to compare a device against the same calendar period a year prior; going further gains little reliability value while extending exposure. This is a reliability-engineering rationale, not a legal one — a real reviewer may set a shorter period for legal reasons that override it. |
| Stage 3c device×day feature table (row 2) | 24 months at per-device grain, then aggregate/delete | Longer than raw readings because per-day features are the input to slower-moving analyses (e.g. multi-year battery degradation, which is a legitimate use Tesla/Tesla-like fleets track), but still bounded rather than indefinite. |
| Device-history join table — device_id → account/address (row 4) | Retain the address/account linkage only while the device is on an active account, plus a 90-day grace period after account closure/decommission, then the linkage row is deleted (the bare `device_id` and its non-address technical history may persist) | The join is what makes every other table person-identifying (Part 1 row 4). Deleting the linkage promptly after it's no longer operationally needed is the single highest-leverage retention control in this whole table, because it downgrades everything joined through it back toward Confidential once it's gone. 90 days is an assumed grace period for RMA/billing wind-down, not a researched figure. |
| Stage 4 residential-traceable findings (row 7, pre-aggregation) | Same as the row-1 telemetry they were computed from (13 months), then the finding is either re-expressed in aggregate form or deleted | Keeps the retention clock on findings tied to the retention clock on their source data rather than drifting independently. |

**Explicit flag:** every number above (13 months, 24 months, 90 days) is this session's
engineering-judgment default, not a legal minimum or maximum. Real privacy counsel may require a
shorter period (e.g. if a specific regulation mandates deletion on request or a fixed cap) or
permit a longer one; Review A must set the real number. What P3-12 can safely build now is the
*mechanism* (a per-table, per-tier retention/purge job keyed on event time, consistent with
invariant 4's "never partition by device_id" — the purge job should still operate over Stage
partitions by time, then filter/delete by device_id within them) rather than the specific
durations, which should be read from a config value the real review sets.

### 3.1.d Minimum aggregation / cohort-size rule (Review A decision 4)

**Assumed answer:** no Stage 4 mart or dashboard cell should be built from fewer than 20 distinct
households for a given cohort/day (or cohort/period) grouping; cells below that threshold are
suppressed or merged into a coarser cohort rather than shown. Reasoning: 20 is a commonly used,
conservative small-cell-suppression threshold in comparable data-release practice (many public
data releases use thresholds in the 5–20 range depending on sensitivity; this pipeline's data is
on the more sensitive end because it includes inferred behavioral/occupancy signals per Part 1
row 2, so this assumption picks the higher end of that common range rather than the lower one).
**Flag:** this is a reasonable engineering default to implement now (P3-12 can build the
suppression mechanism against a configurable threshold), but the actual number is a policy call
Review A should confirm — some organizations use a materially different threshold depending on
their risk tolerance and any applicable regulatory guidance, which again is not something this
session can determine.

### 3.1.e Who holds the device-history join, and access-review cadence (Review A decision 5)

**Assumed answer:** the device-history join (row 4) should not be a table that any engineer with
warehouse access can query ad hoc. Proposed design for P3-12:

1. The join lives in a table/view reachable only through a named, Restricted-tier role (e.g.
   `residential_identity_restricted`), distinct from the general analytics role.
2. Access to that role requires an individual, named grant with a stated business
   justification (not a team-wide default), consistent with Part 1's Restricted-tier
   definition ("requires the strictest access tier").
3. Every read of the joined (identity-bearing) form of the table is logged with who, when, and
   the stated justification — this is an extension of Part 1's existing "audit logging" language
   for Confidential/Restricted data, not a new requirement invented here.
4. Access is reviewed quarterly, and any grant unused for 90 days is revoked and must be
   re-requested with a fresh justification.
5. No bulk export or notebook-level ad hoc join against this table; downstream consumers get
   either a pre-aggregated result or a mediated query path that enforces the row-4 access
   control server-side, not client-side.

**Flag:** the specific cadence (quarterly, 90 days) and the exact role/grant mechanics are this
session's proposal, built from ordinary least-privilege practice, not from any stated company
policy (none is on file in this repo). Review A should confirm this matches how the company
actually manages access grants elsewhere, or substitute its existing access-review process if one
exists outside this repo.

### 3.1.f Does invariant 8 satisfy the review's requirements (Review A decision 6)

**Partial answer.** Invariant 8 ("No real residential data outside prod. Tests use synthetic
fixtures in `tests/fixtures/`.") is necessary but, on its own, not sufficient for what Part 1 and
this section describe as needed: it governs *where real data may exist* (prod only) but says
nothing about retention (3.1.c), access tiering within prod (3.1.e), or aggregation before data
leaves Stage 3 (3.1.d). Those are gaps in invariant coverage, not violations of invariant 8 as
written. **Assumed answer:** Review A should either extend `CLAUDE.md`'s invariants to cover
retention/access/aggregation once P3-12 lands, or explicitly decide those belong in P3-12's own
design doc instead of `CLAUDE.md` — either is defensible, but the gap should be closed
deliberately rather than left implicit. This is a documentation/process observation this session
can make confidently from the repo's own text; it is not a legal or security determination.

### 3.1.g Address-level data minimization (Part 1 row 5's flagged open question)

**Assumed answer:** residential street-address text should not be stored in, or joinable from,
any Stage 3+ table other than the device-history join table itself (row 4), and should never
reach Stage 4 marts or dashboards in any form finer than a coarse geography needed for a stated
downstream use. Concretely:

- The device-history join table may hold (or reference, via an opaque foreign key into an
  account/CRM system of record) the exact address, because that is its whole purpose and it is
  already proposed as the most tightly access-controlled table in the pipeline (3.1.e).
- Anything downstream of that join that needs geography (e.g. climate correlation for battery
  performance, which is a plausible legitimate analytics need) should carry a coarsened
  derivative only — e.g. a climate-zone code or a coordinate rounded to a grid cell on the order
  of several kilometers, not the address or precise lat/long — computed once at the join and
  never re-derivable back to the address from data outside the join table.
- No new table should be added anywhere in the pipeline that stores raw address text as a
  convenience join key; if a future ticket proposes that, it should be treated as a repeat of
  this same open question, not a fresh one.

**Flag:** this is a data-minimization design pattern (store precise data once, in the
most-restricted place, and only ever pass coarsened derivatives downstream), not a claim about
what any specific law requires. Review A may still require additional controls (e.g. a stricter
grid-cell size, or barring geography entirely from Stage 4) — this is a floor this session
believes is safe to build to now, not a ceiling.

## 3.2 Review B open questions (utility / commercial-site)

### 3.2.a Grid-security framing for row 3 (Review B decision 1)

**Partial answer.** This session cannot determine whether formal critical-infrastructure or
grid-security regulation (the kind of thing a real security/compliance function would check,
e.g. whether any of this pipeline's data or systems fall under a grid-reliability regulatory
regime) actually applies — that depends on facts about the company's interconnection
relationships and jurisdiction that are not in this repo, and getting this wrong in either
direction (assuming regulation applies when it doesn't, or the reverse) is the kind of mistake
only a security/compliance specialist should make. **What is safe to assume now:** real-time
output/status data from grid-connected assets (Megapack, Powerpack, and to a lesser extent
Supercharger draw data) is operationally sensitive on ordinary security grounds regardless of
whether formal critical-infrastructure regulation applies — it should not be exposed externally
or to broad internal audiences without a stated need, consistent with Part 1 row 3's existing
Confidential tier. **No responsible guess** on the specific regulatory classification or which
team should formally own it (security vs. legal/commercial) — that determination is Review B's
first agenda item precisely because it decides who leads the rest of the review, and this session
has no basis to make that call.

### 3.2.b Tier assignment for utility/commercial-site data (Review B decision 2)

**Assumed answer:** keep Part 1's **Confidential** tier for row 3 and the commercial-site subset
of rows 5 and 7 as-is. Reasoning: none of this data identifies a specific person on its own (per
Part 1's own "reversible to a person/household" analysis), which is the dividing line this
project's scheme uses between Confidential and Restricted, and nothing in Build backlog.md or
`README.md` suggests a reason to diverge from that. Safe to build against now.

### 3.2.c Residential-address-like installations reclassification rule (Review B decision 3)

**Assumed answer:** at provisioning time, run the installation address through an
address-type check (e.g. a standard postal/address-validation lookup that classifies a delivery
point as residential vs. commercial — this is a common, already-solved problem in
address-verification tooling, not something novel to invent here). Any installation whose address
resolves as residential-type is automatically flagged and its data treated as row 1/2 (Restricted)
until a human confirms otherwise, rather than defaulting to row 3's Confidential tier. This makes
the fallback direction the safer one (stricter tier by default, loosened only on confirmation)
rather than the reverse. **Flag:** this is a workable mechanism, not a policy decision — Review B
should confirm this is the right trigger and that a human confirmation step (rather than a fully
automated reclassification) is the right level of caution for what is likely to be a rare case.

### 3.2.d Contractual terms with site hosts / utility partners (Review B decision 4)

**No responsible guess.** This repo contains no site-host or utility-partner contracts, and
inventing plausible-sounding contract terms would be actively misleading — a real contract could
easily impose something this session would have no way to anticipate (e.g. a specific data
retention cap, a prohibition on a specific downstream use, or a notification requirement on
security incidents). **This stays fully blocked on whoever owns those commercial relationships**;
no downstream ticket should assume any contractual constraint (or its absence) until Review B
surfaces the actual terms.

## 3.3 Handoff — what P3-12 (or any downstream ticket) can build against now

**Safe to build against today** (engineering-judgment defaults from this section; each is
explicitly overridable by the real Review A/B, but none require waiting for them to start work):

1. Tier assignments as given in Part 1, unchanged (§3.1.b, §3.2.b).
2. The device-history join (row 4) as a separately access-controlled table/role, with named
   grants, audit logging, and quarterly access review (§3.1.e).
3. A retention/purge mechanism parameterized by table, keyed on event time per invariant 4, with
   *placeholder* durations of 13 months (raw residential telemetry), 24 months (device×day
   features), and a 90-day post-closure grace period before deleting the device-history address
   linkage (§3.1.c) — build the mechanism now, treat the numbers as a config value Review A will
   set for real.
4. A minimum-cohort-size suppression rule for Stage 4 outputs, parameterized (not hardcoded) at
   an initial default of 20 households per cell (§3.1.d).
5. Address minimization: no raw address outside the device-history join table; only coarsened
   geography derivatives flow downstream of it (§3.1.g).
6. An automated residential-type address check at Powerpack provisioning that defaults new,
   ambiguous installations to the stricter (Restricted) tier pending human confirmation (§3.2.c).
7. Extending `CLAUDE.md`'s invariants (or a P3-12-owned design doc) to explicitly cover
   retention, access-tiering, and aggregation once the above lands, closing the gap noted in
   §3.1.f.

**Hard-blocked on the real reviews — do not build against any assumption here:**

1. Which specific privacy/consumer-data regulation(s) actually apply to residential data
   (§3.1.a) — no jurisdiction or entity information exists in this repo to found a guess on.
2. Whether formal critical-infrastructure/grid-security regulation applies to utility-site data,
   and therefore whether security or legal leads that review (§3.2.a).
3. Any contractual data-handling term with a site host or utility partner (§3.2.d).
4. The *exact* retention durations and cohort-size threshold (the mechanisms in items 3–4 above
   are safe to build; the numbers 13 months / 24 months / 90 days / 20 households are this
   session's defaults and must be treated as provisional until Review A sets the real values).

If P3-12 is picked up before Review A/B happen, the most defensible path is: build items 1–7
above as configurable, reviewable mechanisms (not hardcoded assumptions baked into schema or
code that would be expensive to change), and leave the four hard-blocked items as explicit open
config/policy inputs — not guesses — so the real review's output is a parameter change, not a
re-architecture.
