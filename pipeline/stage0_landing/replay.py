"""Stage 0 replay: read landed message envelopes back out of the landing bucket.

Ticket: P1-14 ("Replay-from-Stage-0 determinism test"). CLAUDE.md invariant 7: "Replaying a
closed window from Stage 0 must reproduce production output exactly." Proving that requires
actually reading a closed window's worth of objects back out of Stage 0 - something no module
in this repo does yet. pipeline/stage0_landing/capture.py only WRITES (see its own module
docstring); this module is the minimal read-back counterpart, added alongside it rather than
into it, per this ticket's scope.

What this is NOT
------------------
- Not a general Stage 0 query/browsing tool. It inverts exactly what capture.py's
  arrival_key() wrote - list everything under one arrival_date's hour prefixes, GET each
  object, decode the JSON back into the envelope dict capture_messages() serialized - and
  nothing more. A caller that wants to query Stage 0 by device, by message_id, or across an
  unbounded date range should build that separately; this exists only so a replay test (or a
  future replay job) can pull back "everything that landed for this window."
- Not a change to capture.py. capture.py's public functions and behavior are untouched;
  arrival_key()'s layout (raw/arrival_date=YYYY-MM-DD/hour=HH/<message_id>.json) is simply
  read here as data, via the same object-key convention, not re-derived independently.

Round-trip guarantee
----------------------
capture_messages() writes `json.dumps(message, sort_keys=True).encode("utf-8")` per object
(see capture.py). `read_landed_messages` reads each object back with `json.loads`, which is a
lossless inverse for any JSON-serializable envelope - including one whose `readings` list has
more than one entry; JSON round-trips a list of dicts exactly like any other JSON value, no
special-casing needed here for multi-reading envelopes.

Ordering
---------
S3's `list_objects_v2` returns keys in lexical (UTF-8 byte) order, which for this layout is
effectively message_id order within an hour prefix - not arrival order, not insertion order,
and not necessarily the order `capture_messages()` was originally called with. Callers that
need to compare replayed messages against another representation of the same batch (e.g. an
in-memory list from a fixture generator) must therefore compare by identity/key (message_id,
or whatever key the comparison is over), never by list position. See
pipeline/replay_determinism.py, which does exactly that for Stage 1/Stage 2 output.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def read_landed_messages(
    bucket: str,
    s3_client: Any,
    *,
    arrival_date: str,
    hours: Iterable[int],
) -> list[dict[str, Any]]:
    """Read back every Stage 0 message envelope landed under one arrival_date, across `hours`.

    For each hour in `hours`, lists every object under
    `raw/arrival_date=<arrival_date>/hour=<HH>/` (zero-padded to two digits, matching
    arrival_key()'s `{arrival:%H}` formatting) and GETs + `json.loads`s it back into a message
    envelope dict.

    `arrival_date` is a single "YYYY-MM-DD" string. A window whose messages span more than one
    calendar arrival date (e.g. late arrivals crossing midnight) is read by calling this once
    per distinct arrival_date and concatenating the results - this function deliberately
    doesn't do that multi-date fan-out itself, to keep its own contract to exactly "one
    arrival_date's worth of hour prefixes" (see module docstring's scope note).

    Uses a paginator so an hour prefix with more than 1000 objects (list_objects_v2's page
    size) is still read completely, not silently truncated to the first page.
    """
    messages: list[dict[str, Any]] = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for hour in hours:
        prefix = f"raw/arrival_date={arrival_date}/hour={int(hour):02d}/"
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                body = s3_client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                messages.append(json.loads(body))

    return messages
