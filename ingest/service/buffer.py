"""Production ingest buffer: accept-and-spool for incoming device message envelopes.

Ticket: P1-01 ("Production ingest buffer with accept-and-spool").

What this is
------------
The live accept path in front of Stage 0 capture (pipeline/stage0_landing/capture.py).
capture.py's own docstring says it plainly: "P1-01 is what will eventually call something like
this (or replace it) from a live accept-and-spool path." This module is that path: it is the
thing a device message actually hits first. It does two jobs, per ingest/service/README.md:

1. Validate JUST ENOUGH to route - not full business-logic validation (that is parsing's job,
   starting at Stage 1; see parsers/framework.py). "Enough to route" here means: the envelope
   has the fields capture.py needs to key and land it (`message_id`, `arrival_ts_ms`), and its
   `device_class` is one capture.py is scoped to accept (SUPPORTED_DEVICE_CLASSES, imported
   from capture.py rather than redefined here - one source of truth). A message that fails this
   check is rejected immediately and reported as rejected; it is never spooled, because
   spooling it would just mean retrying something that can never succeed (CLAUDE.md invariant 7
   needs Stage 0 to be a *complete* replay source, not a place unsupported garbage circulates
   forever).
2. Hand off validated messages to a downstream sink (modeled here as any
   `Callable[[Sequence[Mapping]], None]` - see `Sink` below; `capture_sink()` builds one backed
   by capture.py's `capture_messages()`). A successful handoff means the message is done. A
   FAILED handoff - the sink raising, modeling a downstream outage or backpressure - must never
   drop the message. It is spooled (held in an in-process, in-memory queue keyed by nothing
   more than arrival order) and retried later via `retry_spooled()` / `drain()`. That is the
   entire point of "accept-and-spool": a downstream hiccup degrades to added latency, never to
   silent loss.

What this is NOT
-----------------
- Not a message broker. `IngestBuffer`'s spool is a plain in-process `collections.deque` - fine
  for proving the accept-and-spool *logic* is correct and lossless (this ticket's job), not a
  production-grade durable queue. A real deployment would back this with something that
  survives a process restart (Kafka, SQS, a WAL on local disk, ...); picking which is an
  infrastructure decision this ticket deliberately does not make (see ingest/README.md: the
  ingest service and the buffer/queue config in ingest/buffer/ are sized "from the load test in
  P1-01 and the capacity plan in P3-01" - P3-01, capacity planning/autoscaling, is explicitly a
  later, separate ticket). If this process dies with messages still in `_spool`, those messages
  are lost - a real deployment closes that gap with a durable backing store, not with anything
  added here.
- Not full envelope/business validation. A `readings` list with corrupt timestamps, an
  unrecognized firmware version, a garbled payload_hash - all pass through untouched. That is
  parsing's job (parsers/framework.py, starting at Stage 1), not this service's.
- Not a change to pipeline/stage0_landing/capture.py. `IngestBuffer` calls it (through
  `capture_sink()`) exactly as-is; nothing about capture.py's behavior changes here.
- Not a real 10x-current-Supercharger-rate load test - see "The honest gap" below.

The honest gap: this ticket's literal "done when" vs. what is actually provable here
--------------------------------------------------------------------------------------
The backlog's "done when" for P1-01 is "Load test at 10x current Supercharger rate with zero
loss." That cannot be honestly satisfied as literally written, for two concrete reasons, the
same shape of gap this repo already documents plainly for other human/infra-gated tickets (see
Build backlog.md's notes on P0-01, P0-10 and P0-12, and pipeline/stage0_landing/capture.py's own
"one region" scoping note):

1. Nobody knows "current Supercharger rate". That number requires P0-01 ("Audit existing
   telemetry backend and data flows"), which needs live backend access and stakeholder
   interviews - still "To do" in Build backlog.md, explicitly human-owned. There is no real
   baseline to multiply by 10x; inventing one here would be a fabricated number dressed up as a
   measurement.
2. A genuine load test needs infrastructure sized for it - at minimum a LocalStack/Floci
   environment standing in for the real landing bucket at production-like scale, which depends
   on P0-02 (Terraform baseline), also still "To do" per the ticket brief and a stale, untouched
   branch as of this work.

So this module does NOT claim a 10x-production-rate load test, because there is no production
rate on record to be 10x of. What it DOES prove, honestly labeled as synthetic throughout
(tests/unit/test_ingest_buffer.py):

- `devices_per_firmware=10` (2.5x the fixture generator's own illustrative default of 4)
  across its 5 firmware/device-class combinations = 50 distinct synthetic devices, which the
  generator's own batching, duplicate and late-arrival injection turns into 6,557 message
  envelopes - delivered end-to-end through `IngestBuffer` -> `capture_sink()` -> a moto-mocked
  S3 bucket, with zero loss, when the sink never fails.
- The same scale, but with a sink that fails for a stretch (modeling a downstream outage) and
  then recovers, and separately with a sink that fails intermittently at random: every accepted
  message is still accounted for at every point checked - either delivered or sitting in the
  spool, never dropped - and all eventually deliver once the sink recovers.
- An unsupported-`device_class` message is rejected at accept time, not spooled.
- A standing reconciliation invariant, checked at multiple points in a run, including mid-outage:
  `messages_accepted == messages_delivered + messages_spooled_pending`, always.

6,557 synthetic messages is "comfortably above the generator's default illustrative scale" (its
own default run produces roughly 2,600 messages) and large enough to exercise real batching
(multiple ~200-message chunks per submit() call), spooling and multi-round retry behavior - not
a claim that this is any particular multiple of a real production rate, because no real
production rate is on record (see above). Whether the right number to actually protect against
in production is 6,557, 65,000 or 6.5 million messages per run is exactly the question P0-01's
audit and P3-01's capacity plan exist to answer with real numbers; this ticket proves the
accept-and-spool mechanism is correct and lossless at a synthetic scale large enough to exercise
its real mechanics, not that it is sized correctly for production load.
"""
from __future__ import annotations

import collections
import dataclasses
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from ingest.buffer.config import BufferConfig
from pipeline.stage0_landing.capture import SUPPORTED_DEVICE_CLASSES, capture_messages

# Fields a message envelope must carry for this service to route it at all. Not full
# validation - see module docstring. `message_id` and `arrival_ts_ms` are exactly what
# capture.py's arrival_key()/capture_messages() need to key and land a message; `device_class`
# is what determines whether it is routable at all; `readings` is what makes an envelope worth
# landing in the first place (an envelope with no readings is a malformed producer, not a
# message downstream has any use for).
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "message_id",
    "device_id",
    "device_class",
    "arrival_ts_ms",
    "readings",
)

# A sink is anything that attempts to hand a batch of validated envelopes to whatever is
# downstream (Stage 0 capture, in production; a moto-mocked capture_messages() call in tests).
# It must raise on a failed/partial handoff and must not raise on success - IngestBuffer treats
# "no exception" as "every message in this batch is durably delivered" and "raised" as "none of
# them are, spool the whole batch". A sink that could partially succeed then raise would need a
# richer contract than this ticket's scope requires; capture_sink() below satisfies this
# contract because capture_messages() is itself all-or-nothing per call (it raises immediately
# on the first unsupported device_class or backing-store error, before returning a result).
Sink = Callable[[Sequence[Mapping[str, Any]]], None]


def _validate_envelope(message: Mapping[str, Any]) -> str | None:
    """Return None if `message` is well-formed enough to route, else a rejection reason.

    Deliberately NOT full business validation (see module docstring) - just what this service
    and capture.py need to key, route and land the message.
    """
    missing = [field for field in REQUIRED_ENVELOPE_FIELDS if field not in message]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"

    device_class = message["device_class"]
    if device_class not in SUPPORTED_DEVICE_CLASSES:
        return (
            f"unsupported device_class {device_class!r} "
            f"(not one of {sorted(SUPPORTED_DEVICE_CLASSES)})"
        )

    readings = message["readings"]
    if not isinstance(readings, list) or not readings:
        return "readings must be a non-empty list"

    return None


def capture_sink(*, bucket: str, s3_client: Any = None, endpoint_url: str | None = None) -> Sink:
    """Build a Sink that hands a batch off to Stage 0 capture (capture_messages()).

    This is the production integration point: capture.py's own docstring names P1-01 as "what
    will eventually call something like this (or replace it) from a live accept-and-spool
    path" - this is that call. Every message passed here has already cleared
    `_validate_envelope`, so the only way `capture_messages` raises is a genuine backing-store
    failure (or, in tests, a deliberately injected one) - exactly the case IngestBuffer needs to
    treat as "spool and retry".
    """

    def _sink(batch: Sequence[Mapping[str, Any]]) -> None:
        capture_messages(batch, bucket=bucket, s3_client=s3_client, endpoint_url=endpoint_url)

    return _sink


@dataclasses.dataclass
class _SpooledMessage:
    envelope: Mapping[str, Any]
    attempts: int


@dataclasses.dataclass(frozen=True)
class BufferResult:
    """Point-in-time summary of an IngestBuffer's state, used to log and to reconcile.

    Mirrors the CaptureResult/MergeResult pattern already used in pipeline/ - a plain,
    frozen snapshot plus a reconciles() check that is the automated "done when" this ticket
    actually proves (see module docstring's honest-gap section).
    """

    messages_accepted: int
    messages_rejected: int
    messages_delivered: int
    messages_spooled_pending: int
    rejected: tuple[tuple[Any, str], ...]

    def reconciles(self) -> bool:
        """True iff every accepted message is currently delivered or spooled - never both,
        never neither. This is the "no message silently dropped" check: it can be called at
        ANY point during or after a run, not just once at the end, because IngestBuffer never
        removes a message from the spool except by successfully delivering it."""
        return self.messages_accepted == self.messages_delivered + self.messages_spooled_pending


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class IngestBuffer:
    """Accept-and-spool front door for device message envelopes.

    `submit()` validates and attempts immediate delivery. A message that fails delivery (the
    sink raises) is moved to an internal spool rather than dropped; `retry_spooled()` / `drain()`
    attempt redelivery of whatever is currently spooled, for as long as it takes the downstream
    sink to recover. See the module docstring for what "spool" does and does not mean here
    (in-process only, not a durable broker).
    """

    def __init__(self, sink: Sink, *, config: BufferConfig | None = None) -> None:
        config = config or BufferConfig()
        if config.batch_size < 1:
            raise ValueError("config.batch_size must be >= 1")
        self._sink = sink
        self._batch_size = config.batch_size
        self._spool: collections.deque[_SpooledMessage] = collections.deque()
        self._messages_accepted = 0
        self._messages_delivered = 0
        self._rejected: list[tuple[Any, str]] = []

    def submit(self, messages: Iterable[Mapping[str, Any]]) -> None:
        """Accept, validate and attempt immediate delivery of `messages`.

        Messages are chunked into batches of `batch_size` for delivery (matching
        capture_messages()'s own per-call batching), so one bad batch's failure never blocks
        validation or delivery of unrelated batches. A message that fails validation is
        rejected immediately and never touches the spool - see module docstring.
        """
        accepted_batch: list[Mapping[str, Any]] = []
        for message in messages:
            reason = _validate_envelope(message)
            if reason is not None:
                self._rejected.append((message.get("message_id"), reason))
                continue
            self._messages_accepted += 1
            accepted_batch.append(message)
            if len(accepted_batch) >= self._batch_size:
                self._attempt_delivery(accepted_batch)
                accepted_batch = []
        if accepted_batch:
            self._attempt_delivery(accepted_batch)

    def _attempt_delivery(self, batch: Sequence[Mapping[str, Any]], *, attempts: int = 1) -> None:
        try:
            self._sink(batch)
        except Exception:  # noqa: BLE001 - any sink failure must degrade to spool-and-retry,
            # never a lost message or a crashed accept path (see module docstring).
            for message in batch:
                self._spool.append(_SpooledMessage(envelope=message, attempts=attempts))
            return
        self._messages_delivered += len(batch)

    def retry_spooled(self) -> int:
        """Attempt one redelivery pass over everything currently spooled.

        Whatever is in the spool at the moment this is called is drained and re-batched; a
        batch that still fails goes right back into the spool (attempts incremented) rather
        than being lost. Returns how many messages were delivered by this pass.
        """
        pending = list(self._spool)
        self._spool.clear()
        delivered_before = self._messages_delivered
        for batch_of_spooled in _chunks(pending, self._batch_size):
            envelopes = [sm.envelope for sm in batch_of_spooled]
            next_attempt = max(sm.attempts for sm in batch_of_spooled) + 1
            self._attempt_delivery(envelopes, attempts=next_attempt)
        return self._messages_delivered - delivered_before

    def drain(self, *, max_rounds: int = 1000) -> int:
        """Call retry_spooled() repeatedly until the spool is empty or max_rounds is hit.

        Convenience for tests and for a background worker driving the spool down once a
        downstream outage clears - not a substitute for retry_spooled() being called on
        whatever cadence/backoff a real deployment chooses (out of this ticket's scope; see
        module docstring's "not a message broker" note).
        """
        rounds = 0
        total_delivered = 0
        while self._spool and rounds < max_rounds:
            total_delivered += self.retry_spooled()
            rounds += 1
        return total_delivered

    def result(self) -> BufferResult:
        """A point-in-time snapshot, safe to call at any point during or after a run."""
        return BufferResult(
            messages_accepted=self._messages_accepted,
            messages_rejected=len(self._rejected),
            messages_delivered=self._messages_delivered,
            messages_spooled_pending=len(self._spool),
            rejected=tuple(self._rejected),
        )

    @property
    def spooled_pending(self) -> int:
        return len(self._spool)
