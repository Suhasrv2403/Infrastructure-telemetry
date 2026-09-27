variable "use_local_stack" {
  type        = bool
  default     = true
  description = <<-EOT
    When true (the default), the aws provider points at LocalStack and no real credentials
    are needed - safe to plan/apply repeatedly. Staging is meant to eventually point at a real
    (non-production) account, but only after P0-02's plan is reviewed (see docs/KICKOFF.md).
    Flip to false only together with human_approved_real_account = true.
  EOT
}

variable "human_approved_real_account" {
  type        = bool
  default     = false
  description = <<-EOT
    Set true only after a human has explicitly reviewed and approved applying this
    environment against a real cloud account (see docs/KICKOFF.md: "stop and report before
    anything touches a real account", and CLAUDE.md: "Humans own... anything touching prod
    data"). Left false, setting use_local_stack = false is refused at plan time. See
    real-account.tfvars.example for the shape of a real-account override.
  EOT
}

variable "cloud_endpoints" {
  description = <<-EOT
    Per-service endpoint overrides - this is the dependency-injection point: modules and this
    root module never hardcode which cloud they're talking to, only this variable does.
    Defaults target LocalStack's single edge port. Ignored when use_local_stack = false.
  EOT
  type = object({
    s3  = optional(string)
    iam = optional(string)
    sts = optional(string)
    ec2 = optional(string)
  })
  default = {
    s3  = "http://localhost:4566"
    iam = "http://localhost:4566"
    sts = "http://localhost:4566"
    ec2 = "http://localhost:4566"
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}
