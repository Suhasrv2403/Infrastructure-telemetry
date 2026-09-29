# Reports index

Every real, human-readable report/document produced so far by the backlog work, gathered in one place. These are **read-only snapshots** copied from their source branches (see the PROVENANCE header at the top of each file) — the source branch is still the canonical copy, and none of these branches have been merged into trunk. If a branch is later amended, re-run the consolidation to refresh its snapshot here.

The signal catalog (`catalog/signals.yaml`, `catalog/README.md`) is a separate, structured data artifact rather than a narrative report, and already lives on trunk directly (carried over when P1-08 was built).

| Report | Ticket | Status | Source branch (commit) | What it covers |
|---|---|---|---|---|
| [P0-01-telemetry-backend-audit-template.md](P0-01-telemetry-backend-audit-template.md) | P0-01 | To do — real backend audit needs a human/SME to fill it in against the real Supercharger telemetry backend | `P0-01-telemetry-backend-audit` (21eb8b4) | Template + structure for auditing the current telemetry backend: what it ingests, current throughput, gaps vs. the target architecture. |
| [P0-06-arrival-shape.md](P0-06-arrival-shape.md) | P0-06 | Done | `P0-06-arrival-shape-profiler` (c22a303) | Profiling of message arrival-time shape (burstiness, inter-arrival distribution) from synthetic fixtures. |
| [P0-07-timestamp-clock-quality.md](P0-07-timestamp-clock-quality.md) | P0-07 | Done | `P0-07-timestamp-clock-quality-profiler` (fd385bd) | Device clock-skew / clock-quality profiling: how far device timestamps drift from arrival time. |
| [P0-08-lateness-duplicates.md](P0-08-lateness-duplicates.md) | P0-08 | Done | `P0-08-lateness-duplicate-profiler` (e0e6aec) | Profiling of late-arriving and duplicate messages; informs the lateness-horizon decision. |
| [P0-09-retry-behavior-test-plan.md](P0-09-retry-behavior-test-plan.md) | P0-09 | Done | `P0-09-device-retry-behavior-profiler` (473db87) | Test plan + findings for device retry/backoff behavior under simulated outages. |
| [P0-11-data-access-requests.md](P0-11-data-access-requests.md) | P0-11 | To do — needs real external data-owner sign-off | `P0-11-data-access-requests` (e0bdf1c) | Drafted access-request documents for the real external data sources the pipeline will eventually need. |
| [P0-12-privacy-security-review-brief.md](P0-12-privacy-security-review-brief.md) | P0-12 | To do — needs human privacy/security reviewer sign-off | `P0-12-privacy-security-review` (1c92a1a) | Privacy and security review brief covering PII handling, retention, and access-control posture. |
| [P0-13-gate0-report.md](P0-13-gate0-report.md) | P0-13 | To do — Gate 0 sign-off is human-owned | `P0-13-profiling-report-design-lock` (185df2d) | Consolidated Gate 0 report pulling together the P0-06/07/08/09 profiling findings, with the 24h lateness-horizon recommendation used throughout the rest of the backlog. |
| [P1-16-late-data-backfill-runbook.md](P1-16-late-data-backfill-runbook.md) | P1-16 | To do — end-to-end technical work is done and tested, but the ticket's "(Gate 1)" done-when criterion is human-owned | `P1-16-backfill-e2e` (0fc4f76) | Operational runbook for backfilling data after a device/outage gap, backed by a deterministic 72h-outage test fixture and full e2e test. |

For the full backlog status (which tickets are Done vs. To do, branch ledger, push status), see the "Telemetry Backlog Status" page: https://claude.ai/artifact/5yzxfRxVAzeFPYB7a8KbvW
