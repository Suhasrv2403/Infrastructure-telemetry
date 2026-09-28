output "landing_bucket_name" {
  value = aws_s3_bucket.landing.bucket
}

output "landing_bucket_arn" {
  value = aws_s3_bucket.landing.arn
}

output "warehouse_bucket_name" {
  value = aws_s3_bucket.warehouse.bucket
}

output "warehouse_bucket_arn" {
  value = aws_s3_bucket.warehouse.arn
}

output "glue_database_name" {
  value = aws_glue_catalog_database.telemetry.name
}

output "kms_key_arn" {
  value = aws_kms_key.object_store.arn
}
