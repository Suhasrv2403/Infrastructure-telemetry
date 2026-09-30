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

**Update (2026-09-29):** Real backend access and stakeholder interviews still do not exist.
Per explicit direction from the project owner, Sections 3 and 5 below have now additionally
been filled in with a **synthetic/assumed substitute**, built the same way the rest of this
project runs on synthetic fixtures where real data isn't available (see
`tests/fixtures/generators/supercharger.py`). This exists so downstream work (P0-13 and
beyond) has a concrete, falsifiable map to build against instead of nothing — **it is not,
and does not claim to be, the real audit.** Every filled-in field below is marked
`**ASSUMED:**` and must be replaced with real findings once real backend access and
interviews are eventually obtained. The blank template and usage instructions in Sections
2–4 remain unchanged and still apply to whoever performs the real audit. Filling in
Sections 3/5 with assumptions does **not** satisfy this ticket's real "done when" criterion
("written map of sources, formats, retention and known drops" — from real findings, not
assumed ones); per explicit project-owner and human direction this ticket correctly remains
**"To do"** in `Build backlog.md`.

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

> ## SYNTHETIC/ASSUMED SUBSTITUTE — READ BEFORE TRUSTING ANYTHING BELOW
>
> Real access to the legacy telemetry backend and its owning teams was never obtained for
> this ticket. Everything in this section, down to every field marked **`ASSUMED:`**, is a
> plausible invention — not a real finding — built by reasoning from (a) this project's own
> design in `CLAUDE.md` (asking "what gap would justify building each new invariant this
> way?") and (b) established repo facts about the devices themselves (firmware naming and
> transport protocols in `tests/fixtures/generators/supercharger.py`). It is offered so
> P0-13 and downstream engineering have *something concrete* to sanity-check, argue with,
> and replace, rather than nothing. **Treat every claim below as a hypothesis to be
> falsified by the real audit, never as evidence.** None of it should be cited as a fact
> about any real Tesla system. When real access is obtained, this whole synthetic section
> should be replaced wholesale, not patched.

### ASSUMED Source 1: Powerwall Legacy Telemetry Backend

**Owner / team:** **ASSUMED:** Home Energy Products telemetry services sub-team, informal
best-effort on-call (no dedicated SRE rotation) — assumed because Powerwall telemetry is
treated as a product-analytics feature (app history, warranty diagnostics) rather than a
grid-critical system, so it likely never got dedicated infra ownership.

**Device classes / data types covered:**
**ASSUMED:** Powerwall only, across effectively all fielded firmware (no fleet segment
excluded by design) — carrying state of charge, AC/DC power flow, grid/backup mode,
inverter fault codes, and battery temperature. **ASSUMED:** this is the single largest
source by device count, roughly 800k–900k of the ~1.1M fleet total, since residential
storage is assumed to dominate total unit count versus commercial/DCFC hardware.

**Ingestion protocol / format:**
- Transport: **ASSUMED:** MQTT over TLS, one persistent connection per home gateway.
- Message format: **ASSUMED:** loosely-typed JSON, not protobuf — assumed because this
  path is old (predates this project's protobuf-leaning design) and consumer-hardware IoT
  stacks of that era favored JSON for easy field debugging.
- Schema versioning: **ASSUMED:** an informal `schema_version` integer field exists but is
  not contractually enforced; **ASSUMED:** breaking field changes have historically been
  rolled out by adding new optional fields and hoping old code ignores them, not by a
  coordinated migration window. This is offered as a plausible reason `catalog/signals.yaml`
  (a single canonical, versioned signal catalog per firmware) exists in the new design —
  it's the thing this legacy path never had.
- Batching: **ASSUMED:** small batches, roughly one message every 1–5 minutes per device,
  not per-reading streaming — home network cost/reliability likely favored batching over
  per-sample sends.

**Current storage location and format:**
- **ASSUMED:** recent raw-ish data lands in a proprietary/vendor time-series database
  (optimized for the mobile app's "last N days" charts); a nightly job additionally writes
  **pre-aggregated 15-minute rollups** into a central data-warehouse table.
- **ASSUMED:** the warehouse copy is *already aggregated*, not raw-as-received; the raw
  per-message form only survives inside the time-series DB's own retention window and is
  not otherwise archived. Once that window rolls off, sub-15-minute detail is gone forever.
- **ASSUMED:** partitioning inside the TSDB is by device_id range (for per-device app
  queries), not by time — a layout that would make any fleet-wide time-windowed replay or
  backfill query (which the new pipeline explicitly avoids by partitioning Stage 0 by
  arrival time and never by device_id, per CLAUDE.md invariant 4) slow or impractical here.

**Retention policy — stated vs. observed:**
- Stated: **ASSUMED:** an old internal wiki page cites "90 days" raw retention in the TSDB;
  aggregated rollups "kept indefinitely."
- Observed: **ASSUMED — genuinely unverified, flagged as a gap rather than guessed away:**
  nobody has actually confirmed the 90-day figure still holds; it is plausible (though
  unconfirmed) that raw data is pruned earlier, around 45–60 days, whenever the TSDB comes
  under storage pressure, since no automated proof of the stated window has ever been run.
- **ASSUMED:** no distinct enforcement mechanism beyond "the TSDB's own default retention
  policy, set once at deployment and possibly never revisited" — i.e., closer to ad hoc
  than to a deliberately tiered raw/aggregate policy.

**Known reliability issues / data drops:**
- **ASSUMED:** each Powerwall gateway buffers locally during a home internet/Wi‑Fi outage,
  with a limited local buffer (assumed on the order of hours, not days); on overflow it is
  assumed to drop **oldest-first**, so a multi-day outage (a real and common residential
  failure mode) silently loses the earliest part of the gap with no record it ever existed.
- **ASSUMED:** no ingestion-side backpressure/dedup logic — retries on a missed ack are
  assumed to be resent verbatim with a *new* message identifier, which would defeat any
  downstream dedup keyed on message ID rather than payload content. This is offered as a
  plausible reason the new pipeline's Stage 1 MERGE key is `(device_id, device_ts,
  payload_hash)` (CLAUDE.md invariant 2) rather than message ID — an assumed real gap this
  invariant would fix.
- **ASSUMED:** no completeness/freshness monitoring exists for this source today; the only
  detection mechanism is reactive — a customer noticing gaps in their app history and
  filing a support ticket, or a warranty/RMA investigation surfacing a gap after the fact.
- **ASSUMED:** no known formal postmortems exist for this path (or none are indexed
  anywhere this audit could plausibly find) — itself worth flagging honestly as "unknown"
  rather than invented as "none."

**Access method for extracting historical data:**
- **ASSUMED:** a ticket-based request to the Home Energy Products team, granting read access
  to the warehouse rollup table; direct TSDB access is assumed rarer and more tightly held.
- **ASSUMED:** extraction is ad hoc SQL against the warehouse table; large (>30 day) range
  queries are assumed to be slow or to time out, since the table was not designed for bulk
  historical export.
- **ASSUMED:** no non-production/sample copy exists; any exploratory work would touch the
  same production warehouse table other product analytics rely on.

**Rough volume / scale estimates:**
- **ASSUMED:** ~800k–900k devices, ~1 message per 3 minutes per device on average →
  roughly 4,500–5,000 messages/sec fleet-wide, each ~1–2 KB JSON.
- **ASSUMED:** aggregate stored volume in the warehouse on the order of low-tens-of-TB and
  growing steadily with fleet size; the TSDB's raw tier is smaller due to its short window.
- **ASSUMED:** peak/burst behavior after a regional grid event (many homes losing/regaining
  power near-simultaneously) is plausible and would stress both the buffering behavior above
  and any (nonexistent) backpressure handling, but this has not actually been observed.

**Anything else notable:**
**ASSUMED:** none identified beyond the above; flagged as an open item for the real audit
rather than left blank.

---

### ASSUMED Source 2: Megapack/Powerpack Legacy Telemetry Backend

**Owner / team:** **ASSUMED:** Grid Storage Engineering, working closely with a commercial
deployment/commissioning team — assumed because Megapack/Powerpack units are commissioned
individually at customer sites rather than self-onboarded via a consumer app, implying more
white-glove, per-site human involvement than Powerwall.

**Device classes / data types covered:**
**ASSUMED:** Megapack and Powerpack are assumed to **share one legacy ingestion path**,
unlike Supercharger stalls/cabinets (see below) — the working assumption is that both are
architecturally similar utility/commercial-scale battery systems deployed and commissioned
by the same team using the same site-controller software, so it would have been unusual for
that team to build two separate backends for what is largely the same hardware family at
different scale. Carries pack-level state of charge, grid-frequency/voltage response,
thermal management status, and fault codes. **ASSUMED:** a much smaller fleet than Powerwall
— on the order of 5,000–15,000 units total, most of the remaining ~1.1M fleet count being
made up by Supercharger hardware instead.

**Ingestion protocol / format:**
- Transport: **ASSUMED:** a per-site controller aggregates multiple units on-site (via an
  internal fieldbus/Modbus-like link) and is the thing that actually talks to the backend,
  over HTTPS — individual packs are not assumed to have their own independent WAN uplink.
- Message format: **ASSUMED:** a stricter, versioned binary/protobuf-like schema — grid-code
  compliance and utility contractual reporting requirements plausibly forced more rigor here
  than on the consumer-facing Powerwall path, making this the one legacy source assumed to
  already resemble the new pipeline's schema discipline more closely (though still without
  Stage-0-style immutable raw landing — see storage below).
- Batching: **ASSUMED:** per-site batches covering all units at that site in one payload,
  sent on a fixed interval (assumed minutes, not seconds).

**Current storage location and format:**
- **ASSUMED:** a relational database, sharded **per region or per deployment**, with nightly
  export to a central warehouse. Raw payloads are assumed to be kept in the regional shard
  itself (not immediately transformed away, unlike Powerwall).
- **ASSUMED key gap:** no clear cross-region consolidation exists; a region's data prior to
  that region being wired into the central warehouse export job is assumed to remain
  effectively siloed and hard to discover fleet-wide.

**Retention policy — stated vs. observed:**
- Stated: **ASSUMED:** "retained indefinitely," plausible given utility contracts often
  carry long-term performance/warranty reporting obligations.
- Observed: **ASSUMED:** in practice, older regional shards are assumed to be rotated to
  cold/archival storage with no documented, tested restore procedure — meaning "indefinite"
  retention may be true in principle but not practically retrievable within any reasonable
  SLA. This discrepancy (documented "indefinite" vs. observed "restorable only in theory")
  is exactly the kind of stated-vs-observed gap Section 2's instructions call out.

**Known reliability issues / data drops:**
- **ASSUMED — larger blast radius than Powerwall:** because a single site controller is
  assumed to be the sole path for every unit at that site, a site-controller outage is
  assumed to take the *entire site* dark simultaneously, with no per-unit fallback path —
  a materially worse failure mode than Powerwall's one-device-per-household blast radius.
- **ASSUMED:** no fleet-wide expected-unit-count reconciliation exists; detection of a
  dropped site is assumed to be manual, surfacing via a utility SLA complaint or a
  commissioning engineer noticing a site has gone quiet during unrelated site work.
- **ASSUMED:** dedup behavior on reconnect is unknown/unverified — flagged honestly as an
  open question rather than assumed either way, since per-site batching could plausibly
  either mask or amplify duplicate-delivery behavior versus the per-device case.

**Access method for extracting historical data:**
- **ASSUMED:** per-region database credentials, requested via a data access form; assumed
  historically held by only a small number (~2) of engineers, making this the most
  access-constrained of the four sources.
- **ASSUMED:** extraction is a manual full-shard database dump handed over per incident —
  no self-serve query tool — making bulk historical backfill from this source
  labor-intensive even where the data still exists.
- **ASSUMED:** no non-production sample dataset exists.

**Rough volume / scale estimates:**
- **ASSUMED:** low overall message rate (small fleet) but larger per-message payload size
  (aggregated multi-unit, multi-KB per site batch) than Powerwall's per-device messages.
- **ASSUMED:** burst behavior is assumed to be *whole-site*, not per-device — a site
  reconnecting after an outage is assumed to resend a backlog for every unit at that site
  at once, a "thundering herd" shaped differently from Powerwall's per-household bursts.

**Anything else notable:**
**ASSUMED:** possible undocumented per-region protocol drift (a region's controller
software forked independently before central standardization) — plausible given the
regional-shard storage model above, but unverified; flagged as a specific thing the real
audit should check rather than assumed true or false here.

---

### ASSUMED Source 3: Supercharger Stall Legacy Telemetry Backend

**Owner / team:** **ASSUMED:** Charging Infrastructure Backend team.

**Device classes / data types covered:**
**ASSUMED:** Supercharger stall telemetry only — charging session state, connector
current/voltage/power, connector temperature, and fault codes (module fault, thermal,
comms — matching the fault families already established in
`tests/fixtures/generators/supercharger.py`). **ASSUMED:** covers the currently-fielded
stall firmware generations broadly, but see the fleet-segment gap called out in Section 5
regarding older/retired firmware.

**Ingestion protocol / format:**
- Transport: **`mqtt_batch`** — this matches the protocol already established for
  `supercharger_stall` in `tests/fixtures/generators/supercharger.py` (an established repo
  fact about the device, not an assumption); the **legacy backend's handling of that
  transport** is what's assumed below.
- Message format: **ASSUMED:** JSON, batched per active charging session with a variable
  batch size (consistent with the generator's variable `max_batch_size` behavior).
- Schema versioning: **ASSUMED:** no central schema catalog — each downstream consumer is
  assumed to have maintained its own ad hoc per-firmware parsing logic keyed off a raw
  `fw_version` string, with no shared source of truth for what each firmware's fields mean.
  This is offered as a plausible motivating gap for why the new pipeline centralizes this
  in `catalog/signals.yaml` (one canonical signal catalog per firmware) and versioned
  `parsers/<class>/<firmware>/` code — the legacy path is assumed to have neither.

**Current storage location and format:**
- **ASSUMED — the most consequential assumed gap for this project:** raw per-reading
  telemetry is assumed to flow through a stream processor straight into an aggregated
  `charging_sessions` fact table (total energy delivered, session duration, fault
  flag/code) — with **sub-session, per-reading granularity not retained** beyond a short
  raw window (see retention below). This would mean the legacy backend cannot answer many
  of the reliability questions this project cares about (e.g., per-reading gap/lateness
  patterns within a session) even where a session technically "has data," because that
  detail was never preserved past the short raw-retention window.
- **ASSUMED:** no dedicated raw immutable landing layer analogous to Stage 0 exists;
  transformation into the aggregate table is assumed to happen close to ingestion time,
  with no replay-from-raw capability once the raw window expires. This is offered as a
  plausible reason CLAUDE.md invariant 1 (Stage 0 append-only, the replay source) and
  invariant 7 (replay must reproduce production exactly) are treated as first-class design
  requirements in the new system — properties this assumed legacy path is assumed to lack.

**Retention policy — stated vs. observed:**
- Stated: **ASSUMED:** raw MQTT payloads retained ~7 days for debugging, then deleted;
  session-summary rows retained indefinitely.
- Observed: **ASSUMED:** under load (e.g., assumed holiday-travel charging peaks), raw
  retention is assumed to sometimes shrink to 3–4 days in practice as storage is reclaimed
  faster than the stated policy implies — another stated-vs-observed gap, flagged rather
  than resolved.

**Known reliability issues / data drops:**
- **ASSUMED, and directly modeled by this repo's own synthetic generator's stated intent:**
  stalls buffer locally during connectivity outages and burst the buffer on reconnect,
  dropping some fraction of buffered readings if the outage outlasts the local buffer —
  this mirrors exactly the behavior `tests/fixtures/generators/supercharger.py` says it
  exists to synthesize ahead of real measurement (P0-09), so it is assumed here as the most
  defensible guess for what real stalls plausibly do, while remaining unconfirmed.
- **ASSUMED — a specific, previously-undetected drop mode:** because the legacy stream
  processor is assumed to have no reconciliation against an *expected* session-reading
  count, a session that gets fully or partially dropped mid-outage is assumed to be
  **indistinguishable from a session that simply ended normally** — i.e., a real drop
  event is assumed to leave no trace that anyone would currently notice.
- **ASSUMED dedup gap:** if legacy dedup (where it exists at all) keys on `message_id`
  rather than payload content, it would assumed-fail to catch exactly the kind of
  duplicate this repo's generator injects (a resend with a new `-retry`-suffixed
  `message_id` but identical `device_ts`/`payload_hash`) — offered as a concrete, plausible
  reason the new pipeline's MERGE key explicitly includes `payload_hash` rather than a
  message identifier (CLAUDE.md invariant 2).
- **ASSUMED:** no known formal incident postmortems for this path could plausibly be
  located without real access; flagged as unknown rather than invented.

**Access method for extracting historical data:**
- **ASSUMED:** ticket-based access via on-call engineering; session-summary data (retained
  indefinitely) is assumed to be bulk-extractable, but raw sub-session telemetry is assumed
  to be **unavailable for backfill beyond roughly the retention window** (~1 week) once
  it's gone — a real constraint on how far back this project could lean on this source for
  historical backfill, independent of what the real audit eventually confirms.

**Rough volume / scale estimates:**
- **ASSUMED:** the highest per-device message rate of the four sources, given frequent
  in-session sampling (illustratively similar in shape, though not necessarily in exact
  value, to this repo's own synthetic generator's 15-second reading interval default —
  that default is explicitly a generator convenience, not evidence about real cadence, and
  is cited here only to keep the assumption internally consistent with established repo
  facts, not as a measurement).
- **ASSUMED:** stall fleet count plus cabinet fleet count together are assumed to make up
  most of the remaining ~1.1M devices not accounted for by Powerwall/Megapack/Powerpack
  above.

**Anything else notable:**
**ASSUMED:** none identified beyond the above.

---

### ASSUMED Source 4: Supercharger Cabinet Legacy Telemetry Backend

**Owner / team:** **ASSUMED:** same Charging Infrastructure Backend team as stalls
organizationally, but — per the judgment call below — a **separate legacy ingestion
system**, not a shared one.

**Judgment call — why stall and cabinet are assumed to be separate legacy paths, not one:**
The per-data-source template explicitly flags this as a real open question ("Supercharger
stalls and cabinets may not [share a path]"), and `tests/fixtures/generators/supercharger.py`
already establishes, as fact, that the two use genuinely different transports today
(`mqtt_batch` for stalls vs. `https_poll` for cabinets). A push-based MQTT broker and a
pull-based HTTPS polling scheduler are different enough pieces of infrastructure (different
failure modes, different operational surface) that it is assumed more plausible they grew
up as two separate legacy backends — possibly built at different times, for different
original purposes (charging-session product telemetry vs. site/facilities health
monitoring) — than that one team built and maintained a single system straddling both
transports. This is an assumption, not a confirmed fact, and the real audit should
explicitly verify or refute it.

**Device classes / data types covered:**
**ASSUMED:** cabinet-only — grid voltage/frequency at the cabinet, transformer temperature,
contactor state, active-stall count, aggregate power, and cabinet-level fault codes (fault
families consistent with `tests/fixtures/generators/supercharger.py`'s cabinet model).

**Ingestion protocol / format:**
- Transport: **`https_poll`** — established repo fact for `supercharger_cabinet`
  (`tests/fixtures/generators/supercharger.py`); the legacy backend's use of it is assumed
  to be a **central poller service** that periodically hits each cabinet's local API,
  rather than cabinets pushing data themselves.
- Message format: **ASSUMED:** JSON, one poll response per cabinet per interval; the
  polling interval is assumed to be longer than a stall's reporting cadence (cabinets
  aggregate slower-changing site-level state), consistent with the generator's own
  assumption that cabinets report on an interval several times longer than stalls'.

**Current storage location and format:**
- **ASSUMED:** feeds a site-health/facilities-monitoring system oriented around real-time
  alerting thresholds (e.g., transformer over-temp, contactor stuck open) rather than
  historical analytics — stored in a time-series DB tuned for dashboards and alerts, not
  for long-range querying.

**Retention policy — stated vs. observed:**
- Stated: **ASSUMED:** ~30 days, since the primary consumer (real-time alerting) has no
  strong need for longer history.
- Observed: **ASSUMED — genuinely unverified, flagged honestly rather than guessed:**
  nobody is assumed to have actually checked whether the stated 30-day window is what's
  really enforced; recorded here as an open gap, not resolved either way.

**Known reliability issues / data drops:**
- **ASSUMED — a structurally different drop pattern from the stall path:** because this
  path is poll-based (pull), an outage of the **central poller itself** (not the cabinet)
  would be assumed to produce a gap in the data that looks identical, from the stored data
  alone, to the cabinet itself being silent — a "which side actually failed" blind spot
  that a push-based system doesn't have in the same way.
- **ASSUMED:** on a timed-out poll, the assumed behavior is to silently skip that interval
  with no retry — meaning cabinet data drops are assumed to be **quantized to the poll
  interval**, a different shape than the stall path's outage/buffer/burst pattern.
- **ASSUMED:** no completeness monitoring beyond whatever alerting thresholds already exist
  for the metrics themselves (e.g., an over-temp alert firing) — a *missing* poll is
  assumed to not itself be alerted on, only an *out-of-range value* is, meaning a fully
  silent cabinet could go unnoticed if none of its last-known values were already in an
  alerting state.

**Access method for extracting historical data:**
- **ASSUMED — an access silo despite shared team ownership:** because this data is assumed
  to live in an ops/facilities-facing monitoring stack (plausibly a vendor SaaS dashboard
  product) rather than an internal engineering database, access is assumed to require a
  *different* request process (a vendor account/license) than the stall path's engineering
  DB grant, even though both are nominally owned by the same team.

**Rough volume / scale estimates:**
- **ASSUMED:** lower message rate than stalls (longer poll interval, aggregate rather than
  per-session metrics); cabinet count is assumed to be roughly proportional to site count
  (one or a small number of cabinets per site) rather than to stall count directly, since
  one cabinet is assumed to serve multiple stalls at a site.

**Anything else notable:**
**ASSUMED:** if the vendor-SaaS storage assumption above is correct, a vendor contract
change or platform migration could plausibly have silently changed retention/format in the
past with no internal record — flagged as a specific thing worth asking about directly in
the real audit (see interview question 10) rather than assumed true or false here.

---

### Blank template (unmodified — for use by the real audit)

The block below is the **original, unfilled** per-source template, preserved exactly as
originally drafted for whoever performs the real audit. It has **not** been populated with
assumptions — copy it per newly discovered real source, the same way Section 2's
instructions describe.

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

### ASSUMED closing checklist / compiled map (synthetic substitute — not real findings)

**This subsection does not satisfy the "done" criterion above.** It is the same synthetic
substitute exercise applied to Section 5's checklist, so a reviewer has a compiled view to
react to. Every line is `**ASSUMED:**` and unverified.

| # | Source | Owner (assumed) | Transport (assumed/established) | Format retained past ingest (assumed) | Stated retention (assumed) | Biggest assumed known drop |
|---|--------|------------------|----------------------------------|----------------------------------------|------------------------------|------------------------------|
| 1 | Powerwall | **ASSUMED:** Home Energy Products telemetry | **ASSUMED:** MQTT/TLS, JSON | **ASSUMED:** raw in TSDB (short window) + 15-min aggregates in warehouse | **ASSUMED:** 90d raw (doc) vs. **ASSUMED:** ~45–60d observed | **ASSUMED:** oldest-dropped local buffer overflow on multi-day home outage, undetected until a customer complains |
| 2 | Megapack/Powerpack | **ASSUMED:** Grid Storage Engineering | **ASSUMED:** per-site controller over HTTPS, versioned binary/protobuf-like | **ASSUMED:** raw kept in regional relational shards | **ASSUMED:** "indefinite" (doc) vs. **ASSUMED:** practically unrestorable from cold archive | **ASSUMED:** whole-site outage (single site-controller is a single point of failure for every unit at that site) |
| 3 | Supercharger stall | **ASSUMED:** Charging Infrastructure Backend | **`mqtt_batch`** (established) | **ASSUMED:** aggregated into session-summary table; raw sub-session detail not retained past a short window | **ASSUMED:** 7d raw (doc) vs. **ASSUMED:** ~3–4d observed under load | **ASSUMED:** a dropped/partial session during a buffer-overflow outage is indistinguishable from a normal session ending — no reconciliation exists to catch it |
| 4 | Supercharger cabinet | **ASSUMED:** Charging Infrastructure Backend (separate system from stalls — see judgment call in Section 3) | **`https_poll`** (established) | **ASSUMED:** feeds an alerting-oriented TSDB, not analytics-oriented storage | **ASSUMED:** 30d (doc), **observed: genuinely unverified** | **ASSUMED:** a poller-side outage is indistinguishable from a silent cabinet; missed polls are not themselves alerted on |

**ASSUMED explicit gap (per Section 5's requirement that gaps be called out, not left as an
absent row):** it is assumed plausible that a small population of very old/retired stall
firmware — predating the earliest firmware currently modeled in
`tests/fixtures/generators/supercharger.py` (`2.1.4`/`1.8.2`) — may still be physically
present in the field on legacy hardware batches, potentially served by an even older,
undocumented radio-gateway path with **no modern telemetry backend at all**. This is
recorded here as an assumed gap to specifically check for in the real audit, not resolved
one way or the other.

**ASSUMED systemic gap across all four sources:** no source above is assumed to have any
working reconciliation against an independent fleet-inventory/provisioning system's expected
device count (interview question 17) — meaning the fraction of the ~1.1M fleet actually
reporting at any given time is assumed to be genuinely unknown today across the board, not
just per-source.

**What would need to happen for this checklist to actually satisfy Section 5:** every
`**ASSUMED:**` marker above replaced by a cited, verified answer from a real interview or a
real system query, reviewed by the ticket's Staff-level owner, before P0-13 cites this
document as anything other than a synthetic placeholder.
