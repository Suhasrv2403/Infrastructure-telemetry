"""Tests for pipeline/stage3_enrich/device_history.py (P2-01).

Done-when this ticket is checked against: "As-of joins return the firmware a device ran at any
timestamp." These tests exercise that literally - before commissioning, exactly at a change,
one ms before a change, a never-changed device, and arbitrarily far in the future - plus a
stress/property check across many devices and seeds cross-verified against
provisioning.py's own reference `current_as_of()`.
"""
from __future__ import annotations

from pipeline.stage3_enrich.device_history import (
    _ASSUMED_CLIMATE_ZONES,
    DuplicateEffectiveTimestampError,
    as_of_join,
    assumed_climate_zone_for_site,
    ingest_provisioning_records,
)
from tests.fixtures.generators.access_requests.provisioning import (
    GeneratorConfig,
    current_as_of,
    generate,
)

ONE_MS = 1
ONE_DAY_MS = 24 * 3600 * 1000


def _one_device_records():
    """A small, hand-built 3-event history for one device: commissioning, then a firmware
    upgrade, then a site reassignment - enough to exercise every boundary by hand rather than
    only through the generator."""
    return [
        {
            "device_id": "dev-1",
            "effective_ts": 1_000_000,
            "firmware_version": "1.0.0",
            "hardware_rev": "rev-a",
            "cell_lot": "LOT-201901-A1",
            "site_id": "site-000",
        },
        {
            "device_id": "dev-1",
            "effective_ts": 2_000_000,
            "firmware_version": "1.1.0",
            "hardware_rev": "rev-a",
            "cell_lot": "LOT-201901-A1",
            "site_id": "site-000",
        },
        {
            "device_id": "dev-1",
            "effective_ts": 3_000_000,
            "firmware_version": "1.1.0",
            "hardware_rev": "rev-a",
            "cell_lot": "LOT-201901-A1",
            "site_id": "site-005",
        },
    ]


def test_ingest_builds_contiguous_chained_validity_periods():
    history = ingest_provisioning_records(_one_device_records())
    rows = history["dev-1"]
    assert len(rows) == 3
    assert rows[0].valid_from_ms == 1_000_000
    assert rows[0].valid_to_ms == 2_000_000
    assert rows[1].valid_from_ms == 2_000_000
    assert rows[1].valid_to_ms == 3_000_000
    assert rows[2].valid_from_ms == 3_000_000
    assert rows[2].valid_to_ms is None


def test_before_first_record_returns_none():
    history = ingest_provisioning_records(_one_device_records())
    assert as_of_join(history, "dev-1", 1_000_000 - ONE_MS) is None
    assert as_of_join(history, "dev-1", 0) is None


def test_at_exact_effective_ts_returns_new_row():
    history = ingest_provisioning_records(_one_device_records())
    row = as_of_join(history, "dev-1", 2_000_000)
    assert row is not None
    assert row.firmware_version == "1.1.0"
    assert row.valid_from_ms == 2_000_000

    row2 = as_of_join(history, "dev-1", 3_000_000)
    assert row2 is not None
    assert row2.site_id == "site-005"


def test_one_ms_before_change_returns_old_row():
    history = ingest_provisioning_records(_one_device_records())
    row = as_of_join(history, "dev-1", 2_000_000 - ONE_MS)
    assert row is not None
    assert row.firmware_version == "1.0.0"

    row2 = as_of_join(history, "dev-1", 3_000_000 - ONE_MS)
    assert row2 is not None
    assert row2.site_id == "site-000"
    assert row2.firmware_version == "1.1.0"


def test_far_future_returns_latest_open_row():
    history = ingest_provisioning_records(_one_device_records())
    far_future = 3_000_000 + 10_000 * ONE_DAY_MS
    row = as_of_join(history, "dev-1", far_future)
    assert row is not None
    assert row.site_id == "site-005"
    assert row.valid_to_ms is None


def test_single_record_device_always_returns_that_record_after_commissioning():
    records = [
        {
            "device_id": "dev-solo",
            "effective_ts": 5_000_000,
            "firmware_version": "2.0.0",
            "hardware_rev": "rev-b",
            "cell_lot": "LOT-202001-B2",
            "site_id": "site-002",
        }
    ]
    history = ingest_provisioning_records(records)
    assert as_of_join(history, "dev-solo", 5_000_000 - ONE_MS) is None

    for offset_days in (0, 1, 100, 100_000):
        ts = 5_000_000 + offset_days * ONE_DAY_MS
        row = as_of_join(history, "dev-solo", ts)
        assert row is not None
        assert row.firmware_version == "2.0.0"
        assert row.hardware_rev == "rev-b"
        assert row.valid_to_ms is None


def test_unknown_device_id_returns_none():
    history = ingest_provisioning_records(_one_device_records())
    assert as_of_join(history, "does-not-exist", 2_000_000) is None


def test_duplicate_effective_ts_raises():
    records = _one_device_records()
    records.append(dict(records[0]))  # same device_id, same effective_ts
    try:
        ingest_provisioning_records(records)
    except DuplicateEffectiveTimestampError:
        pass
    else:
        raise AssertionError("expected DuplicateEffectiveTimestampError")


def test_climate_zone_is_deterministic_and_derived_from_site():
    zone_a = assumed_climate_zone_for_site("site-000")
    zone_a_again = assumed_climate_zone_for_site("site-000")
    zone_b = assumed_climate_zone_for_site("site-999")
    assert zone_a == zone_a_again
    assert zone_a in _ASSUMED_CLIMATE_ZONES
    assert zone_b in _ASSUMED_CLIMATE_ZONES

    # climate_zone on an ingested row tracks that row's own site_id, since a site
    # reassignment changes the assumed climate along with everything else about the site.
    history = ingest_provisioning_records(_one_device_records())
    rows = history["dev-1"]
    assert rows[0].climate_zone == assumed_climate_zone_for_site("site-000")
    assert rows[2].climate_zone == assumed_climate_zone_for_site("site-005")


def test_stress_against_provisioning_generator_multiple_seeds():
    """Property-style cross-check: for many devices across several seeds/configs, sample a
    grid of timestamps per device (before commissioning, at/around every change, and far
    future) and assert this module's as_of_join() agrees exactly with provisioning.py's own
    independent reference implementation, current_as_of()."""
    for seed in (1, 2, 3, 42, 12345):
        generated = generate(GeneratorConfig(seed=seed, devices_per_firmware=5))
        history = ingest_provisioning_records(generated.records)

        by_device: dict[str, list[dict]] = {}
        for record in generated.records:
            by_device.setdefault(record["device_id"], []).append(record)

        assert set(history.keys()) == set(by_device.keys())

        for device_id, records in by_device.items():
            records_sorted = sorted(records, key=lambda r: r["effective_ts"])
            sample_points = [records_sorted[0]["effective_ts"] - 1]
            for i, record in enumerate(records_sorted):
                ts = record["effective_ts"]
                sample_points.extend([ts - 1, ts, ts + 1])
            sample_points.append(records_sorted[-1]["effective_ts"] + 50 * ONE_DAY_MS)

            for ts in sample_points:
                expected_record = current_as_of(generated.records, device_id, ts)
                actual_row = as_of_join(history, device_id, ts)

                if expected_record is None:
                    assert actual_row is None, (
                        f"seed={seed} device={device_id} ts={ts}: expected None, "
                        f"got {actual_row}"
                    )
                else:
                    assert actual_row is not None, (
                        f"seed={seed} device={device_id} ts={ts}: expected a row, got None"
                    )
                    assert actual_row.firmware_version == expected_record["firmware_version"]
                    assert actual_row.hardware_rev == expected_record["hardware_rev"]
                    assert actual_row.cell_lot == expected_record["cell_lot"]
                    assert actual_row.site_id == expected_record["site_id"]
                    assert actual_row.valid_from_ms == expected_record["effective_ts"]
