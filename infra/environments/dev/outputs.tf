output "vpc_id" {
  value = module.networking.vpc_id
}

output "public_subnet_ids" {
  value = module.networking.public_subnet_ids
}

output "private_subnet_ids" {
  value = module.networking.private_subnet_ids
}

output "ingest_service_role_arn" {
  value = module.iam.ingest_service_role_arn
}

output "orchestrator_role_arn" {
  value = module.iam.orchestrator_role_arn
}
