variable "use_local_stack" {
  type        = bool
  default     = true
  description = <<-EOT
    When true (the default), the aws provider points at LocalStack and no real credentials
    are needed - safe to plan/apply repeatedly. Flip to false only for a real account, and
    only together with human_approved_real_account = true (enforced by the validation block
    below, not a locals/tobool hack).
  EOT
}

variable "human_approved_real_account" {
  type        = bool
  default     = false
  description = <<-EOT
    Set true only after a human has explicitly reviewed and approved applying this
    environment against a real (non-production) cloud account (see docs/KICKOFF.md: "stop and
    report before anything touches a real account", and CLAUDE.md: "Humans own... anything
    touching prod data"). Left false, setting use_local_stack = false is refused at plan time.
    Copy real-account.tfvars.example to real-account.tfvars to exercise this path.
  EOT

  validation {
    condition     = var.use_local_stack || var.human_approved_real_account
    error_message = "BLOCKED: set human_approved_real_account = true only after a human has reviewed and approved applying staging against a real cloud account. See docs/KICKOFF.md."
  }
}

variable "cloud_endpoints" {
  description = <<-EOT
    Per-service endpoint overrides - this is the dependency-injection point: modules and this
    root module never hardcode which cloud they're talking to, only this variable does.
    Defaults target LocalStack's single edge port (4566, used for every service in modern
    LocalStack). Every service any module in this repo actually calls must be listed here -
    kms and logs were added after the flow-log KMS key and log group were caught hitting real
    AWS during testing because they weren't overridden. Ignored when use_local_stack = false
    (the real provider defaults apply).
  EOT
  type = object({
    s3   = optional(string)
    iam  = optional(string)
    sts  = optional(string)
    ec2  = optional(string)
    kms  = optional(string)
    logs = optional(string)
  })
  default = {
    s3   = "http://localhost:4566"
    iam  = "http://localhost:4566"
    sts  = "http://localhost:4566"
    ec2  = "http://localhost:4566"
    kms  = "http://localhost:4566"
    logs = "http://localhost:4566"
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}
