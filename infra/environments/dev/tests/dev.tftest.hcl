# Native Terraform/OpenTofu tests for the dev environment root module (P0-02).
#
# This root module's own provider block already targets LocalStack by default. The one
# remaining live call is module.networking's data.aws_caller_identity.current (STS), which
# `plan` reads even when every resource is being created fresh - overridden below so these
# tests need no LocalStack and no real account, only the aws provider plugin itself. Run with
# `tofu test` / `terraform test` from infra/environments/dev in any environment with normal
# registry access.

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
  # (infra/environments/dev/variables.tf) actually fires when someone tries to point dev at a
  # real account without setting human_approved_real_account = true.
  expect_failures = [
    var.human_approved_real_account,
  ]
}

# A "real account WITH approval succeeds" run is deliberately not included here: with
# use_local_stack = false, the provider is configured for real AWS, so a full plan needs real,
# non-LocalStack credentials - not something that belongs in an automated test run. Exercise
# that path manually only after a human has actually approved it, per docs/KICKOFF.md.

run "networking_module_is_wired_up_with_the_dev_environment_name" {
  command = plan

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  # flow_log_group_name is a static string built only from var.environment - it's known at
  # plan time (not a cloud-generated ID), so it's safe to assert on without an apply.
  assert {
    condition     = module.networking.flow_log_group_name == "/telemetry/dev/vpc-flow-logs"
    error_message = "The networking module must be invoked with environment = \"dev\"."
  }
}
