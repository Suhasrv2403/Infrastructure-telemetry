output "ingest_service_role_arn" {
  value = aws_iam_role.ingest_service.arn
}

output "orchestrator_role_arn" {
  value = aws_iam_role.orchestrator.arn
}
