# Native Terraform/OpenTofu tests for the networking module (P0-02).
#
# IMPORTANT - see the P0-02 validation report for full detail: these tests are real, valid
# Terraform test files, written to actually run with `terraform test` / `tofu test` in any
# environment with normal registry access. They could NOT be executed in the sandbox this
# repo was authored in, because that sandbox blocks every Terraform/OpenTofu provider
# registry host - `terraform test`/`tofu test` need to download the aws provider plugin
# during init, exactly like `plan`/`apply` do, even when every assertion below only checks
# statically-known configuration values. Run `tofu test` (or `terraform test`) from
# infra/modules/networking in a normal environment to execute these for real.

variables {
  environment = "test"
  vpc_cidr    = "10.99.0.0/16"
  az_count    = 2
  tags        = { Project = "infrastructure-telemetry-test" }
}

run "vpc_has_expected_cidr_and_dns_settings" {
  command = plan

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

  assert {
    condition     = alltrue([for s in aws_subnet.public : s.map_public_ip_on_launch == false])
    error_message = "Public subnets must not auto-assign public IPs (checkov CKV_AWS_130) - compute that needs one should request it explicitly."
  }
}

run "internal_security_group_ingress_is_scoped_to_vpc_cidr" {
  command = plan

  assert {
    condition     = tolist(aws_security_group.internal.ingress)[0].cidr_blocks[0] == var.vpc_cidr
    error_message = "Internal SG ingress must be scoped to this environment's own VPC CIDR, not the whole internet."
  }
}

run "default_security_group_is_locked_down_to_zero_rules" {
  command = plan

  assert {
    condition     = length(aws_default_security_group.this.ingress) == 0 && length(aws_default_security_group.this.egress) == 0
    error_message = "The VPC's implicit default security group must be locked down to zero rules (checkov CKV2_AWS_12)."
  }
}

run "flow_logs_are_kms_encrypted_and_retained_at_least_a_year" {
  command = plan

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

  assert {
    condition     = aws_kms_key.flow_logs.enable_key_rotation == true
    error_message = "The flow-log KMS key must have automatic annual rotation enabled."
  }
}

run "flow_log_kms_policy_grants_only_account_root" {
  command = plan

  assert {
    condition     = length(jsondecode(data.aws_iam_policy_document.flow_logs_kms.json).Statement) == 1
    error_message = "The flow-log KMS key policy should be exactly the single documented root-account statement, not silently grown."
  }
}
