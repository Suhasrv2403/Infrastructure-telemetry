# parsers/

Versioned, per-firmware parsers: `parsers/<device_class>/<firmware_version>/`. Unknown
formats are routed to quarantine rather than dropped or force-parsed (P1-03).

Device classes (from README.md):
- `supercharger_stall/`, `supercharger_cabinet/` - Phase 1 pilot (P1-04).
- `powerwall/` - Phase 3 (P3-03).
- `megapack/`, `powerpack/` - Phase 2 (P2-13), including a cell-level child table for
  Megapack/Powerpack.

Every parser needs fixture tests built from real payloads once they're available (P1-04,
P2-13, P3-03); until real Supercharger payloads land (P0-05), `tests/fixtures/` holds
synthetic fixtures so parser/pipeline development isn't blocked on capture.
