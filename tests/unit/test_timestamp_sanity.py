"""Tests for Stage 1 timestamp sanity checks and quarantine classification (P1-05:
"Timestamp sanity checks and quarantine table").

Covers the classification logic in pipeline/stage1_parsed/timestamp_sanity.py: each of the
three reason codes individually, the FUTURE_THRESHOLD_MS boundary, and a run against rows
derived from the real synthetic generator/parser framework to sanity-check measured rates
against the generator's configured injection rates.
"""
from __future__ import annotations

import copy

import pytest

from parsers.framework import parse_messages

# Importing the demo parser module triggers its @register_parser("supercharger_stall",
# "2.1.4") decorator, populating the framework's global registry - see
# tests/unit/test_parser_framework.py for the same pattern.
from parsers.supercharger_stall.firmware_2_1_4 import parse_supercharger_stall_2_1_4  # noqa: F401
from pipeline.stage1_parsed.timestamp_sanity import (
    FUTURE_THRESHOLD_MS,
    REASON_EPOCH_DEFAULT_TIMESTAMP,
    REASON_FUTURE_TIMESTAMP,
    REASON_MISSING_TIMESTAMP,
    ReconciliationError,
    check_timestamps,
    reconcile,
)
from tests.fixtures.generators.supercharger import GeneratorConfig, generate


def _sample_row(**overrides) -> dict:
    base = {
        "device_id": "stall-2140001-0000",
        "device_class": "supercharger_stall",
        "firmware_version": "2.1.4",
        "site_id": "site-000",
        "session_id": "sess-stall-2140001-0000-0",
        "device_ts_ms": 1_780_358_400_000,
        "arrival_ts_ms": 1_780_358_400_000,
        "payload_hash": "sha1:deadbeef",
    }
    base.update(overrides)
    return base


def test_plausible_row_is_accepted():
    row = _sample_row(device_ts_ms=1_780_358_400_000, arrival_ts_ms=1_780_358_401_000)

    result = check_timestamps([row])

    assert result.rows_total == 1
    assert list(result.accepted_rows) == [row]
    assert result.quarantined_rows == ()
    assert result.missing_count == result.epoch_default_count == result.future_count == 0
    assert result.reconciles()
    reconcile(result)  # does not raise


def test_missing_timestamp_is_quarantined_with_reason_and_row_preserved():
    row = _sample_row(device_ts_ms=None)
    original = copy.deepcopy(row)

    result = check_timestamps([row])

    assert result.accepted_rows == ()
    assert len(result.quarantined_rows) == 1
    quarantined = result.quarantined_rows[0]
    assert quarantined.reason == REASON_MISSING_TIMESTAMP
    assert quarantined.row == original
    assert quarantined.row is row  # preserved verbatim, not copied/rebuilt
    assert result.missing_count == 1
    assert result.epoch_default_count == 0
    assert result.future_count == 0
    assert result.reconciles()


def test_epoch_default_timestamp_is_quarantined_with_reason_and_row_preserved():
    row = _sample_row(device_ts_ms=0)
    original = copy.deepcopy(row)

    result = check_timestamps([row])

    assert result.accepted_rows == ()
    assert len(result.quarantined_rows) == 1
    quarantined = result.quarantined_rows[0]
    assert quarantined.reason == REASON_EPOCH_DEFAULT_TIMESTAMP
    assert quarantined.row == original
    assert result.epoch_default_count == 1
    assert result.missing_count == 0
    assert result.future_count == 0
    assert result.reconciles()


def test_future_timestamp_is_quarantined_with_reason_and_row_preserved():
    arrival_ts_ms = 1_780_358_400_000
    row = _sample_row(
        device_ts_ms=arrival_ts_ms + FUTURE_THRESHOLD_MS + 1,
        arrival_ts_ms=arrival_ts_ms,
    )
    original = copy.deepcopy(row)

    result = check_timestamps([row])

    assert result.accepted_rows == ()
    assert len(result.quarantined_rows) == 1
    quarantined = result.quarantined_rows[0]
    assert quarantined.reason == REASON_FUTURE_TIMESTAMP
    assert quarantined.row == original
    assert result.future_count == 1
    assert result.missing_count == 0
    assert result.epoch_default_count == 0
    assert result.reconciles()


def test_future_threshold_boundary_is_not_off_by_one():
    arrival_ts_ms = 1_780_358_400_000

    just_under = _sample_row(
        device_ts_ms=arrival_ts_ms + FUTURE_THRESHOLD_MS,
        arrival_ts_ms=arrival_ts_ms,
    )
    just_over = _sample_row(
        device_ts_ms=arrival_ts_ms + FUTURE_THRESHOLD_MS + 1,
        arrival_ts_ms=arrival_ts_ms,
    )

    under_result = check_timestamps([just_under])
    over_result = check_timestamps([just_over])

    # Exactly at the threshold is still plausible - only *more than* FUTURE_THRESHOLD_MS ahead
    # of arrival counts as implausible.
    assert under_result.accepted_rows == (just_under,)
    assert under_result.quarantined_rows == ()

    assert over_result.accepted_rows == ()
    assert len(over_result.quarantined_rows) == 1
    assert over_result.quarantined_rows[0].reason == REASON_FUTURE_TIMESTAMP


def test_row_missing_arrival_ts_ms_is_not_flagged_future():
    # A row with no arrival_ts_ms at all (production wiring not yet threaded through - see
    # module docstring) can't be checked for future_timestamp, and is accepted on that check
    # alone (matching profiling/clock_quality/profiler.py's own _is_future, which treats a
    # missing arrival_ts_ms as "not future" rather than guessing).
    row = _sample_row(device_ts_ms=99_999_999_999_999)
    del row["arrival_ts_ms"]

    result = check_timestamps([row])

    assert result.accepted_rows == (row,)
    assert result.quarantined_rows == ()


def test_reconciliation_error_raised_on_mismatch():
    from pipeline.stage1_parsed.timestamp_sanity import TimestampSanityResult

    bad_result = TimestampSanityResult(
        rows_total=2,
        accepted_rows=({"device_id": "x"},),
        quarantined_rows=(),
        missing_count=0,
        epoch_default_count=0,
        future_count=0,
    )
    assert not bad_result.reconciles()
    with pytest.raises(ReconciliationError):
        reconcile(bad_result)


def _rows_from_generator(config: GeneratorConfig) -> list[dict]:
    """Parse each generated message individually (so each row can be tagged with its
    originating message's arrival_ts_ms) through the real parser framework, using whatever
    parsers happen to be registered.

    parsers/framework.py's demo parser (supercharger_stall/2.1.4) doesn't yet thread
    arrival_ts_ms from the envelope into each row it emits (see this module's docstring), so
    this test fixture adds it itself rather than waiting on that wiring. Messages whose
    (device_class, firmware_version) has no registered parser are quarantined by the parser
    framework and never reach here - correct, since P1-04's real parser coverage is a separate
    ticket and this sanity check only ever sees rows that made it past parsing.
    """
    fixtures = generate(config)
    rows: list[dict] = []
    for message in fixtures.all_messages():
        result = parse_messages([message])
        for row in result.rows:
            enriched = dict(row)
            enriched["arrival_ts_ms"] = message.get("arrival_ts_ms")
            rows.append(enriched)
    return rows


def test_against_real_generator_rates_roughly_track_configured_injection_rates():
    config = GeneratorConfig(devices_per_firmware=2)
    rows = _rows_from_generator(config)

    # Only supercharger_stall/2.1.4 has a registered parser in this worktree - see
    # parsers/supercharger_stall/firmware_2_1_4.py's module docstring. That's enough rows to
    # sanity-check rates against.
    assert len(rows) > 100, "expected a meaningful sample of rows from the demo parser"

    result = check_timestamps(rows)
    reconcile(result)  # must not raise
    assert result.reconciles()

    missing_rate = result.missing_count / result.rows_total
    epoch_rate = result.epoch_default_count / result.rows_total
    future_rate = result.future_count / result.rows_total

    # Generous tolerance: this is a modest sample (devices_per_firmware=2) of a stochastic
    # generator, and clock_drift/late-arrival jitter can nudge a few readings across the
    # future/epoch/missing boundaries at the margins. This is a "roughly tracks" check, not an
    # exact-rate assertion.
    tolerance = 0.03
    assert abs(missing_rate - config.missing_timestamp_rate) < tolerance, (
        f"missing_rate={missing_rate} vs configured {config.missing_timestamp_rate}"
    )
    assert abs(epoch_rate - config.epoch_default_rate) < tolerance, (
        f"epoch_rate={epoch_rate} vs configured {config.epoch_default_rate}"
    )
    assert abs(future_rate - config.future_timestamp_rate) < tolerance, (
        f"future_rate={future_rate} vs configured {config.future_timestamp_rate}"
    )
