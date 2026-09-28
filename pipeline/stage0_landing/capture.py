"""Stage 0 capture: land Supercharger message envelopes into the landing bucket.

Ticket: P0-05 ("Stage 0 capture for one Supercharger region").

Scope note: the fixture generator (tests/fixtures/generators/supercharger.py) doesn't model
multiple geographic regions - it produces one pool of Supercharger stall/cabinet devices,
tagged only by `site_id`. Treating one full generator run as "one region" is the deliberate
scoping decision for this ticket: production capture (P1-01's ingest service) is what will
actually gate by region for blast-radius reasons; this ticket's job is only to prove that
messages land correctly, immutably, and completely under the P0-03 layout, using whatever
stand-in for "one region" is available before real Supercharger telemetry access exists.

What this is NOT: the production ingest service (P1-01, still a placeholder - see
ingest/service/README.md). This module is a batch capture job: given an iterable of already-
received message envelopes (real, once captured, or synthetic via the fixture generator in the
meantime), it lands each one as a single object in the landing bucket, keyed by ARRIVAL hour
per docs/decisions/0001 and CLAUDE.md invariant 4 (never partition by device_id). P1-01 is what
will eventually call something like this (or replace it) from a live accept-and-spool path.

Invariant 1 (Stage 0 is append-only and never rewritten) is enforced here at the object level:
before writing, capture checks whether a message's key already exists and skips it rather than
overwriting - re-running capture over the same messages (e.g. after a partial failure partway
through a batch) must never mutate an object that already landed.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from collections.abc import Iterable
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Device classes this capture job accepts. Stage 0 landing is shared across all device classes
# eventually, but P0-05 is scoped to Supercharger only (see module docstring) - a message from
# any other class is a bug in the caller, not something to silently accept.
SUPPORTED_DEVICE_CLASSES = frozenset({"supercharger_stall", "supercharger_cabinet"})


class CaptureError(Exception):
    """Base class for capture-time errors."""


class UnsupportedDeviceClassError(CaptureError):
    """Raised when a message's device_class isn't one P0-05 is scoped to capture."""


class ReconciliationError(CaptureError):
    """Raised when the number of messages seen doesn't match the number landed.

    This is the automated check behind P0-05's "done when": counts reconcile with source.
    """


@dataclasses.dataclass(frozen=True)
class CaptureResult:
    """Summary of one capture run, used both to log and to reconcile."""

    messages_seen: int
    objects_written: int
    objects_already_present: int
    keys_written: tuple[str, ...]

    @property
    def objects_landed(self) -> int:
        return self.objects_written + self.objects_already_present

    def reconciles(self) -> bool:
        """True iff every message seen resulted in exactly one landed object (new or already
        present from a prior run) - the "counts reconcile with source" check."""
        return self.objects_landed == self.messages_seen


def arrival_key(message: dict[str, Any]) -> str:
    """The Stage 0 object key for one message envelope.

    Layout per docs/decisions/0001-object-store-layout.md:
        raw/arrival_date=YYYY-MM-DD/hour=HH/<message_id>.json

    Partitioned by the envelope's `arrival_ts_ms` (ingest-side wall-clock receipt time), never
    by `device_ts_ms` or `device_id` (CLAUDE.md invariant 4). `message_id` is already unique
    per physical message the generator emits - including the synthetic "-retry" suffix on
    injected duplicates - so it doubles as the object key's uniqueness guarantee: two arrivals
    of the "same" reading (a real device retransmitting) land as two distinct Stage 0 objects,
    exactly as Stage 0's "one row per message as received" grain requires. Deduplication on
    (device_id, device_ts, payload_hash) happens at Stage 1 (invariant 2), not here.
    """
    arrival = dt.datetime.fromtimestamp(message["arrival_ts_ms"] / 1000, tz=dt.timezone.utc)
    return f"raw/arrival_date={arrival:%Y-%m-%d}/hour={arrival:%H}/{message['message_id']}.json"


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def capture_messages(
    messages: Iterable[dict[str, Any]],
    *,
    bucket: str,
    s3_client: Any = None,
    endpoint_url: str | None = None,
) -> CaptureResult:
    """Land each message envelope as one immutable object in the landing bucket.

    Raises UnsupportedDeviceClassError immediately on a message outside
    SUPPORTED_DEVICE_CLASSES, before writing anything for that message - a capture job that
    silently dropped or mis-shelved an unexpected class would defeat the point of Stage 0 being
    a complete, reconcilable replay source (invariant 7).
    """
    client = s3_client or boto3.client("s3", endpoint_url=endpoint_url)

    messages_seen = 0
    objects_written = 0
    objects_already_present = 0
    keys_written: list[str] = []

    for message in messages:
        messages_seen += 1
        device_class = message.get("device_class")
        if device_class not in SUPPORTED_DEVICE_CLASSES:
            raise UnsupportedDeviceClassError(
                f"message {message.get('message_id')!r} has device_class {device_class!r}, "
                f"not one of {sorted(SUPPORTED_DEVICE_CLASSES)} (P0-05 is Supercharger-only)"
            )

        key = arrival_key(message)

        if _object_exists(client, bucket, key):
            # Invariant 1: never overwrite a Stage 0 object that already landed.
            objects_already_present += 1
            continue

        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(message, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )
        objects_written += 1
        keys_written.append(key)

    return CaptureResult(
        messages_seen=messages_seen,
        objects_written=objects_written,
        objects_already_present=objects_already_present,
        keys_written=tuple(keys_written),
    )


def reconcile(result: CaptureResult) -> None:
    """Raise ReconciliationError unless every message seen landed exactly once."""
    if not result.reconciles():
        raise ReconciliationError(
            f"{result.messages_seen} messages seen but {result.objects_landed} landed "
            f"({result.objects_written} written, {result.objects_already_present} already "
            "present) - Stage 0 must account for every message exactly once"
        )


def _cli() -> None:
    """Run capture from the synthetic Supercharger fixture generator against a landing bucket.

    This is the "one region" capture run for P0-05: until real Supercharger telemetry access
    exists (still gated on the profiling tickets, P0-06..P0-09), this is what "capture" means
    in dev/staging - see the module docstring's scope note.
    """
    from tests.fixtures.generators.supercharger import GeneratorConfig, generate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Landing bucket name, e.g. telemetry-dev-landing")
    parser.add_argument("--endpoint-url", default=None, help="S3 endpoint override (LocalStack/Floci)")
    parser.add_argument("--seed", type=int, default=GeneratorConfig().seed)
    parser.add_argument("--devices-per-firmware", type=int, default=GeneratorConfig().devices_per_firmware)
    args = parser.parse_args()

    config = GeneratorConfig(seed=args.seed, devices_per_firmware=args.devices_per_firmware)
    fixtures = generate(config)
    messages = list(fixtures.all_messages())

    result = capture_messages(messages, bucket=args.bucket, endpoint_url=args.endpoint_url)

    print(
        f"messages_seen={result.messages_seen} objects_written={result.objects_written} "
        f"objects_already_present={result.objects_already_present}"
    )
    try:
        reconcile(result)
    except ReconciliationError as exc:
        print(f"RECONCILIATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Reconciled: counts match source.")


if __name__ == "__main__":
    _cli()
