"""Pipeline observability watchdog: freshness, completeness and a cost proxy.

Ticket: P1-15 ("Pipeline observability: freshness, completeness, cost").

What this is
------------
Three independently testable metric computations - `compute_freshness()`,
`compute_completeness()` and `estimate_cost()` - plus `run_watchdog()`, which runs all three
against a given data source and returns one structured `WatchdogReport` with a per-metric
`Status` (OK / DEGRADED / CRITICAL) and an overall status (the worst of the three). The report
is exactly the shape a real alerting integration (PagerDuty, Slack, ...) would consume - but
building that integration is explicitly out of scope here (see "The honest gap" below).

The honest gap: this ticket's literal "done when" vs. what is actually provable here
----------------------------------------------------------------------------------------
The backlog's "done when" for P1-15 is "Dashboards and alerts live; watchdog runs outside
orchestrator." The first half cannot be honestly claimed here, for the same reason this repo
already states plainly for other infra-gated tickets (see ingest/service/buffer.py's "honest
gap" section, and Build backlog.md's notes on P0-01/P0-10/P0-12): "dashboards ... live" needs
a real deployed observability stack - Grafana, CloudWatch, Datadog or similar, wired to a real
data source on a real schedule - which does not exist in this repo or in this sandbox, and
inventing a screenshot or a fake dashboard config here would be fabricating infrastructure, not
building it. Likewise "alerts live" needs a real paging/notification integration, which is
explicitly out of scope (see "Scope boundaries" in the ticket).

What this module DOES build, honestly and for real: the actual metric computations, as plain,
deterministic, unit-tested Python functions with no dashboard or alerting system behind them -
and a watchdog entry point genuinely runnable today, right now, by anyone with a bucket name
and (optionally) some rows, that prints a structured pass/fail result and a nonzero exit code
on anything short of fully healthy. That is the real, provable half of this ticket; wiring its
output into Grafana/CloudWatch/PagerDuty is real future infra work, not simulated here.

Why "outside the orchestrator" matters, and what that means concretely here
---------------------------------------------------------------------------
A watchdog that only runs AS a Dagster asset cannot detect "the orchestrator itself is down" -
if Dagster's scheduler stops firing, an asset-shaped watchdog stops firing right along with it,
silently, which is exactly the failure mode a pipeline-health watchdog most needs to catch.
So this module does not import `dagster` anywhere, and never will - `_cli()` below is a plain
`argparse` script (the same shape as `pipeline/stage0_landing/capture.py`'s own `_cli()`), that
a cron job, a systemd timer or a small standalone process could invoke completely independently
of whatever state the orchestrator is in.

Attribution / lineage - completeness reuses P1-11's definitions, not a new invention
------------------------------------------------------------------------------------
`pipeline/stage2_canonical/completeness_sidecar.py` (P1-11, "Completeness and lateness sidecar:
device x hour", commit 896a38b on sibling branch `P1-11-completeness-lateness-sidecar` - not in
this branch's lineage, so it cannot be imported here) already defines, per CLAUDE.md's own
framing of the Stage 2 sidecar, what "completeness" means at the (device_id, event_hour) grain:
`expected = round(3600 / expected_interval_s)`, a reading counts `on_time` vs. `late` against a
lateness threshold, and `missing = max(expected - (on_time + late), 0)`. The private helpers
below (`_event_hour`, `_device_hour_key`, `_is_plausible_ts`, `_lateness_s`) reproduce that
exact per-bucket logic - same source field (device_ts_ms, never arrival_ts_ms, per CLAUDE.md
invariant 4), same lateness formula, same "exclude a row whose timestamps aren't plausible
rather than guess" behavior - so this module's numbers do not contradict P1-11's once that
branch lands (same cross-branch-reuse-via-reproduction pattern P1-11's own docstring uses for
`pipeline/stage1_parsed/merge.py`'s `device_hour_key()`). What's different here is the grain:
`compute_completeness()` aggregates across ALL buckets it sees into one fleet-wide summary
(a single ratio + counts), not one row per device-hour - suited to a watchdog-level "is the
pipeline healthy" signal, not per-device downstream analysis. A caller who wants completeness
scoped to one device class or cohort should pre-filter `rows` before passing them in.

Both `DEFAULT_EXPECTED_INTERVAL_S` (15s) and `DEFAULT_ON_TIME_THRESHOLD_S` (180s) below use
P1-11's exact defaults and exact justification (the fixture generator's modeled Supercharger
reading cadence, and 180s as comfortably above that generator's pure-batching-position
lateness) - see completeness_sidecar.py's module docstring for the full derivation. They are
placeholders pending a real per-device-class SLA/history dimension (P2-01), same as there.

Freshness
---------
"How long since Stage 0 last landed anything" - `compute_freshness()` takes an iterable of
landing timestamps (S3 object `LastModified` times, in production; `list_stage0_landing_
timestamps()` below is the standalone listing helper that gets those from a real or
moto-mocked bucket) and compares the most recent one to `now`. Thresholds
(`DEFAULT_FRESHNESS_WARN_AFTER_S` = 900s / 15 min, `DEFAULT_FRESHNESS_CRITICAL_AFTER_S` = 3600s
/ 60 min): chosen so that ordinary batching/network jitter (P1-11's own analysis puts normal
per-message lateness at under ~3 minutes for this repo's synthetic fixtures) never trips even
the warn threshold, while a full hour with nothing landing at all - for a live fleet of
~1.1M devices per CLAUDE.md - is a genuine, unambiguous outage signal. Like every threshold in
this module, these are reasoned placeholders pending a real measured baseline (P0-01), not
measured SLAs - override them with `warn_after_s`/`critical_after_s` once one exists. Zero
timestamps landed (an empty landing bucket, or a totally empty `prefix`) reports CRITICAL:
"nothing has ever landed" is unambiguously the worst case, not a vacuous pass.

Completeness
------------
"How much of the expected data has actually landed" - `compute_completeness()`'s
`completeness_ratio` is `min((on_time_total + late_total) / expected_total, 1.0)`: the fraction
of expected readings that showed up at all (on time or late), across every (device_id,
event_hour) bucket seen in `rows`. Thresholds (`DEFAULT_COMPLETENESS_WARN_RATIO` = 0.98,
`DEFAULT_COMPLETENESS_CRITICAL_RATIO` = 0.90): loosely mirror a typical completeness SLA band
(a couple percent of gaps is routine late-arrival noise per P1-11's own "missing is provisional,
not confirmed" caveat; double-digit-percent gaps are not routine noise). These are illustrative
severity bands chosen for this ticket, explicitly not a negotiated SLA - same honest-placeholder
status as P1-11's own numbers. Zero rows/zero buckets seen reports CRITICAL for the same reason
as freshness above.

Cost (illustrative proxy - read this before trusting a dollar figure from this module)
----------------------------------------------------------------------------------------
There is no real AWS billing data or usage-based billing integration anywhere in this repo or
sandbox (same gap this module documents for dashboards/alerts). `estimate_cost()` produces a
rough order-of-magnitude number from message/byte VOLUME alone -
`(messages / 1e6) * cost_per_million_messages_usd + (bytes_landed / 1e9) * cost_per_gb_usd` -
using made-up, clearly-labeled-as-fake per-unit rates (`DEFAULT_COST_PER_MILLION_MESSAGES_USD`
= $5.00, `DEFAULT_COST_PER_GB_USD` = $0.023 - the latter is simply S3 Standard's public list
price per GB-month as of this writing, reused here only because it is a real, checkable number
of the right order of magnitude, not because storage-for-a-month is actually what's being
measured; the former is not tied to any real service's pricing at all). Every `CostEstimate`
carries `note = COST_DISCLAIMER` verbatim, so the disclaimer travels with the number wherever
it's printed or logged, not just in this docstring. The watchdog does not use this number to
flag "the pipeline is unhealthy because data volume dropped" (that is freshness/completeness's
job); it flags an unexpectedly large ESTIMATED spend against a budget
(`DEFAULT_COST_WARN_BUDGET_USD` = $50, `DEFAULT_COST_CRITICAL_BUDGET_USD` = $200, per watchdog
run/window) - catching a runaway-retry or duplicate-storm volume spike, which a cost proxy is
well suited to surface even though its absolute dollar figure is not trustworthy.

`estimate_cost_from_pipeline()` is a convenience adapter that builds messages/bytes straight
from a Stage 0 `CaptureResult` (pipeline/stage0_landing/capture.py, P0-05) and/or an
`IngestBuffer` `BufferResult` (ingest/service/buffer.py, P1-01) - the two existing volume
signals the ticket names - without changing either module's behavior at all (read-only use of
their existing result dataclasses).

The watchdog
------------
`run_watchdog()` runs all three checks and returns a `WatchdogReport` whose `.status` is the
worst of the three sub-statuses (`Status` is ordered OK < DEGRADED < CRITICAL) - one degraded
or critical metric is enough to make the whole report non-OK, by design: a watchdog that
averaged its sub-signals into a false "mostly fine" could hide a real, single-metric outage.
`_cli()` is the standalone, Dagster-free entry point described above.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import enum
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import boto3

from ingest.service.buffer import BufferResult
from pipeline.stage0_landing.capture import CaptureResult

# ---------------------------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------------------------


class Status(enum.Enum):
    """Ordered health status for one metric or for an overall WatchdogReport."""

    OK = "ok"
    DEGRADED = "degraded"
    CRITICAL = "critical"


_STATUS_SEVERITY: dict[Status, int] = {Status.OK: 0, Status.DEGRADED: 1, Status.CRITICAL: 2}


def _worst(statuses: Iterable[Status]) -> Status:
    """The most severe status among `statuses`. Used to roll the three sub-checks into one
    overall WatchdogReport.status - see module docstring's "The watchdog" section for why the
    worst, not an average, is the right combinator here."""
    return max(statuses, key=lambda status: _STATUS_SEVERITY[status])


# ---------------------------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------------------------

# See module docstring's "Freshness" section for the derivation of both defaults.
DEFAULT_FRESHNESS_WARN_AFTER_S: float = 15 * 60
DEFAULT_FRESHNESS_CRITICAL_AFTER_S: float = 60 * 60


@dataclasses.dataclass(frozen=True)
class FreshnessResult:
    """How long since Stage 0 (or whatever `landed_timestamps` scopes to) last landed data."""

    status: Status
    last_landed_at: dt.datetime | None
    staleness_s: float | None
    checked_at: dt.datetime
    objects_seen: int
    warn_after_s: float
    critical_after_s: float


def compute_freshness(
    landed_timestamps: Iterable[dt.datetime],
    *,
    now: dt.datetime | None = None,
    warn_after_s: float = DEFAULT_FRESHNESS_WARN_AFTER_S,
    critical_after_s: float = DEFAULT_FRESHNESS_CRITICAL_AFTER_S,
) -> FreshnessResult:
    """Freshness relative to `now` (default: real UTC now) of the most recent timestamp in
    `landed_timestamps`. An empty `landed_timestamps` reports CRITICAL - see module docstring.
    """
    if warn_after_s < 0 or critical_after_s < 0:
        raise ValueError("warn_after_s and critical_after_s must be >= 0")
    if critical_after_s < warn_after_s:
        raise ValueError("critical_after_s must be >= warn_after_s")

    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    timestamps = list(landed_timestamps)
    objects_seen = len(timestamps)

    if not timestamps:
        return FreshnessResult(
            status=Status.CRITICAL,
            last_landed_at=None,
            staleness_s=None,
            checked_at=now,
            objects_seen=0,
            warn_after_s=warn_after_s,
            critical_after_s=critical_after_s,
        )

    last_landed_at = max(timestamps)
    # A landing timestamp "after" now (clock skew) is treated as perfectly fresh, not an error.
    staleness_s = max((now - last_landed_at).total_seconds(), 0.0)

    if staleness_s <= warn_after_s:
        status = Status.OK
    elif staleness_s <= critical_after_s:
        status = Status.DEGRADED
    else:
        status = Status.CRITICAL

    return FreshnessResult(
        status=status,
        last_landed_at=last_landed_at,
        staleness_s=staleness_s,
        checked_at=now,
        objects_seen=objects_seen,
        warn_after_s=warn_after_s,
        critical_after_s=critical_after_s,
    )


def list_stage0_landing_timestamps(
    bucket: str,
    *,
    prefix: str = "raw/",
    s3_client: Any = None,
    endpoint_url: str | None = None,
) -> list[dt.datetime]:
    """List the `LastModified` time of every object under `prefix` in the Stage 0 landing
    bucket - the standalone listing helper `compute_freshness()` needs, independent of
    Dagster. `prefix` defaults to "raw/", the whole landing prefix per
    docs/decisions/0001-object-store-layout.md; narrow it (e.g. to one arrival_date/hour) to
    scope freshness to a partition. Uses `LastModified` (S3's own record of when the PUT
    happened), not any timestamp encoded in the object's key or body - that is the real
    "when did this land" signal, at full precision, versus the key's hour-bucket granularity.
    Read-only: does not touch pipeline/stage0_landing/capture.py's write path at all.
    """
    client = s3_client or boto3.client("s3", endpoint_url=endpoint_url)
    paginator = client.get_paginator("list_objects_v2")
    timestamps: list[dt.datetime] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", ()):
            timestamps.append(obj["LastModified"])
    return timestamps


# ---------------------------------------------------------------------------------------------
# Completeness (device_id, event_hour) bucketing/lateness logic reproduced from P1-11's
# completeness_sidecar.py - see module docstring's "Attribution / lineage" section.
# ---------------------------------------------------------------------------------------------


class InvalidEventTimestampError(ValueError):
    """Raised when a row's `device_ts_ms` isn't a usable epoch-ms int. Mirrors
    completeness_sidecar.py's error of the same name/purpose (see module docstring)."""


def _event_hour(row: Mapping[str, Any]) -> dt.datetime:
    """A row's event time, parsed from `device_ts_ms` only - never `arrival_ts_ms`
    (CLAUDE.md invariant 4). Reproduces completeness_sidecar.py's `_event_hour()`."""
    device_ts_ms = row.get("device_ts_ms")
    if not isinstance(device_ts_ms, int) or isinstance(device_ts_ms, bool):
        raise InvalidEventTimestampError(
            f"row for device_id={row.get('device_id')!r} has device_ts_ms={device_ts_ms!r}, "
            "not a usable epoch-ms int"
        )
    return dt.datetime.fromtimestamp(device_ts_ms / 1000, tz=dt.timezone.utc)


def _device_hour_key(row: Mapping[str, Any]) -> tuple[Any, str]:
    """The (device_id, event_hour) key for one row, formatted "YYYY-MM-DDTHH" in UTC.
    Reproduces completeness_sidecar.py's `device_hour_key()`."""
    event_time = _event_hour(row)
    return (row.get("device_id"), f"{event_time:%Y-%m-%dT%H}")


def _is_plausible_ts(value: Any) -> bool:
    """Same sanity guard as completeness_sidecar.py's `_is_plausible_ts()`: excludes missing
    (None) and epoch-default (0) timestamps."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _lateness_s(row: Mapping[str, Any]) -> float | None:
    """(arrival_ts_ms - device_ts_ms) / 1000, or None if either timestamp isn't plausible.
    Reproduces completeness_sidecar.py's `_lateness_s()`."""
    arrival_ts_ms = row.get("arrival_ts_ms")
    device_ts_ms = row.get("device_ts_ms")
    if not _is_plausible_ts(arrival_ts_ms) or not _is_plausible_ts(device_ts_ms):
        return None
    return (arrival_ts_ms - device_ts_ms) / 1000.0


# See module docstring's "Attribution / lineage" section: identical values and justification to
# completeness_sidecar.py's DEFAULT_EXPECTED_INTERVAL_S / DEFAULT_ON_TIME_THRESHOLD_S.
DEFAULT_EXPECTED_INTERVAL_S: float = 15.0
DEFAULT_ON_TIME_THRESHOLD_S: float = 180.0

# See module docstring's "Completeness" section.
DEFAULT_COMPLETENESS_WARN_RATIO: float = 0.98
DEFAULT_COMPLETENESS_CRITICAL_RATIO: float = 0.90


@dataclasses.dataclass(frozen=True)
class CompletenessResult:
    """Fleet-wide (or however `rows` was pre-scoped) completeness summary, aggregated across
    every (device_id, event_hour) bucket seen - see module docstring's "Completeness" section
    for what `completeness_ratio` means and how it's not the same claim as P1-11's per-bucket
    `missing` (both inherit the same "provisional, not confirmed" caveat from there)."""

    status: Status
    buckets_seen: int
    expected_total: int
    on_time_total: int
    late_total: int
    missing_total: int
    completeness_ratio: float
    on_time_ratio: float
    warn_ratio: float
    critical_ratio: float


def compute_completeness(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_interval_s: float = DEFAULT_EXPECTED_INTERVAL_S,
    on_time_threshold_s: float = DEFAULT_ON_TIME_THRESHOLD_S,
    warn_ratio: float = DEFAULT_COMPLETENESS_WARN_RATIO,
    critical_ratio: float = DEFAULT_COMPLETENESS_CRITICAL_RATIO,
) -> CompletenessResult:
    """Aggregate completeness across every (device_id, event_hour) bucket present in `rows`.

    `rows` carry the same `device_id`/`device_ts_ms`/`arrival_ts_ms` shape
    completeness_sidecar.py expects - raw Stage 1 rows or Stage 2 `CanonicalRow.fields` dicts
    both work unchanged (see that module's docstring). Zero rows reports CRITICAL - see module
    docstring's "Completeness" section. Raises InvalidEventTimestampError under the same
    conditions as completeness_sidecar.py's device_hour_key(); ValueError on out-of-range
    threshold arguments.
    """
    if expected_interval_s <= 0:
        raise ValueError(f"expected_interval_s must be > 0, got {expected_interval_s!r}")
    if not 0.0 <= critical_ratio <= warn_ratio <= 1.0:
        raise ValueError("expected 0 <= critical_ratio <= warn_ratio <= 1")

    expected_per_bucket = round(3600 / expected_interval_s)

    on_time_counts: Counter[tuple[Any, str]] = Counter()
    late_counts: Counter[tuple[Any, str]] = Counter()
    keys_seen: set[tuple[Any, str]] = set()

    for row in rows:
        key = _device_hour_key(row)
        keys_seen.add(key)
        lateness_s = _lateness_s(row)
        if lateness_s is None:
            # Can't tell on-time vs. late - excluded from both, same as completeness_sidecar.py.
            continue
        if lateness_s <= on_time_threshold_s:
            on_time_counts[key] += 1
        else:
            late_counts[key] += 1

    buckets_seen = len(keys_seen)
    expected_total = buckets_seen * expected_per_bucket
    on_time_total = sum(on_time_counts.values())
    late_total = sum(late_counts.values())
    missing_total = sum(
        max(expected_per_bucket - (on_time_counts[key] + late_counts[key]), 0)
        for key in keys_seen
    )

    if buckets_seen == 0:
        # No data at all is the worst-case completeness signal, not a vacuous pass.
        completeness_ratio = 0.0
        on_time_ratio = 0.0
        status = Status.CRITICAL
    else:
        landed_total = on_time_total + late_total
        completeness_ratio = min(landed_total / expected_total, 1.0) if expected_total else 1.0
        on_time_ratio = min(on_time_total / expected_total, 1.0) if expected_total else 1.0
        if completeness_ratio >= warn_ratio:
            status = Status.OK
        elif completeness_ratio >= critical_ratio:
            status = Status.DEGRADED
        else:
            status = Status.CRITICAL

    return CompletenessResult(
        status=status,
        buckets_seen=buckets_seen,
        expected_total=expected_total,
        on_time_total=on_time_total,
        late_total=late_total,
        missing_total=missing_total,
        completeness_ratio=completeness_ratio,
        on_time_ratio=on_time_ratio,
        warn_ratio=warn_ratio,
        critical_ratio=critical_ratio,
    )


# ---------------------------------------------------------------------------------------------
# Cost (illustrative proxy)
# ---------------------------------------------------------------------------------------------

# See module docstring's "Cost" section for what each of these is (and, for the message rate,
# isn't) grounded in.
DEFAULT_COST_PER_MILLION_MESSAGES_USD: float = 5.00
DEFAULT_COST_PER_GB_USD: float = 0.023
DEFAULT_COST_WARN_BUDGET_USD: float = 50.0
DEFAULT_COST_CRITICAL_BUDGET_USD: float = 200.0
DEFAULT_AVG_BYTES_PER_MESSAGE: float = 512.0

COST_DISCLAIMER: str = (
    "ILLUSTRATIVE ONLY: a rough order-of-magnitude estimate from a documented, made-up $/GB "
    "and $/message assumption. Not real AWS billing and not a usage-based billing integration."
)


@dataclasses.dataclass(frozen=True)
class CostEstimate:
    """A rough, explicitly-illustrative cost proxy from message/byte volume - see module
    docstring's "Cost" section. `note` carries COST_DISCLAIMER so it travels with the number."""

    status: Status
    messages: int
    bytes_landed: int
    estimated_usd: float
    cost_per_million_messages_usd: float
    cost_per_gb_usd: float
    warn_budget_usd: float
    critical_budget_usd: float
    note: str = COST_DISCLAIMER


def estimate_cost(
    *,
    messages: int = 0,
    bytes_landed: int = 0,
    cost_per_million_messages_usd: float = DEFAULT_COST_PER_MILLION_MESSAGES_USD,
    cost_per_gb_usd: float = DEFAULT_COST_PER_GB_USD,
    warn_budget_usd: float = DEFAULT_COST_WARN_BUDGET_USD,
    critical_budget_usd: float = DEFAULT_COST_CRITICAL_BUDGET_USD,
) -> CostEstimate:
    """Illustrative cost estimate: `(messages / 1e6) * cost_per_million_messages_usd +
    (bytes_landed / 1e9) * cost_per_gb_usd`, evaluated against a per-run budget - see module
    docstring's "Cost" section for why a budget, not a volume threshold, drives `status` here.
    """
    if messages < 0 or bytes_landed < 0:
        raise ValueError("messages and bytes_landed must be >= 0")
    if cost_per_million_messages_usd < 0 or cost_per_gb_usd < 0:
        raise ValueError("cost rates must be >= 0")
    if critical_budget_usd < warn_budget_usd:
        raise ValueError("critical_budget_usd must be >= warn_budget_usd")

    message_cost_usd = (messages / 1_000_000) * cost_per_million_messages_usd
    storage_cost_usd = (bytes_landed / 1_000_000_000) * cost_per_gb_usd
    estimated_usd = message_cost_usd + storage_cost_usd

    if estimated_usd <= warn_budget_usd:
        status = Status.OK
    elif estimated_usd <= critical_budget_usd:
        status = Status.DEGRADED
    else:
        status = Status.CRITICAL

    return CostEstimate(
        status=status,
        messages=messages,
        bytes_landed=bytes_landed,
        estimated_usd=estimated_usd,
        cost_per_million_messages_usd=cost_per_million_messages_usd,
        cost_per_gb_usd=cost_per_gb_usd,
        warn_budget_usd=warn_budget_usd,
        critical_budget_usd=critical_budget_usd,
    )


def estimate_cost_from_pipeline(
    *,
    capture_result: CaptureResult | None = None,
    buffer_result: BufferResult | None = None,
    avg_bytes_per_message: float = DEFAULT_AVG_BYTES_PER_MESSAGE,
    **kwargs: Any,
) -> CostEstimate:
    """Convenience adapter: build an illustrative `CostEstimate` straight from a Stage 0
    `CaptureResult` and/or an `IngestBuffer` `BufferResult`, instead of computing
    messages/bytes by hand. Read-only use of both dataclasses; neither module's behavior is
    touched. If both are given, `buffer_result.messages_delivered` is used (the ingest-side
    delivered count) rather than summing it with `capture_result.objects_landed`, since in
    production the buffer delivers into Stage 0 capture - the same underlying messages, not
    two independent volumes. `avg_bytes_per_message` is a second, separate illustrative
    assumption (see DEFAULT_AVG_BYTES_PER_MESSAGE) used only because neither result dataclass
    tracks payload bytes today. Extra `kwargs` pass straight through to `estimate_cost()`
    (e.g. to override the per-unit rates or budget thresholds).
    """
    if buffer_result is not None:
        messages = buffer_result.messages_delivered
    elif capture_result is not None:
        messages = capture_result.objects_landed
    else:
        messages = 0

    bytes_landed = round(messages * avg_bytes_per_message)
    return estimate_cost(messages=messages, bytes_landed=bytes_landed, **kwargs)


# ---------------------------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WatchdogReport:
    """One watchdog run's result: an overall Status (the worst of the three sub-checks) plus
    each sub-check's own result, for a real alerting integration to consume later (not built
    here - see module docstring)."""

    status: Status
    checked_at: dt.datetime
    freshness: FreshnessResult
    completeness: CompletenessResult
    cost: CostEstimate

    def summary_lines(self) -> tuple[str, ...]:
        """One line per metric plus an overall line - what `_cli()` prints. Plain text for a
        cron job's stdout/log line; not itself an alerting integration."""
        return (
            f"overall={self.status.value} checked_at={self.checked_at.isoformat()}",
            (
                f"freshness={self.freshness.status.value} "
                f"staleness_s={self.freshness.staleness_s} "
                f"objects_seen={self.freshness.objects_seen}"
            ),
            (
                f"completeness={self.completeness.status.value} "
                f"ratio={self.completeness.completeness_ratio:.3f} "
                f"buckets_seen={self.completeness.buckets_seen} "
                f"missing_total={self.completeness.missing_total}"
            ),
            (
                f"cost={self.cost.status.value} "
                f"estimated_usd={self.cost.estimated_usd:.2f} "
                f"({self.cost.note})"
            ),
        )


def run_watchdog(
    *,
    landed_timestamps: Iterable[dt.datetime],
    completeness_rows: Iterable[Mapping[str, Any]],
    cost_messages: int = 0,
    cost_bytes: int = 0,
    now: dt.datetime | None = None,
    freshness_warn_after_s: float = DEFAULT_FRESHNESS_WARN_AFTER_S,
    freshness_critical_after_s: float = DEFAULT_FRESHNESS_CRITICAL_AFTER_S,
    expected_interval_s: float = DEFAULT_EXPECTED_INTERVAL_S,
    on_time_threshold_s: float = DEFAULT_ON_TIME_THRESHOLD_S,
    completeness_warn_ratio: float = DEFAULT_COMPLETENESS_WARN_RATIO,
    completeness_critical_ratio: float = DEFAULT_COMPLETENESS_CRITICAL_RATIO,
    cost_per_million_messages_usd: float = DEFAULT_COST_PER_MILLION_MESSAGES_USD,
    cost_per_gb_usd: float = DEFAULT_COST_PER_GB_USD,
    cost_warn_budget_usd: float = DEFAULT_COST_WARN_BUDGET_USD,
    cost_critical_budget_usd: float = DEFAULT_COST_CRITICAL_BUDGET_USD,
) -> WatchdogReport:
    """Run all three checks and combine them into one WatchdogReport.

    `completeness_rows` is intentionally a required argument, not defaulted to an empty
    iterable: passing rows is a deliberate choice by the caller, since an empty iterable
    reports completeness as CRITICAL (see compute_completeness()) - a silent default here
    would make every caller who forgot to wire up completeness rows get a misleading
    always-critical report rather than an explicit reminder to supply them.
    """
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)

    freshness = compute_freshness(
        landed_timestamps,
        now=now,
        warn_after_s=freshness_warn_after_s,
        critical_after_s=freshness_critical_after_s,
    )
    completeness = compute_completeness(
        completeness_rows,
        expected_interval_s=expected_interval_s,
        on_time_threshold_s=on_time_threshold_s,
        warn_ratio=completeness_warn_ratio,
        critical_ratio=completeness_critical_ratio,
    )
    cost = estimate_cost(
        messages=cost_messages,
        bytes_landed=cost_bytes,
        cost_per_million_messages_usd=cost_per_million_messages_usd,
        cost_per_gb_usd=cost_per_gb_usd,
        warn_budget_usd=cost_warn_budget_usd,
        critical_budget_usd=cost_critical_budget_usd,
    )

    overall_status = _worst((freshness.status, completeness.status, cost.status))
    return WatchdogReport(
        status=overall_status,
        checked_at=now,
        freshness=freshness,
        completeness=completeness,
        cost=cost,
    )


def _cli() -> None:
    """Standalone entry point - plain argparse, no Dagster import anywhere in this module.

    Invocable by cron, a systemd timer or any other process genuinely independent of the
    orchestrator - see module docstring's "outside the orchestrator" section for why that
    independence is the point. Exits nonzero (1) when the overall status isn't OK, so a cron
    wrapper's own failure/alerting (mail on nonzero exit, a monitoring check on the job itself,
    etc.) has something to key off - still not itself an alerting integration (out of scope).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Stage 0 landing bucket")
    parser.add_argument("--endpoint-url", default=None, help="S3 endpoint override (LocalStack)")
    parser.add_argument("--prefix", default="raw/", help="Landing key prefix to scan")
    parser.add_argument(
        "--completeness-rows-json",
        default=None,
        help=(
            "Path to a JSON file holding a list of {device_id, device_ts_ms, arrival_ts_ms} "
            "rows. If omitted, completeness is evaluated against zero rows, which reports "
            "CRITICAL - that means 'no completeness data was supplied to this run', not "
            "necessarily a real pipeline gap."
        ),
    )
    parser.add_argument("--cost-messages", type=int, default=0)
    parser.add_argument("--cost-bytes", type=int, default=0)
    args = parser.parse_args()

    landed_timestamps = list_stage0_landing_timestamps(
        args.bucket, prefix=args.prefix, endpoint_url=args.endpoint_url
    )

    completeness_rows: list[dict[str, Any]] = []
    if args.completeness_rows_json:
        with open(args.completeness_rows_json) as rows_file:
            completeness_rows = json.load(rows_file)

    report = run_watchdog(
        landed_timestamps=landed_timestamps,
        completeness_rows=completeness_rows,
        cost_messages=args.cost_messages,
        cost_bytes=args.cost_bytes,
    )

    for line in report.summary_lines():
        print(line)

    if report.status is not Status.OK:
        sys.exit(1)


if __name__ == "__main__":
    _cli()
