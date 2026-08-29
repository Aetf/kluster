"""Replacing one consumer's compartment, which several suites need.

The tenancy is one frozen structure (`conventions.OCI_TENANCY`), so a test that
wants a compartment in another state — named but not yet created, recorded
against an OCID the tenancy does not have — replaces the whole tenancy for the
length of the test rather than reaching into a mapping.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kluster import conventions


def with_compartment(monkeypatch: pytest.MonkeyPatch, compartment: conventions.Compartment) -> None:
    """Make `compartment` the one its consumer administers, for this test."""
    tenancy = conventions.OCI_TENANCY
    monkeypatch.setattr(
        conventions,
        'OCI_TENANCY',
        replace(tenancy, compartments={**tenancy.compartments, compartment.consumer: compartment}),
    )
