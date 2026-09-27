"""Shared pytest fixtures for the test suite.

Kept intentionally small for now - most fixture-generation logic lives in
tests/fixtures/generators/ so it can be reused outside of pytest too (e.g. to pre-generate a
fixture corpus for manual parser debugging).
"""
from __future__ import annotations

import random

import pytest


@pytest.fixture
def seeded_rng() -> random.Random:
    """A deterministic RNG so fixture-generator tests aren't flaky."""
    return random.Random(1337)
