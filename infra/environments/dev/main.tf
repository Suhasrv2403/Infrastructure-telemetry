# Guard: refuse to point this environment at a real account without explicit, recorded human
# sign-off. tobool() on a non-boolean string is a deliberate trick to fail plan/apply with a
# readable error message when the condition doesn't hold (works on any Terraform >= 1.5,
# doesn't rely on cross-variable validation support).
locals {
  _require_human_approval_for_real_account = (
    var.use_local_stack || var.human_approved_real_account
    ? true
    : tobool("BLOCKED: set human_approved_real_account = true only after a human has reviewed and approved applying dev against a real cloud account. See docs/KICKOFF.md.")
  )

  common_tags = {
    Project     = "infrastructure-telemetry"
    Environment = "dev"
    ManagedBy   = "terraform"
  }
}

provider "aws" {
  region = var.aws_region

  # LocalStack doesn't need real credentials; these placeholders satisfy the provider's
  # validation without ever touching a real account.
  access_key                  = var.use_local_stack ? "test" : null
  secret_key                  = var.use_local_stack ? "test" : null
  skip_credentials_validation = var.use_local_stack
  skip_metadata_api_check     = var.use_local_stack
  skip_requesting_account_id  = var.use_local_stack

  endpoints {
    s3  = var.use_local_stack ? var.cloud_endpoints.s3 : null
    iam = var.use_local_stack ? var.cloud_endpoints.iam : null
    sts = var.use_local_stack ? var.cloud_endpoints.sts : null
    ec2 = var.use_local_stack ? var.cloud_endpoints.ec2 : null
  }
}

module "networking" {
  source      = "../../modules/networking"
  environment = "dev"
  tags        = local.common_tags
}

module "iam" {
  source      = "../../modules/iam"
  environment = "dev"
  tags        = local.common_tags
  # landing_bucket_arn intentionally omitted (defaults to null) until P0-03 creates the
  # Stage 0 landing bucket.
}
