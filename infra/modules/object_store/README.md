# object_store

P0-03: the Stage 0 landing bucket, the Stage 1-4 warehouse bucket, and the Iceberg catalog
(a Glue Data Catalog database) for one environment.

Naming and layout conventions are recorded in `docs/decisions/0001-object-store-layout.md` -
this module implements that decision, it doesn't redefine it.

- `landing_bucket_arn` feeds the `iam` module's `landing_bucket_arn` variable, which is what
  finally attaches the `ingest_service` role's S3 write policy (invariant 1: write-only,
  append-only).
- `glue_database_name` and `warehouse_bucket_name` are what P0-04's orchestrator and P1-03's
  parsers will point Spark/Iceberg at.

Like `networking` and `iam`, this module is cloud-agnostic - it takes no opinion on
LocalStack vs. a real account. That's the root modules' job (`cloud_endpoints`).

One wrinkle specific to this module: LocalStack's Glue Data Catalog support requires their
paid Ultimate plan, and silently hangs (rather than erroring) if you try to create a Glue
database against a non-Ultimate LocalStack - confirmed by actually running `terraform apply`
against it. `cloud_endpoints.glue` defaults to a separate emulator, Floci (https://floci.io),
run alongside LocalStack on a remapped port:

    docker run -d --name floci -p 4567:4566 -v /var/run/docker.sock:/var/run/docker.sock floci/floci:latest

Every other service this module (and every other module) uses still goes through LocalStack
on its usual port 4566.
