locals {
  name_prefix = "telemetry-${var.environment}"
}

# Generic assume-role policy. Real principals (e.g. a specific ECS task role, or an EC2
# instance profile) get wired in once the ingest service/orchestrator compute exists
# (P0-04, P1-01) - this is deliberately a placeholder shape, not a finished trust policy.
data "aws_iam_policy_document" "assume_generic_service" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest_service" {
  name               = "${local.name_prefix}-ingest-service"
  assume_role_policy = data.aws_iam_policy_document.assume_generic_service.json
  tags               = var.tags
}

resource "aws_iam_role" "orchestrator" {
  name               = "${local.name_prefix}-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.assume_generic_service.json
  tags               = var.tags
}

# Only attached once a landing bucket ARN is supplied (P0-03+). Scoped to writes only - the
# ingest path should never need to read or delete from Stage 0 (invariant 1: append-only).
data "aws_iam_policy_document" "ingest_service_s3_write" {
  count = var.landing_bucket_arn == null ? 0 : 1

  statement {
    sid       = "WriteStage0Landing"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${var.landing_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "ingest_service_s3_write" {
  count  = var.landing_bucket_arn == null ? 0 : 1
  name   = "${local.name_prefix}-ingest-s3-write"
  role   = aws_iam_role.ingest_service.id
  policy = data.aws_iam_policy_document.ingest_service_s3_write[0].json
}
