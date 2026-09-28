# P0-01: Telemetry Backend Audit Template & Interview Questions

**Status of this document:** This is a template, not a completed audit. Per
`docs/KICKOFF.md`, the concrete Cowork deliverable for P0-01 is "draft the telemetry
backend audit template and interview questions" — not the audit findings themselves.
Filling this out requires interviews with, and system access to, the people and
infrastructure that operate the *existing* (pre-this-project) telemetry backend for
Powerwall, Megapack, Powerpack, and Supercharger stalls/cabinets. No such access or
interview data exists in this repo, so no source/format/retention/drop findings below
are populated. This document contains no claims about any real system — every example
value is a placeholder to be replaced during the actual audit.

Ticket reference: `Build backlog.md`, P0-01 — "Audit existing telemetry backend and data
flows" (DISC, Staff, 2 weeks). Done when: **"Written map of sources, formats, retention
and known drops."**

---

## 1. Purpose

This project (`CLAUDE.md`) is building a new batch lakehouse pipeline that will re-ingest
telemetry from ~1.1M energy devices from Stage 0 (landing) onward, with grain, retention,
and partitioning decisions that are meant to be locked at Gate 0 (see P0-13, dependent on
P0-06–P0-10). Those decisions are being made largely from *new* measurements this project
takes itself (arrival-shape profiling, timestamp/clock-quality profiling, lateness/duplicate
profiling — P0-06 through P0-08). That's necessary but not sufficient: this project is not
starting from a green field. Telemetry already flows from these devices into *something*
today — whatever ingestion, storage, and retention systems predate this pipeline.

Before Phase 0 measurements lock in new storage and grain decisions, someone with real
access needs to answer:

- What telemetry sources already exist, and what do they actually carry today?
- What do existing systems store, in what format, and for how long — as documented
  *and* as actually observed in practice (these often diverge)?
- What is already known to be broken, lossy, or unreliable in the current backend, and
  how (if at all) is that currently detected?
- What can be reused (as a migration source, a cross-check for the new pipeline's own
  profiling, or a source of historical backfill) versus what must be rebuilt from
  scratch because it doesn't exist or can't be trusted?

Getting this wrong has direct consequences for the new pipeline: Stage 0's invariant that
"replaying a closed window from Stage 0 must reproduce production output exactly" (CLAUDE.md
invariant 7) only means something if Stage 0 is actually capturing what devices send — and
whether the *existing* backend already silently drops or truncates data before it would
even reach a new Stage 0 capture point is exactly the kind of gap this audit needs to
surface. Likewise, retention policy on the existing backend determines how much historical
data is available to seed backfills at all.

This audit is scoped to *discovery*, not remediation. The output is a map, not a redesign.

---

## 2. How to use this document

1. Copy Section 3 (per-source template) once for every distinct telemetry source/system
   you find. "Distinct" means a different ingestion path, storage system, or owning team —
   not necessarily a different device class (e.g., Powerwall and Megapack may currently
   share one ingestion pipeline; Supercharger stalls and cabinets may not).
2. Use Section 4's interview questions as a starting point for conversations with each
   source's engineers/owners. Adapt per system — a probing question about a system you
   don't yet understand often surfaces the next system to audit.
3. Don't stop at the first source found per device class. Ask explicitly whether there are
   parallel or legacy paths (e.g., a newer path for recent firmware plus an older path
   still serving older fleets), and whether any of the four device classes above are
   *not yet* captured by any backend at all.
4. Track drops and quality issues even when nobody currently measures them — "nobody
   knows if this drops data, and there's no way to check" is itself a finding, and is more
   honest than leaving the field blank.
5. When done, compile the filled Section 3 instances into a single map (one row/section per
   source) and check it against Section 5 before calling the ticket done.

---

## 3. Per-data-source template (copy this block per source)

### Source: `<system/service name>`

**Owner / team:** `<name, team, on-call rotation or Slack channel if known>`

**Device classes / data types covered:**
`<e.g. Powerwall telemetry only | Supercharger stall + cabinet | mixed | unknown>`
- Which specific signals or message types does it carry (state of charge, fault codes,
  power flow, connector status, etc.)? Attach or link a sample payload if one exists.
- Does it cover *all* devices of that class, or only a subset (e.g., only devices on a
  particular firmware, region, or fleet)?

**Ingestion protocol / format:**
- Transport: `<MQTT | HTTPS webhook | polling | proprietary radio gateway | other>`
- Message format: `<protobuf | JSON | binary/proprietary | CSV batch export | other>`
- Message/schema versioning: is there a version field? How are breaking format changes
  currently rolled out and coordinated with the device fleet?
- Batching behavior: one message per reading, or are readings batched/buffered on-device
  before sending?

**Current storage location and format:**
- Where does data land and stay: `<data warehouse table | object storage | time-series DB
  | relational DB | vendor SaaS platform | log files only | other>`
- Storage format: `<Parquet | JSON blobs | proprietary DB format | other>`
- Is the stored data raw-as-received, or already transformed/aggregated/sampled before
  storage? If transformed, is the raw form kept anywhere, or is it lost after transform?
- Partitioning/indexing scheme, if known (by device, by arrival time, by event time, other).

**Retention policy — stated vs. observed:**
- Stated/documented retention: `<e.g. "90 days" per <doc/runbook link>, or "none found">`
- Observed retention: has anyone actually verified data older than the stated window is
  gone (or still there)? Has anyone found data *missing* well before the stated window
  expired?
- Are there different retention tiers (e.g., raw vs. downsampled/aggregated kept longer)?
- Is retention enforced automatically (TTL/lifecycle policy) or manually/ad hoc?

**Known reliability issues / data drops:**
- What failure modes are known today (device-side buffering/drop on disconnect,
  ingestion-side backpressure drops, dedup bugs, clock skew, partial batch loss, etc.)?
- How would anyone currently *notice* a drop — is there any completeness/freshness
  monitoring, alerting, or reconciliation against an expected device count/rate? Or is
  detection purely reactive (customer complaint, RMA investigation, ad hoc query)?
- Any known incidents, postmortems, or tickets describing past data loss for this source?
  Link them if they exist.
- Any known duplicate-delivery behavior, and is dedup handled anywhere today?

**Access method for extracting historical data:**
- Who can grant access, and what's the process (ticket, access request form, direct DB
  grant, vendor API key)?
- What's the extraction mechanism: `<direct query | export job | vendor API | file dump
  handed over manually>`? Any rate limits, cost, or extraction-time constraints
  (e.g., queries over N days time out)?
- Is there a non-production/sample copy available for engineering use, or is only the
  production system accessible?

**Rough volume / scale estimates:**
- Approximate message rate (messages/sec or messages/day) and per-message size.
- Approximate total device count feeding this source, and how that compares to the
  ~1.1M fleet total.
- Approximate total stored data volume (current size, and growth rate).
- Peak/burst behavior — does volume spike (e.g., fleet-wide reconnect after an outage)?

**Anything else notable:**
`<free text — undocumented quirks, planned deprecations, migrations in flight, etc.>`

---

## 4. Interview questions

Use these as prompts, not a script — the goal is specific, falsifiable answers, not "no
known issues." When an answer is vague, ask for a concrete example or a link.

### Ingestion & protocols

1. Walk me through exactly what happens from the moment a device sends a message to the
   moment it's queryable in the current system. What are every hop and every queue/broker
   in between?
2. What transport and format does each device class actually use today, and has that
   changed across firmware versions? Are there devices in the field still using an older
   protocol that's technically deprecated?
3. Is there a maximum message size or batch size? What happens when a device exceeds it —
   silent truncation, rejection, or something else?
4. Do devices buffer locally when disconnected, and if so, for how long and how much? What
   happens when the buffer fills before reconnecting — oldest-dropped, newest-dropped, or
   does the device stop sending entirely?
5. Is there an ingestion-side rate limit or backpressure mechanism? What happens to
   messages that arrive during a backpressure event — queued, dropped, or rejected with
   retry?
6. How are schema/format changes deployed — is there a migration window where two message
   versions coexist, and does the ingestion pipeline handle both, or does it silently
   mis-parse the wrong version?

### Storage & retention

7. What's actually enforced for retention today — a database TTL, a scheduled deletion
   job, a storage lifecycle policy, or nothing (i.e., data just accumulates until someone
   notices cost)?
8. Has anyone gone and checked whether data older than the stated retention window is
   really gone, or whether it's still sitting somewhere (e.g., in a backup, a downstream
   export, or an unmonitored bucket)?
9. Is raw/unaggregated data kept, or does anything get pre-aggregated or downsampled before
   long-term storage? If so, at what point, and is the pre-aggregation logic documented
   or does it live only in code?
10. Has retention ever been silently shortened (e.g., during a cost-cutting pass) without
    downstream consumers being told? How would we find out if that happened?
11. Is there more than one copy of this data anywhere (a replica, an export to a warehouse,
    a vendor-side copy), and do those copies actually agree with each other?

### Known issues & data quality

12. What's the worst data-loss incident you know of for this source — what caused it, how
    was it discovered, and how long did it go undetected?
13. Is there *any* automated check today that would catch a device (or a whole fleet
    segment) going silent — a per-device last-seen check, an expected-vs-actual message
    count, anything? If not, how has silent data loss historically been discovered in
    practice?
14. Are there known duplicate-message patterns (e.g., retries on ack timeout producing
    dupes), and if so, is anything deduplicating today, and on what key?
15. Are there known clock-skew or timestamp problems on any device class/firmware — e.g.,
    devices reporting a default/epoch timestamp, future timestamps, or timestamps that
    drift over time before an NTP resync?
16. Are there specific firmware versions or device batches known to be unreliable senders?
    Is that tracked anywhere, or is it tribal knowledge?
17. Has anyone compared message counts against an independent source of truth (e.g., a
    provisioning/fleet-inventory system's expected device count) to estimate what fraction
    of expected telemetry is actually arriving? If yes, what was the result; if no, is
    that comparison possible with current access?

### Access & extraction

18. Who currently has read access to this system, and what's the process to request it
    for a new team?
19. Can historical data be bulk-extracted (e.g., the last 12 months for a given device
    class), or is the system only practical for point/recent queries? Are there cost or
    performance constraints on large extracts?
20. Is there a non-production or sampled dataset available, or would any exploratory work
    have to touch production directly?
21. Are there compliance/privacy constraints on this data (e.g., anything that ties to
    residential customers) that affect who can access it or how it can be exported? (Note:
    this overlaps with P0-12's privacy/security review — flag anything found here for
    that ticket rather than resolving it in this audit.)
22. If this system were to be decommissioned or migrated away from, is there anything
    (config, undocumented transform logic, tribal-knowledge parsing rules) that only lives
    in this system or in someone's head, and nowhere written down?

---

## 5. What "done" looks like

Per `Build backlog.md`, P0-01 is done when there is a **written map of sources, formats,
retention, and known drops.** Concretely, that means:

- Every distinct existing telemetry source/system feeding these device classes (Powerwall,
  Megapack, Powerpack, Supercharger stalls, Supercharger cabinets) has a filled-out
  Section 3 instance — including sources found to be undocumented, informal, or "nobody's
  sure this still runs," which should be recorded as such rather than omitted.
- For each source, format and retention are stated with **both** the documented policy and
  what was actually verified/observed, with any discrepancy between the two called out
  explicitly rather than resolved in favor of whichever sounds better.
- Known reliability issues and data drops are listed per source, along with whether and how
  they're currently detected — including sources where the honest answer is "no detection
  exists today."
- Gaps are explicit: any device class, region, or fleet segment with **no** known existing
  telemetry backend at all should be called out as a gap, not left as an absent row.
- The compiled map is reviewed by whoever owns this ticket (Staff-level, per the backlog)
  and is in a state where P0-13 (profiling report and design lock, Gate 0) can cite it
  as an input alongside the P0-06–P0-08 in-repo profiling results.

This template file itself does not satisfy that criterion — it only enables someone with
real access to the existing backend to produce the written map described above.
