output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "internal_security_group_id" {
  value = aws_security_group.internal.id
}

output "flow_log_group_name" {
  value = aws_cloudwatch_log_group.flow_logs.name
}

output "flow_log_kms_key_arn" {
  value = aws_kms_key.flow_logs.arn
}
