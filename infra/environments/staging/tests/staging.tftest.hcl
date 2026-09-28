# Native Terraform/OpenTofu tests for the staging environment root module (P0-02).
#
# See infra/environments/dev/tests/dev.tftest.hcl for why every run overrides
# module.networking's STS call - these tests need no LocalStack and no real account, only the
# aws provider plugin itself. Run with `tofu test` / `terraform test` from
# infra/environments/staging in any environment with normal registry access.

run "defaults_are_localstack_first_and_never_pre_approved" {
  command = plan

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = var.use_local_stack == true
    error_message = "use_local_stack must default to true - safe to plan/apply repeatedly with no real credentials."
  }

  assert {
    condition     = var.human_approved_real_account == false
    error_message = "human_approved_real_account must default to false - a real-account apply is always an explicit, human, opt-in action."
  }

  assert {
    condition = (
      var.cloud_endpoints.s3 == "http://localhost:4566" &&
      var.cloud_endpoints.iam == "http://localhost:4566" &&
      var.cloud_endpoints.sts == "http://localhost:4566" &&
      var.cloud_endpoints.ec2 == "http://localhost:4566"
    )
    error_message = "Default cloud_endpoints must all point at LocalStack's single edge port (4566)."
  }
}

run "real_account_without_human_approval_is_blocked_at_plan_time" {
  command = plan

  variables {
    use_local_stack             = false
    human_approved_real_account = false
  }

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  # Confirms the cross-variable validation on human_approved_real_account
  # (infra/environments/staging/variables.tf) actually fires - this is the exact guard that
  # protects the real-account.tfvars.example path from being used accidentally.
  expect_failures = [
    var.human_approved_real_account,
  ]
}

# A "real account WITH approval succeeds" run is deliberately not included here: with
# use_local_stack = false, the provider is configured for real AWS, so a full plan needs real
# credentials. Exercise real-account.tfvars.example manually only after a human has actually
# approved it, per docs/KICKOFF.md.

run "networking_module_is_wired_up_with_the_staging_environment_name" {
  command = plan

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = module.networking.flow_log_group_name == "/telemetry/staging/vpc-flow-logs"
    error_message = "The networking module must be invoked with environment = \"staging\"."
  }
}
