# pipeline/stage0_landing/

Stage 0 Landing: one row per message as received, partitioned by **arrival** hour, immutable.
This is the replay source for the whole pipeline (invariant 1 and 7 in CLAUDE.md) - it is
never rewritten, and replaying a closed window from here must reproduce production output
exactly.

Implemented starting P0-05 (single-region Supercharger capture), hardened in P1-01
(production ingest buffer) and P1-02 (compaction/retention tiering).

`capture.py` is P0-05's batch capture job, not the production ingest service (that's P1-01,
still a placeholder in `ingest/service/`). Given an iterable of message envelopes - today, the
synthetic Supercharger fixture generator standing in for real capture access - it lands each
one as a single immutable object at `raw/arrival_date=YYYY-MM-DD/hour=HH/<message_id>.json`
in the landing bucket, skipping (never overwriting) any key that's already landed, and reports
a `CaptureResult` that `reconcile()` checks against the source message count - the automated
version of this ticket's "done when": raw messages land by arrival hour, counts reconcile with
source. Run it directly against a bucket with:

    python -m pipeline.stage0_landing.capture --bucket telemetry-dev-landing \
        --endpoint-url http://localhost:4566

Unit tests (`tests/unit/test_stage0_capture.py`) mock S3 with moto rather than requiring
LocalStack/Floci to be running.
