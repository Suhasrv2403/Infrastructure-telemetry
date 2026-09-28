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
- [docs/profiling/](docs/profiling/) - Phase 0 profiling reports (protocol, batching, message
  size, timestamp quality, lateness, etc.) run against the synthetic fixtures standing in for
  real telemetry until real capture access exists. `profiling/arrival_shape/` (P0-06) is the
  first profiler; run it with `python -m profiling.arrival_shape.profiler`.

Currently in **Phase 0: discover and capture** (see Build backlog.md). Nothing in this repo
talks to a real cloud account yet; local/LocalStack-backed development only until Gate 0.
