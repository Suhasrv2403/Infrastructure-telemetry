# pipeline/

Dagster assets, one directory per pipeline stage. See CLAUDE.md for the full grain and
invariant definitions - this file is just an index.

`definitions.py` is the Dagster entrypoint (P0-04): `dagster dev -f pipeline/definitions.py`
(or `TELEMETRY_ENV=staging dagster dev -f pipeline/definitions.py` for staging). It registers
each stage's assets as they land - today just `stage0_landing` (P0-05's capture, wrapped as a
partitioned asset in `stage0_landing/dagster_assets.py`).

| Directory | Stage | Grain | Ticket(s) |
| --- | --- | --- | --- |
| `stage0_landing/` | Stage 0 Landing | One row per message as received; partitioned by ARRIVAL hour; immutable | P0-05, P1-02 |
| `stage1_parsed/` | Stage 1 Parsed | One row per reading; partitioned by device_class x event date/hour, bucketed by device_id hash | P1-03..P1-07 |
| `stage2_canonical/` | Stage 2 Canonical | Same grain as Stage 1; canonical signals/units, corrected event time, quality flags | P1-08..P1-10, P2-14 |
| `stage3_enrich/` | Stage 3 Enrich | 3a time grid, 3b device x event, 3c device x day; device history dimension | P2-01..P2-05 |
| `stage4_outputs/` | Stage 4 Outputs | Cohort-day stats, versioned findings, device snapshot, fleet marts, lifetime table | P2-07, P2-11, P3-07, P3-08, P3-11 |
| `telemetry_health/` | Telemetry health | Last-seen snapshot (15-min job), silence episodes, dropout by cohort | P1-11, P1-12, P2-10, P3-05 |

Every asset here must respect the invariants in CLAUDE.md, in particular: Stage 0 is never
rewritten; Stage 1 writes are MERGE on (device_id, device_ts, payload_hash), never a plain
append; cleaning flags bad values rather than deleting rows; late data recomputes only dirty
(device, hour) keys.
