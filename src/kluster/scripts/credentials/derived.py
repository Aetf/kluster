"""The derived credentials (docs/credentials.md §3): minted from a seed, pushed to a slot.

One function per register row, each of them mint -> push -> verify inside a
single run, and therefore idempotent: rotating a row is re-running it. Nothing
here ever writes to the kit — a derived credential in the offline store would
be the staging area §1 rule 2 forbids.

A row appears here when its consumer exists. The one Cloudflare row that is
still absent — the DNS-01 token for cert-manager — has nowhere to be delivered
yet, and a mint with no slot would be exactly the parked secret the register
rules out.

Which slot a row is pushed into follows from what consumes it. A stack's
credential goes into that stack's committed configuration, where the program
reads it before it can run. The state-backend appliance's OCI key is the one
row whose consumer is not a stack — `state-backend provision` builds the
backend the config secrets are stored behind — so it goes into a workstation
slot instead (`oci_slot.py`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ... import conventions
from . import b2, cloudflare, entries, oci_iam, oci_slot, pulumi_config
from .kdbx import KdbxStore
from .masters import CredentialRejected

log = logging.getLogger(__name__)

#: The name the zones token is minted under. Stable, because it is the name
#: retirement matches on: a re-run deletes the same-named predecessor once its
#: successor is verified, so one live token of this name is the invariant.
ZONES_TOKEN_NAME = 'kluster-zones'

#: The stack that manages the installation's DNS records, and therefore the slot
#: the zones token is delivered into.
ZONES_STACK = 'dns'

#: The name the gateway's ACME token is minted under, on the same rule as the
#: zones token: it is what retirement matches on, so one live token of this
#: name is the invariant a re-run restores.
GATEWAY_ACME_TOKEN_NAME = 'kluster-gateway-acme'

#: The zones the gateway may answer a DNS-01 challenge in, and the whole of its
#: token's scope.
#:
#: Two zones, because its caddy holds two wildcards. The controller console
#: and both resolver interfaces are names under the primary zone. The names it
#: still serves for applications that have not migrated are under
#: `lan.<short zone>`, and a challenge for that wildcard is written into the
#: short zone itself — so the scope is wider than the installation's end state
#: by exactly one zone, and **narrows back to the primary alone when
#: `conventions.gateway.LEGACY_VHOSTS` empties**, which the plan puts at the
#: end of Wave D. It stays deliberately narrower than the zones token's: the
#: gateway issues for itself, and a credential on a device the cluster cannot
#: re-seal carries no reach it does not use.
#:
#: Stated here rather than imported from `kluster.components.gateway`, which would drag
#: the Pulumi SDKs into `credentials --help`; a test holds the two equal
#: instead, so a vhost moved to another zone fails there.
GATEWAY_ACME_ZONES = (conventions.ZONE_PRIMARY, conventions.ZONE_SHORT)

#: Where the `dns` stack reads the Cloudflare provider's credential. Bare, and
#: therefore in this project's own namespace: `pulumi config set` prefixes an
#: unqualified key with the project's name, which is the same name
#: `pulumi.Config()` resolves against inside the program. Spelling that prefix
#: out here would be a second place for the project name to live, and the place
#: it could be wrong.
#:
#: The provider package's own `cloudflare:` namespace holds nothing. The stack
#: builds its provider from this value rather than being handed one by ambient
#: configuration (rfc-002 §8.1), so the key belongs to the program that reads
#: it and to no provider — and a key that did sit in the provider's namespace
#: would be indistinguishable from the ambient configuration this repository
#: has removed everywhere else.
#:
#: The account those zones live in is not delivered beside it: it names the
#: account rather than opening it, so it is `conventions.CLOUDFLARE_ACCOUNT`
#: and the mint proves the token it issues was minted there.
API_TOKEN_KEY = 'cloudflareApiToken'

#: Where the `physical` stack reads the gateway's ACME token before writing it
#: onto the device beside the nspawn unit it belongs to
#: (`components/gateway/container.py`).
#: Bare, and therefore in this project's namespace, for the reason the zones
#: token above is: nothing but this repository's own programs read it, and the
#: prefix `pulumi config set` applies is the one `pulumi.Config()` resolves
#: against.
GATEWAY_ACME_KEY = 'gatewayAcmeToken'

#: The stack that runs on the cloud account and the backup account and that
#: declares the gateway's services, and therefore the slot the OCI, B2 and
#: gateway-ACME credentials are delivered into.
#:
#: Not an argument, unlike the zones token's stack. What each of those rows
#: mints is named after the row -- one IAM user, one B2 key name, one
#: Cloudflare token -- and the mint retires every other credential of that
#: name, so a delivery aimed somewhere else would revoke this stack's live
#: credential on its way to filling another stack's slot. Identity is fixed,
#: so delivery is too.
PHYSICAL_STACK = conventions.PHYSICAL

#: Where the `physical` stack reads the OCI signing configuration. Bare, and
#: therefore in this project's namespace, for the reason the zones token above
#: is: the provider is built by the stack program from these values rather than
#: configured by an ambient namespace (rfc-002 §8.1), so the keys belong to the
#: program that reads them and to no provider.
#:
#: Neither the region, nor the tenancy, nor the compartment is among them. All
#: three are facts about the account rather than parts of the signing
#: configuration — the region and the tenancy OCID are permanent per account
#: and the compartment is a boundary this program decides — so all three live
#: in `conventions` and the stack reads them there.
OCI_USER_KEY = 'ociUserOcid'
OCI_FINGERPRINT_KEY = 'ociFingerprint'
OCI_PRIVATE_KEY_KEY = 'ociPrivateKey'

#: Where the backup bucket reads its account's key pair, in the same namespace
#: and for the same reason.
B2_KEY_ID_KEY = 'b2ApplicationKeyId'
B2_KEY_KEY = 'b2ApplicationKey'

#: The names these rows carry on the command line and in the slot map. One
#: string per row, defined here because this is where the mint lives: the map
#: imports them (`slots.py`), so a row cannot be spelled one way in the tree
#: and another way in the register's machine-readable half. Each is the
#: function below it with `-` where the identifier has `_`, which is the whole
#: of the convention.
ZONES_ROW = 'cloudflare-zones'
GATEWAY_ACME_ROW = 'cloudflare-gateway-acme'
OCI_PHYSICAL_ROW = 'oci-physical'
OCI_STATE_BACKEND_ROW = f'oci-{conventions.STATE_BACKEND}'
B2_MANAGEMENT_ROW = 'b2-management'

CLOUDFLARE_SEED_ENTRY = entries.SEEDS['cloudflare'].entry
OCI_SEED_ENTRY = entries.SEEDS['oci'].entry
B2_SEED_ENTRY = entries.SEEDS['b2'].entry


def cloudflare_zones(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = CLOUDFLARE_SEED_ENTRY) -> None:
    """Mint the zones token from the seed and install it in a stack's config.

    The scope is the installation's zones as `conventions` lists them, so adding
    a zone there and re-running is the whole procedure for widening it.

    The token is the whole of the delivery. Which account those zones live in
    is a fact this program already holds (`conventions.CLOUDFLARE_ACCOUNT`),
    and a fact with one home is not copied into a second — so what is left for
    the mint is to prove the token it just issued was minted in that account,
    and to refuse if it was not.
    """
    zones = conventions.ALL_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    minted = cloudflare.mint_zone_token(session, name=ZONES_TOKEN_NAME, zones=zones)
    _verify_account(minted.account_id)

    stack.fill(
        secret={API_TOKEN_KEY: minted.value},
        plain={},
        holds=f'a token scoped to {", ".join(zones)}',
    )


def _verify_account(account_id: str) -> None:
    """Hold a minted token's account against the one `conventions` records.

    The two ways the recorded fact goes stale are a kit re-seeded from another
    Cloudflare account and an identifier written down wrong, and both would
    deliver a token for zones the stack does not declare into while the stack
    keeps naming the account it does. Both are worth stopping over, and neither
    is visible once the token is in the slot.
    """
    intended = conventions.CLOUDFLARE_ACCOUNT.account_id
    if account_id != intended:
        raise CredentialRejected(
            f'the seed minted this token in the Cloudflare account {account_id}, but '
            f'`conventions.CLOUDFLARE_ACCOUNT` records {intended} as the account this installation declares '
            'into: one of the two is stale, and delivering the token would point the stack at zones it does '
            'not manage'
        )
    log.info('the minted token is scoped inside %s, which is the account `conventions` records', account_id)


def cloudflare_gateway_acme(
    kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = CLOUDFLARE_SEED_ENTRY
) -> str:
    """Mint the gateway's ACME token from the seed into a stack's config. Returns its id.

    The gateway buys the certificates for its own vhosts over a DNS-01
    challenge, with a credential separate from cert-manager's on purpose: two
    issuers that have to survive each other's outage do not share one, and the
    device holding this half is the one machine the cluster cannot re-seal. So
    this is a second token from the same seed, scoped to `GATEWAY_ACME_ZONES`
    and to nothing else.

    Only the token is delivered, and the account id the mint discovers on the
    way is not even checked here: the consumer is caddy, which signs with the
    token and never names an account. The zones row is where that identifier is
    held against the recorded one.

    Which stack takes it is not a choice, for the reason `PHYSICAL_STACK`
    states: the token is named after this row and the mint retires every other
    token of that name, so a delivery aimed elsewhere would revoke the
    gateway's live credential on its way to filling a different stack's slot.
    """
    zones = GATEWAY_ACME_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    minted = cloudflare.mint_zone_token(session, name=GATEWAY_ACME_TOKEN_NAME, zones=zones)

    stack.fill(
        secret={GATEWAY_ACME_KEY: minted.value},
        plain={},
        holds=f'{GATEWAY_ACME_TOKEN_NAME} ({minted.token_id}), scoped to {", ".join(zones)}',
    )
    return minted.token_id


def _push_api_key(stack: pulumi_config.Stack, key: oci_iam.ApiKey, *, holds: str) -> None:
    """Write one OCI signing configuration into a stack's committed config.

    All three are secrets. The key obviously is; the user OCID is the identity
    it signs as, which the kit itself keeps as a protected attribute (§2.1).
    The fingerprint is written here although §2.1 declines to store one,
    because the provider takes it as a separate input rather than deriving
    it — and it is computed from the key at push time, so the two cannot
    disagree.

    What the push does *not* write is which account the key acts in and where
    inside it, and that is three things: the tenancy OCID, the region and the
    compartment are all constants of this installation (`conventions`). A fact
    the program already holds is not something a credential delivery gets to
    restate — the mint proves the key it issues matches it instead
    (`oci_iam.verify_tenancy`).
    """
    stack.fill(
        secret={
            OCI_USER_KEY: key.user,
            OCI_FINGERPRINT_KEY: key.fingerprint,
            # The PEM goes in without its trailing newline: a push proves
            # itself by reading the value back through `pulumi config get`,
            # which is line-oriented, so a value ending in a newline can never
            # compare equal to what was written. The end marker is the end of a
            # PEM to every reader of one, this provider included.
            OCI_PRIVATE_KEY_KEY: key.private_key.strip(),
        },
        plain={},
        holds=holds,
    )


def oci_physical(
    kit: KdbxStore,
    *,
    stack: pulumi_config.Stack,
    compartment_id: str | None = None,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> str:
    """Mint the `physical` stack's OCI user and key into its config. Returns the user OCID.

    The compartment is not delivered with the credential: it is the boundary
    `conventions` gives this consumer, created here if it does not exist yet,
    and the stack reads it from the same place. `compartment_id` overrides that
    for a drill tenancy, where none of those names mean anything.

    Neither is the tenancy. `conventions` names the account this program
    declares into, so what is left for the mint is to prove that the key it
    just issued signs for that account and to refuse if it does not — which is
    the check that catches a seed swapped for another tenancy's before the key
    reaches a stack that would then act in the wrong account. A run given
    `compartment_id` is pointed at a drill tenancy and is not held to it, for
    the reason `oci_iam.ensure_compartment` is not.
    """
    minted = oci_iam.mint_api_key(
        kit, consumer=PHYSICAL_STACK, compartment_id=compartment_id, seed_entry=seed_entry, connect=connect
    )
    if compartment_id is None:
        oci_iam.verify_tenancy(minted.tenancy)

    _push_api_key(stack, minted, holds=f'a key for {oci_iam.Identity.name_for(PHYSICAL_STACK)}')
    return minted.user


def oci_state_backend(
    kit: KdbxStore,
    *,
    compartment_id: str | None = None,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> Path:
    """Mint the appliance's own OCI key into its workstation slot. Returns the slot's path.

    The one §3 OCI row that is not pushed to a stack: `state-backend provision`
    is workstation-only by design — bring-up and rebuild, never CI — and it
    runs before there is a Pulumi backend to hold a secret at all, so the slot
    is what a non-interactive reader can be pointed at (`oci_slot.py`).
    """
    minted = oci_iam.mint_api_key(
        kit,
        consumer=conventions.STATE_BACKEND,
        compartment_id=compartment_id,
        seed_entry=seed_entry,
        connect=connect,
    )

    written = oci_slot.write(minted)
    log.info(
        '`state-backend provision` signs as %s from now on, reading %s',
        oci_iam.Identity.name_for(conventions.STATE_BACKEND),
        written,
    )
    return written


def b2_management(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = B2_SEED_ENTRY) -> str:
    """Mint the B2 management key into a stack's config. Returns its key id.

    Bucket, key and lifecycle administration and no file capability at all
    (`b2.py`): the credential that manages the backup buckets cannot read a
    byte out of them. Both halves are secrets — the id is not the key, but it
    names the one key of that name the account holds, and the pair is one
    credential.
    """
    log.info('opening the B2 seed from the kit')
    minted = b2.mint_management(kit, seed_entry=seed_entry)

    stack.fill(
        secret={B2_KEY_ID_KEY: minted.key_id, B2_KEY_KEY: minted.key},
        plain={},
        holds=f'{b2.MANAGEMENT_KEY_NAME} ({minted.key_id})',
    )
    return minted.key_id
