# pipeline/stage2_canonical/

Stage 2 Canonical: same grain as Stage 1, canonical signal names/units (via
catalog/signals.yaml), corrected event time, and row-level quality flags. A completeness-and-
lateness sidecar (device x hour) lives alongside this stage.

Cleaning flags bad values; it never deletes rows (invariant 3).

Implemented starting P1-08 (canonicalization) through P1-11 (completeness/lateness sidecar),
extended in P2-14 for Megapack/Powerpack.
