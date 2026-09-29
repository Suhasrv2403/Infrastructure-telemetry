"""Demonstration detector: flags a device-hour whose Stage 3a grid is entirely `gap`.

Ticket: P2-06 ("Detector plugin framework and findings schema").

Scope note - read this before assuming this is real detector logic
--------------------------------------------------------------------
This is a STAND-IN detector that exists only to prove detectors/framework/'s registry/config/
dispatch/findings machinery works end to end against a real input shape (Stage 3a
GridBucket tuples - see pipeline/stage3_enrich/time_grid.py, P2-03), the same role
parsers/supercharger_stall/firmware_2_1_4.py plays for parsers/framework.py (P1-03): a real,
working example, not SME-reviewed detection logic. "All buckets in this device-hour are gap"
is a deliberately simple, easy-to-hand-verify condition (a device that reported literally
nothing for a whole hour) - not a considered telemetry-health signal. Real dropout/silence
detection (correlated vs. isolated, thresholds, etc.) is P2-10's job
(detectors/telemetry_health/), a separate, later ticket.

Input contract: `DetectorContext.payload` must be a `tuple[GridBucket, ...]` - one device's
full-hour Stage 3a grid (e.g. one DeviceHourGridResult.buckets from
pipeline.stage3_enrich.time_grid.rebuild_dirty_grid_buckets(), or a hand-built grid in tests).
This detector does not itself build the grid - that's Stage 3a's job; it only reads
`coverage`/`reading_count`/`bucket_start_ms` off buckets already produced.

Config: `params["min_gap_buckets"]` (default 1, see detectors/detectors.yaml) - the minimum
number of buckets a device-hour's grid must have before an all-gap hour is worth flagging (a
context with fewer buckets than this, or zero buckets at all, produces no finding - there is
nothing to meaningfully call "entirely gap").
"""
from __future__ import annotations

from detectors.framework.findings import FindingQuality, FindingStatus
from detectors.framework.registry import DetectorContext, DetectorOutcome, register_detector
from pipeline.stage3_enrich.time_grid import MEASURED

DETECTOR_NAME = "all_gap_hour"


@register_detector(DETECTOR_NAME, detector_version="0.1.0")
def detect_all_gap_hour(context: DetectorContext) -> list[DetectorOutcome]:
    """Flag `context.subject` if its Stage 3a grid (`context.payload`) has at least
    `params["min_gap_buckets"]` buckets and NONE of them are MEASURED.

    Evidence carries the actual bucket timestamps and reading counts that justified the
    finding (not just a bare "fired" flag) - a human reviewing the finding can see exactly
    which minutes were empty. Quality is grounded in the same measured/total bucket counts
    P2-03's grid already tracks (see FindingQuality's docstring).
    """
    buckets = context.payload
    total = len(buckets)
    min_gap_buckets = context.params.get("min_gap_buckets", 1)
    if total < min_gap_buckets:
        return []

    measured = sum(1 for bucket in buckets if bucket.coverage == MEASURED)
    if measured > 0:
        return []

    device_id, event_hour = context.subject
    quality = FindingQuality(label="low", measured_buckets=measured, total_buckets=total)
    evidence = {
        "bucket_start_ms": tuple(bucket.bucket_start_ms for bucket in buckets),
        "reading_counts": tuple(bucket.reading_count for bucket in buckets),
    }
    summary = (
        f"device {device_id} had zero measured buckets across all {total} buckets in "
        f"hour {event_hour}"
    )
    return [
        DetectorOutcome(
            status=FindingStatus.PROVISIONAL,
            summary=summary,
            evidence=evidence,
            quality=quality,
        )
    ]
