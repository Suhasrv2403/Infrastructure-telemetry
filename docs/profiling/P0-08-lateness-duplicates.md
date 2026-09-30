# P0-08: Lateness and duplicate profiler

**Ticket:** P0-08 ("Lateness and duplicate profiler"), Phase 0, DISC.
**Done when:** Lateness distribution and duplicate rate per class.
**Code:** `profiling/lateness_duplicates/profiler.py`, tested in
`tests/unit/test_lateness_duplicates_profiler.py`.

## Scope note

Real Supercharger network access doesn't exist yet (gated on later Phase 1 tickets), so this
report profiles the synthetic fixture generator's output
(`tests/fixtures/generators/supercharger.py`) captured through P0-05's Stage 0 batch capture
path, exactly as P0-05 itself scopes "one region" for now. **Everything below describes the
generator's modeled lateness/duplicate behavior, not confirmed production reality.** Once real
(or real-shaped) Stage 0 objects exist, re-run this profiler against them and replace these
numbers before anyone treats them as a production signal.

All numbers below are real output from running:

```
python3 -m profiling.lateness_duplicates.profiler
```

against `GeneratorConfig()` defaults (`devices_per_firmware=4`, `seed=1337`) - nothing here is
hand-typed or estimated.

## Method

Per `device_class`:

- **Lateness** = `(envelope arrival_ts_ms - reading device_ts_ms) / 1000`, in seconds, for
  every reading whose `device_ts_ms` passes a basic plausibility guard (`is not None and > 0`).
  This is intentionally *not* P0-07's full clock-skew profiler - just enough to keep
  missing/epoch-default timestamps from poisoning the distribution. Reported as
  min/median/p90/p99/max via linear-interpolation percentiles (numpy's default method,
  reimplemented by hand - no numpy dependency).
- **Duplicates** use exactly Stage 1's future merge key
  (`device_id, device_ts_ms, payload_hash` - CLAUDE.md invariant 2). A reading-key is
  "duplicated" if it appears on more than one message envelope. Message-level pure/partial
  classification walks messages in `arrival_ts_ms` order and checks whether *all*, *some*, or
  *none* of a message's reading-keys were already seen on an earlier message.

Neither computation reads `_debug_injected_issues` - see the profiler module's docstring for
why that matters.

## Lateness distribution

| device_class | readings considered | skipped (implausible ts) | min (s) | median (s) | p90 (s) | p99 (s) | max (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| supercharger_cabinet | 11,492 | 319 | -1,192,914.0 | 240.0 | 605.0 | 71,101.4 | 1,143,515.0 |
| supercharger_stall   | 5,160  | 140 | -1,198,003.0 | 60.0  | 946.0 | 597,310.0 | 1,181,186.0 |

**Read the median/p90 numbers against the batching caveat, not on their own.** The generator
anchors each message's `arrival_ts_ms` to its *last* reading's `device_ts_ms` (plus jitter) -
see `_emit_device_messages`'s `base_arrival_ms = last_ts_ms`. That means every reading in a
batch *except the last* shows positive lateness purely from being batched together, with zero
network delay or retry involved: a stall batching up to 12 readings at a 15s cadence can show
~165s of "lateness" on its earliest reading under completely normal operation; a cabinet
batching up to 12 readings at a 60s cadence, ~660s. The stall median (60s) and p90 (946s) and
the cabinet median (240s) are consistent with this batching effect dominating the body of the
distribution, not with genuine network/retry delay - this profiler doesn't currently separate
"lateness from batch position" from "lateness from actual late arrival," which would need a
per-batch-position breakdown (a natural follow-up, out of scope here).

**The extreme tails (p99, max, and the large negative min) are not batching - they're two
distinct, real interactions with timestamp corruption worth calling out explicitly:**

1. **Negative min (~-1,192,914s to -1,198,003s, ~13.8 days):** the generator's
   `future_timestamp` corruption can push an individual reading's `device_ts_ms` up to
   `future_offset_max_s` (14 days) ahead of its true time. The plausibility guard
   (`device_ts_ms > 0`) doesn't filter this out - correctly, since a real device could plausibly
   send a bogus-but-positive future timestamp - so it shows up as strongly negative lateness.
   This is a real, useful signal (a device with corrupted future timestamps is a device worth
   flagging), not a profiler bug.
2. **Extreme positive tail (p99, max):** more interesting. Checked directly against the
   readings that produce it: 131 of the stall class's high-lateness readings (>400,000s) sit in
   batches whose *last* reading's `device_ts_ms` is itself missing or epoch-default (`0`). The
   generator's batching code falls back to `SYNTHETIC_NOW_MS` (2026-06-01T00:00:00Z, its fixed
   synthetic "now") as the arrival anchor whenever the last reading's timestamp is falsy. Since
   these devices' true readings are dated about a week before that synthetic "now," every other
   reading in that one corrupted batch appears to arrive ~7 days late - not because of any
   modeled network or retry behavior, but because a single corrupted last-in-batch timestamp
   drags the whole batch's apparent arrival time to "now." This affects a small but consistent
   slice of messages: 22/847 (2.60%) of stall messages and 39/1,784 (2.19%) of cabinet messages
   have a missing/epoch-default last-reading timestamp, and it alone accounts for the p99/max
   values above. Worth flagging to whoever next touches the generator's `_emit_device_messages`
   (not this ticket's job to fix) - a real device isn't likely to reproduce "anchor silently
   snaps to now" the same way, so this specific tail shape is a generator artifact, not
   something to extrapolate to production lateness expectations.

## Duplicate rate

| device_class | distinct reading-keys | duplicated keys | duplicate key rate | excess instance rate | messages | pure-duplicate messages | partial-overlap messages |
|---|---:|---:|---:|---:|---:|---:|---:|
| supercharger_cabinet | 11,507 | 304 | 2.64% | 2.57% (of 11,811 total reading instances) | 1,784 | 45 (2.52%) | 0 (0.00%) |
| supercharger_stall   | 5,168  | 132 | 2.55% | 2.49% (of 5,300 total reading instances)  | 847   | 22 (2.60%) | 0 (0.00%) |

- **Duplicate key rate** = fraction of distinct `(device_id, device_ts_ms, payload_hash)`
  reading-keys that show up on more than one message. ~2.5-2.6% for both classes here, close to
  the generator's configured `duplicate_message_rate=0.03` (message-level retransmit
  probability), as expected since the generator's only mechanism for exact key repeats is its
  deliberate duplicate-message injection (`_maybe_duplicate`).
- **Zero partial-overlap messages** in this run: every generator-injected duplicate is an exact
  deep copy of a full prior envelope (`_maybe_duplicate` copies the whole message), so it always
  classifies as "pure" - a partial overlap (a retransmit that only repeats *some* of a batch's
  readings) isn't something this generator currently models. Real device retransmission could
  plausibly produce partial overlaps (e.g. a device that re-sends a batch after a partial ack),
  so a 0% partial rate here is a generator-scope statement, not a claim that partial overlaps
  won't happen against real traffic.
- Cross-checked in `tests/unit/test_lateness_duplicates_profiler.py` against the generator's
  own `duplicate_message` stat counter (and, separately, against `_debug_injected_issues`
  entries starting with `duplicate_of:`): the profiler's `pure_duplicate_messages` count matches
  that ground truth *exactly*, per class, confirming the reading-key-based detection correctly
  recovers every injected retransmit with no false positives or misses in this generator model.

### Forward-looking: this is a preview of Stage 1's merge workload

The duplicate-rate metric here is computed on exactly the key Stage 1's MERGE will use
(CLAUDE.md invariant 2: `MERGE on (device_id, device_ts, payload_hash)`, never a plain append).
At ~2.5% of reading-keys duplicated and ~2.5% of messages being pure retransmits in this
generator run, whoever builds **P1-06 (idempotent merge)** should expect Stage 1's merge step to
be doing real de-duplication work on every batch, not handling a rare edge case - and should
design/test the merge for a workload where a non-trivial minority of incoming reading-keys
already exist in the target partition. The corollary is also worth carrying forward: because
`_maybe_duplicate` here always produces *exact* full-message repeats, this generator run alone
doesn't exercise partial-batch retransmits or key collisions with subtly different payloads
(same device_id/device_ts, different payload_hash) - both plausible real-world cases P1-06's
tests should construct by hand rather than assuming the generator will produce them.

## Reproducing this report

```
python3 -m profiling.lateness_duplicates.profiler                 # human-readable
python3 -m profiling.lateness_duplicates.profiler --json          # full ClassProfile as JSON
python3 -m profiling.lateness_duplicates.profiler --seed 7 --devices-per-firmware 8
```

## Limitations / follow-ups

- No per-batch-position breakdown of lateness (would separate "batching lateness" from
  "arrival lateness" cleanly - see the median/p90 caveat above).
- No per-firmware breakdown (device_class only, per the ticket's "done when"); firmware quirk
  multipliers in the generator (`FIRMWARE_QUIRK_MULTIPLIER`) suggest older firmware would show
  worse duplicate/lateness numbers than a class-level average shows.
- Partial-overlap duplicate detection is implemented and tested but never exercised by this
  generator (see above) - worth a hand-built fixture in a future ticket if partial-batch
  retransmit behavior needs dedicated coverage before P1-06.
