"""The accounts this program declares into, as facts rather than credentials.

What identifies an account — its region, the compartments it is carved into,
the mail domain its users are addressed in — is a decision this repository
makes and shares between the stacks and the credentials scripts. What
authenticates to it is stack configuration, read where the provider is built.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kluster.conventions.identity import CLUSTER_NAME, PHYSICAL, STATE_BACKEND


class CompartmentMissing(LookupError):
    """A consumer's compartment is named here but does not exist in the tenancy yet."""


class TenancyUnrecorded(LookupError):
    """The account's own OCID belongs here and has not been written down yet."""


@dataclass(frozen=True)
class Compartment:
    """The OCI compartment one consumer administers, under both of its names.

    A compartment is the whole of what a §3 OCI key may touch
    (docs/credentials.md §3): each consumer administers its own and is a
    stranger outside it. That makes the boundary a decision of this program
    rather than a fact of the tenancy, so it is named here — and named twice,
    because the two halves are established at different moments.

    The **name** is a convention: it is chosen here, it is what the mint
    creates or adopts, and it is the only form OCI's quota statements accept
    (`components/cloud/guardrails.py`). The **OCID** is the site fact that follows
    from creating it — an identifier the committed file may carry in the clear,
    for the reason `cloudflareAccountId` may: it names a container inside the
    tenancy rather than the account that owns it, and everything it admits is
    still behind a key.

    A compartment that has not been created yet therefore has a name and no
    OCID. That is a state rather than a gap: `credentials derived oci-<consumer>
    mint` creates it, prints the OCID, and the edit that records it here is one
    line to commit. Until then `require` refuses by naming that command, which
    is what keeps a stack from failing on a lookup instead.
    """

    #: The `credentials derived oci-<consumer>` row, and the stack or command
    #: the compartment belongs to.
    consumer: str
    #: What the compartment is called in the tenancy.
    name: str
    #: What OCI calls it, once it exists.
    ocid: str | None = None

    @property
    def mint(self) -> str:
        """The command that creates the compartment and mints the key confined to it."""
        return f'credentials derived oci-{self.consumer} mint'

    def require(self) -> str:
        """The OCID, or a refusal naming the command that produces one."""
        if self.ocid is None:
            raise CompartmentMissing(
                f'the {self.name} compartment does not exist yet, so nothing can be declared in it: '
                f'`{self.mint}` creates it and prints the OCID to record as the `{self.consumer}` entry '
                'of `conventions.OCI_TENANCY.compartments`'
            )
        return self.ocid


@dataclass(frozen=True)
class OciTenancy:
    """The cloud account, as everything that declares into it has to know it.

    The account's own identifiers are facts this file carries; the key that
    signs for it is stack configuration, read at the line that builds the
    provider (rfc-002 §8.1, §10.3). The tenancy OCID is on the first side of
    that split: it names the account rather than authenticating to it, so it
    has one home here and `credentials derived oci-physical mint` proves the
    key it issues belongs to that account instead of copying the OCID beside
    it.
    """

    #: Home region — permanent per tenancy, and where the whole free envelope
    #: (A1 OCPU-hours, the 200 GB boot+block allowance) is redeemable.
    region: str
    #: What OCI calls the account itself. Also its root compartment, which is
    #: why the two tenancy-level guardrail resources — the quota policy and the
    #: budget — are declared against it rather than against a compartment.
    #:
    #: `None` is a state this field is passing through rather than one it
    #: keeps: see the TODO on `OCI_TENANCY` below.
    tenancy_ocid: str | None
    #: The mail domain every OCI user this program creates is addressed in. An
    #: identity-domains tenancy converts every legacy-IAM user into a domain
    #: user, and the conversion refuses a user without a primary address; the
    #: address must also be unique within the domain, so each user is named
    #: after itself there rather than sharing one mailbox.
    user_email_domain: str
    #: One compartment per consumer, which is what makes the §3 OCI rows
    #: independent of each other.
    compartments: Mapping[str, Compartment]

    def require_tenancy_ocid(self) -> str:
        """The account's OCID, or a refusal naming what has to be written here."""
        if self.tenancy_ocid is None:
            raise TenancyUnrecorded(
                'the tenancy OCID is not recorded in `conventions.OCI_TENANCY`, so nothing that declares into '
                'the account can name it: `pulumi config get kluster-py:ociTenancyOcid --stack physical` prints '
                'the value this estate has been using, and writing it as the `tenancy_ocid` argument is the one '
                'line to commit'
            )
        return self.tenancy_ocid


@dataclass(frozen=True)
class B2Account:
    """The backup account.

    Its region is an account property rather than a setting: it does not change
    while the account exists, and no B2 API returns it in the form its
    S3-compatible endpoint is spelled with.
    """

    region: str


#: The appliance's compartment is the tenancy's original one: it was made by
#: hand before this program existed, so it carries the estate's own name rather
#: than a per-consumer one, and the mint adopts it exactly as it adopts a user
#: or a group that is already there.
#:
#: TODO(kluster-ops#117): `tenancy_ocid` is the literal, and it is `None` only
#: because the value has never been written down in the clear — it lives as an
#: encrypted `kluster-py:ociTenancyOcid` in `Pulumi.physical.yaml`, which
#: `pulumi config get` prints and this checkout cannot.
#:
#: Writing it here is one line, and it makes the whole unrecorded state
#: unreachable — so the same commit deletes all of it, or the tree is left
#: carrying machinery for a case that can no longer happen:
#:
#: - this comment, the `| None` on the field above and the note under it;
#: - `require_tenancy_ocid` and its two callers' use of it — `stacks/physical.py`
#:   and `oci_iam.verify_tenancy` read the field directly;
#: - the `TenancyUnrecorded` class, its `except` arm in `oci_iam.verify_tenancy`,
#:   and its import and `__all__` entry in `conventions/__init__.py`;
#: - `with_tenancy_ocid`'s `None` mode (`tests/oci_conventions.py`), and the two
#:   cases that are the only callers of it —
#:   `test_derived.py::test_an_account_conventions_has_not_recorded_refuses_to_deliver`
#:   and
#:   `test_physical_stack.py::test_a_tenancy_nobody_has_written_down_refuses_by_naming_the_line_to_write`;
#: - the `kluster-py:ociTenancyOcid` entry in `Pulumi.physical.yaml`, which
#:   nothing reads once the field carries the value.
OCI_TENANCY = OciTenancy(
    region='us-phoenix-1',
    tenancy_ocid=None,
    user_email_domain='unlimited-code.works',
    compartments={
        compartment.consumer: compartment
        for compartment in (
            Compartment(
                consumer=STATE_BACKEND,
                name=CLUSTER_NAME,
                ocid='ocid1.compartment.oc1..aaaaaaaaapllt64sf7e4gwnbka7l6d2hrblj6wvca7avtu6mrt6jaouallaq',
            ),
            Compartment(
                consumer=PHYSICAL,
                name=f'{CLUSTER_NAME}-{PHYSICAL}',
                ocid='ocid1.compartment.oc1..aaaaaaaajoaiz6cho6dnufutp6nrqyzhp6dswoi4hssa4o4sks276areztna',
            ),
        )
    },
)

#: The seed user's primary email.
OCI_SEED_USER_EMAIL = f'pulumi@{OCI_TENANCY.user_email_domain}'

B2_ACCOUNT = B2Account(region='us-west-002')
