# pipeline/telemetry_health/

Telemetry health, tracked independently of the reliability detectors in `detectors/`:
- Last-seen snapshot, refreshed every 15 minutes.
- Silence-episode open/close job, with a cause field.
- Dropout-by-cohort detectors (correlated vs. isolated).
- Mode-aware expected counts (uses outage/VPP records where available, a lower bound
  otherwise).

Implemented starting P1-11/P1-12 through P2-10 and P3-05.
