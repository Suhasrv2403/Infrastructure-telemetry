locals {
  name_prefix   = "telemetry-${var.environment}"
  glue_db_name  = replace(local.name_prefix, "-", "_")
}

# KMS key for object store (S3) encryption - separate from the networking module's flow-log
# key: different data (device telemetry vs. VPC flow logs), different blast radius and rotation
# reasoning, so a shared key would couple two things that should be able to change independently.
data "aws_caller_identity" "current" {}

# Standard AWS-default KMS key policy shape (grant the account root kms:* on resources = ["*"]).
# CKV_AWS_111/109/356 flag this as an over-broad grant; accepted and suppressed via
# infra/.checkov.yaml (inline `checkov:skip` comments don't actually suppress these three checks
# in checkov 3.3.20 - verified by running checkov for real, not assumed).
data "aws_iam_policy_document" "object_store_kms" {
  statement {
    sid       = "EnableRootAccountAccess"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

# CKV2_AWS_64 false positive: a policy IS attached (above) - checkov's graph scanner can't
# statically resolve it through the aws_iam_policy_document data source's .json output. Same
# false positive as the networking module's flow_logs KMS key. Suppressed via infra/.checkov.yaml.
resource "aws_kms_key" "object_store" {
  description             = "${local.name_prefix} object store (S3) encryption"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_key_deletion_window_days
  policy                  = data.aws_iam_policy_document.object_store_kms.json

  tags = merge(var.tags, { Name = "${local.name_prefix}-object-store-kms" })
}

resource "aws_kms_alias" "object_store" {
  name          = "alias/${local.name_prefix}-object-store"
  target_key_id = aws_kms_key.object_store.key_id
}

# ---------------------------------------------------------------------------
# Landing bucket - Stage 0 (raw, append-only, arrival-hour partitioned).
# Layout convention (docs/decisions/0001-object-store-layout.md):
#   s3://<landing bucket>/raw/arrival_date=YYYY-MM-DD/hour=HH/<file>
# Only the ingest service writes here (iam module's ingest_service role, scoped to
# PutObject/AbortMultipartUpload only via landing_bucket_arn - invariant 1: append-only).
# ---------------------------------------------------------------------------

# Real-account note: S3 bucket names are globally unique across all AWS accounts, not just this
# one - "telemetry-<env>-landing" is fine for LocalStack but likely needs an account-id or
# random suffix before a real (non-LocalStack) apply. Revisit per docs/decisions/0001.
#
# CKV_AWS_18 (access logging), CKV_AWS_144 (cross-region replication), CKV2_AWS_61 (lifecycle
# config), CKV2_AWS_62 (event notifications): all deferred/out-of-scope for P0-03, not
# oversights - see infra/.checkov.yaml for the per-check rationale and why these are suppressed
# there instead of inline (checkov 3.3.20 doesn't honor inline skips for these four checks).
resource "aws_s3_bucket" "landing" {
  bucket        = "${local.name_prefix}-landing"
  force_destroy = var.force_destroy

  tags = merge(var.tags, { Name = "${local.name_prefix}-landing", Stage = "0-landing" })
}

resource "aws_s3_bucket_versioning" "landing" {
  bucket = aws_s3_bucket.landing.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.object_store.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "landing" {
  bucket = aws_s3_bucket.landing.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "landing_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.landing.arn, "${aws_s3_bucket.landing.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "landing" {
  bucket = aws_s3_bucket.landing.id
  policy = data.aws_iam_policy_document.landing_tls_only.json
}

# ---------------------------------------------------------------------------
# Landing bucket lifecycle - 30-day hot-then-archive tiering (P1-02, "Stage 0 compaction and
# retention tiering"; resolves CKV2_AWS_61 for this bucket - see infra/.checkov.yaml).
#
# "Hot" means STANDARD (the storage class every object lands in via capture.py's put_object,
# and via pipeline/stage0_landing/compaction.py's consolidated objects) for the first
# var.landing_hot_days days after an object's own creation date, then a transition to
# GLACIER_IR (Glacier Instant Retrieval) - deliberately NOT plain GLACIER or DEEP_ARCHIVE.
# Reasoning: CLAUDE.md invariant 7 requires that replaying a closed window from Stage 0
# reproduce production output exactly, and a backfill/replay job reading an old partition
# shouldn't have to special-case "this object is archived, issue a restore request and wait
# hours before it's readable." GLACIER_IR gives most of Glacier's cost savings over STANDARD
# (meant for rarely-accessed data) while keeping every Stage 0 object - however old -
# millisecond-retrievable, so Stage 0 stays usable as a replay source at any age without an
# out-of-band restore step. Revisit the storage class (e.g. plain GLACIER once actual replay
# frequency for old partitions is known) once there's real usage data to size that tradeoff by.
#
# Scope: applies bucket-wide (filter {} - no prefix scoping) since every object under both
# raw/ (capture.py's small originals) and compacted/ (this ticket's consolidated objects) is
# equally eligible to tier - compaction only ever ADDS objects under compacted/, it never
# deletes or rewrites the small raw/ originals (invariant 1), so both kinds of object age and
# transition independently here. A storage-class transition preserves an object's content
# (unlike deletion), so this rule does NOT run into the invariant-1 tension compaction's own
# code has to reconcile - see pipeline/stage0_landing/compaction.py's module docstring.
#
# NOT VERIFIED against real Terraform tooling: this sandbox has no terraform/tofu binary (see
# infra/README.md's "Running this locally" note, itself unverified for the same reason).
# Written by hand against this module's existing resource/tagging/variable conventions -
# please run `terraform validate` and this module's `terraform test` suite for real before
# trusting it further.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_lifecycle_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id

  rule {
    id     = "hot-${var.landing_hot_days}d-then-archive"
    status = "Enabled"

    filter {}

    transition {
      days          = var.landing_hot_days
      storage_class = "GLACIER_IR"
    }
  }
}

# ---------------------------------------------------------------------------
# Warehouse bucket - Stages 1-4 (Iceberg table data + metadata).
# Layout convention (docs/decisions/0001-object-store-layout.md):
#   s3://<warehouse bucket>/<table name>/data/... and /metadata/... (Iceberg-managed)
# Table names match the stage they belong to: stage1_parsed, stage2_canonical,
# stage3_grid / stage3_device_event / stage3_device_day, stage4_cohort_day / stage4_findings /
# stage4_device_snapshot / stage4_fleet_marts / stage4_lifetime.
# ---------------------------------------------------------------------------

# Same four accepted/deferred findings as the landing bucket above - see infra/.checkov.yaml.
resource "aws_s3_bucket" "warehouse" {
  bucket        = "${local.name_prefix}-warehouse"
  force_destroy = var.force_destroy

  tags = merge(var.tags, { Name = "${local.name_prefix}-warehouse", Stage = "1-4-warehouse" })
}

resource "aws_s3_bucket_versioning" "warehouse" {
  bucket = aws_s3_bucket.warehouse.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "warehouse" {
  bucket = aws_s3_bucket.warehouse.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.object_store.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "warehouse" {
  bucket = aws_s3_bucket.warehouse.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "warehouse_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.warehouse.arn, "${aws_s3_bucket.warehouse.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "warehouse" {
  bucket = aws_s3_bucket.warehouse.id
  policy = data.aws_iam_policy_document.warehouse_tls_only.json
}

# ---------------------------------------------------------------------------
# Iceberg catalog - one Glue Data Catalog database per environment. Spark/Dagster register
# every stage's Iceberg table into this database; its location_uri is the default table root
# under the warehouse bucket, so a table created without an explicit LOCATION lands in the
# right place automatically.
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "telemetry" {
  name         = local.glue_db_name
  description  = "Iceberg catalog for the ${var.environment} telemetry lakehouse (P0-03)."
  location_uri = "s3://${aws_s3_bucket.warehouse.bucket}/"
}
