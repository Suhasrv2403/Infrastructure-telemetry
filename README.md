# Infrastructure-telemetry

Batch lakehouse pipeline turning telemetry from ~1.1M energy devices (Powerwall, Megapack,
Powerpack, Supercharger stalls and cabinets) into reliability findings, telemetry-health
metrics and downstream datasets.

Start here:
- [CLAUDE.md](CLAUDE.md) - stages, grain, invariants, stack, repo layout, sub-agents.
- [Build backlog.md](Build%20backlog.md) - the 64-ticket / 5-phase backlog.
- [docs/KICKOFF.md](docs/KICKOFF.md) - first-session prompts for Phase 0.
- [docs/decisions/](docs/decisions/) - one file per design decision (required whenever a
  change touches a table's grain).

Currently in **Phase 0: discover and capture** (see Build backlog.md). Nothing in this repo
talks to a real cloud account yet; local/LocalStack-backed development only until Gate 0.
