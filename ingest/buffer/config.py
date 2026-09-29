"""Buffer/queue configuration for the accept-and-spool ingest path (P1-01).

What this is
------------
ingest/README.md describes `ingest/buffer/` as "buffer/queue configuration (topics,
partitions, retention) shared by the ingest service and Stage 0 writers." This module is that
configuration surface, scoped to what `ingest.service.buffer.IngestBuffer` actually uses today:
how many messages it batches into one downstream handoff attempt.

What this is NOT
-----------------
There is no real message broker behind this yet (see ingest/service/buffer.py's module
docstring - the spool is an in-process deque, not Kafka/SQS/anything durable), so there are no
real topics, partitions or a retention policy to configure. Modeling those here now would be
inventing infrastructure that does not exist rather than configuring infrastructure that does.
That is genuinely later work: ingest/README.md itself says this directory gets "sized from the
load test in P1-01 and the capacity plan in P3-01" - P3-01 (capacity planning/autoscaling) is a
separate, later, real-infra-sizing ticket, explicitly out of this ticket's scope. `BufferConfig`
below holds only the one setting the current in-process implementation genuinely has; extending
it with broker-shaped fields (topic names, partition counts, retention windows) belongs to
whichever ticket actually picks the broker technology.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class BufferConfig:
    """Tunable settings for IngestBuffer.

    batch_size: how many validated messages IngestBuffer groups into one downstream delivery
    attempt (one capture_messages() call, in production). Larger batches amortize per-call
    overhead but mean a single downstream failure spools more messages at once; smaller batches
    isolate failures more finely at the cost of more downstream calls. 200 is a starting point
    consistent with the synthetic load test in tests/unit/test_ingest_buffer.py, not a measured
    production value - see ingest/service/buffer.py's module docstring for why no measured
    production value exists yet (P0-01, P0-02 both still "To do").
    """

    batch_size: int = 200
