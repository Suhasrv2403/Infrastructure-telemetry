# infra/

Terraform for this project's cloud footprint.

- `modules/` - reusable Terraform modules (networking, IAM, object storage buckets, the
  Iceberg catalog, orchestrator compute). Modules take provider-endpoint variables so the
  same module works against a real cloud or a local emulator.
- `environments/dev/`, `environments/staging/` - per-environment root modules that wire the
  shared modules together with environment-specific variables. There is deliberately no
  `environments/prod/` yet: creating a production account is human-owned (see CLAUDE.md,
  "Humans own"), and happens only after Gate 0 sign-off.

## Local-first / LocalStack convention (P0-02)

Every module accepts a `cloud_endpoints` object (see `modules/provider_endpoints`) instead of
hardcoding provider SDK defaults. In `environments/dev`, that variable defaults to LocalStack
endpoints (`http://localhost:4566` for every service) so the whole stack - buckets, IAM roles,
networking - can be planned and applied against a local emulator with zero real-cloud
credentials. Pointing an environment at a real account later is a variable/backend change,
not a rewrite: swap `cloud_endpoints` to the real provider endpoints and supply real
credentials via the normal provider auth chain.

This is a form of dependency injection at the infra layer: modules depend on an endpoints
abstraction, never on "which cloud we're actually talking to."

Nothing here has been `apply`'d against a real account. Per docs/KICKOFF.md: stop and report
before that changes.

## Running this locally

Not run/validated from this session (no `terraform` binary or Docker available in that
environment) - written by hand against the LocalStack + `hashicorp/aws` provider convention.
Please do a real `terraform plan` locally before trusting it further. To try it:

```bash
# 1. Start LocalStack (needs Docker):
docker run -d --name localstack -p 4566:4566 localstack/localstack

# 2. Plan/apply dev - defaults already point at LocalStack, no real credentials needed:
cd infra/environments/dev
terraform init
terraform plan
terraform apply
```

`infra/environments/staging` works the same way by default. To eventually point staging at a
real (non-production) account, see `real-account.tfvars.example` in that directory - it
requires `human_approved_real_account = true`, which a `locals` guard in `main.tf` enforces at
plan time (refuses with a clear error otherwise). That flip should only happen after the human
review docs/KICKOFF.md calls for, not as a routine `tfvars` edit.
