# pipeline/stage1_parsed/

Stage 1 Parsed: one row per reading (device_id, device_ts); partitioned by device_class x
event date/hour, bucketed by device_id hash. Timestamp sanity checks and dedup happen here.

Writes are always a MERGE on (device_id, device_ts, payload_hash) - never a plain append
(invariant 2). Replaying a message N times must yield exactly one row (P1-06). Late merges
record their (device, hour) keys into the dirty-keys table (P1-07) so downstream recompute
stays targeted.

Implemented starting P1-03 (parser framework) through P1-07 (dirty-keys table).
