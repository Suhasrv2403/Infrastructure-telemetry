# Native Terraform/OpenTofu tests for the iam module (P0-02).
#
# Standalone modules have no provider configuration of their own - without one, `terraform
# test` picks an implicit default `aws` provider and tries to authenticate against real AWS.
# The provider block below configures it the same way the dev/staging root modules do
# (LocalStack-shaped, dummy credentials). Unlike the networking module, nothing here reads a
# live data source (aws_iam_policy_document just renders JSON locally), so these tests need no
# LocalStack and no real account - only the aws provider plugin itself. Run with `tofu test` /
# `terraform test` from infra/modules/iam in any environment with normal registry access.

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
  tags        = { Project = "infrastructure-telemetry-test" }
}

run "both_service_roles_are_created_with_an_assume_role_policy" {
  command = plan

  assert {
    condition     = can(aws_iam_role.ingest_service.assume_role_policy) && aws_iam_role.ingest_service.assume_role_policy != ""
    error_message = "ingest_service role must have a non-empty assume-role policy."
  }

  assert {
    condition     = can(aws_iam_role.orchestrator.assume_role_policy) && aws_iam_role.orchestrator.assume_role_policy != ""
    error_message = "orchestrator role must have a non-empty assume-role policy."
  }
}

run "s3_write_policy_is_not_attached_without_a_landing_bucket" {
  command = plan

  # landing_bucket_arn defaults to null - P0-03 (the Stage 0 landing bucket) doesn't exist yet.

  assert {
    condition     = length(aws_iam_role_policy.ingest_service_s3_write) == 0
    error_message = "The ingest_service S3 write policy must stay unattached until landing_bucket_arn is set (P0-03+) - invariant: the ingest path should never get write access it doesn't need yet."
  }

  assert {
    condition     = output.ingest_s3_write_attached == false
    error_message = "ingest_s3_write_attached output must report false when no landing bucket is configured."
  }
}

run "s3_write_policy_attaches_and_is_scoped_once_landing_bucket_is_set" {
  command = plan

  variables {
    landing_bucket_arn = "arn:aws:s3:::telemetry-test-landing"
  }

  assert {
    condition     = length(aws_iam_role_policy.ingest_service_s3_write) == 1
    error_message = "Setting landing_bucket_arn should attach exactly one S3 write policy to the ingest_service role."
  }

  assert {
    condition     = output.ingest_s3_write_attached == true
    error_message = "ingest_s3_write_attached output must report true once landing_bucket_arn is set."
  }

  assert {
    condition = (
      length(jsondecode(data.aws_iam_policy_document.ingest_service_s3_write[0].json).Statement) == 1 &&
      # aws_iam_policy_document renders a single-element Resource set as a bare JSON string,
      # not a one-item array (confirmed by running this test for real) - compare directly,
      # don't index into it.
      jsondecode(data.aws_iam_policy_document.ingest_service_s3_write[0].json).Statement[0].Resource == "arn:aws:s3:::telemetry-test-landing/*"
    )
    error_message = "The S3 write policy must be scoped to objects under the landing bucket only, not the bucket ARN itself or a wider resource."
  }

  assert {
    condition = alltrue([
      for a in jsondecode(data.aws_iam_policy_document.ingest_service_s3_write[0].json).Statement[0].Action :
      contains(["s3:PutObject", "s3:AbortMultipartUpload"], a)
    ])
    error_message = "The S3 write policy must grant only write actions (PutObject, AbortMultipartUpload) - never read/list/delete (invariant 1: Stage 0 is append-only)."
  }
}
