"""Synthetic-proxy profiling for device retry/buffer/drop behavior (ticket P0-09).

See profiling/retry_behavior/profiler.py for the detector itself, and
docs/profiling/P0-09-retry-behavior-test-plan.md for the actual P0-09 deliverable this
package supports: a test plan for humans to run real fault injection against real
Supercharger hardware. This package does NOT do that test - see this module's own
docstring caveats in profiler.py before reading anything here as a finding about real
device behavior.
"""
from __future__ import annotations
