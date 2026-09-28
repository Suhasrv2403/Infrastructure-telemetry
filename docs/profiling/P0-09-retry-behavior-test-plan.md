# P0-09: Device retry/buffer/drop behavior - fault injection test plan

**Ticket:** P0-09 ("Device retry-behavior test with fault injection")
**Owner:** Senior SRE + firmware
**Done when:** Retry, buffer or drop behavior documented per firmware
**Status of this document:** a proposed test plan for the humans who own this ticket to
execute and adapt. It is not itself the "done when" result.

## Why this document exists, and what it is not

CLAUDE.md scopes "firmware fault-injection tests" against real hardware as explicitly
**human-owned**, alongside gate reviews, prod-data changes and access requests. Nothing in
this repo, including the detector described below, can substitute for actually inducing a
connectivity fault on a real Supercharger stall or cabinet and watching what its firmware
does. This document is the deliverable an agent *can* produce for P0-09: a concrete plan for
the real test, plus motivating hypotheses about what to look for, derived from a synthetic
model - not from any real device.

**Nothing in Part A below is a confirmed finding about real device behavior.** It describes
patterns a *simulation* of buffer/burst/drop behavior leaves in synthetic arrival data, offered
only as hypotheses worth checking against real firmware. Part B is the actual test plan.

## Part A - synthetic-proxy motivating context (hypotheses, not findings)

### What was built

`profiling/retry_behavior/profiler.py` implements a heuristic detector: given one device's
messages sorted by arrival time, it flags a candidate "buffer-and-burst" event where (a) the
gap since that device's previous message is unusually long relative to its own normal cadence,
and (b) the message's batch size isn't a trivially small leftover. It reads only fields a real
device's envelope could plausibly send (`arrival_ts_ms`, `readings`) - never the fixture
generator's own `_debug_injected_issues` ground-truth field.

Run against `tests/fixtures/generators/supercharger.py`'s synthetic outage simulation (which
buffers/drops/bursts readings by construction, purely to give Phase 0 something to build
against before real telemetry exists - see that module's docstring), the detector's measured
accuracy against the generator's own known ground truth was, honestly:

- **Recall (does the detector catch the true burst message): ~40-65%** across several random
  seeds at the generator's default scale (small sample, ~6-10 true events per run - treat the
  exact number as a ballpark, not a precise estimate).
- **Precision (of flagged candidates, how many are real): ~3-4%.** Most flagged candidates are
  false alarms. The dominant confound: the generator independently injects `late_arrival` on
  ~6% of messages (a single message simply delayed, unrelated to any outage), which produces
  the *same* "isolated long gap since this device's last message" signature as a true
  outage-then-reconnect, from the arrival-time series alone. A gap-based signal cannot
  distinguish "one message got delayed on the way in" from "the device went quiet and is now
  catching up," using only arrival timing.
- Batch size was a much weaker disambiguating signal than intuition suggests: in this
  generator's implementation, the post-outage message is simply whichever batch happens to
  fall last in the device's stream, sized by the same random draw as every other batch - it is
  **not** reliably larger than that device's typical batch. A literal "batch size much bigger
  than usual" filter, which is the naive expectation, drove recall toward zero when tested.

Full numbers and methodology: `tests/unit/test_retry_behavior_profiler.py` (see especially the
comment block in `test_detector_against_generator_ground_truth_reports_honest_accuracy`).

### Hypotheses to validate against real hardware

These are things worth specifically watching for in Part B's real test, precisely *because*
the synthetic model above suggests arrival-timing alone won't cleanly separate them:

1. **A single delayed message and a true buffer-then-flush may look identical from arrival
   timing alone.** Real firmware, unlike the synthetic model, may leave other signals that
   disambiguate them - e.g. a flushed buffer's readings likely span a wider `device_ts` range
   than their message count would suggest for normal traffic, even if the *message-level*
   batch size doesn't look unusual. Worth checking directly in Part B (compare `device_ts`
   span per message, not just reading count).
2. **Whether batched/buffered readings arrive in one oversized message, several
   normal-sized messages back-to-back, or are silently dropped is a genuine per-firmware
   unknown** - the synthetic model deliberately doesn't know the real answer here (it just
   picks one of these behaviors as a stand-in). This is exactly what Part B needs to establish.
3. **Older/cheaper firmware likely has a smaller local buffer and drops more under a longer
   outage** is a reasonable prior (reflected as a difficulty multiplier in the synthetic
   generator, itself just an assumption, not a measurement) but is unverified and should be a
   specific comparison axis across firmware versions in Part B.

## Part B - real fault-injection test plan

### Scope and safety

- **Test environment only.** Never run against the production Supercharger fleet or any site
  currently serving customers. Use a lab/bench rig or a site explicitly designated for
  firmware testing (coordinate with site ops before use).
- **Coordinate with the firmware team before every run.** They should confirm which firmware
  builds are in scope, whether any build has known watchdog/recovery quirks that could turn a
  short test fault into a longer outage, and how to safely recover a device that doesn't
  reconnect on its own (manual power cycle procedure, on-site contact).
- **One device under test at a time per condition**, at least initially, so a device that
  reboots or otherwise misbehaves doesn't confound results for others sharing the same network
  segment.
- **Do not induce faults that could affect charging safety** (e.g. don't fault-inject during an
  active vehicle charging session on hardware that isn't isolated from real vehicles). Bench/lab
  rigs with dummy loads are strongly preferred over live stalls.
- Get sign-off from Senior SRE + firmware leads on the specific device units, firmware
  versions and fault durations before running anything beyond the shortest/mildest condition.

### Devices and firmware in scope

Per firmware version actually seen in the field (via P0-11's access work once granted, or
whatever bench units are available) for both `supercharger_stall` and `supercharger_cabinet`.
Prioritize the firmware versions with the most fleet devices first, since this is what most
determines the aggregate telemetry-completeness impact once P1-01+ are live.

### Fault conditions to induce

Run each condition for a **short duration first** (see durations below), observe, then escalate
only if the device recovers cleanly. Suggested conditions, roughly in order of realism to a
real network fault and of increasing severity:

1. **Brief network blackout, varying duration.** Physically or via a managed switch port,
   cut the device's network path (not just rate-limit) for: 30s, 2min, 15min, 1hr, 4hr. Use a
   fixed set of durations across every firmware version tested, so results are comparable.
2. **DNS/broker unreachable, network otherwise up.** Block resolution or connectivity to just
   the MQTT broker / ingest endpoint (e.g. via firewall rule on the test network), leaving the
   device's general network path alive. This isolates "can't reach our service" from "no
   network at all," which firmware may handle differently (e.g. retry logic may key off TCP
   connect failures specifically).
3. **Partial packet loss / high latency**, not a hard outage - e.g. 20-50% loss or added
   200-2000ms latency via `tc netem` (or equivalent) on the path for 5-15min. Tests whether
   firmware treats degraded connectivity like an outage (buffers) or keeps trying to stream
   live (and how much it drops under sustained loss).
4. **Repeated short outages** (e.g. 10s blackout every 60s for 10min) to see whether firmware
   treats a flapping connection differently from one sustained outage of the same total
   downtime - a real-world pattern (marginal cell/wifi signal) that a single long blackout
   test wouldn't reveal.

### What to measure per firmware version, per condition

For each run, capture and record:

- **Does the device buffer locally and flush on reconnect, drop silently, retry
  immediately (re-sending on a tight loop), or reboot/reset its connection stack?** This is
  the core "done when" question. Look for:
  - A burst of data on reconnect covering the outage window (buffer-and-flush) vs. a gap in
    the data with no catch-up (drop) vs. immediate small retries throughout the outage
    (aggressive retry, likely against the ingest layer's buffer/backpressure) vs. a device-side
    reboot/reconnect log entry (recovery via reset).
  - If it buffers: how much history does it actually keep (test with outage durations well
    past any suspected buffer limit to find where it starts dropping), and does it flush as
    one oversized message, several normal messages sent back-to-back, or something else? (Part
    A's finding suggests message-level batch size might NOT visibly signal a flush - check the
    payload's reading count and `device_ts` span directly, not just "does this message look
    big.")
  - Time-to-first-message after reconnect, and total messages/readings recovered vs. expected
    for the outage window (an explicit count, not just "some data came back").
- **Device-side logs/telemetry**, if firmware/engineering can expose them for the test: any
  local error/reconnect log, buffer-full or buffer-overflow indicators, retry-attempt counters.
  This is the most direct way to disambiguate "buffered X readings, dropped Y" from
  externally-inferred guesses, and should be prioritized over inferring behavior from arrival
  data alone wherever firmware can provide it.
- **Network-side capture** (packet capture or broker-side connection log) for exact
  reconnect timing, retry cadence and backoff pattern (fixed interval? exponential backoff?
  jittered?), and TLS/auth handshake behavior on reconnect (does it re-auth every retry, or
  reuse a session - relevant to how much load a fleet-wide reconnect storm would put on
  real ingest infrastructure).
- **Whether the fault duration matters** - i.e., does behavior change qualitatively (drop
  starts happening) past some duration threshold, or is it continuous/gradual. Record the
  crossover point per firmware if one exists.

### Run length per condition

- Each blackout/DNS-block condition: hold the fault for its assigned duration (30s through
  4hr per the list above), then observe for at least **2x the fault duration** afterward (or a
  minimum of 15min for the shortest faults) to confirm full recovery and catch delayed/slow
  buffer flushes.
- Partial-loss and flapping conditions: run for the full 10-15min window, then observe 15min
  of clean recovery afterward.
- Repeat each condition **at least 3 times per firmware version** before concluding a
  behavior is consistent - a single run could catch a coincidental reboot or an unrelated
  network blip.

### Output format

Document, per firmware version x device class: a short behavior classification (buffer-and-
flush / drop / immediate-retry / reboot-recover, or a mix depending on fault duration), the
buffer capacity estimate if applicable (in readings and/or wall-clock minutes), the
retry/backoff pattern observed, and any duration threshold where behavior changes. This is
the artifact that satisfies P0-09's actual "done when": *retry, buffer, or drop behavior
documented per firmware.*

### Downstream consumers of this data

Once real per-firmware behavior is documented, it directly informs:

- **P1-01** (production ingest buffer): sizing the accept-and-spool buffer needs to account for
  a fleet-wide reconnect burst after a shared network event, not just steady-state load.
- **Telemetry health / dropout detectors** (per CLAUDE.md's "Telemetry health" stage): a
  silence episode that's actually a buffered-and-recovering device looks different from one
  that's actually dropping data, and detector logic should be able to tell them apart once
  real per-firmware behavior is known - something Part A's synthetic detector could not
  reliably do and real device logs/behavior should resolve.
- **P0-10** (signal catalog): any buffer/retry-related fields firmware actually exposes
  (buffer depth, retry counters) are candidates for the canonical signal catalog.
