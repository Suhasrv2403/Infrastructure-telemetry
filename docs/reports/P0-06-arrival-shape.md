<!--
PROVENANCE: copied from branch `P0-06-arrival-shape-profiler` (commit c22a303), path `docs/profiling/P0-06-arrival-shape.md`.
This branch is not yet merged into trunk. This file is a read-only snapshot
for easy reading; the source branch remains the canonical, editable copy.
Consolidated into docs/reports/ on trunk (P0-04-orchestrator-partition-model).
-->

# P0-06: Arrival-shape profiling report

**Ticket:** P0-06 ("Arrival-shape profiler"). **Depends on:** P0-05.

## Scope note

Real Supercharger network/protocol access is still gated on later tickets (real device fleet
access is not yet in place - see the scope note in `pipeline/stage0_landing/capture.py`, P0-05).
This report profiles the **synthetic Supercharger fixture generator's** output
(`tests/fixtures/generators/supercharger.py`, `GeneratorConfig()` defaults, seed `1337`) as a
stand-in for real telemetry, using the arrival-shape profiler built for this ticket
(`profiling/arrival_shape/profiler.py`), run exactly as its CLI does:

```
python -m profiling.arrival_shape.profiler
```

Every number below is copy-pasted from that command's real output, not hand-derived or
estimated. **These findings describe the generator's modeled shape, not confirmed production
reality**, and should be revisited once real Stage 0 capture exists (post P0-05's downstream,
real-fleet-access tickets). The generator's own module docstring is explicit that its rates are
"illustrative defaults, not measurements."

The profiler itself never reads the generator-only `_debug_injected_issues` field - it computes
everything from fields a real Stage 0 message actually carries (see the module docstring in
`profiling/arrival_shape/profiler.py`), so it will run unchanged against real Stage 0 objects
once they exist.

## Run parameters

- Generator: `GeneratorConfig()` defaults (`devices_per_firmware=4`, `seed=1337`, all other
  fields at their default values).
- Total messages profiled: **2,631**
- Total readings profiled: **17,111**
- Groups: 5 `(device_class, firmware_version)` combinations - 3 stall firmwares (`2.1.4`,
  `2.3.0`, `3.0.1`), 2 cabinet firmwares (`1.8.2`, `1.9.0`), per `DEFAULT_FIRMWARE` in the
  generator.

## 1. Protocol distribution

Deterministic per class in the generator today, not a surprising finding: every
`supercharger_stall` message uses `mqtt_batch`, every `supercharger_cabinet` message uses
`https_poll`. Observed exactly (100% each way) across all 2,631 messages and all 5 firmware
groups - there is no protocol variation within a class to report yet. Worth re-checking once
real telemetry exists, since a real fleet could plausibly mix protocols within a class (e.g. a
firmware migration in flight) in a way this generator doesn't model.

## 2. Batching shape (readings per message)

| device_class | firmware | n msgs | min | median | p90 | max | mean |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| supercharger_cabinet | 1.8.2 | 917 | 1 | 7 | 11 | 12 | 6.4 |
| supercharger_cabinet | 1.9.0 | 867 | 1 | 7 | 11 | 12 | 6.8 |
| supercharger_stall | 2.1.4 | 295 | 1 | 6 | 11 | 12 | 6.2 |
| supercharger_stall | 2.3.0 | 252 | 1 | 6 | 11 | 12 | 6.3 |
| supercharger_stall | 3.0.1 | 300 | 1 | 6 | 11 | 12 | 6.3 |

All five groups top out at 12 readings/message, matching the generator's
`max_batch_size=12` config directly - batch size is uniformly random in `[1, max_batch_size]`
per the generator's `_batch()` function, which is visible in the histograms below (roughly flat
across sizes 1-12, no strong skew toward small or large batches). Cabinets batch very slightly
larger on average (mean ~6.4-6.8) than stalls (mean ~6.2-6.3) - a small effect, not something to
read much into given both draw from the same uniform-random batching logic; the difference
likely just reflects each group's different total reading counts interacting with batch-boundary
effects, not a real protocol-level distinction.

Full per-size histograms (batch size -> message count):

- `supercharger_cabinet / 1.8.2`: `{1: 79, 2: 87, 3: 66, 4: 73, 5: 66, 6: 77, 7: 88, 8: 88, 9: 72, 10: 86, 11: 76, 12: 59}`
- `supercharger_cabinet / 1.9.0`: `{1: 63, 2: 62, 3: 70, 4: 79, 5: 64, 6: 76, 7: 67, 8: 65, 9: 52, 10: 86, 11: 98, 12: 85}`
- `supercharger_stall / 2.1.4`: `{1: 21, 2: 36, 3: 20, 4: 31, 5: 28, 6: 24, 7: 25, 8: 31, 9: 19, 10: 12, 11: 23, 12: 25}`
- `supercharger_stall / 2.3.0`: `{1: 27, 2: 18, 3: 17, 4: 29, 5: 16, 6: 22, 7: 23, 8: 26, 9: 19, 10: 20, 11: 22, 12: 13}`
- `supercharger_stall / 3.0.1`: `{1: 29, 2: 27, 3: 31, 4: 26, 5: 21, 6: 18, 7: 28, 8: 25, 9: 18, 10: 21, 11: 41, 12: 15}`

## 3. Message size (bytes)

Measured as `len(json.dumps(msg, sort_keys=True).encode("utf-8"))` per envelope - the same
serialization `capture.py`'s `capture_messages()` writes to the landing bucket, so this
approximates real on-the-wire/on-disk envelope size. Note this includes the (small)
`_debug_injected_issues` field, since that's literally what lands in Stage 0 objects today; a
real, debug-field-free message will be marginally smaller than these numbers once real capture
replaces the generator.

| device_class | firmware | min (B) | median (B) | p90 (B) | max (B) | mean (B) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| supercharger_cabinet | 1.8.2 | 546 | 2,062 | 3,082 | 3,395 | 1,932.3 |
| supercharger_cabinet | 1.9.0 | 558 | 2,070 | 3,130 | 3,374 | 2,025.3 |
| supercharger_stall | 2.1.4 | 596 | 2,044 | 3,495 | 3,828 | 2,101.4 |
| supercharger_stall | 2.3.0 | 593 | 2,048 | 3,486.9 | 3,799 | 2,122.4 |
| supercharger_stall | 3.0.1 | 593 | 2,049 | 3,493.1 | 3,798 | 2,133.9 |

Every group's envelopes fall almost entirely in the 512 B - 4 KB range (no messages under 256 B
or over 8 KB observed), tracking directly with batch size 2 above - a single-reading message
lands around 500-600 bytes, and a full 12-reading batch lands just under 4 KB. Stall messages
run slightly larger than cabinet messages at the same percentile despite similar batch-size
stats, consistent with stall readings carrying a few more/longer fields (session id, fault code,
multiple power/energy/temperature values) than cabinet readings.

Size histograms (bucket -> message count; `0-255` and `4096-8191`/`8192+` buckets are all zero
in every group, omitted from the table but shown here for completeness):

- `supercharger_cabinet / 1.8.2`: `512-1023: 166, 1024-2047: 282, 2048-4095: 469`
- `supercharger_cabinet / 1.9.0`: `512-1023: 125, 1024-2047: 289, 2048-4095: 453`
- `supercharger_stall / 2.1.4`: `512-1023: 57, 1024-2047: 99, 2048-4095: 139`
- `supercharger_stall / 2.3.0`: `512-1023: 45, 1024-2047: 78, 2048-4095: 129`
- `supercharger_stall / 3.0.1`: `512-1023: 56, 1024-2047: 88, 2048-4095: 156`

## 4. Arrival cadence (inter-arrival gap, seconds) - stretch goal

Gaps between consecutive-by-arrival messages from the same device, pooled across all devices in
each group. Included since it fell out of the same per-group data at low extra cost, per the
ticket's stretch-goal framing.

| device_class | firmware | min (s) | median (s) | p90 (s) | max (s) | mean (s) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| supercharger_cabinet | 1.8.2 | 0 | 361 | 719 | 1,134,569 | 4,086.7 |
| supercharger_cabinet | 1.9.0 | 0 | 365 | 719 | 894,281 | 2,463.8 |
| supercharger_stall | 2.1.4 | 0 | 100 | 270 | 572,841 | 6,261.0 |
| supercharger_stall | 2.3.0 | 0 | 107.5 | 1,274.2 | 1,177,409 | 12,105.4 |
| supercharger_stall | 3.0.1 | 1 | 106 | 587.5 | 637,029 | 10,721.6 |

Medians are consistent with each class's modeled reporting cadence: cabinets report every
`reading_interval_s * 4` = 60 s per reading with median batch size ~7, so ~360-420 s between
batches lines up with the observed ~361-365 s median; stalls report every 15 s per reading with
median batch size ~6, so ~90 s between batches is in the right range of the observed ~100-108 s
median. The very heavy right tail (max values in the hundreds of thousands of seconds, i.e.
multiple days) comes directly from the generator's injected late-arrival and post-outage-burst
messages (`late_message_rate`, `outage_probability_per_device` in `GeneratorConfig`) - a message
arriving hours-to-days after its predecessor pulls the mean far above the median in every group.
This is exactly the kind of arrival-shape messiness P0-08 (lateness/duplicate profiler) will
characterize in more depth; this profiler only reports the resulting gap distribution, not the
cause.

## Caveats and follow-ups

- All numbers above come from one seed (`1337`) at default `devices_per_firmware=4`. They
  describe this one generator run's shape, not a statistically robust estimate of the
  generator's own parameter distributions (let alone real telemetry).
- Message size includes `_debug_injected_issues`, which a real message won't carry - treat the
  sizes above as a slight overestimate of real on-the-wire size once real capture exists.
- Batch size topping out uniformly at `max_batch_size=12` in every group is a generator
  configuration artifact (`GeneratorConfig.max_batch_size`), not something to treat as a
  discovered real-world batch cap.
- Revisit this whole report once P0-05's downstream real-fleet-access tickets land and real
  Stage 0 objects are available to run `profiling.arrival_shape.profiler` against instead.
