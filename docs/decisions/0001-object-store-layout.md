# 0001: Object store layout and Iceberg catalog naming

**Status:** accepted
**Date:** 2026-09-27
**Ticket:** P0-03

## Context

P0-03's "done when" is "buckets, catalog and naming conventions documented and live." CLAUDE.md
already fixes each stage's grain and partitioning (Stage 0 by arrival hour, Stage 1+ by event
time, never by `device_id`); this decision is about where those tables physically live and what
they're called, not about the grain itself.

## Decision

Two S3 buckets per environment, plus one Glue Data Catalog database as the Iceberg catalog:

- **Landing bucket** (`telemetry-<env>-landing`): Stage 0 only. Layout:
  `s3://telemetry-<env>-landing/raw/arrival_date=YYYY-MM-DD/hour=HH/<file>`. Append-only,
  versioned, KMS-encrypted. Only the `ingest_service` IAM role writes here
  (`PutObject`/`AbortMultipartUpload` only, per invariant 1).
- **Warehouse bucket** (`telemetry-<env>-warehouse`): Stages 1-4's Iceberg table data and
  metadata. Iceberg manages the internal `data/`/`metadata/` layout per table once a table
  exists; this decision fixes the table *names*, not their internal file layout:
  `stage1_parsed`, `stage2_canonical`, `stage3_grid`, `stage3_device_event`, `stage3_device_day`,
  `stage4_cohort_day`, `stage4_findings`, `stage4_device_snapshot`, `stage4_fleet_marts`,
  `stage4_lifetime`. Names track the stage a table belongs to, not what happens to be convenient
  at query time.
- **Catalog**: one Glue Data Catalog database per environment, named `telemetry_<env>` (Glue
  disallows hyphens), with `location_uri` set to the warehouse bucket's root so a table created
  without an explicit `LOCATION` lands under it automatically.
- Landing and warehouse are **separate buckets**, not one bucket with two prefixes, so the IAM
  boundary between "the ingest service can only ever write to Stage 0" and "everything else
  reads/writes the warehouse" is a bucket boundary, not a prefix convention someone could get
  wrong in a policy.

## Consequences

- Makes it easy to keep invariant 1 (Stage 0 append-only) enforced by IAM policy rather than by
  convention: the `ingest_service` role's policy only ever needs to name the landing bucket.
- Makes invariant 4 (never partition by `device_id`) visible in the layout itself: nothing in
  either bucket's path convention keys on `device_id` above the file level.
- Two buckets doubles the KMS/versioning/public-access-block boilerplate versus one bucket with
  prefixes - accepted, because the IAM clarity is worth more than the boilerplate at this scale.
- S3 bucket names are globally unique across every AWS account, not just this one -
  `telemetry-<env>-landing`/`-warehouse` are fine against LocalStack (which doesn't enforce
  global uniqueness) but will likely need an account-id or random suffix before a real
  (non-LocalStack) apply. Flagged in `infra/modules/object_store/main.tf`, not yet resolved.
- Access logging, lifecycle tiering, cross-region replication and event notifications are all
  deliberately deferred (see the module's `checkov:skip` comments) - lifecycle tiering
  specifically is P1-02's ticket, not this one's.

## Alternatives considered

- **One bucket, two prefixes (`raw/` and `warehouse/`)**: rejected - would need every IAM policy
  and lifecycle rule to get prefix-scoping right by hand, where a bucket boundary gets it right
  by construction.
- **Hive Metastore instead of Glue Data Catalog**: rejected for now - Glue is natively supported
  by LocalStack (matching the LocalStack-first approach from P0-02) and needs no separate
  service to run; revisit if a real deployment's engine choice (Spark distribution, Athena use)
  makes Glue a worse fit than it looks today.
- **A bucket per stage** (five+ buckets instead of two): rejected as needless IAM/policy
  surface area - the landing/warehouse split already captures the one boundary that matters
  (who can write Stage 0), and Iceberg table names inside the warehouse bucket capture the
  per-stage separation that matters for everything else.
