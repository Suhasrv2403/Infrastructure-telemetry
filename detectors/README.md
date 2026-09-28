# detectors/

Detector plugin framework: detectors are declared in config and implemented as plugins that
consume Stage 3/4 tables and emit versioned findings (P2-06).

Planned layout (populated starting P2-06):
- `framework/` - plugin registration, the findings schema, provisional-to-final window
  lifecycle (P2-06, P2-11).
- `rules/` - rule-based detectors v1: session failures, derates, module faults, thermal
  (P2-08).
- `cohort/` - cohort outlier detectors, robust z-score by firmware/hardware/site (P2-09).
- `telemetry_health/` - correlated vs. isolated dropout detectors (P2-10) - note this reads
  from `pipeline/telemetry_health/` but the detector logic itself lives here.
- `battery/` - cell imbalance, capacity fade, dispatch under-delivery (P2-15).
- `backtest/` - harness against RMA and tickets, precision/lead-time reporting (P2-12).

Findings are versioned and never overwritten (see CLAUDE.md invariant 6).
