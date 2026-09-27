# pipeline/stage4_outputs/

Stage 4 Outputs: cohort-day statistics, versioned findings, device health snapshot, fleet
marts, and the lifetime/survival table.

Windows are provisional until the lateness horizon passes; findings are versioned and never
overwritten (invariant 6). Dashboards read only from here, never from Stage 3 (see
P3-07).

Implemented starting P2-07 (cohort-day stats) through P3-11 (lifetime table).
