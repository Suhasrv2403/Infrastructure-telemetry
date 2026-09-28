locals {
  common_tags = {
    Project     = "infrastructure-telemetry"
    Environment = "dev"
    ManagedBy   = "terraform"
  }
}

provider "aws" {
  region     = var.aws_region
  access_key = var.use_local_stack ? "test" : null
  secret_key = var.use_local_stack ? "test" : null

  # Always skip the provider's own implicit "who am I" check at configure time - it's a
  # redundant early ping (real resource operations against a real account will surface bad
  # credentials on their own, immediately). Doing this unconditionally, rather than only when
  # use_local_stack = true, means the human_approved_real_account guard (variables.tf) is what
  # blocks a real-account plan, not an incidental STS call racing ahead of that validation.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  # LocalStack (and LocalStack-compatible emulators like Floci) serve S3 in path-style only
  # (http://<host>/<bucket>/...), not the AWS SDK's virtual-hosted-style default
  # (http://<bucket>.<host>/...). Without this, S3 API calls against LocalStack return HTTP 500
  # for virtual-hosted-style requests, which the AWS SDK's retry/backoff logic turns into what
  # looks like an indefinite hang rather than a clean error (confirmed via TF_LOG=DEBUG on a real
  # `terraform test` run against P0-03's object_store module - not assumed). Real AWS accounts
  # support both styles, so this only needs to be forced on for LocalStack.
  s3_use_path_style = var.use_local_stack ? true : false

  endpoints {
    s3   = var.use_local_stack ? var.cloud_endpoints.s3 : null
    iam  = var.use_local_stack ? var.cloud_endpoints.iam : null
    sts  = var.use_local_stack ? var.cloud_endpoints.sts : null
    ec2  = var.use_local_stack ? var.cloud_endpoints.ec2 : null
    kms  = var.use_local_stack ? var.cloud_endpoints.kms : null
    logs = var.use_local_stack ? var.cloud_endpoints.logs : null
    glue = var.use_local_stack ? var.cloud_endpoints.glue : null
  }
}

module "networking" {
  source      = "../../modules/networking"
  environment = "dev"
  tags        = local.common_tags
}

module "object_store" {
  source      = "../../modules/object_store"
  environment = "dev"
  tags        = local.common_tags
}

module "iam" {
  source                  = "../../modules/iam"
  environment             = "dev"
  tags                    = local.common_tags
  attach_ingest_s3_write  = true
  landing_bucket_arn      = module.object_store.landing_bucket_arn
}
