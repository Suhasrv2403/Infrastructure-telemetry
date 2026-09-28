"""Parser for supercharger_cabinet firmware 1.9.0.

Ticket: P1-04 ("Supercharger stall and cabinet parsers"). Built against the synthetic fixture
generator (tests/fixtures/generators/supercharger.py), not a firmware-SME-reviewed real payload
capture - real Supercharger payloads don't exist yet (P0-05/P1-01). See
parsers/supercharger_cabinet/README.md.

The actual row-building logic lives in parsers/supercharger_cabinet/_common.py and is shared
across both supercharger_cabinet firmware versions, since the synthetic generator does not
currently vary raw field names across them - see that module's docstring for why sharing code
here is deliberate, not a shortcut.
"""
from __future__ import annotations

from typing import Any

from parsers.framework import register_parser
from parsers.supercharger_cabinet._common import parse_supercharger_cabinet_message


@register_parser("supercharger_cabinet", "1.9.0")
def parse_supercharger_cabinet_1_9_0(message: dict[str, Any]) -> list[dict[str, Any]]:
    """See parsers.supercharger_cabinet._common.parse_supercharger_cabinet_message."""
    return parse_supercharger_cabinet_message(message)
