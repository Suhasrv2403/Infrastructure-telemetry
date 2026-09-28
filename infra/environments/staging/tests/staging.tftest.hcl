# Native Terraform/OpenTofu tests for the staging environment root module (P0-02, extended by P0-03).
#
# See infra/modules/networking/tests/networking.tftest.hcl for why every run overrides
# module.networking's and (as of P0-03) module.object_store's STS calls. Run with `tofu test` /
# `terraform test` from infra/environments/staging in any environment with normal registry access.

run "defaults_are_localstack_first_and_never_pre_approved" {
  command = plan

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = module.object_store.data.aws_caller_identity.current
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
      var.cloud_endpoints.ec2 == "http://localhost:4566" &&
      var.cloud_endpoints.kms == "http://localhost:4566" &&
      var.cloud_endpoints.logs == "http://localhost:4566" &&
      var.cloud_endpoints.glue == "http://localhost:4566"
    )
    error_message = "Default cloud_endpoints must all point at LocalStack's single edge port (4566)."
  }
}

run "real_account_without_human_approval_is_blocked_at_plan_time" {
  command = plan

  variables {
    use_local_stack              = false
    human_approved_real_account  = false
  }

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = module.object_store.data.aws_caller_identity.current
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

  override_data {
    target = module.object_store.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = module.networking.flow_log_group_name == "/telemetry/staging/vpc-flow-logs"
    error_message = "The networking module must be invoked with environment = \"staging\"."
  }
}

run "object_store_is_wired_up_and_feeds_the_iam_s3_write_policy" {
  command = plan

  override_data {
    target = module.networking.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = module.object_store.data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = module.object_store.landing_bucket_name == "telemetry-staging-landing"
    error_message = "The object_store module must be invoked with environment = \"staging\"."
  }

  assert {
    condition     = module.object_store.glue_database_name == "telemetry_staging"
    error_message = "The Iceberg catalog database name must be telemetry_staging (P0-03, docs/decisions/0001)."
  }

  assert {
    condition     = module.iam.ingest_s3_write_attached == true
    error_message = "module.iam must receive module.object_store.landing_bucket_arn and attach the S3 write policy."
  }
}
