# parsers/supercharger_stall/

All three firmware versions in `DEFAULT_FIRMWARE` (`2.1.4`, `2.3.0`, `3.0.1`) are registered
(P1-04): `firmware_2_1_4.py`, `firmware_2_3_0.py`, `firmware_3_0_1.py`. Each is a thin
`@register_parser(...)` wrapper; the actual row-building logic lives in `_common.py` and is
shared across all three, because the synthetic fixture generator
(`tests/fixtures/generators/supercharger.py`) does not currently vary raw reading field
*names* across supercharger_stall firmware versions - only the rates of injected
clock/lateness/retry messiness. Each firmware version still has its own registered entry, so
it's independently swappable the moment real per-firmware divergence shows up.

**Honest scope note:** none of this is firmware-SME-reviewed against a real payload capture.
Real Supercharger payloads don't exist yet - P0-05 only captures this generator's synthetic
output until P1-01 replaces the source - so these parsers, and their fixture tests, are built
against `tests/fixtures/generators/supercharger.py`'s synthetic output. That's the established
pattern for every Phase 0/1 ticket so far, not a shortcut specific to this one. Re-validate
field names/types against real payloads once P1-01/P0-05 land them.
