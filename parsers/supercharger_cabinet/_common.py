"""Shared row-building logic for supercharger_cabinet parsers.

Ticket: P1-04 ("Supercharger stall and cabinet parsers").

Honest scope note: the synthetic fixture generator (tests/fixtures/generators/supercharger.py)
does not vary raw reading field *names* across supercharger_cabinet firmware versions - per
that module's own docstring, firmware only changes the *rates* of clock/lateness/retry
messiness injected, not the payload shape. So, as of this ticket, both supercharger_cabinet
firmware versions need the exact same reshaping. Rather than duplicate that reshaping twice
(once per firmware_<version>.py file), each firmware-specific module below registers this same
function under its own (device_class, firmware_version) key. That keeps each firmware version
an independently swappable registry entry - ready to be given real firmware-specific logic the
moment real payloads (P0-05) or firmware documentation show any divergence - without two
copies of identical logic to keep in sync today. See parsers/supercharger_cabinet/README.md.

Field passthrough only - no canonicalization; see parsers/framework.py's module docstring for
why (Stage 2 / P1-08's job, via catalog/signals.yaml, itself still an unreviewed skeleton).
"""
from __future__ import annotations

from typing import Any

# Raw per-reading fields this device class's synthetic generator emits (see
# tests/fixtures/generators/supercharger.py, _simulate_cabinet_stream/_finalize_readings). A
# real parser would derive this list from firmware documentation or captured payloads, not
# from the test fixture generator - see the module scope note above.
_READING_FIELDS = (
    "grid_voltage_v",
    "grid_frequency_hz",
    "transformer_temp_c",
    "contactor_closed",
    "active_stall_count",
    "aggregate_power_kw",
    "fault_code",
    "device_ts_ms",
    "payload_hash",
)


def parse_supercharger_cabinet_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Reshape one supercharger_cabinet Stage 0 envelope into flat Stage 1 rows.

    One row per reading in `message["readings"]`, each carrying the parent envelope's identity
    fields (device_id, device_class, firmware_version, site_id) plus `arrival_ts_ms`, alongside
    the reading's own raw fields, unchanged - still raw field names, no timestamp sanity/dedup
    (that's Stage 1's MERGE step, P1-06) and no unit/name canonicalization (Stage 2, P1-08).

    `arrival_ts_ms` is the envelope's own field (when Stage 0 capture received the message),
    not a per-reading field - it's stamped onto every row from that envelope's batch so
    downstream consumers (e.g. P1-05's timestamp sanity checks) can compare a reading's
    device_ts_ms against when its envelope actually arrived.

    A reading missing an expected field raises KeyError, which parsers/framework.py's dispatch
    loop catches and turns into a quarantine for that message - a parser bug degrades to
    quarantine, not a crashed batch.
    """
    rows: list[dict[str, Any]] = []
    for reading in message["readings"]:
        row: dict[str, Any] = {
            "device_id": message["device_id"],
            "device_class": message["device_class"],
            "firmware_version": message["firmware_version"],
            "site_id": message["site_id"],
            "arrival_ts_ms": message["arrival_ts_ms"],
        }
        for field in _READING_FIELDS:
            row[field] = reading[field]
        rows.append(row)
    return rows
