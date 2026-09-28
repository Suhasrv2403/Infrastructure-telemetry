"""Dagster Definitions entrypoint (P0-04: deploy orchestrator with partition model).

Run locally against dev (the default) with:

    dagster dev -f pipeline/definitions.py

...or against staging:

    TELEMETRY_ENV=staging dagster dev -f pipeline/definitions.py

Either way, LocalStack (port 4566) and, for anything touching Glue, Floci (port 4567) must
already be running - see infra/modules/object_store/README.md. This module only wires up
Stage 0 landing so far (P0-05's capture, via pipeline/stage0_landing/dagster_assets.py);
later stages register their own assets here as they land (P1-03 onward).
"""
from __future__ import annotations

import os

import dagster as dg

from pipeline.stage0_landing.dagster_assets import (
    LandingBucketResource,
    SyntheticSuperchargerSource,
    stage0_landing,
)


def _landing_bucket_resource() -> LandingBucketResource:
    environment = os.environ.get("TELEMETRY_ENV", "dev")
    return LandingBucketResource(
        bucket=os.environ.get("LANDING_BUCKET", f"telemetry-{environment}-landing"),
        endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:4566"),
    )


defs = dg.Definitions(
    assets=[stage0_landing],
    resources={
        "landing_bucket": _landing_bucket_resource(),
        "supercharger_source": SyntheticSuperchargerSource(),
    },
)
