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
