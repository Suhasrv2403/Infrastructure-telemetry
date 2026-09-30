# tests/unit/

Unit tests for pipeline, parser and detector code. `test_supercharger_fixture_generator.py`
tests the fixture generator itself (schema shape + injected-messiness invariants), not a
parser - parser fixture tests land with P1-04.

`test_stage0_capture.py` tests `pipeline/stage0_landing/capture.py` (P0-05): arrival-hour key
layout, immutability (never overwriting an already-landed object), rejection of non-Supercharger
messages, and reconciliation between messages seen and objects landed. Mocks S3 with moto -
does not require LocalStack/Floci.

`test_access_requests_*_fixture_generator.py` (five files, one per source) test the synthetic
RMA/tickets/dispatch/outage/provisioning-history generators under
`tests/fixtures/generators/access_requests/` (P0-11's synthetic substitute for real sample
extracts - see that directory's README). Each checks determinism, the assumed schema's shape
and nullability rules, and device_id/site_id overlap with the Supercharger fixture scheme.
`test_access_requests_provisioning_fixture_generator.py` additionally tests the one real
correctness property in that set: that the provisioning history supports an as-of join, i.e.
exactly one row is "current" for a device at any given timestamp.
