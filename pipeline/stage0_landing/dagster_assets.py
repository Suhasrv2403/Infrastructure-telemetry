"""Dagster orchestration for Stage 0 landing capture (P0-04: orchestrator + partition model).

Wraps pipeline/stage0_landing/capture.py's capture_messages()/reconcile() - the actual landing
logic built for P0-05 - in a partitioned Dagster asset, so it can be run and backfilled through
the orchestrator rather than only via capture.py's standalone CLI.

Scope note (mirrors capture.py's own docstring): the message source here is still the synthetic
Supercharger fixture generator, not a real feed - P1-01's ingest service is what eventually
replaces it. Because that source has a fixed, synthetic clock (see
tests/fixtures/generators/supercharger.py's SYNTHETIC_NOW_MS), this asset's partition set is
bounded to the date range that generator actually produces arrivals in (see
STAGE0_PARTITIONS_START/END below), not to real wall-clock "now". Once P1-01 exists, Stage 0's
partition set should track real wall-clock arrival hours instead - open-ended, ending at "now".
This fixed window exists only to prove out the orchestrator/partition/backfill mechanics P0-04
is scoped to, against the data P0-05 already knows how to land.
"""
# Deliberately NOT `from __future__ import annotations` here: Dagster's @asset decorator
# inspects the *raw* annotation on the `context` parameter to validate it's exactly
# AssetExecutionContext (or left blank) - under PEP 563 lazy annotations that raw annotation
# would be the string "AssetExecutionContext" instead of the class itself, which Dagster
# rejects (confirmed by actually running it, not assumed - it raised
# DagsterInvalidDefinitionError until this import was removed). Every other annotation in this
# file still uses modern syntax (`dict[str, Any]`, `str | None`), which is fine at runtime on
# Python 3.10+ without the future import (PEP 585 / PEP 604).
import datetime as dt
import functools
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext

from pipeline.stage0_landing.capture import ReconciliationError, capture_messages, reconcile
from tests.fixtures.generators.supercharger import GeneratedFixtures, GeneratorConfig, generate

# Covers the full arrival range the default-config generator produces (empirically confirmed:
# ~2026-05-26 00:00 to ~2026-06-02 02:47 UTC for GeneratorConfig() defaults, a ~7-day window
# driven by the session/cabinet-window simulation range - see supercharger.py's
# _simulate_stall_device/_simulate_cabinet_device). Arrival is anchored on each batch's
# ground-truth event time (supercharger.py's _emit_device_messages), independent of a device's
# own reported clock, so a corrupted or drifted device_ts_ms no longer stretches this range -
# see that function's comment for the bug this used to have. Bounds below are padded generously
# past the actual range (extra partitions will simply be empty on materialize/backfill), not
# trimmed tightly to it, so this doesn't need updating if the generator's config changes
# slightly; re-measure if it ever looks wrong.
STAGE0_PARTITIONS_START = "2026-05-25-00:00"
STAGE0_PARTITIONS_END = "2026-06-04-00:00"  # exclusive

stage0_landing_partitions = dg.HourlyPartitionsDefinition(
    start_date=STAGE0_PARTITIONS_START,
    end_date=STAGE0_PARTITIONS_END,
)


@functools.lru_cache(maxsize=8)
def _generate_cached(seed: int, devices_per_firmware: int) -> GeneratedFixtures:
    """Cached so a multi-partition backfill generates the ~2.6k-message synthetic corpus once,
    not once per partition - materializing all 528 partitions would otherwise regenerate the
    whole corpus 528 times for no reason."""
    return generate(GeneratorConfig(seed=seed, devices_per_firmware=devices_per_firmware))


class LandingBucketResource(dg.ConfigurableResource):
    """Where this asset lands Stage 0 objects - kept as plain resource config, not read from
    Terraform state, so the same asset code runs against dev, staging, or a future prod bucket
    by passing different config. Mirrors the LocalStack-first cloud_endpoints convention the
    Terraform modules use: endpoint_url is set for LocalStack/Floci, left unset for a real
    account.
    """

    bucket: str
    endpoint_url: str | None = None


class SyntheticSuperchargerSource(dg.ConfigurableResource):
    """Stand-in Stage 0 message source until P1-01's real ingest service exists (see this
    module's docstring). Generates the same corpus capture.py's CLI uses by default."""

    seed: int = GeneratorConfig().seed
    devices_per_firmware: int = GeneratorConfig().devices_per_firmware

    def messages_in_window(self, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        """Every generated message whose arrival_ts_ms falls in [start, end)."""
        fixtures = _generate_cached(self.seed, self.devices_per_firmware)
        return [
            message
            for message in fixtures.all_messages()
            if start <= _arrival_dt(message) < end
        ]


def _arrival_dt(message: dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromtimestamp(message["arrival_ts_ms"] / 1000, tz=dt.timezone.utc)


@dg.asset(
    partitions_def=stage0_landing_partitions,
    group_name="stage0_landing",
    description=(
        "Lands Supercharger message envelopes arriving in this partition's hour into the "
        "landing bucket, keyed by arrival hour per docs/decisions/0001 and CLAUDE.md "
        "invariant 4 (never partition by device_id). Backed by pipeline/stage0_landing/"
        "capture.py's capture_messages()/reconcile() - see that module for the invariant-1 "
        "(append-only) and reconciliation guarantees this asset inherits."
    ),
)
def stage0_landing(
    context: AssetExecutionContext,
    landing_bucket: LandingBucketResource,
    supercharger_source: SyntheticSuperchargerSource,
) -> dg.MaterializeResult:
    window = context.partition_time_window
    messages = supercharger_source.messages_in_window(window.start, window.end)

    result = capture_messages(
        messages, bucket=landing_bucket.bucket, endpoint_url=landing_bucket.endpoint_url
    )

    try:
        reconcile(result)
    except ReconciliationError as exc:
        # A partition that doesn't reconcile is a failed run, not a warning - Stage 0 must
        # account for every message exactly once (see capture.py's ReconciliationError).
        raise dg.Failure(description=str(exc)) from exc

    return dg.MaterializeResult(
        metadata={
            "messages_seen": result.messages_seen,
            "objects_written": result.objects_written,
            "objects_already_present": result.objects_already_present,
            "partition_window_start": dg.MetadataValue.text(window.start.isoformat()),
            "partition_window_end": dg.MetadataValue.text(window.end.isoformat()),
        }
    )
