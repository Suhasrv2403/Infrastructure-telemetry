variable "environment" {
  type        = string
  description = "Environment name (dev, staging). Used for naming/tagging only."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to every resource this module creates."
}

variable "landing_bucket_arn" {
  type        = string
  default     = null
  description = <<-EOT
    ARN of the Stage 0 landing bucket, created in P0-03. Only read when attach_ingest_s3_write
    is true - see that variable for why the two are separate.
  EOT

  validation {
    condition     = !var.attach_ingest_s3_write || var.landing_bucket_arn != null
    error_message = "landing_bucket_arn must be set when attach_ingest_s3_write = true."
  }
}

variable "attach_ingest_s3_write" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether to create and attach the ingest service's S3 write policy. Deliberately a plain
    bool, not `landing_bucket_arn == null`: gating a `count` on landing_bucket_arn directly
    breaks any `terraform test`/plan that wires this module to a real (not-yet-applied) bucket
    resource in the same run, because a new resource's arn is "known after apply", and `count`
    requires a plan-time-known value (confirmed by running `terraform test` for real against
    P0-03's object_store module wiring - not assumed). The ARN itself can stay unknown at plan
    time inside the policy body; only the count gate needs to be plan-time-known.
  EOT
}
