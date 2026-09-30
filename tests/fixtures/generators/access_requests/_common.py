"""Shared helpers for the P0-11 synthetic access-request fixture generators.

Why this exists
----------------
P0-11 ("Access to RMA, tickets, dispatch, outage, provisioning data") is blocked on a human
(the EM) actually contacting and getting sign-off from five external system owners - see
`docs/access-requests/P0-11-data-access-requests.md`. That can't happen from this environment,
and the ticket correctly stays "To do" in the backlog: no real access exists, and nothing in
this package changes that.

What this package is instead: a synthetic substitute for the "sample extracts loaded" half of
P0-11's done-when, so the Phase 2 tickets that consume these five sources (P2-01, P2-02, and
downstream P2-04/P2-12/P3-05) have something concrete to build and test against instead of
waiting indefinitely for real access. See this package's `README.md` for the full explanation
and the ASSUMED-SCHEMA caveat that applies to every generator here.

This module holds only the pieces shared across all five generators: millisecond/day
constants and a fixed synthetic "now", plus - most importantly - the device_id/site_id
scheme. That scheme is reproduced by hand (not imported) from
`tests/fixtures/generators/supercharger.py`, so records generated here can be joined against
that generator's Supercharger fixtures in cross-source tests: an RMA record and a Supercharger
telemetry fixture that describe "the same" synthetic device really do share a device_id.
"""
from __future__ import annotations

import dataclasses

from tests.fixtures.generators.supercharger import DEFAULT_FIRMWARE, DeviceClass

MS_PER_S = 1000
S_PER_DAY = 86_400

# Same fixed synthetic "now" as supercharger.py (2026-06-01T00:00:00Z), so timestamps
# produced by different generators in this package land on the same synthetic timeline and
# can be compared directly.
SYNTHETIC_NOW_MS = 1_780_358_400_000


def supercharger_device_id(device_class: DeviceClass, firmware: str, index: int) -> str:
    """Reproduces supercharger.py's private `_device_id` formula exactly. Kept as a literal
    copy (rather than importing that underscore-prefixed name) since it's private to that
    module; if supercharger.py's scheme ever changes, this must change with it."""
    prefix = "stall" if device_class == "supercharger_stall" else "cab"
    fw_tag = firmware.replace(".", "")
    return f"{prefix}-{fw_tag}-{index:04d}"


def supercharger_site_id(index: int) -> str:
    """Reproduces supercharger.py's site_id formula (`f"site-{index % 6:03d}"`) exactly."""
    return f"site-{index % 6:03d}"


@dataclasses.dataclass(frozen=True)
class DeviceRef:
    device_id: str
    site_id: str
    device_class: DeviceClass
    firmware_version: str


def iter_supercharger_devices(devices_per_firmware: int = 4) -> list[DeviceRef]:
    """All (device_id, site_id, device_class, firmware) combinations that
    `supercharger.generate()` would produce for `devices_per_firmware` devices per firmware
    version. Used as the device population for the other generators in this package (RMA,
    tickets, dispatch, provisioning), so their device_id/site_id values are guaranteed to
    overlap with supercharger.py's own fixtures rather than living in a disjoint id space.

    Note: this only covers the two Supercharger device classes, since supercharger.py is the
    only fixture generator this repo currently has for a specific device class/firmware
    scheme. The real fleet also includes Powerwall, Megapack and Powerpack devices (see
    CLAUDE.md); outage/RMA/tickets/dispatch records that are not Supercharger-specific are
    still plausible against a fuller fleet, but this generator package deliberately keeps its
    device population joinable against what already exists in this repo rather than inventing
    a second, unrelated id scheme for device classes with no fixture generator (yet) to join
    against.
    """
    devices: list[DeviceRef] = []
    for device_class, firmwares in DEFAULT_FIRMWARE.items():
        for firmware in firmwares:
            for index in range(devices_per_firmware):
                devices.append(
                    DeviceRef(
                        device_id=supercharger_device_id(device_class, firmware, index),
                        site_id=supercharger_site_id(index),
                        device_class=device_class,
                        firmware_version=firmware,
                    )
                )
    return devices
