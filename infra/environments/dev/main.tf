locals {
  common_tags = {
    Project     = "infrastructure-telemetry"
    Environment = "dev"
    ManagedBy   = "terraform"
  }
}

provider "aws" {
  region                      = var.aws_region
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
}
