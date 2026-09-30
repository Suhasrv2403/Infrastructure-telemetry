# catalog/

`signals.yaml` is the canonical signal catalog: per-firmware raw field -> canonical name,
unit, type and semantics.

**Status: v0 draft, NOT firmware-SME-reviewed.** P0-10's actual "done when" (Build backlog.md)
is firmware SME sign-off on canonical names/units/semantics, and no such review has happened.
This file was produced by reading the *synthetic* Supercharger fixture generator's source
(`tests/fixtures/generators/supercharger.py`) field-by-field and inferring names, units,
semantics and value ranges from its value-generation logic -- it stands in for real device
payloads because real Supercharger firmware/payload access doesn't exist yet (P0-05 hasn't
landed real capture). Every entry cites the generator function/logic it was derived from
(`source_ref`), so a firmware SME reviewing this has something concrete to check or correct
against real hardware -- but until that review happens, do not trust the names, units, or
ranges in this file, and do not build P1-03/P1-04 real parsers against its semantics.

Firmware version coverage is limited to whatever the generator currently models: 3 versions
for supercharger_stall (2.1.4, 2.3.0, 3.0.1), 2 for supercharger_cabinet (1.8.2, 1.9.0). That
happens to be "all of them" rather than a "top 3" selection -- see the header comment in
signals.yaml.

Stage 2 canonicalization (`pipeline/stage2_canonical/`, P1-08) is meant to read this file;
nothing should hardcode a signal name/unit mapping outside of it. `tests/unit/test_signal_catalog.py`
checks the catalog against a real run of the generator (not just static code-reading) so the
two don't silently drift apart -- run it after any change to either the generator's reading
fields or this file.
