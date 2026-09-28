# tests/unit/

Unit tests for pipeline, parser and detector code. `test_supercharger_fixture_generator.py`
tests the fixture generator itself (schema shape + injected-messiness invariants), not a
parser - parser fixture tests land with P1-04.

`test_stage0_capture.py` tests `pipeline/stage0_landing/capture.py` (P0-05): arrival-hour key
layout, immutability (never overwriting an already-landed object), rejection of non-Supercharger
messages, and reconciliation between messages seen and objects landed. Mocks S3 with moto -
does not require LocalStack/Floci.
