"""Fixtures shared across the credential suites.

The kit they share is `memory_kit.MemoryKit`, a kit that is not a file; it
lives in its own module because test modules import the class directly, and
`conftest` is not a name an import can aim at.
"""

from __future__ import annotations

import pytest
from memory_kit import MemoryKit

from kluster.scripts.credentials.kdbx import KdbxStore


@pytest.fixture
def memory_kit() -> KdbxStore:
    return MemoryKit()
