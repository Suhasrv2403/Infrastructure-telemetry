# tests/

- `fixtures/` - synthetic and (once captured) real payload fixtures per device class and
  firmware, plus the generators that produce the synthetic ones.
- `unit/` - unit tests for pipeline/parser/detector code.

Per CLAUDE.md invariant 8: no real residential data outside prod. Anything under `tests/`
must be synthetic or a scrubbed/synthetic-equivalent fixture.
