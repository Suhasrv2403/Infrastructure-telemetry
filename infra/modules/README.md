# infra/modules/

Reusable Terraform modules. Each module takes a `cloud_endpoints` variable (see
`provider_endpoints/`) rather than assuming a specific cloud - that's what lets
`environments/dev` run entirely against LocalStack while `environments/staging` (later,
after human review) can point at a real account.

Planned modules (added as their tickets land):
- `provider_endpoints/` - the shared endpoints variable object + LocalStack defaults (P0-02).
- `networking/` - VPC/subnets/security groups (P0-02).
- `iam/` - roles and policies, scoped per service (P0-02).
- `object_store/` - buckets + lifecycle rules for the Iceberg-backed lake (P0-03).
- `iceberg_catalog/` - catalog service/config (P0-03).
- `orchestrator/` - Dagster deployment (P0-04).
