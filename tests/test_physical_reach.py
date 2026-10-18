"""What `physical` declares in the tenancy's root compartment, its key may create.

The `physical` key administers its own compartment, and a resource OCI keeps
in the root compartment — a compartment quota, a budget — is outside it, whatever
compartment it acts on. Each such resource is authorized under a policy family
of its own, which the key's group has to hold in the tenancy
(`oci_iam.TENANCY_POLICIES`). A `pulumi preview` plans a create without asking
IAM, so a root resource the policy does not cover is first refused at the
stack's first `up`; these cases hold the two together before that.

The program's side is read from a run of it, and the policy's side is the
statements the mint converges the group to. What joins them is `FAMILIES`,
which belongs to neither side: it is Oracle's, and each entry names the page it
is taken from. A budget's alert rules are declared under the budget rather than
in a compartment, and the Budgets page authorizes them under the budget's
family.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from mock_monitor import declaring
from physical_installation import COMPARTMENT, Installation, install

from kluster import conventions
from kluster.scripts.credentials import oci_iam
from kluster.stacks import physical

#: The IAM policy family each OCI resource type is authorized under, as
#: Oracle's documentation names it, keyed by the type the program declares.
#: A type the program declares in the root compartment and this table lacks
#: fails the case by name: its entry comes from Oracle's policy reference for
#: that service, and its grant goes into the key's policy beside it.
FAMILIES = {
    # "Details for the Quotas Service": resource-type `quota`, whose
    # createQuota, updateQuota and deleteQuota are granted at `manage` alone.
    # https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/quotaspolicyreference.htm
    'oci:Limits/quota:Quota': 'quota',
    # "Budgets": "To use budgets, you must be in a group that can use
    # usage-budgets in the tenancy (which is the root compartment)", and
    # "Create, edit, and delete budgets and alerts rules" is `manage`.
    # https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm
    'oci:Budget/budget:Budget': 'usage-budgets',
}

#: The verb every family is needed at. A stack creates, updates and deletes
#: what it declares, and both pages above grant deletion at `manage` alone.
VERB = 'manage'

#: The family OCI's policy language names for every resource type at once.
EVERY_FAMILY = 'all-resources'

#: One statement granting a family in the tenancy itself, as OCI's policy
#: language writes it.
TENANCY_STATEMENT = re.compile(r'Allow group (?P<group>\S+) to (?P<verb>\S+) (?P<family>\S+) in tenancy')


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installation:
    return await install(monkeypatch, tmp_path)


async def _root_types(setup: Installation) -> set[str]:
    """Every resource type the program declares in the tenancy's root compartment."""
    async with declaring():
        await physical.main()
    tenancy = conventions.OCI_TENANCY.tenancy_ocid
    return {declaration.typ for declaration in setup.declared if declaration.inputs.get('compartmentId') == tenancy}


def _tenancy_grants() -> Iterator[tuple[str, str]]:
    """Each `(verb, family)` the `physical` key's group holds in the tenancy."""
    identity = oci_iam.Identity.for_consumer(conventions.PHYSICAL, compartment_id=COMPARTMENT.require())
    for statement in identity.statements:
        if (granted := TENANCY_STATEMENT.fullmatch(statement)) is not None:
            assert granted['group'] == identity.name, statement
            yield granted['verb'], granted['family']


def _family(typ: str) -> str:
    family = FAMILIES.get(typ)
    assert family is not None, (
        f'{typ} is declared in the root compartment and no policy family is recorded for it: '
        "add the family Oracle's policy reference authorizes it under to FAMILIES, "
        "and the grant to the physical key's tenancy policy"
    )
    return family


@pytest.mark.asyncio
async def test_every_root_resource_the_program_declares_is_granted_to_its_key(setup: Installation) -> None:
    roots = await _root_types(setup)
    # Not vacuous: the guardrails are declared there, so the run has some.
    assert roots

    granted = set(_tenancy_grants())
    for typ in sorted(roots):
        family = _family(typ)
        assert {(VERB, family), (VERB, EVERY_FAMILY)} & granted, (
            f"{typ} is declared in the root compartment, and the physical key's policy grants no "
            f'`{VERB} {family} in tenancy`'
        )


@pytest.mark.asyncio
async def test_the_key_holds_nothing_in_the_tenancy_its_root_resources_do_not_need(setup: Installation) -> None:
    needed = {(VERB, _family(typ)) for typ in await _root_types(setup)}

    assert set(_tenancy_grants()) <= needed
