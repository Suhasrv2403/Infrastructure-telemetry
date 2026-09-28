terraform {
  # >= 1.9 for cross-variable validation (human_approved_real_account's validation block
  # references var.use_local_stack, which Terraform only allowed starting in 1.9 - also
  # supported by OpenTofu, used to author/validate this repo's config in this environment).
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
