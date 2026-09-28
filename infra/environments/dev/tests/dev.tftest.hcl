# Native Terraform/OpenTofu tests for the dev environment root module (P0-02).
# See infra/modules/networking/tests/networking.tftest.hcl for why these could not be
# executed in the sandbox this repo was authored in. Run with `tofu test` / `terraform test`
# from infra/environments/dev in a normal, network-connected environment.

run "defaults_are_localstack_first_and_never_pre_approved" {
  command = plan

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

  # Confirms the cross-variable validation on human_approved_real_account
  # (infra/environments/dev/variables.tf) actually fires when someone tries to point dev at a
  # real account without setting human_approved_real_account = true.
  expect_failures = [
    var.human_approved_real_account,
  ]
}

# A "real account WITH approval succeeds" run is deliberately not included here: with
# use_local_stack = false, `plan` initializes the real aws provider and needs real,
# non-LocalStack credentials and registry access for the aws provider - neither belongs in an
# automated test run. Exercise that path manually only after a human has actually approved it,
# per docs/KICKOFF.md.

run "networking_module_is_wired_up_with_the_dev_environment_name" {
  command = plan

  # flow_log_group_name is a static string built only from var.environment - it's known at
  # plan time (not a cloud-generated ID), so it's safe to assert on without an apply.
  assert {
    condition     = module.networking.flow_log_group_name == "/telemetry/dev/vpc-flow-logs"
    error_message = "The networking module must be invoked with environment = \"dev\"."
  }
}
