"""Tests for replay-from-Stage-0 determinism (P1-14: "Replay-from-Stage-0 determinism test").

Covers three things:
  1. pipeline/stage0_landing/replay.read_landed_messages actually reads back exactly what
     pipeline/stage0_landing/capture.capture_messages wrote (a focused round-trip test,
     independent of the rest of the pipeline).
  2. The ticket's central proof, CLAUDE.md invariant 7 ("Replaying a closed window from
     Stage 0 must reproduce production output exactly"): the same batch of messages, pushed
     through parse -> merge -> canonicalize once in-memory ("production") and once after a
     land-then-read-back round trip through a moto-mocked Stage 0 bucket ("replay"), produces
     identical Stage 1 and Stage 2 output. See pipeline/replay_determinism.py's module
     docstring for the precise definitions this test relies on.
  3. That the comparison in (2) isn't vacuously true - it actually detects an injected
     mismatch.

Uses moto to mock S3, the same pattern tests/unit/test_stage0_capture.py uses.
"""
from __future__ import annotations

import copy
import datetime as dt

import boto3
import pytest
from moto import mock_aws

# Importing the demo parser registers supercharger_stall/2.1.4 with parsers.framework's global
# registry - the same import-for-side-effect pattern tests/unit/test_stage1_merge.py uses.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from pipeline.replay_determinism import check_replay_determinism, run_pipeline
from pipeline.stage0_landing.capture import capture_messages
from pipeline.stage0_landing.replay import read_landed_messages
from pipeline.stage2_canonical.canonicalize import load_catalog
from tests.fixtures.generators.supercharger import GeneratorConfig, generate

BUCKET = "telemetry-test-landing"
REGION = "us-east-1"


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _arrival_date(message: dict) -> str:
    arrival = dt.datetime.fromtimestamp(message["arrival_ts_ms"] / 1000, tz=dt.timezone.utc)
    return f"{arrival:%Y-%m-%d}"


def _read_back_all(messages: list[dict], s3_client) -> list[dict]:
    """Read back every message in `messages` from Stage 0.

    read_landed_messages' own contract is "one arrival_date's worth of hour prefixes" (see its
    docstring), so a batch spanning several arrival dates - as a generated batch full of late
    and future-shifted timestamps does - is read by calling it once per distinct date actually
    used and concatenating. Querying all 24 hours of each date is simple and cheap here rather
    than re-deriving the exact hour each message landed in, which arrival_key() already decided
    at write time.
    """
    dates = sorted({_arrival_date(m) for m in messages})
    replayed: list[dict] = []
    for date in dates:
        replayed.extend(read_landed_messages(BUCKET, s3_client, arrival_date=date, hours=range(24)))
    return replayed


def _stall_envelope(**overrides) -> dict:
    base = {
        "message_id": "msg-roundtrip-0001",
        "device_id": "stall-9999-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "protocol": "mqtt_batch",
        "sent_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_000,  # 2026-06-01T00:00:00Z
        "readings": [
            {
                "session_id": "sess-stall-9999-0000-0",
                "state": "plugged_in",
                "output_voltage_v": 401.2,
                "output_current_a": 12.5,
                "output_power_kw": 5.01,
                "connector_temp_c": 31.0,
                "energy_delivered_kwh": 0.02,
                "fault_code": 0,
                "device_ts_ms": 1_780_358_400_000,
                "payload_hash": "sha1:aaaa0001",
            },
            {
                "session_id": "sess-stall-9999-0000-0",
                "state": "charging",
                "output_voltage_v": 401.5,
                "output_current_a": 140.3,
                "output_power_kw": 56.29,
                "connector_temp_c": 36.7,
                "energy_delivered_kwh": 0.65,
                "fault_code": 0,
                "device_ts_ms": 1_780_358_400_000 + 15_000,
                "payload_hash": "sha1:aaaa0002",
            },
        ],
        "_debug_injected_issues": [],
    }
    base.update(overrides)
    return base


# -----------------------------------------------------------------------------------------
# Stage 0 reader round trips
# -----------------------------------------------------------------------------------------


def test_read_landed_messages_round_trips_a_multi_reading_envelope(s3_client):
    """A message whose envelope carries more than one reading - exactly the batched shape real
    (and synthetic) Supercharger messages take - round-trips through land -> read-back with no
    special-casing, because it's decoded with a plain json.loads of what capture_messages
    serialized."""
    message = _stall_envelope()
    capture_messages([message], bucket=BUCKET, s3_client=s3_client)

    replayed = read_landed_messages(
        BUCKET, s3_client, arrival_date=_arrival_date(message), hours=[0]
    )

    assert replayed == [message]
    assert len(replayed[0]["readings"]) == 2


def test_read_landed_messages_round_trips_a_full_generated_batch(s3_client):
    """Independent of any pipeline comparison: capture a real generated batch and confirm the
    reader lists and reads back exactly what capture_messages wrote - same message_ids, same
    content - regardless of read-back order (list_objects_v2 returns lexical key order, not
    capture order; see read_landed_messages' own docstring)."""
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())

    capture_result = capture_messages(messages, bucket=BUCKET, s3_client=s3_client)
    assert capture_result.reconciles()

    replayed = _read_back_all(messages, s3_client)

    assert len(replayed) == len(messages)
    by_id_original = {m["message_id"]: m for m in messages}
    by_id_replayed = {m["message_id"]: m for m in replayed}
    assert by_id_replayed == by_id_original


def test_read_landed_messages_returns_nothing_for_an_hour_with_no_traffic(s3_client):
    """A prefix with no landed objects is an empty list, not an error - the boundary case a
    caller relies on when scanning a full day's worth of hours that mostly have no traffic
    (see _read_back_all above)."""
    message = _stall_envelope()
    capture_messages([message], bucket=BUCKET, s3_client=s3_client)

    replayed = read_landed_messages(
        BUCKET, s3_client, arrival_date=_arrival_date(message), hours=[7]
    )

    assert replayed == []


# -----------------------------------------------------------------------------------------
# The core determinism proof (CLAUDE.md invariant 7)
# -----------------------------------------------------------------------------------------


def test_replay_from_stage0_matches_production_pipeline_output(s3_client):
    """The ticket's central proof: the SAME closed window of messages, pushed through
    parse -> merge -> canonicalize twice - once straight from memory ("production"), once
    after landing to and reading back from a moto-mocked Stage 0 bucket ("replay") - produces
    identical Stage 1 and Stage 2 output, compared by natural key rather than list order (see
    pipeline/replay_determinism.py's module docstring for why).
    """
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())
    catalog = load_catalog()

    # Sanity on the batch itself, so this is a meaningful volume/variety fixture, not a
    # trivial one-message case: multiple firmware versions, both device classes, some messages
    # that will quarantine (only supercharger_stall/2.1.4 has a registered parser - see
    # parsers/supercharger_stall/firmware_2_1_4.py), and some genuine duplicate
    # retransmissions (message_id ending "-retry", injected by the generator's
    # _maybe_duplicate).
    assert len(messages) > 500
    firmware_versions = {m["firmware_version"] for m in messages}
    assert firmware_versions == {"2.1.4", "2.3.0", "3.0.1", "1.8.2", "1.9.0"}
    assert {m["device_class"] for m in messages} == {
        "supercharger_stall",
        "supercharger_cabinet",
    }
    assert any(m["message_id"].endswith("-retry") for m in messages)

    capture_result = capture_messages(messages, bucket=BUCKET, s3_client=s3_client)
    assert capture_result.reconciles()

    replay_messages = _read_back_all(messages, s3_client)
    assert len(replay_messages) == len(messages)

    result = check_replay_determinism(messages, replay_messages, catalog=catalog)

    assert result.stage1.matches(), result.stage1
    assert result.stage2.matches(), result.stage2
    assert result.reconciles()

    # And this batch actually exercised parsing/merge/canonicalization non-trivially - it isn't
    # trivially agreeing because both sides did nothing:
    production = run_pipeline(messages, catalog=catalog)
    assert production.parse_result.messages_quarantined > 0
    assert production.parse_result.rows_parsed > 0
    assert len(production.stage1_by_key) > 0
    # At least one duplicate natural key actually collapsed during Stage 1 merge.
    assert len(production.stage1_by_key) < production.parse_result.rows_parsed
    assert len(production.stage2_by_key) == len(production.stage1_by_key)


def test_check_replay_determinism_detects_a_mutated_field(s3_client):
    """Confirms the comparison function isn't vacuously true. Mutates one reading's value on
    the replay side only, leaving payload_hash (part of the natural key) untouched - so this is
    a same-key, different-field-value divergence, exactly the class of bug (e.g. a lossy
    re-serialization somewhere in a real replay path) invariant 7 exists to catch - and
    confirms check_replay_determinism reports it rather than silently agreeing.
    """
    fixtures = generate(GeneratorConfig(devices_per_firmware=2))
    messages = list(fixtures.all_messages())
    catalog = load_catalog()

    capture_messages(messages, bucket=BUCKET, s3_client=s3_client)
    replay_messages = _read_back_all(messages, s3_client)

    mutated = copy.deepcopy(replay_messages)
    target = next(
        m
        for m in mutated
        if m["device_class"] == "supercharger_stall" and m["firmware_version"] == "2.1.4"
    )
    target["readings"][0]["output_voltage_v"] += 1000.0

    result = check_replay_determinism(messages, mutated, catalog=catalog)

    assert not result.reconciles()
    assert not result.stage1.matches()
    assert result.stage1.mismatched
    assert not result.stage2.matches()
    assert result.stage2.mismatched
