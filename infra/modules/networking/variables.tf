variable "environment" {
  type        = string
  description = "Environment name (dev, staging). Used for naming/tagging only."
}

variable "vpc_cidr" {
  type        = string
  default     = "10.42.0.0/16"
  description = "CIDR block for this environment's VPC."
}

variable "az_count" {
  type        = number
  default     = 2
  description = "Number of public/private subnet pairs to create."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to every resource this module creates."
}
