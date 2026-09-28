# pipeline/stage0_landing/

Stage 0 Landing: one row per message as received, partitioned by **arrival** hour, immutable.
This is the replay source for the whole pipeline (invariant 1 and 7 in CLAUDE.md) - it is
never rewritten, and replaying a closed window from here must reproduce production output
exactly.

Implemented starting P0-05 (single-region Supercharger capture), hardened in P1-01
(production ingest buffer) and P1-02 (compaction/retention tiering).
