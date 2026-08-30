"""Putting `conventions.OCI_TENANCY` into another state, which several suites need.

The tenancy is one frozen structure, so a test that wants part of it different —
a compartment named but not yet created, a compartment recorded against an OCID
the tenancy does not have, an account that is not this estate's — replaces the
whole structure for the length of the test rather than reaching into it.
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


def with_tenancy_ocid(monkeypatch: pytest.MonkeyPatch, ocid: str) -> None:
    """Make `ocid` the account `conventions` records, for this test."""
    monkeypatch.setattr(conventions, 'OCI_TENANCY', replace(conventions.OCI_TENANCY, tenancy_ocid=ocid))
