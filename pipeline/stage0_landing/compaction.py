"""Stage 0 compaction: consolidate small landing objects into larger per-partition objects.

Ticket: P1-02 ("Stage 0 compaction and retention tiering").

Reconciling "compaction" with CLAUDE.md invariant 1
----------------------------------------------------
Invariant 1 says Stage 0 is "append-only and never rewritten; it is the replay source." A naive
reading of "compaction" - merge many small objects into one and delete the small ones - would
directly violate that invariant, since it deletes objects that already landed. This module does
NOT do that.

What it does instead: for a given (arrival_date, hour) partition, it reads every small object
capture.py already landed under `raw/arrival_date=.../hour=.../` and writes ONE ADDITIONAL,
NEW, consolidated object representing all of them, under a *separate* top-level prefix -
`compacted/arrival_date=.../hour=.../messages.ndjson` - never touching, overwriting, or
deleting anything under `raw/`. The separate prefix isn't cosmetic: it's what makes the
"reconciling" checks below trivially correct on a second run - `raw/` always contains exactly
the small originals capture.py wrote, and `compacted/` always contains exactly the objects this
module owns, so listing one prefix never picks up the other's output. This mirrors the same
boundary-by-construction reasoning docs/decisions/0001 uses for the landing/warehouse bucket
split (a prefix boundary, not a convention someone could get wrong).

Because the consolidated object is new and owned by this job (not a Stage 0 object capture.py
ever wrote), overwriting *it* on a re-run is fine and is what makes a compaction run idempotent
- unlike capture.py's `_object_exists` check (which protects objects this job did NOT write),
compact_partition() always recomputes and rewrites its own consolidated object from the current
set of small originals, deterministically (sorted by message_id, JSON keys sorted), so re-
running it against an unchanged partition produces byte-identical output.

What this deliberately does NOT do: delete or supersede the small originals under `raw/` -
that's a real "vacuum" step (the operation that would actually realize compaction's storage-
cost benefit), and it's out of scope for this ticket on purpose. Deleting Stage 0 objects, even
ones a compacted object faithfully supersedes, is exactly the kind of "anything touching prod
data" CLAUDE.md marks human-owned - a vacuum step needs its own ticket, its own review, and
almost certainly a waiting/verification period (e.g. "only vacuum a partition once its
compacted object has reconciled AND survived N days") that is a policy decision, not something
to bake into a batch job's default behavior. What this module gives that future step is exactly
the proof it would need: compact_partition()'s result object (and reconcile() below) show that
the consolidated object contains every message the small originals held, exactly once, before
any deletion could be considered safe.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

import boto3

# Small Stage 0 objects live here (capture.py's arrival_key layout, docs/decisions/0001).
RAW_PREFIX = "raw"
# Consolidated objects this module owns live under a separate top-level prefix, on purpose -
# see the module docstring. Never `raw/...`, so it can never be mistaken for (or collide with)
# a Stage 0 object capture.py wrote, and a partition listing under RAW_PREFIX never includes it.
COMPACTED_PREFIX = "compacted"


class CompactionError(Exception):
    """Base class for compaction-time errors."""


class CompactionReconciliationError(CompactionError):
    """Raised when the consolidated object doesn't faithfully represent the small originals -
    every message seen in the small objects must appear exactly once in the consolidated
    object. This is the automated check behind P1-02's "nothing lost" requirement."""


@dataclasses.dataclass(frozen=True)
class CompactionResult:
    """Summary of one compaction run over a single (arrival_date, hour) partition."""

    arrival_date: str
    hour: str
    small_object_keys: tuple[str, ...]
    consolidated_key: str | None
    consolidated_written: bool
    bytes_before: int
    bytes_after: int
    messages_in_consolidated: int

    @property
    def messages_seen(self) -> int:
        """Number of small Stage 0 objects read from `raw/` for this partition. Each small
        object is exactly one message (capture.py's grain), so this doubles as the message
        count the consolidated object must reproduce."""
        return len(self.small_object_keys)

    def reconciles(self) -> bool:
        """True iff every message found in the small originals is represented exactly once in
        the consolidated object - including the zero-object case, where an empty partition
        trivially reconciles (0 seen, 0 in the consolidated body, nothing written)."""
        return self.messages_in_consolidated == self.messages_seen


def partition_prefix(arrival_date: str, hour: str) -> str:
    """The `raw/` prefix under which capture.py lands small objects for one partition."""
    return f"{RAW_PREFIX}/arrival_date={arrival_date}/hour={hour}/"


def compacted_key(arrival_date: str, hour: str) -> str:
    """The single consolidated object key this module owns for one partition. Deterministic
    (no random/timestamp component) so re-running compaction targets the same object rather
    than accumulating a new one per run."""
    return f"{COMPACTED_PREFIX}/arrival_date={arrival_date}/hour={hour}/messages.ndjson"


def _list_small_objects(client, bucket: str, arrival_date: str, hour: str) -> list[dict[str, Any]]:
    """List every small Stage 0 object for one partition, with size, via list_objects_v2's own
    `Size` field - no need for a per-key head_object just to total bytes_before."""
    prefix = partition_prefix(arrival_date, hour)
    objects: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    return objects


def _read_message(client, bucket: str, key: str) -> dict[str, Any]:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def compact_partition(
    *,
    bucket: str,
    arrival_date: str,
    hour: str,
    s3_client: Any = None,
    endpoint_url: str | None = None,
) -> CompactionResult:
    """Consolidate one (arrival_date, hour) partition's small Stage 0 objects into a single
    newline-delimited-JSON object, without touching the small originals (see module docstring
    for how this keeps invariant 1 intact).

    Idempotent: re-running this against an unchanged partition reads the same small objects and
    writes byte-identical content to the same consolidated key - safe to run any number of
    times, and safe to run concurrently with capture.py landing more messages into a *different*
    partition (this only ever reads/writes within the one (arrival_date, hour) it's given).

    Zero-object partitions: an hour with no small objects yet (or a partition that never got
    any traffic) compacts to a documented no-op - consolidated_key is still reported for
    reference, but consolidated_written is False and nothing is written to the bucket. Writing
    an empty placeholder object for every never-populated hour would create unbounded busywork
    (and clutter) for partitions that may never see data; a partition with real data always
    produces a real consolidated object, so "no consolidated object exists" and "this partition
    never had traffic" coincide by construction. This is exercised explicitly in the test suite,
    not left implicit.
    """
    client = s3_client or boto3.client("s3", endpoint_url=endpoint_url)

    small_objects = _list_small_objects(client, bucket, arrival_date, hour)
    small_object_keys = tuple(sorted(obj["Key"] for obj in small_objects))
    bytes_before = sum(obj["Size"] for obj in small_objects)
    target_key = compacted_key(arrival_date, hour)

    if not small_object_keys:
        return CompactionResult(
            arrival_date=arrival_date,
            hour=hour,
            small_object_keys=(),
            consolidated_key=target_key,
            consolidated_written=False,
            bytes_before=0,
            bytes_after=0,
            messages_in_consolidated=0,
        )

    messages_by_id: dict[str, dict[str, Any]] = {}
    for key in small_object_keys:
        message = _read_message(client, bucket, key)
        message_id = message.get("message_id", key)
        # capture.py keys every small object by message_id (arrival_key), so two distinct keys
        # colliding on message_id would mean a landing-layer bug, not a compaction one - fail
        # loudly rather than silently dropping one copy.
        if message_id in messages_by_id:
            raise CompactionReconciliationError(
                f"message_id {message_id!r} appears under more than one Stage 0 object in "
                f"partition arrival_date={arrival_date} hour={hour} - refusing to compact a "
                "partition where the small objects themselves aren't uniquely keyed"
            )
        messages_by_id[message_id] = message

    # Deterministic ordering/serialization (sorted message_id, sorted JSON keys) is what makes
    # re-running this against an unchanged partition produce byte-identical output.
    body_lines = [
        json.dumps(messages_by_id[message_id], sort_keys=True)
        for message_id in sorted(messages_by_id)
    ]
    body = ("\n".join(body_lines) + "\n").encode("utf-8")

    client.put_object(
        Bucket=bucket,
        Key=target_key,
        Body=body,
        ContentType="application/x-ndjson",
    )

    # Read the just-written object back rather than trusting the put_object call succeeded -
    # this is the actual proof (not an assumption) that the consolidated object holds every
    # message the small originals did, exactly once.
    written_body = client.get_object(Bucket=bucket, Key=target_key)["Body"].read()
    written_messages = [json.loads(line) for line in written_body.decode("utf-8").splitlines()]
    written_ids = {m.get("message_id") for m in written_messages}
    expected_ids = set(messages_by_id)
    if written_ids != expected_ids or len(written_messages) != len(messages_by_id):
        raise CompactionReconciliationError(
            f"consolidated object {target_key!r} does not faithfully represent partition "
            f"arrival_date={arrival_date} hour={hour}: expected {len(messages_by_id)} messages "
            f"({sorted(expected_ids)}), found {len(written_messages)} "
            f"({sorted(written_ids)})"
        )

    return CompactionResult(
        arrival_date=arrival_date,
        hour=hour,
        small_object_keys=small_object_keys,
        consolidated_key=target_key,
        consolidated_written=True,
        bytes_before=bytes_before,
        bytes_after=len(written_body),
        messages_in_consolidated=len(written_messages),
    )


def reconcile(result: CompactionResult) -> None:
    """Raise CompactionReconciliationError unless the consolidated object accounts for every
    message the small originals held, exactly once (trivially true for a zero-object
    partition)."""
    if not result.reconciles():
        raise CompactionReconciliationError(
            f"partition arrival_date={result.arrival_date} hour={result.hour}: "
            f"{result.messages_seen} small objects seen but consolidated object holds "
            f"{result.messages_in_consolidated} messages - compaction must account for every "
            "Stage 0 message exactly once"
        )


def _cli() -> None:
    """Run compaction for one (arrival_date, hour) partition against a landing bucket."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Landing bucket name, e.g. telemetry-dev-landing")
    parser.add_argument("--arrival-date", required=True, help="YYYY-MM-DD, matches capture.py's arrival_key")
    parser.add_argument("--hour", required=True, help="HH (zero-padded), matches capture.py's arrival_key")
    parser.add_argument("--endpoint-url", default=None, help="S3 endpoint override (LocalStack/Floci)")
    args = parser.parse_args()

    result = compact_partition(
        bucket=args.bucket,
        arrival_date=args.arrival_date,
        hour=args.hour,
        endpoint_url=args.endpoint_url,
    )

    print(
        f"messages_seen={result.messages_seen} consolidated_written={result.consolidated_written} "
        f"bytes_before={result.bytes_before} bytes_after={result.bytes_after} "
        f"consolidated_key={result.consolidated_key}"
    )
    try:
        reconcile(result)
    except CompactionReconciliationError as exc:
        print(f"RECONCILIATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Reconciled: consolidated object accounts for every message exactly once.")


if __name__ == "__main__":
    _cli()
