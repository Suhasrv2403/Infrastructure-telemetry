# catalog/

`signals.yaml` is the canonical signal catalog: per-firmware raw field -> canonical name,
unit, type and semantics, reviewed by firmware SMEs (P0-10 for the v0 Supercharger catalog,
extended in P2-14 for Megapack/Powerpack and P3-03 for Powerwall).

Stage 2 canonicalization (`pipeline/stage2_canonical/`) reads this file; nothing should
hardcode a signal name/unit mapping outside of it.

The file currently checked in (as of P1-08) is the v0 DRAFT Supercharger catalog copied
verbatim from branch `P0-10-signal-catalog-v0`'s `catalog/signals.yaml` - real, concrete
coverage of the synthetic Supercharger fixture generator's raw fields, but still NOT
reviewed by a firmware SME. P0-10's own "done when" (firmware SME sign-off) has not
closed; do not treat its canonical names/units/ranges as ground truth. Stage 2
canonicalization (P1-08) reads it as-is so canonicalization has something real to run
against, honestly labeled as still-draft - see the PROVENANCE note at the top of
signals.yaml.
