<!--
PROVENANCE: copied from branch `P0-07-timestamp-clock-quality-profiler` (commit fd385bd), path `docs/profiling/P0-07-timestamp-clock-quality.md`.
This branch is not yet merged into trunk. This file is a read-only snapshot
for easy reading; the source branch remains the canonical, editable copy.
Consolidated into docs/reports/ on trunk (P0-04-orchestrator-partition-model).
-->

# P0-07: Timestamp and clock-quality profiler

**Ticket:** P0-07 ("Timestamp and clock-quality profiler"), DISC, depends on P0-05.
**Done when (per Build backlog.md):** "Missing, epoch-default, future and drift rates per firmware."
**Code:** `profiling/clock_quality/profiler.py`; tests: `tests/unit/test_clock_quality_profiler.py`.

## Scope note (read this first)

Real Supercharger telemetry access is still gated on later tickets (see
`pipeline/stage0_landing/capture.py`'s module docstring). Everything below was produced by
running this profiler for real against the synthetic fixture generator
(`tests/fixtures/generators/supercharger.py`), which stands in for Stage 0 output until real
capture exists. **These findings describe the generator's modeled clock-quality issues, not
confirmed production device behavior.** The future-timestamp threshold and the drift-estimation
method (below) are judgment calls made against this synthetic setup; both should be revisited
once real telemetry is available. All numbers in this document come from an actual run of
`python3 -m profiling.clock_quality.profiler`, not hand-picked or fabricated.

## Method

For each `(device_class, firmware_version)` group, over every reading in every Stage 0 message
envelope:

1. **Missing-timestamp rate** - fraction of readings with `device_ts_ms is None`.
2. **Epoch-default rate** - fraction of readings with `device_ts_ms == 0` (uninitialized RTC,
   no GPS/NTP fix since boot).
3. **Future-timestamp rate** - fraction of readings where `device_ts_ms - arrival_ts_ms` exceeds
   `FUTURE_THRESHOLD_MS = 6 hours`. **Threshold reasoning:** a device clock that's merely fast
   (NTP drift, no sync since boot) still lands within a few minutes-to-hours of the message's own
   arrival time once ordinary network/queueing delay and this generator's late-arrival jitter
   (up to 4h, `late_delay_max_s`) are accounted for. A reading claiming to have happened *hours to
   days after the message that carries it arrived* is a different, categorical failure - the
   clock is simply wrong. 6h sits comfortably above plausible skew+jitter and comfortably below
   the generator's injected future offsets (`future_offset_min_s`=1h but `future_offset_max_s`=14
   days, so most injected future timestamps land far past 6h). This is a judgment call, not a
   measured cutoff - revisit once real telemetry is available.
4. **Clock drift** - for each device, the median of `(arrival_ts_ms of message) - (device_ts_ms
   of reading)` across that device's *clean* readings (excluding ones already flagged
   missing/epoch/future), then the min/median/p90/max of those per-device estimates within each
   firmware group.

Only fields a real Stage 0 message would carry are read (`device_ts_ms`, `arrival_ts_ms`,
`sent_ts_ms`, `device_id`, `firmware_version`, `device_class`). `_debug_injected_issues` -
generator-only test metadata that a real device would never send - is never read by the
profiler; it's used only inside the test file, as ground truth to sanity-check the profiler's
independently-computed rates.

**Drift is an indirect estimate, not a measurement.** `arrival_ts_ms` is ingest-side wall-clock
receipt time, not "true" event time - it carries network transit and buffering delay on top of
whatever the device's own clock is off by. Taking the median across many readings per device is
meant to average out that per-message noise and recover a systematic offset, but this doesn't
recover an *exact* clock-offset figure.

**Important limitation found while validating this ticket, specific to this generator:** the
fixture generator sets a batch's `arrival_ts_ms` from the *device's own* last reading's
(already clock-skewed) `device_ts_ms` plus a few seconds of jitter
(`supercharger.py::_emit_device_messages`, `base_arrival_ms = last_ts_ms`) rather than from an
independently simulated network-arrival clock. That means the generator's injected per-device
`clock_drift_max_s` (±600s) mostly cancels out of `arrival_ts_ms - device_ts_ms`, because it
appears in both terms. Measuring the correlation directly (by instrumenting the generator to
capture its injected per-device offset and comparing it to this profiler's independent
per-device drift estimate, across 100 devices at `devices_per_firmware=20`) gave **correlation
≈ -0.07** - effectively zero. So on *this* generator's output, the drift numbers below mostly
reflect a reading's typical position within its batch (batch size and reading interval), not
the generator's injected clock skew. This is a property of the generator's arrival simulation,
not a flaw in the estimation method: on real telemetry, `arrival_ts_ms` is an independent
ingest-side receipt time (see `pipeline/stage0_landing/capture.py`) that doesn't share the
device's clock error the way this generator's does. The hand-constructed unit tests (which use
independently-set arrival and device timestamps, not generator output) confirm the estimation
logic itself is correct; fixing the generator's arrival simulation is out of scope for this
ticket. **Treat the drift numbers below as "typical batch latency," not "measured clock skew,"
until the generator (or real data) models arrival independently.**

## Results

Real output of `python3 -m profiling.clock_quality.profiler` (default config: seed=1337,
`devices_per_firmware=4`, matching the fixture generator's own defaults):

```
supercharger_cabinet/1.8.2: readings=5907 missing=0.0098 epoch_default=0.0200 future=0.0059 drift[min=185.0s median=212.5s p90=240.7s max=241.0s (n_devices=4)]
supercharger_cabinet/1.9.0: readings=5904 missing=0.0113 epoch_default=0.0129 future=0.0085 drift[min=240.0s median=241.0s p90=242.0s max=242.0s (n_devices=4)]
supercharger_stall/2.1.4: readings=1828 missing=0.0115 epoch_default=0.0137 future=0.0115 drift[min=49.0s median=50.0s p90=57.7s max=61.0s (n_devices=4)]
supercharger_stall/2.3.0: readings=1580 missing=0.0146 epoch_default=0.0158 future=0.0063 drift[min=50.0s median=60.5s p90=61.0s max=61.0s (n_devices=4)]
supercharger_stall/3.0.1: readings=1892 missing=0.0085 epoch_default=0.0159 future=0.0063 drift[min=50.0s median=60.0s p90=63.5s max=65.0s (n_devices=4)]
```

| device_class | firmware | readings | missing | epoch_default | future | drift median (s) | drift p90 (s) |
|---|---|---:|---:|---:|---:|---:|---:|
| supercharger_cabinet | 1.8.2 | 5,907 | 0.98% | 2.00% | 0.59% | 212.5 | 240.7 |
| supercharger_cabinet | 1.9.0 | 5,904 | 1.13% | 1.29% | 0.85% | 241.0 | 242.0 |
| supercharger_stall | 2.1.4 | 1,828 | 1.15% | 1.37% | 1.15% | 50.0 | 57.7 |
| supercharger_stall | 2.3.0 | 1,580 | 1.46% | 1.58% | 0.63% | 60.5 | 61.0 |
| supercharger_stall | 3.0.1 | 1,892 | 0.85% | 1.59% | 0.63% | 60.0 | 63.5 |

Re-running at higher density (`--devices-per-firmware 20`, seed unchanged) gives rates within
~0.3 percentage points of the table above for every group, and drift distributions in the same
range (e.g. cabinet 1.8.2 median 240.0s vs 212.5s above) - consistent with sampling noise at
`devices_per_firmware=4`, not a density-dependent effect.

## Observations

- **Missing/epoch/future rates are essentially flat across firmware within a device class**
  (roughly 0.9-1.6% each). This is a fact about the generator, not a finding about real
  firmware: `GeneratorConfig`'s `missing_timestamp_rate`, `epoch_default_rate` and
  `future_timestamp_rate` are global constants, not scaled by `FIRMWARE_QUIRK_MULTIPLIER` the
  way outage/drop behavior is (see `supercharger.py`'s module docstring, which claims "older
  firmware is modeled as buggier" - true for connectivity/buffering, not (currently) true for
  clock-quality in this generator). Real firmware may well show much more per-firmware spread
  in clock-quality issues than this generator currently models; this profiler is ready to
  surface that the moment real (or more firmware-differentiated synthetic) data exists.
- **Cabinet drift estimates (~210-240s) run higher than stall drift estimates (~50-65s)**,
  tracking the two device classes' different reading intervals and batch sizes (cabinets report
  every 60s, stalls every 15s, same `max_batch_size=12`) - exactly what's expected once you know
  (per the limitation above) that these numbers mostly measure batch-position latency rather
  than injected clock skew on this generator.
- No group shows a future-timestamp rate above ~1.2%, and none shows missing/epoch above ~2% -
  low enough that a downstream consumer clamping/dropping clock-flagged readings would only be
  discarding a small fraction of data, though this is again a generator-default number, not a
  production one.

## Verification

- `python3 -m pytest tests/unit/test_clock_quality_profiler.py -v` - 5 passed.
- `python3 -m pytest tests/unit -q` - full suite (33 tests) passes, nothing else broken.
- `python3 -m ruff check profiling/clock_quality/profiler.py tests/unit/test_clock_quality_profiler.py` - clean.
