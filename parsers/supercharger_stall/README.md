# parsers/supercharger_stall/

`firmware_2_1_4.py` (P1-03) is a **demonstration parser only**, registered for
`("supercharger_stall", "2.1.4")` to prove `parsers/framework.py`'s registry/dispatch/
quarantine machinery works end to end. It was written against the synthetic fixture
generator (`tests/fixtures/generators/supercharger.py`), not a firmware-SME-reviewed payload
capture, and does not cover this device class's other two firmware versions (2.3.0, 3.0.1) -
messages from those still land in quarantine. Real per-firmware parser coverage, including
reorganizing into the `parsers/supercharger_stall/<firmware_version>/` directory layout
described in `parsers/README.md`, is P1-04.

Until real payloads land from P0-05, develop against the synthetic fixtures in
`tests/fixtures/supercharger_stall/` (see `tests/fixtures/generators/supercharger.py`).
