"""Tests for catalog/signals.yaml (P0-10 v0 signal catalog).

These check that the catalog is well-formed and that it does not drift from the (synthetic)
schema the fixture generator actually produces -- they do NOT and cannot check that the
catalog's names/units/semantics are *correct* against real hardware. That's a firmware SME
review (P0-10's actual "done when"), which is still outstanding; see catalog/README.md.

A stale catalog (fields the generator produces that aren't cataloged, or cataloged fields the
generator no longer produces) is worse than no catalog, so these tests run the generator for
real and inspect real message readings dicts, rather than trusting either file in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE, GeneratorConfig, generate

CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "signals.yaml"

REQUIRED_ENTRY_KEYS = {"canonical_name", "kind", "unit", "type", "description", "valid_range"}
VALID_KINDS = {"signal", "envelope_adjacent"}

_ALL_GROUPS = [
    (device_class, firmware)
    for device_class, firmwares in DEFAULT_FIRMWARE.items()
    for firmware in firmwares
]


def _load_catalog() -> dict:
    with CATALOG_PATH.open() as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), "catalog/signals.yaml did not parse to a mapping"
    return loaded


def _device_class_entries(catalog: dict) -> dict:
    """Top-level catalog keys that are actual device classes (skips metadata like
    catalog_status/catalog_version/source_generator)."""
    return {k: v for k, v in catalog.items() if k in DEFAULT_FIRMWARE}


def _catalog_fields(catalog: dict, device_class: str, firmware: str) -> dict:
    by_firmware = catalog.get(device_class, {})
    assert firmware in by_firmware, (
        f"catalog/signals.yaml has no entry for {device_class}/{firmware}, but the generator "
        f"models it (DEFAULT_FIRMWARE)"
    )
    fields = by_firmware[firmware]
    assert isinstance(fields, dict) and fields, (
        f"catalog/signals.yaml entry for {device_class}/{firmware} is empty or malformed"
    )
    return fields


def _generated_reading_fields(fixtures, device_class: str, firmware: str) -> set[str]:
    """Field names actually present on real generated readings dicts for one group, from a
    real generate() run -- not read off the generator's source statically."""
    messages = fixtures.messages_by_group[(device_class, firmware)]
    assert messages, f"generator produced no messages for {device_class}/{firmware}"
    field_names: set[str] = set()
    saw_reading = False
    for msg in messages:
        for reading in msg["readings"]:
            saw_reading = True
            field_names.update(reading.keys())
    assert saw_reading, f"generator produced messages with no readings for {device_class}/{firmware}"
    return field_names


@pytest.fixture(scope="module")
def fixtures():
    # devices_per_firmware=2 (rather than the default 4) is enough to see every field on every
    # group while keeping the test fast; field *presence* doesn't depend on device count.
    return generate(GeneratorConfig(devices_per_firmware=2))


@pytest.fixture(scope="module")
def catalog():
    return _load_catalog()


def test_catalog_file_exists_and_is_well_formed_yaml():
    catalog = _load_catalog()
    assert catalog, "catalog/signals.yaml parsed to an empty document"


def test_catalog_declares_itself_an_unreviewed_draft(catalog):
    status = str(catalog.get("catalog_status", "")).lower()
    assert "draft" in status or "unreviewed" in status, (
        "catalog/signals.yaml's catalog_status should plainly flag this as an unreviewed "
        "draft until a firmware SME actually reviews it (P0-10 done-when)"
    )


def test_catalog_covers_every_device_class_and_firmware_the_generator_models(catalog):
    by_class = _device_class_entries(catalog)
    assert set(by_class.keys()) == set(DEFAULT_FIRMWARE.keys()), (
        "catalog/signals.yaml device_class keys don't match DEFAULT_FIRMWARE in the generator"
    )
    for device_class, firmwares in DEFAULT_FIRMWARE.items():
        cataloged_firmwares = set(by_class[device_class].keys())
        missing = set(firmwares) - cataloged_firmwares
        assert not missing, (
            f"{device_class}: generator models firmware {sorted(missing)} with no catalog entry"
        )


@pytest.mark.parametrize("device_class,firmware", _ALL_GROUPS)
def test_every_generator_produced_field_is_cataloged(fixtures, catalog, device_class, firmware):
    """Catches the dangerous drift direction: the generator emits a field the catalog doesn't
    know about, e.g. after someone adds a reading field to the generator."""
    generated = _generated_reading_fields(fixtures, device_class, firmware)
    cataloged = set(_catalog_fields(catalog, device_class, firmware).keys())
    missing = generated - cataloged
    assert not missing, (
        f"{device_class}/{firmware}: generator produces field(s) {sorted(missing)} with no "
        f"catalog entry -- add them to catalog/signals.yaml"
    )


@pytest.mark.parametrize("device_class,firmware", _ALL_GROUPS)
def test_no_cataloged_field_the_generator_no_longer_produces(fixtures, catalog, device_class, firmware):
    """Catches the other drift direction: a stale catalog entry for a field the generator
    stopped producing (renamed/removed)."""
    generated = _generated_reading_fields(fixtures, device_class, firmware)
    cataloged = set(_catalog_fields(catalog, device_class, firmware).keys())
    extra = cataloged - generated
    assert not extra, (
        f"{device_class}/{firmware}: catalog has field(s) {sorted(extra)} the generator does "
        f"not actually produce -- remove or fix them in catalog/signals.yaml"
    )


@pytest.mark.parametrize("device_class,firmware", _ALL_GROUPS)
def test_catalog_entries_have_required_keys(catalog, device_class, firmware):
    fields = _catalog_fields(catalog, device_class, firmware)
    for field_name, entry in fields.items():
        assert isinstance(entry, dict), f"{device_class}/{firmware}/{field_name} entry is not a mapping"
        missing_keys = REQUIRED_ENTRY_KEYS - entry.keys()
        assert not missing_keys, (
            f"{device_class}/{firmware}/{field_name} missing required key(s): {sorted(missing_keys)}"
        )
        assert entry["kind"] in VALID_KINDS, (
            f"{device_class}/{firmware}/{field_name} has unknown kind {entry['kind']!r}"
        )


def test_envelope_adjacent_fields_present_in_every_group(catalog):
    """device_ts_ms and payload_hash are Stage 1 merge-key components (CLAUDE.md invariant 2),
    not physical signals -- every group should carry both, flagged as envelope_adjacent."""
    for device_class, firmwares in DEFAULT_FIRMWARE.items():
        for firmware in firmwares:
            fields = _catalog_fields(catalog, device_class, firmware)
            for name in ("device_ts_ms", "payload_hash"):
                assert name in fields, f"{device_class}/{firmware} missing {name}"
                assert fields[name]["kind"] == "envelope_adjacent", (
                    f"{device_class}/{firmware}/{name} should be kind: envelope_adjacent"
                )


def test_fault_code_fields_are_class_specific_not_shared(catalog):
    """stall and cabinet fault codes use different code spaces (documented in signals.yaml's
    header); the catalog should not use one shared 'fault_code' canonical name for both."""
    stall_fields = _catalog_fields(catalog, "supercharger_stall", DEFAULT_FIRMWARE["supercharger_stall"][0])
    cabinet_fields = _catalog_fields(
        catalog, "supercharger_cabinet", DEFAULT_FIRMWARE["supercharger_cabinet"][0]
    )
    stall_names = {e["canonical_name"] for e in stall_fields.values()}
    cabinet_names = {e["canonical_name"] for e in cabinet_fields.values()}
    assert "fault_code" not in stall_names
    assert "fault_code" not in cabinet_names
