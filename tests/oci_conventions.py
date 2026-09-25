"""Putting `conventions.OCI_TENANCY` into another state, which several suites need.

The tenancy is one frozen structure, so a test that wants part of it different —
a compartment named but not yet created, a compartment recorded against an OCID
the tenancy does not have, an account that is not this installation's —
replaces the whole structure for the length of the test rather than reaching
into it.

Whether a compartment's OCID is recorded yet is a state of the census like any
other, and recording one is a one-line edit to `conventions`. A test that
exercises either state sets it up here, against an OCID of its own, rather
than inheriting whichever state the live entry is in.
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


def with_recorded_compartment(monkeypatch: pytest.MonkeyPatch, consumer: str, ocid: str) -> conventions.Compartment:
    """Make `consumer`'s compartment recorded against `ocid`, for this test.

    The name and the minting row stay the live entry's; the OCID is the
    test's, so a fake tenancy holding a compartment of that name and OCID is
    the state an installation is in once the consumer has been minted for.
    """
    compartment = replace(conventions.OCI_TENANCY.compartments[consumer], ocid=ocid)
    with_compartment(monkeypatch, compartment)
    return compartment


def with_unrecorded_compartment(monkeypatch: pytest.MonkeyPatch, consumer: str) -> conventions.Compartment:
    """Make `consumer`'s compartment named but not yet recorded, for this test.

    The state before the consumer's first mint: the name and the minting row
    are the live entry's, and there is no OCID for a mint to be held to.
    """
    compartment = replace(conventions.OCI_TENANCY.compartments[consumer], ocid=None)
    with_compartment(monkeypatch, compartment)
    return compartment
