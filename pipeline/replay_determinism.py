"""Replay-from-Stage-0 determinism check.

Ticket: P1-14 ("Replay-from-Stage-0 determinism test"). CLAUDE.md invariant 7: "Replaying a
closed window from Stage 0 must reproduce production output exactly."

What "production" and "replay" mean here
-------------------------------------------
There is no separately-persisted "the real production run" artifact anywhere in this repo yet
(no Iceberg tables, no deployed Dagster run history) - so this module defines the proof
invariant 7 asks for directly: take the SAME closed window of messages and push it through the
identical parse -> merge -> canonicalize chain by two different paths, then assert the
outputs are equal.

  - "Production" is messages fed straight into the pipeline in-memory, the way a live run
    would receive them (skipping capture/replay entirely - this is "the pipeline ran once").
  - "Replay" is the same messages, but obtained by reading them back out of Stage 0 after
    landing them there (see pipeline/stage0_landing/replay.read_landed_messages) - this is
    "the pipeline re-derived its input from the append-only Stage 0 record", exactly what a
    real replay-from-Stage-0 job would do after an outage, a bug fix, or a backfill.

`check_replay_determinism` takes both message lists directly (already separated by whatever
got them - see tests/unit/test_replay_determinism.py for the moto-backed capture/read-back
that produces the replay list in practice) and runs each through its own fresh pipeline state
(a new parser dispatch, a new Stage1MergeStore, a new canonicalize_rows call) so there is no
possibility of the two paths accidentally sharing state.

Why comparison is by natural key, not list order
----------------------------------------------------
Stage 0 object listing (S3 `list_objects_v2`) returns keys in lexical order, not the order
messages were originally captured or generated in (see replay.py's module docstring) - so the
replay path's message list, and therefore its Stage 1/Stage 2 row order, is not expected to
match the production path's order even when the two are otherwise identical. Comparing ordered
lists would produce false mismatches from pure reordering, not real defects. Instead, both
Stage 1 rows and Stage 2 canonical rows are compared as dicts keyed by the Stage 1 natural key
(device_id, device_ts_ms, payload_hash) - Stage 1's own merge key (CLAUDE.md invariant 2),
which every Stage 2 canonical row also still carries verbatim as an identity field (see
canonicalize.py's IDENTITY_FIELDS). Two runs "match" here means: same set of natural keys, same
field values per key - regardless of what order either path produced them in.

What this does NOT prove
---------------------------
This is an in-process determinism proof, not an end-to-end one against real infrastructure:
- Stage 1 here is Stage1MergeStore, an in-process dict stand-in (see merge.py's own module
  docstring), not a real Iceberg MERGE INTO. A real Iceberg table's replay would additionally
  need to prove the *write path itself* (partition layout, file compaction, snapshot
  isolation) is replay-safe, which this module cannot exercise.
- Stage 0 here is a moto-mocked S3 bucket (see the test module), not a real object store under
  real lifecycle/versioning policy. Object immutability under concurrent writers, eventual
  consistency edge cases, and real listing pagination at scale are not exercised here.
- This module only carries a message batch through parse -> merge -> canonicalize. Later
  stages (P1-09 clock-offset correction, P1-10 quality flags, P1-11 completeness/lateness,
  Stage 3/4) are out of scope for this ticket and are not part of the comparison.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from parsers.framework import ParseResult, Registry, parse_messages
from pipeline.stage1_parsed.merge import NaturalKey, Stage1MergeStore, natural_key
from pipeline.stage2_canonical.canonicalize import Catalog, canonicalize_rows


@dataclasses.dataclass(frozen=True)
class FieldMismatch:
    """One natural key present on both sides of a comparison, but with differing field values."""

    key: NaturalKey
    production_fields: dict[str, Any]
    replay_fields: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class KeyedComparison:
    """Result of comparing two {natural_key: fields} maps for equality.

    `only_in_production`/`only_in_replay` are natural keys present on one side only - a
    natural-key-level divergence (a row one path produced that the other didn't at all).
    `mismatched` pairs a shared key with both sides' field values when the key was present on
    both sides but the field values differ (a value-level divergence, e.g. a field a real bug
    silently changed between the two paths).
    """

    only_in_production: tuple[NaturalKey, ...]
    only_in_replay: tuple[NaturalKey, ...]
    mismatched: tuple[FieldMismatch, ...]

    def matches(self) -> bool:
        return not (self.only_in_production or self.only_in_replay or self.mismatched)


def compare_by_natural_key(
    production: dict[NaturalKey, dict[str, Any]],
    replay: dict[NaturalKey, dict[str, Any]],
) -> KeyedComparison:
    """Compare two {natural_key: fields} maps, ignoring iteration/insertion order entirely.

    Used for both Stage 1 rows and Stage 2 canonical fields - see module docstring for why
    natural-key comparison, not list-order comparison, is the right notion of "match" here.
    """
    production_keys = set(production)
    replay_keys = set(replay)

    only_in_production = tuple(sorted(production_keys - replay_keys, key=repr))
    only_in_replay = tuple(sorted(replay_keys - production_keys, key=repr))

    mismatched = tuple(
        FieldMismatch(key=key, production_fields=production[key], replay_fields=replay[key])
        for key in sorted(production_keys & replay_keys, key=repr)
        if production[key] != replay[key]
    )

    return KeyedComparison(
        only_in_production=only_in_production,
        only_in_replay=only_in_replay,
        mismatched=mismatched,
    )


@dataclasses.dataclass(frozen=True)
class PipelineOutput:
    """One path's (production or replay) output from parse -> merge -> canonicalize, keyed by
    Stage 1 natural key throughout so the two paths can be compared regardless of message or
    row order (see module docstring)."""

    parse_result: ParseResult
    stage1_by_key: dict[NaturalKey, dict[str, Any]]
    stage2_by_key: dict[NaturalKey, dict[str, Any]]
    quarantined_message_ids: frozenset[Any]


def run_pipeline(
    messages: Iterable[dict[str, Any]],
    *,
    catalog: Catalog,
    registry: Registry | None = None,
) -> PipelineOutput:
    """Run `messages` through parse_messages -> a fresh Stage1MergeStore -> canonicalize_rows,
    returning every stage's output keyed by Stage 1 natural key.

    A fresh Stage1MergeStore is created on every call - callers comparing two paths (see
    check_replay_determinism) must call this once per path so there is no possibility of
    shared merge state between them.

    Stage 2 rows are keyed by pulling (device_id, device_ts_ms, payload_hash) back out of each
    CanonicalRow's own fields - canonicalize_row always passes these through unchanged as
    identity fields (see canonicalize.py's IDENTITY_FIELDS), so this is the same natural key
    Stage 1 used for that row, not a re-derivation.
    """
    parse_result = parse_messages(messages, registry=registry)

    store = Stage1MergeStore()
    store.merge(parse_result.rows)
    stage1_by_key = {natural_key(row): row for row in store.rows()}

    canon_result = canonicalize_rows(list(stage1_by_key.values()), catalog)
    stage2_by_key = {
        (row.fields["device_id"], row.fields["device_ts_ms"], row.fields["payload_hash"]): row.fields
        for row in canon_result.rows
    }

    quarantined_message_ids = frozenset(
        q.message.get("message_id") for q in parse_result.quarantined
    )

    return PipelineOutput(
        parse_result=parse_result,
        stage1_by_key=stage1_by_key,
        stage2_by_key=stage2_by_key,
        quarantined_message_ids=quarantined_message_ids,
    )


@dataclasses.dataclass(frozen=True)
class ReplayDeterminismResult:
    """Summary of comparing a "production" pipeline run against a "replay from Stage 0" run
    over what should be the same closed window of messages - the automated proof of CLAUDE.md
    invariant 7, mirroring this repo's reconciles()-style result objects (CaptureResult,
    ParseResult, MergeResult, CanonicalizeResult)."""

    production_message_count: int
    replay_message_count: int
    quarantined_only_in_production: frozenset[Any]
    quarantined_only_in_replay: frozenset[Any]
    stage1: KeyedComparison
    stage2: KeyedComparison

    def reconciles(self) -> bool:
        """True iff production and replay agree on: how many messages were seen, which
        messages were quarantined, and every Stage 1 / Stage 2 row (by natural key and by
        field value) - i.e. invariant 7 holds for this window."""
        return (
            self.production_message_count == self.replay_message_count
            and not self.quarantined_only_in_production
            and not self.quarantined_only_in_replay
            and self.stage1.matches()
            and self.stage2.matches()
        )


def check_replay_determinism(
    production_messages: Iterable[dict[str, Any]],
    replay_messages: Iterable[dict[str, Any]],
    *,
    catalog: Catalog,
    registry: Registry | None = None,
) -> ReplayDeterminismResult:
    """Run `production_messages` and `replay_messages` through independent copies of the
    parse -> merge -> canonicalize pipeline and report whether their outputs match.

    `production_messages` and `replay_messages` are expected to be the same underlying batch
    obtained two different ways (see module docstring) - this function makes no assumption
    about their order and doesn't require the caller to have sorted or deduplicated either
    list first; parse_messages/Stage1MergeStore/canonicalize_rows already handle that, and
    comparison here is by natural key, not position.
    """
    production_messages = list(production_messages)
    replay_messages = list(replay_messages)

    production = run_pipeline(production_messages, catalog=catalog, registry=registry)
    replay = run_pipeline(replay_messages, catalog=catalog, registry=registry)

    return ReplayDeterminismResult(
        production_message_count=len(production_messages),
        replay_message_count=len(replay_messages),
        quarantined_only_in_production=(
            production.quarantined_message_ids - replay.quarantined_message_ids
        ),
        quarantined_only_in_replay=(
            replay.quarantined_message_ids - production.quarantined_message_ids
        ),
        stage1=compare_by_natural_key(production.stage1_by_key, replay.stage1_by_key),
        stage2=compare_by_natural_key(production.stage2_by_key, replay.stage2_by_key),
    )
