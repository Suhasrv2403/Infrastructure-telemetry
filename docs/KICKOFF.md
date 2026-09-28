# Kickoff prompts

## First Claude Code session (repo root)
Read CLAUDE.md. We're starting Phase 0. Create the repo layout described there with
placeholder READMEs, a pytest setup, and a synthetic fixture generator for Supercharger
stall and cabinet payloads. Then, in parallel on separate branches:
- use infra-sre for P0-02 (Terraform baseline for dev and staging only),
- use platform-engineer for P0-03 and P0-04 once P0-02's plan is reviewed.
Have design-reviewer review each branch. Stop and report before anything touches a real account.

## Cowork tasks (non-code tickets)
- P0-01: draft the telemetry backend audit template and interview questions.
- P0-11: draft data-access requests for RMA, tickets, dispatch, outage and provisioning data.
- P0-12: prepare the privacy and security review brief for residential and utility-site data.
- Weekly: /schedule a status summary of the backlog tab every Friday.
