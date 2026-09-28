# Native Terraform/OpenTofu tests for the networking module (P0-02).
#
# Standalone modules have no provider configuration of their own - without one, `terraform
# test` picks an implicit default `aws` provider and tries to authenticate against real AWS,
# which is why the first run of this file used to fail with "Invalid provider configuration" /
# an STS 403. The provider block below configures it the same way the dev/staging root modules
# do (LocalStack-shaped, dummy credentials).
#
# That still leaves one live call: `data.aws_caller_identity.current` (used to build the flow
# log KMS key policy) genuinely calls STS during `plan`, even a plan that creates everything
# from scratch - data sources are read, resources are not. Every run overrides that one data
# source with a fixed value (override_data, Terraform/OpenTofu >= 1.7), so none of them need
# LocalStack or a real account for that.
#
# One run is the exception either way: default_security_group_is_locked_down_to_zero_rules
# uses `command = apply` (see its own comment below) because aws_default_security_group's
# reconciled ingress/egress are only known post-apply, not at plan time - that one run does
# need LocalStack actually running. Every other run only needs the aws provider plugin itself.
# Run with `tofu test` / `terraform test` from infra/modules/networking.

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    s3  = "http://localhost:4566"
    iam = "http://localhost:4566"
    sts = "http://localhost:4566"
    ec2 = "http://localhost:4566"
  }
}

variables {
  environment = "test"
  vpc_cidr    = "10.99.0.0/16"
  az_count    = 2
  tags        = { Project = "infrastructure-telemetry-test" }
}

run "vpc_has_expected_cidr_and_dns_settings" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_vpc.this.cidr_block == "10.99.0.0/16"
    error_message = "VPC CIDR did not match the input variable."
  }

  assert {
    condition     = aws_vpc.this.enable_dns_support && aws_vpc.this.enable_dns_hostnames
    error_message = "VPC must have DNS support and DNS hostnames enabled."
  }
}

run "creates_az_count_public_and_private_subnets" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = length(aws_subnet.public) == var.az_count
    error_message = "Expected ${var.az_count} public subnets."
  }

  assert {
    condition     = length(aws_subnet.private) == var.az_count
    error_message = "Expected ${var.az_count} private subnets."
  }
}

run "public_subnets_do_not_auto_assign_public_ips" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = alltrue([for s in aws_subnet.public : s.map_public_ip_on_launch == false])
    error_message = "Public subnets must not auto-assign public IPs (checkov CKV_AWS_130) - compute that needs one should request it explicitly."
  }
}

run "internal_security_group_ingress_is_scoped_to_vpc_cidr" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = tolist(aws_security_group.internal.ingress)[0].cidr_blocks[0] == var.vpc_cidr
    error_message = "Internal SG ingress must be scoped to this environment's own VPC CIDR, not the whole internet."
  }
}

# Unlike every other run in this file, this one needs `apply`, not `plan`: aws_default_security
# _group reconciles whatever rules already exist on the VPC's auto-created default SG, so its
# resulting ingress/egress sets are only known once that reconciliation actually happens
# (confirmed by running this test for real - `plan` reports "Unknown condition value" on both
# sets). This is the one run in the whole P0-02 suite that needs a real target to apply against
# (LocalStack is enough; it does not need a real account).
run "default_security_group_is_locked_down_to_zero_rules" {
  command = apply

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = length(aws_default_security_group.this.ingress) == 0 && length(aws_default_security_group.this.egress) == 0
    error_message = "The VPC's implicit default security group must be locked down to zero rules (checkov CKV2_AWS_12)."
  }
}

run "flow_logs_are_kms_encrypted_and_retained_at_least_a_year" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_cloudwatch_log_group.flow_logs.kms_key_id != null
    error_message = "VPC flow log group must be encrypted with a customer-managed KMS key (checkov CKV_AWS_158)."
  }

  assert {
    condition     = aws_cloudwatch_log_group.flow_logs.retention_in_days >= 365
    error_message = "VPC flow log retention must be >= 365 days (checkov CKV2_AWS_11 / CKV_AWS_338)."
  }
}

run "flow_log_kms_key_has_rotation_enabled" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_kms_key.flow_logs.enable_key_rotation == true
    error_message = "The flow-log KMS key must have automatic annual rotation enabled."
  }
}

run "flow_log_kms_policy_grants_only_account_root" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = length(jsondecode(data.aws_iam_policy_document.flow_logs_kms.json).Statement) == 1
    error_message = "The flow-log KMS key policy should be exactly the single documented root-account statement, not silently grown."
  }
}
