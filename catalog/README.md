# catalog/

`signals.yaml` is the canonical signal catalog: per-firmware raw field -> canonical name,
unit, type and semantics, reviewed by firmware SMEs (P0-10 for the v0 Supercharger catalog,
extended in P2-14 for Megapack/Powerpack and P3-03 for Powerwall).

Stage 2 canonicalization (`pipeline/stage2_canonical/`) reads this file; nothing should
hardcode a signal name/unit mapping outside of it.

The file currently checked in is a schema skeleton with one illustrative entry, not a
reviewed catalog - do not treat its contents as ground truth until P0-10 closes.
