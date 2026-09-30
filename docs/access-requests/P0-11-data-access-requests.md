# P0-11: Data Access Requests — RMA, Tickets, Dispatch, Outage, Provisioning

**Ticket:** P0-11 · Access to RMA, tickets, dispatch, outage, provisioning data
**Owner:** EM + senior DS
**Status of this document:** Draft. Nothing below has been sent yet, and no access has been
granted. Per `docs/KICKOFF.md`, the achievable Cowork deliverable for P0-11 is a set of
ready-to-send access requests — not evidence that access exists. The ticket's real "done
when" (read access granted, sample extracts loaded) requires a human (the EM) to identify
the actual internal system owners, send these requests, and follow up.

## How to use this document

Each section below is a self-contained, ready-to-send request for one data source. To send:

1. Replace every `[... — TBD]` placeholder with the real system/team name and contact.
2. Confirm with the P0-12 privacy/security review (in parallel, also owned by this EM)
   whether the source needs a formal data-governance sign-off before the owner will grant
   access — several of these sources likely touch residential customer or utility-site data.
3. Send the request (email text is drafted and ready to paste); log the send date and
   response in the tracking table below.
4. When a sample extract arrives, hand it to the senior DS to validate schema and quality
   before Phase 2 (P2-01, P2-02) work depends on it.

## Why these five, and what they feed

All five requests exist to unblock two Phase 2 tickets that are blocked on P0-11 in the
backlog:

- **P2-01 — Device history dimension** (firmware, hardware rev, cell lot, site, climate),
  needs **provisioning** data, and specifically needs it as a *history*, not a snapshot: the
  ticket's acceptance test is that an as-of join returns the firmware a device ran at any
  past timestamp. A current-state table cannot support that; only a change log can.
- **P2-02 — Ingest external sources: dispatch, outages, weather, RMA and tickets**, needs
  **dispatch, outage, RMA and tickets** data as ongoing, dated feeds with daily freshness
  checks.

Further downstream, the same sources feed:

- **P2-04** (event detection) reconciles detected events against **dispatch** and **outage**
  records.
- **P2-12** (detector backtest harness) measures detector precision and lead time against
  **RMA** and **tickets** as ground truth for real failures.
- **P3-05** (mode-aware expected counts) uses **outage** (and VPP) records to tell a grid
  outage apart from a device-side fault when explaining telemetry silence.

## Tracking table

| # | Data source | Likely owner (TBD) | Feeds (backlog tickets) | Sent? | Sample received? |
| --- | --- | --- | --- | --- | --- |
| 1 | RMA | [RMA / warranty systems owner — TBD] | P2-02, P2-12 | No | No |
| 2 | Tickets | [Support/service ticketing owner — TBD] | P2-02, P2-04, P2-12 | No | No |
| 3 | Dispatch | [Field service / dispatch systems owner — TBD] | P2-02, P2-04 | No | No |
| 4 | Outage | [Grid/site ops or utility ops owner — TBD] | P2-02, P2-04, P3-05 | No | No |
| 5 | Provisioning | [Manufacturing/deployment systems owner — TBD] | P2-01 | No | No |

## Data handling commitments (apply to all five requests)

Stated once here, and referenced from each request below, so every owner sees the same
commitment:

- **Read-only access only.** No request below asks for write, delete or administrative
  access to the source system.
- **No real residential or utility-site data outside prod.** Per this project's invariant 8,
  any real data pulled under these requests — including sample extracts — is stored only in
  the pipeline's approved production environment. All non-production environments (dev,
  staging, CI, local dev) use synthetic fixtures under `tests/fixtures/`; real extracts are
  never copied into those.
- **Minimum necessary fields.** Each request below asks for device/site identifiers and
  operational fields needed for reliability analysis, and explicitly asks to exclude or mask
  customer PII (name, address, contact info, payment info) wherever the source system allows
  scoping the extract that way.
- **Governance review in parallel.** P0-12 (privacy and security review kickoff) is running
  alongside this ticket and covers residential and utility-site data classification. Any
  source flagged sensitive there will get a follow-up scoped-access conversation rather than
  blocking on P0-12 finishing first — but the requesting team commits to complying with
  whatever P0-12 lands on, including tightening or revoking access if needed.
- **Named requesters and purpose.** Every request states who is asking (EM + senior DS) and
  the specific downstream use (which backlog tickets), so the owner can evaluate a concrete
  request rather than an open-ended one.

---

## 1. RMA (Return Merchandise Authorization) data

**Likely owner:** [RMA / warranty systems owner — TBD, likely Service Ops or Supply Chain]

**Feeds:** P2-02 (ongoing RMA ingestion), P2-12 (backtest ground truth — did a detector flag
a device before it was actually returned as failed, and how much lead time did it give?)

### Draft request

> **Subject:** Data access request — RMA records for Fleet Reliability Pipeline (read-only)
>
> **To:** [RMA system owner — TBD]
> **Cc:** [Data governance / privacy contact — TBD, re: P0-12]
>
> Hi [name],
>
> The Fleet Reliability Pipeline team (EM: [name], senior DS: [name]) is building a data
> pipeline that turns device telemetry from our Powerwall, Megapack, Powerpack and
> Supercharger fleets into reliability findings — for example, flagging a device likely to
> fail before it actually does. To validate and eventually run those detectors, we need
> read-only access to RMA (return/warranty) records.
>
> **What we're requesting:**
> - System/table: [RMA system name — TBD]
> - Grain: one row per RMA case
> - Fields: device serial number or device ID, RMA opened timestamp, RMA closed/resolved
>   timestamp, return/failure reason code (and free-text description if captured), device
>   class/model and hardware revision if available, resolution type (replaced, repaired,
>   credited, no fault found), site or installation identifier — please default to a
>   device/site identifier rather than customer name or address wherever the system allows.
> - Time range: trailing 24 months of historical records for backtesting, plus an ongoing
>   daily or weekly feed going forward.
> - Access level: **read-only**, both a one-time historical bulk extract and an ongoing feed.
>
> **Sample extract first:** before we build the ingestion pipeline, could we get a small
> sample — roughly 50–100 recent, representative RMA records — so we can confirm the schema,
> check field completeness, and catch any format surprises early? We'd validate this sample
> before requesting the full historical extract or turning on the ongoing feed.
>
> **On data handling:** access is read-only. Any real RMA data we receive — including this
> sample — is stored only in our approved production environment; every non-production
> environment we use (dev, staging, tests) runs on synthetic fixtures, never real records.
> Our privacy and security review (P0-12) is running in parallel, and we'll follow whatever
> classification and handling requirements it lands on for RMA data.
>
> Happy to hop on a call if that's easier than reviewing this over email.
>
> Thanks,
> [EM name] / [Senior DS name]

---

## 2. Tickets (support / service tickets)

**Likely owner:** [Support or service ticketing platform owner — TBD, e.g. Customer Support
Ops or Field Service]

**Feeds:** P2-02 (ongoing tickets ingestion), P2-04 (event reconciliation — a support ticket
about a failed charging session or outage is one way a detected event gets corroborated),
P2-12 (backtest ground truth alongside RMA)

### Draft request

> **Subject:** Data access request — support/service ticket records for Fleet Reliability
> Pipeline (read-only)
>
> **To:** [Ticketing system owner — TBD]
> **Cc:** [Data governance / privacy contact — TBD, re: P0-12]
>
> Hi [name],
>
> Same context as our RMA request: the Fleet Reliability Pipeline team is building detectors
> over device telemetry for Powerwall, Megapack, Powerpack and Supercharger fleets, and we'd
> like to cross-reference detected issues against customer-reported support/service tickets.
>
> **What we're requesting:**
> - System/table: [ticketing system name — TBD]
> - Grain: one row per ticket
> - Fields: ticket ID, associated device serial/ID or site ID (preferred over customer
>   name/contact info), created timestamp, closed/resolved timestamp, category or symptom
>   code, intake channel (app, phone, chat, in-person), resolution summary, linked RMA or
>   dispatch ID if the systems cross-reference each other.
> - Time range: trailing 24 months historical, plus an ongoing daily/weekly feed.
> - Access level: **read-only**, historical bulk extract plus ongoing feed.
>
> **Sample extract first:** could we get roughly 50–100 recent, representative tickets
> (PII scrubbed or minimized to device/site identifiers) to validate schema and quality
> before we build against the full feed?
>
> **On data handling:** read-only access; real ticket data is stored only in our approved
> production environment, never in dev/staging/test (those use synthetic fixtures). We'll
> align with whatever P0-12 (our parallel privacy/security review) determines about handling
> customer-reported ticket content, since tickets are more likely than RMA records to contain
> free-text customer PII.
>
> Thanks,
> [EM name] / [Senior DS name]

---

## 3. Dispatch (technician dispatch records)

**Likely owner:** [Field service / dispatch systems owner — TBD, e.g. Field Service Ops or
NOC]

**Feeds:** P2-02 (ongoing dispatch ingestion), P2-04 (event table reconciles detected events
against dispatch records — a truck roll is strong evidence an event actually happened)

### Draft request

> **Subject:** Data access request — technician dispatch records for Fleet Reliability
> Pipeline (read-only)
>
> **To:** [Dispatch system owner — TBD]
> **Cc:** [Data governance / privacy contact — TBD, re: P0-12]
>
> Hi [name],
>
> We're requesting read-only access to technician/field-service dispatch records — truck
> rolls or site visits triggered by faults, RMAs, or operational issues — across our
> residential (Powerwall) and utility-scale (Megapack, Powerpack) sites, as well as
> Supercharger stalls/cabinets. We use this to reconcile our own telemetry-based event
> detection against ground-truth field visits.
>
> **What we're requesting:**
> - System/table: [dispatch system name — TBD]
> - Grain: one row per dispatch/site visit
> - Fields: dispatch ID, device/site identifier (device serial or site/circuit ID), dispatch
>   requested timestamp, technician arrival timestamp, completion timestamp, dispatch
>   reason/fault code, outcome (part replaced, resolved on-site, no fault found, escalated),
>   linked RMA or ticket ID if available.
> - Time range: trailing 24 months historical, plus an ongoing daily/weekly feed.
> - Access level: **read-only**, historical bulk extract plus ongoing feed.
>
> **Sample extract first:** a sample of roughly 50–100 recent dispatch records, spanning a
> mix of device classes if possible, so we can validate schema and coverage before building
> the ingestion job.
>
> **On data handling:** read-only; real dispatch data stored only in our approved production
> environment. We'll route this through our P0-12 privacy/security review if dispatch records
> for residential sites are classified as sensitive.
>
> Thanks,
> [EM name] / [Senior DS name]

---

## 4. Outage (grid / site outage records)

**Likely owner:** [Grid operations, utility ops, or site ops owner — TBD]

**Feeds:** P2-02 (ongoing outage ingestion), P2-04 (event reconciliation), P3-05 (mode-aware
expected counts — lets telemetry health tell "device went quiet because the grid/site lost
power" apart from "device went quiet because it broke")

### Draft request

> **Subject:** Data access request — grid/site outage records for Fleet Reliability Pipeline
> (read-only)
>
> **To:** [Outage/grid ops system owner — TBD]
> **Cc:** [Data governance / privacy contact — TBD, re: P0-12]
>
> Hi [name],
>
> We're requesting read-only access to grid or site outage records covering areas/sites where
> our fleet (Powerwall, Megapack, Powerpack, Supercharger) operates. We use this to explain
> expected telemetry gaps — a device that stops reporting during a known outage shouldn't be
> flagged as a silent/failed device the same way one that goes quiet with no outage nearby
> should be.
>
> **What we're requesting:**
> - System/table: [outage system name — TBD]
> - Grain: one row per outage event
> - Fields: outage ID, affected site/circuit/feeder identifier (or device IDs if the system
>   maps outages to specific devices/sites), outage start timestamp, restoration timestamp,
>   cause code (weather, equipment failure, planned maintenance, other), scope (residential
>   feeder vs. utility-scale site vs. Supercharger site).
> - Time range: trailing 24 months historical, plus an ongoing daily/weekly feed.
> - Access level: **read-only**, historical bulk extract plus ongoing feed.
> - Secondary, optional ask: if your team also has VPP (virtual power plant) dispatch/event
>   records, we'd appreciate the same access — P3-05 downstream uses outage and VPP records
>   together to model expected reporting behavior, so we'll likely follow up on this
>   separately once outage access is in place.
>
> **Sample extract first:** roughly 50–100 recent outage records covering a mix of causes and
> geographies, to validate schema before we build the ingestion job.
>
> **On data handling:** read-only; real outage data stored only in our approved production
> environment. This data is more likely to be grid/infrastructure-sensitive than
> customer-PII-sensitive, so let us know if there's a separate security review process for
> grid operational data beyond our P0-12 privacy review, and we'll route through both.
>
> Thanks,
> [EM name] / [Senior DS name]

---

## 5. Provisioning (device provisioning / deployment records)

**Likely owner:** [Manufacturing, deployment, or device-management systems owner — TBD]

**Feeds:** P2-01 (device history dimension — firmware, hardware rev, cell lot, site,
climate — with validity periods, so as-of joins can answer "what firmware was this device
running at time T?")

### Draft request

> **Subject:** Data access request — device provisioning/history records for Fleet
> Reliability Pipeline (read-only)
>
> **To:** [Provisioning/device-management system owner — TBD]
> **Cc:** [Data governance / privacy contact — TBD, re: P0-12]
>
> Hi [name],
>
> We're building a device history dimension that needs to answer, for any device and any
> point in time in the past, what firmware it was running, what hardware revision and cell
> lot it shipped with, and what site/climate it was installed at. For that we need read-only
> access to provisioning/deployment records.
>
> **Important:** we specifically need the *history of changes*, not just each device's
> current state. A snapshot table (current firmware, current site) can't answer "what
> firmware was device X running last March," and that as-of lookup is our actual acceptance
> test. So we're asking for whichever of the following your system can provide:
> - An audit log / change-history table (each row is one change event: device ID, field
>   changed, old value, new value, effective timestamp), **or**
> - Periodic full snapshots (e.g. daily or weekly) we can diff ourselves to reconstruct
>   history, if a change log isn't available.
>
> **What we're requesting:**
> - System/table: [provisioning system name — TBD]
> - Grain: one row per provisioning/change event (preferred) or per device per snapshot
>   period (fallback)
> - Fields: device serial/ID, hardware revision, cell lot/batch ID, firmware version, site ID
>   assigned, site geographic region/climate zone, install/commission timestamp,
>   decommission timestamp (if applicable), and — for the change-log form — the effective
>   timestamp of each change.
> - Time range: full history since fleet deployment began, plus an ongoing feed of new
>   provisioning/change events.
> - Access level: **read-only**, historical bulk extract plus ongoing feed.
>
> **Sample extract first:** could we get provisioning history for a small sample of ~20–50
> devices (ideally spanning a few firmware updates or site reassignments each, so we can
> confirm the history/as-of shape works) before we build against the full fleet?
>
> **On data handling:** read-only; real provisioning data stored only in our approved
> production environment, never in dev/staging/test. If site-level records for residential
> Powerwall installs are classified as sensitive under our P0-12 review, we'll handle those
> accordingly (e.g. masking exact addresses down to a region/climate identifier).
>
> Thanks,
> [EM name] / [Senior DS name]

---

## Open questions for the EM before sending

- **Real owners.** Every `[... — TBD]` above needs a real team/system name. This document
  assumes plausible org structures (Service Ops for RMA, Field Service for dispatch, Grid/
  Utility Ops for outage, Manufacturing/Deployment for provisioning) that should be checked
  against how this org is actually structured.
- **Sequencing with P0-12.** Whether to wait for P0-12's data classification before sending,
  or send now and loop governance in once classification lands, is a judgment call for the
  EM — this document assumes "send now, cc governance, adjust if flagged."
- **Residential data may need more than an email.** RMA, tickets and provisioning records
  for residential Powerwall sites likely carry customer PII (name, address). If P0-12's
  review concludes that requires formal handling terms (e.g. contractual data-processing
  language, address masking down to a region identifier), that's a heavier one-time
  governance sign-off that sits outside what a single access-request email can settle — the
  EM should flag that possibility to each system owner up front rather than let it surface
  only after a sample extract arrives.
