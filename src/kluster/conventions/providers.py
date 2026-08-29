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

    TODO(kluster-ops#117): the tenancy OCID belongs here too. It is still
    `oci:tenancyOcid` in stack configuration, written there by the mint that
    issues the key, and moving it needs both that sink retired and a ruling on
    committing an account identifier to a public repository.
    """

    #: Home region — permanent per tenancy, and where the whole free envelope
    #: (A1 OCPU-hours, the 200 GB boot+block allowance) is redeemable.
    region: str
    #: The mail domain every OCI user this program creates is addressed in. An
    #: identity-domains tenancy converts every legacy-IAM user into a domain
    #: user, and the conversion refuses a user without a primary address; the
    #: address must also be unique within the domain, so each user is named
    #: after itself there rather than sharing one mailbox.
    user_email_domain: str
    #: One compartment per consumer, which is what makes the §3 OCI rows
    #: independent of each other.
    compartments: Mapping[str, Compartment]


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
OCI_TENANCY = OciTenancy(
    region='us-phoenix-1',
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
