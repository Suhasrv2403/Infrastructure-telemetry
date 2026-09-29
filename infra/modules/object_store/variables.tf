variable "environment" {
  type        = string
  description = "Environment name (dev, staging). Used for naming/tagging only."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to every resource this module creates."
}

variable "force_destroy" {
  type        = bool
  default     = false
  description = <<-EOT
    Allow `terraform destroy` to delete these buckets even if they still hold objects. Default
    false (safe) - set true only for ephemeral dev/test targets (e.g. against LocalStack) where
    losing bucket contents on teardown is expected, never for staging or a real account.
  EOT
}

variable "kms_key_deletion_window_days" {
  type        = number
  default     = 30
  description = "Waiting period before the object-store KMS key is actually deleted, if ever."
}

variable "landing_hot_days" {
  type        = number
  default     = 30
  description = <<-EOT
    Days a Stage 0 landing object stays in STANDARD ("hot") storage before this module's
    lifecycle rule transitions it to GLACIER_IR ("archive") - P1-02's "30-day hot then archive"
    done-when. Applies to every object in the landing bucket (raw/ small originals and
    compacted/ consolidated objects alike; see the aws_s3_bucket_lifecycle_configuration.landing
    resource's comment for why GLACIER_IR specifically, not plain GLACIER). A transition never
    deletes data, only moves its storage class - this does not interact with invariant 1
    (Stage 0 append-only) the way an actual deletion would.
  EOT
}
