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
    ARN of the Stage 0 landing bucket, created in P0-03. Left null before then - the ingest
    service role just won't get its S3 write policy attached yet, rather than the module
    failing outright. Wire this up once P0-03's object_store module exists.
  EOT
}
