# Native Terraform/OpenTofu tests for the object_store module (P0-03).
#
# Standalone modules have no provider configuration of their own (see
# infra/modules/networking/tests/networking.tftest.hcl for the full explanation) - this
# provider block is the same LocalStack-shaped config used everywhere else in this repo. Every
# assertion below targets literal configuration we passed in (bucket names, versioning status,
# public-access-block booleans, policy JSON rendered locally) rather than cloud-generated IDs,
# so every run here only needs `plan`, never LocalStack or a real account - except the one live
# call this module makes (data.aws_caller_identity.current, for the KMS key policy), which is
# overridden the same way networking.tftest.hcl overrides it.

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    s3   = "http://localhost:4566"
    iam  = "http://localhost:4566"
    sts  = "http://localhost:4566"
    ec2  = "http://localhost:4566"
    kms  = "http://localhost:4566"
    logs = "http://localhost:4566"
    glue = "http://localhost:4566"
  }
}

variables {
  environment = "test"
  tags        = { Project = "infrastructure-telemetry-test" }
}

run "landing_and_warehouse_buckets_are_named_per_the_layout_decision" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_s3_bucket.landing.bucket == "telemetry-test-landing"
    error_message = "Landing bucket must follow the telemetry-<env>-landing naming convention (docs/decisions/0001)."
  }

  assert {
    condition     = aws_s3_bucket.warehouse.bucket == "telemetry-test-warehouse"
    error_message = "Warehouse bucket must follow the telemetry-<env>-warehouse naming convention (docs/decisions/0001)."
  }
}

run "both_buckets_are_versioned_and_kms_encrypted" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_s3_bucket_versioning.landing.versioning_configuration[0].status == "Enabled"
    error_message = "Landing bucket must have versioning enabled."
  }

  assert {
    condition     = aws_s3_bucket_versioning.warehouse.versioning_configuration[0].status == "Enabled"
    error_message = "Warehouse bucket must have versioning enabled."
  }

  assert {
    # rule is a TypeSet block (a bucket could in principle have more than one), so it isn't
    # index-able with [0] - confirmed by running this test for real ("Cannot index a set
    # value"). one() is the idiomatic way to pull the single element out of a set/list that's
    # known to have exactly one item, which is always true here (main.tf only ever writes one
    # rule block per bucket).
    condition = (
      one(aws_s3_bucket_server_side_encryption_configuration.landing.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" &&
      one(aws_s3_bucket_server_side_encryption_configuration.warehouse.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms"
    )
    error_message = "Both buckets must be encrypted with a customer-managed KMS key, not SSE-S3."
  }
}

run "both_buckets_block_all_public_access" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.landing.block_public_acls,
      aws_s3_bucket_public_access_block.landing.block_public_policy,
      aws_s3_bucket_public_access_block.landing.ignore_public_acls,
      aws_s3_bucket_public_access_block.landing.restrict_public_buckets,
    ])
    error_message = "Landing bucket must block all four public-access dimensions."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.warehouse.block_public_acls,
      aws_s3_bucket_public_access_block.warehouse.block_public_policy,
      aws_s3_bucket_public_access_block.warehouse.ignore_public_acls,
      aws_s3_bucket_public_access_block.warehouse.restrict_public_buckets,
    ])
    error_message = "Warehouse bucket must block all four public-access dimensions."
  }
}

run "both_buckets_deny_insecure_transport" {
  command = plan

  # landing_tls_only/warehouse_tls_only embed aws_s3_bucket.<x>.arn in their `resources` list,
  # and a brand-new bucket's arn is "known after apply" even though the ARN is actually fully
  # derivable from the (static) bucket name - confirmed by running this test for real ("Unknown
  # condition value": .json itself becomes unknown because an unknown input taints the whole
  # rendered policy document). override_resource stubs the two buckets' arn to a known value so
  # this run can stay command = plan like every other run in this file, rather than needing an
  # apply against LocalStack just for this one assertion. override_during = plan is required -
  # without it, override_resource only takes effect during apply (its default), so it had no
  # effect at all on a command = plan run and the first attempt at this fix still failed with
  # the identical error - confirmed by running this test for real, twice.
  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_resource {
    target           = aws_s3_bucket.landing
    override_during  = plan
    values = {
      arn = "arn:aws:s3:::telemetry-test-landing"
    }
  }

  override_resource {
    target           = aws_s3_bucket.warehouse
    override_during  = plan
    values = {
      arn = "arn:aws:s3:::telemetry-test-warehouse"
    }
  }

  assert {
    condition = (
      jsondecode(data.aws_iam_policy_document.landing_tls_only.json).Statement[0].Effect == "Deny" &&
      jsondecode(data.aws_iam_policy_document.landing_tls_only.json).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "Landing bucket policy must deny requests that aren't over TLS."
  }

  assert {
    condition = (
      jsondecode(data.aws_iam_policy_document.warehouse_tls_only.json).Statement[0].Effect == "Deny" &&
      jsondecode(data.aws_iam_policy_document.warehouse_tls_only.json).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "Warehouse bucket policy must deny requests that aren't over TLS."
  }
}

run "glue_catalog_database_points_at_the_warehouse_bucket" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_glue_catalog_database.telemetry.name == "telemetry_test"
    error_message = "Glue database name must be telemetry_<env> with underscores (Glue disallows hyphens)."
  }

  assert {
    condition     = aws_glue_catalog_database.telemetry.location_uri == "s3://telemetry-test-warehouse/"
    error_message = "Glue database location_uri must default to the warehouse bucket's root, so tables created without an explicit LOCATION land in the right place."
  }
}

run "object_store_kms_key_has_rotation_enabled" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  assert {
    condition     = aws_kms_key.object_store.enable_key_rotation == true
    error_message = "The object-store KMS key must have automatic annual rotation enabled."
  }
}
