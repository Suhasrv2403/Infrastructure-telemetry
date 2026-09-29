"""Synthetic fixture generator for a regional-outage / late-backfill scenario.

Ticket: P1-16 ("Late-data backfill end-to-end test and runbook").

Why this is a separate, purpose-built generator and not tests/fixtures/generators/supercharger.py
----------------------------------------------------------------------------------------------
supercharger.py's GeneratorConfig models per-device outages up to outage_max_s=3*3600 (3h) -
explicitly not long enough for the 72h REGIONAL outage this ticket needs to prove end to end
(see P1-16's ticket brief, which calls this out by name). Rather than stretching that
generator's per-device outage model past what it was built and validated for, this module
builds a small, fully deterministic, hand-specified scenario: no RNG, no injected corruption
(missing/epoch/future timestamps, duplicate retransmits - P1-05/P1-06 already have their own
focused fixtures/tests for that). Every message here is exactly what the scenario needs and
nothing else, so a test built on top of it can assert exact row/message counts, not just
"roughly some rows landed".

Outage model chosen: "no messages arrive at all during the outage, then everything buffered
lands as one late backfill batch on reconnect" - as opposed to "messages trickle in throughout
the outage, each individually very late". This is the more realistic shape for a REGIONAL
network outage (e.g. a cell backhaul or site-to-cloud link down, as opposed to one device's own
radio being flaky): the affected devices keep sampling and buffering locally (the same
"local buffer, burst on reconnect" behavior supercharger.py's own per-device outage model
already assumes), but literally cannot transmit anything to the cloud until connectivity to the
region is restored - it isn't that each message trickles in independently very late, it's that
NOTHING arrives for 72h, then EVERYTHING arrives at once. That also matches the ticket's own
description: "once connectivity is restored, ALL of that buffered data lands at once as one
large late/backfill batch".

Boundary-hour design (why the outage doesn't start on an hour boundary)
-------------------------------------------------------------------------
So the scenario exercises P1-07's actual dirty-marking rule (a (device_id, event_hour) key is
marked dirty only if it already had at least one row from an earlier merge call - see
pipeline/stage1_parsed/dirty_keys.py's module docstring), each outage-affected device reports
normally for `pre_outage_hours` full hours, then the outage begins PARTWAY into the next hour
(the "boundary hour") - after that hour's first on-time reading has already been merged, but
before its next scheduled reading. That next reading, and every reading for the following 72h,
is buffered locally and arrives only in the backfill batch. So, once the backfill lands:

  - the boundary hour already had a row merged BEFORE the outage -> the backfill's row(s) for
    that same (device_id, event_hour) key make it genuinely dirty (P1-07's rule fires as
    designed: late data landing in an already-populated hour).
  - every hour strictly inside the 72h outage window is data the backfill populates for the
    very first time -> per P1-07's rule, correctly NOT dirty (nothing downstream ever processed
    a device-hour that never had data before it).

This is a deliberate, documented choice between the two scenarios the ticket calls out as
defensible ("already merged before" vs. "first-ever data") - see the test module built on top
of this one for how the resulting dirty-key set is asserted against both cases at once.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

MIN_MS = 60_000
HOUR_MS = 3_600_000


def _payload_hash(fields: dict[str, Any]) -> str:
    """Same style as tests/fixtures/generators/supercharger.py's _payload_hash: a hash of the
    reading's own field values, so an identical resend reuses the identical payload_hash -
    exactly what makes Stage 1's merge natural key (device_id, device_ts_ms, payload_hash)
    correctly dedup a replayed backfill batch (see this ticket's idempotent-replay assertion)."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def make_reading(
    device_ts_ms: int,
    *,
    session_id: str,
    state: str = "charging",
    output_voltage_v: float = 400.0,
    output_current_a: float = 150.0,
    output_power_kw: float = 60.0,
    connector_temp_c: float = 35.0,
    energy_delivered_kwh: float = 1.0,
    fault_code: int = 0,
) -> dict[str, Any]:
    """One raw reading in the shape parsers/supercharger_stall/firmware_2_1_4.py expects (see
    that module's _READING_FIELDS)."""
    fields: dict[str, Any] = {
        "session_id": session_id,
        "state": state,
        "output_voltage_v": output_voltage_v,
        "output_current_a": output_current_a,
        "output_power_kw": output_power_kw,
        "connector_temp_c": connector_temp_c,
        "energy_delivered_kwh": energy_delivered_kwh,
        "fault_code": fault_code,
        "device_ts_ms": device_ts_ms,
    }
    fields["payload_hash"] = _payload_hash(fields)
    return fields


def make_envelope(
    *,
    device_id: str,
    device_class: str,
    firmware_version: str,
    site_id: str,
    readings: list[dict[str, Any]],
    arrival_ts_ms: int,
    message_id: str,
) -> dict[str, Any]:
    """One Stage 0 message envelope in the shape parse_messages() / the registered parser
    expects. Real fields only - no generator-only `_debug_...` bookkeeping."""
    return {
        "message_id": message_id,
        "device_id": device_id,
        "device_class": device_class,
        "firmware_version": firmware_version,
        "site_id": site_id,
        "arrival_ts_ms": arrival_ts_ms,
        "readings": readings,
    }


def _chunk(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


@dataclasses.dataclass(frozen=True)
class OutageBackfillScenario:
    """Everything build_outage_backfill_scenario() produces, plus the timing knobs a test
    needs to reason about which (device_id, event_hour) keys should/shouldn't end up dirty."""

    device_class: str
    firmware_version: str
    site_id: str
    interval_ms: int
    pre_outage_hours: int
    outage_start_ms: int
    outage_end_ms: int
    outage_devices: tuple[str, ...]
    control_device: str

    # On-time messages for the outage-affected devices, before the outage began.
    pre_outage_messages: tuple[dict[str, Any], ...]
    # The single backfill batch: every buffered reading for every outage-affected device,
    # delivered all at once at reconnect (every message in the batch shares the same
    # arrival_ts_ms - it lands as one event, not a trickle).
    backfill_messages: tuple[dict[str, Any], ...]
    # On-time messages for the untouched control device, spanning the whole scenario window.
    control_messages: tuple[dict[str, Any], ...]

    readings_per_outage_device_pre: int
    readings_per_outage_device_backfill: int


def build_outage_backfill_scenario(
    *,
    t0_ms: int,
    interval_ms: int = 30 * MIN_MS,
    pre_outage_hours: int = 2,
    boundary_offset_ms: int = 15 * MIN_MS,
    outage_hours: int = 72,
    outage_devices: tuple[str, ...] = ("stall-out-1", "stall-out-2"),
    control_device: str = "stall-ctrl-1",
    device_class: str = "supercharger_stall",
    firmware_version: str = "2.1.4",
    site_id: str = "site-outage-test",
    batch_size: int = 12,
    on_time_delay_ms: int = 5_000,
    backfill_delay_ms: int = 10_000,
) -> OutageBackfillScenario:
    """Build one deterministic 72h-regional-outage-plus-backfill scenario. See module
    docstring for the modeling choices (outage shape, boundary-hour design)."""
    if not (0 < boundary_offset_ms <= interval_ms):
        # Need the boundary hour's first on-time reading (at offset 0) to land before the
        # outage (0 < boundary_offset_ms), and its second (at offset interval_ms) to land only
        # in the backfill (boundary_offset_ms <= interval_ms) - see module docstring's
        # "Boundary-hour design".
        raise ValueError("boundary_offset_ms must be in (0, interval_ms]")
    if HOUR_MS % interval_ms != 0:
        raise ValueError("interval_ms must evenly divide an hour")

    boundary_hour_start_ms = t0_ms + pre_outage_hours * HOUR_MS
    outage_start_ms = boundary_hour_start_ms + boundary_offset_ms
    outage_end_ms = outage_start_ms + outage_hours * HOUR_MS

    def _pre_outage_device_ts() -> list[int]:
        # Full pre-outage hours...
        times = [
            t0_ms + k * interval_ms for k in range(pre_outage_hours * HOUR_MS // interval_ms)
        ]
        # ...then just the boundary hour's reading(s) that land before the outage starts.
        offset = 0
        while boundary_hour_start_ms + offset < outage_start_ms:
            times.append(boundary_hour_start_ms + offset)
            offset += interval_ms
        return times

    def _backfill_device_ts() -> list[int]:
        # Continue the SAME interval_ms cadence from the boundary hour's start (the device's
        # internal clock never stopped ticking, only transmission did), keeping every tick from
        # outage_start_ms up to (not including) reconnect.
        times = []
        k = 0
        while True:
            ts = boundary_hour_start_ms + k * interval_ms
            if ts >= outage_end_ms:
                break
            if ts >= outage_start_ms:
                times.append(ts)
            k += 1
        return times

    pre_times = _pre_outage_device_ts()
    backfill_times = _backfill_device_ts()

    pre_outage_messages: list[dict[str, Any]] = []
    backfill_messages: list[dict[str, Any]] = []

    for device_id in outage_devices:
        session_id = f"sess-{device_id}-outage-scenario"

        for i, ts in enumerate(pre_times):
            reading = make_reading(ts, session_id=session_id)
            pre_outage_messages.append(
                make_envelope(
                    device_id=device_id,
                    device_class=device_class,
                    firmware_version=firmware_version,
                    site_id=site_id,
                    readings=[reading],
                    arrival_ts_ms=ts + on_time_delay_ms,
                    message_id=f"msg-{device_id}-pre-{i}",
                )
            )

        backfill_readings = [make_reading(ts, session_id=session_id) for ts in backfill_times]
        arrival_ts_ms = outage_end_ms + backfill_delay_ms
        for i, batch in enumerate(_chunk(backfill_readings, batch_size)):
            backfill_messages.append(
                make_envelope(
                    device_id=device_id,
                    device_class=device_class,
                    firmware_version=firmware_version,
                    site_id=site_id,
                    readings=batch,
                    arrival_ts_ms=arrival_ts_ms,
                    message_id=f"msg-{device_id}-backfill-{i}",
                )
            )

    # Control device: same cadence, on time, for the whole window (pre-outage through when the
    # backfill lands) - it was never part of the outage, so it should be untouched by every
    # step of the chain this scenario drives.
    control_messages: list[dict[str, Any]] = []
    control_session_id = f"sess-{control_device}-outage-scenario"
    k = 0
    while True:
        ts = t0_ms + k * interval_ms
        if ts >= outage_end_ms:
            break
        reading = make_reading(ts, session_id=control_session_id)
        control_messages.append(
            make_envelope(
                device_id=control_device,
                device_class=device_class,
                firmware_version=firmware_version,
                site_id=site_id,
                readings=[reading],
                arrival_ts_ms=ts + on_time_delay_ms,
                message_id=f"msg-{control_device}-{k}",
            )
        )
        k += 1

    return OutageBackfillScenario(
        device_class=device_class,
        firmware_version=firmware_version,
        site_id=site_id,
        interval_ms=interval_ms,
        pre_outage_hours=pre_outage_hours,
        outage_start_ms=outage_start_ms,
        outage_end_ms=outage_end_ms,
        outage_devices=tuple(outage_devices),
        control_device=control_device,
        pre_outage_messages=tuple(pre_outage_messages),
        backfill_messages=tuple(backfill_messages),
        control_messages=tuple(control_messages),
        readings_per_outage_device_pre=len(pre_times),
        readings_per_outage_device_backfill=len(backfill_times),
    )
