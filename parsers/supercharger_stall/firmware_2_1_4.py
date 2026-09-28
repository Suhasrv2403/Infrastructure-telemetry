"""Demonstration parser for supercharger_stall firmware 2.1.4.

Ticket: P1-03 ("Parser framework with versioned per-firmware parsers").

Scope note: this is a STAND-IN parser that exists only to prove parsers/framework.py's
registry/dispatch/quarantine machinery works end to end against real generator output. It is
NOT a firmware-SME-reviewed parser and does not represent real Supercharger stall 2.1.4
wire-format knowledge - it was written against the synthetic fixture generator
(tests/fixtures/generators/supercharger.py), not a real payload capture. Real per-firmware
parser coverage - this firmware done properly, the other two supercharger_stall firmware
versions (2.3.0, 3.0.1), and both supercharger_cabinet firmware versions (1.8.2, 1.9.0) - is
P1-04, a separate ticket. Leaving those (device_class, firmware_version) pairs unregistered is
deliberate: their messages must land in quarantine, not be guessed at by this parser.

Field passthrough only - no canonicalization. Raw field names/units stay exactly as the
device (synthetically) sends them; canonical name/unit/type mapping is Stage 2's job (P1-08)
via catalog/signals.yaml (still an unreviewed skeleton as of P0-10 - see catalog/README.md).
"""
from __future__ import annotations

from typing import Any

from parsers.framework import register_parser

# The raw per-reading fields this firmware's synthetic generator emits (see
# tests/fixtures/generators/supercharger.py, _simulate_stall_session/_finalize_readings). A
# real 2.1.4 parser would derive this list from actual firmware documentation or captured
# payloads, not from the test fixture generator - see the module scope note above.
_READING_FIELDS = (
    "output_current_a",
    "output_voltage_v",
    "output_power_kw",
    "connector_temp_c",
    "energy_delivered_kwh",
    "fault_code",
    "state",
    "session_id",
    "device_ts_ms",
    "payload_hash",
)


@register_parser("supercharger_stall", "2.1.4")
def parse_supercharger_stall_2_1_4(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Reshape one supercharger_stall/2.1.4 Stage 0 envelope into flat Stage 1 rows.

    One row per reading in `message["readings"]`, each carrying the parent envelope's identity
    fields (device_id, device_class, firmware_version, site_id) alongside the reading's own raw
    fields, unchanged - still raw field names, no timestamp sanity/dedup (that's Stage 1's
    MERGE step, P1-06) and no unit/name canonicalization (Stage 2, P1-08).

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
        }
        for field in _READING_FIELDS:
            row[field] = reading[field]
        rows.append(row)
    return rows
