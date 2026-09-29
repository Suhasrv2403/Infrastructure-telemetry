# tests/unit/

Unit tests for pipeline, parser and detector code. `test_supercharger_fixture_generator.py`
tests the fixture generator itself (schema shape + injected-messiness invariants), not a
parser - parser fixture tests land with P1-04.

`test_megapack_powerpack_fixture_generator.py` - PREP for P2-13 - is the same kind of test for
`tests/fixtures/generators/megapack_powerpack.py`: schema shape, injected-messiness invariants,
plus checks specific to the pack/cell split (pack-level and cell-level are genuinely distinct
streams sharing device_id, cell cadence is coarser than pack cadence, Megapack has more cell
channels than Powerpack) and a check that every generated value respects
catalog/signals.yaml's declared valid_range for the Megapack/Powerpack/*_cell entries added on
this same branch. Not a parser test - real P2-13 parser fixture tests land later.

`test_stage0_capture.py` tests `pipeline/stage0_landing/capture.py` (P0-05): arrival-hour key
layout, immutability (never overwriting an already-landed object), rejection of non-Supercharger
messages, and reconciliation between messages seen and objects landed. Mocks S3 with moto -
does not require LocalStack/Floci.
