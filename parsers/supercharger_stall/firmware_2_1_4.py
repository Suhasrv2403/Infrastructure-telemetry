"""Parser for supercharger_stall firmware 2.1.4.

Ticket: P1-03 registered this as a demonstration parser; P1-04 confirms it as real (synthetic-
fixture-backed) coverage for this firmware version and adds arrival_ts_ms passthrough. See
parsers/supercharger_stall/README.md and parsers/supercharger_stall/_common.py's module
docstring for the honest scope note: this is built against the synthetic fixture generator
(tests/fixtures/generators/supercharger.py), not a firmware-SME-reviewed real payload capture,
because real Supercharger payloads don't exist yet (P0-05/P1-01).

The actual row-building logic lives in parsers/supercharger_stall/_common.py and is shared
across all three supercharger_stall firmware versions, since the synthetic generator does not
currently vary raw field names across them - see that module's docstring for why sharing code
here is deliberate, not a shortcut.
"""
from __future__ import annotations

from typing import Any

from parsers.framework import register_parser
from parsers.supercharger_stall._common import parse_supercharger_stall_message


@register_parser("supercharger_stall", "2.1.4")
def parse_supercharger_stall_2_1_4(message: dict[str, Any]) -> list[dict[str, Any]]:
    """See parsers.supercharger_stall._common.parse_supercharger_stall_message."""
    return parse_supercharger_stall_message(message)
