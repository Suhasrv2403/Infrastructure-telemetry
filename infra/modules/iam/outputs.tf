output "ingest_service_role_arn" {
  value = aws_iam_role.ingest_service.arn
}

output "orchestrator_role_arn" {
  value = aws_iam_role.orchestrator.arn
}

output "ingest_s3_write_attached" {
  description = "True once landing_bucket_arn is set and the S3 write policy is attached (P0-03+)."
  value       = length(aws_iam_role_policy.ingest_service_s3_write) > 0
}
