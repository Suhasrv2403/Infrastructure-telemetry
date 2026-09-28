# P0-12: Privacy and security review kickoff

**Status:** In progress — Part 1 complete, Part 2 is preparation only (see banner below)
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
