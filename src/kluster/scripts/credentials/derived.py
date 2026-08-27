"""The derived credentials (docs/credentials.md §3): minted from a seed, pushed to a slot.

One function per register row, each of them mint -> push -> verify inside a
single run, and therefore idempotent: rotating a row is re-running it. Nothing
here ever writes to the kit — a derived credential in the offline store would
be the staging area §1 rule 2 forbids.

A row appears here when its consumer exists. The remaining Cloudflare rows —
the DNS-01 token for cert-manager and the gateway's ACME token — have nowhere
to be delivered yet, and a mint with no slot would be exactly the parked secret
the register rules out.

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

log = logging.getLogger(__name__)

#: The name the zones token is minted under. Stable, because it is the name
#: retirement matches on: a re-run deletes the same-named predecessor once its
#: successor is verified, so one live token of this name is the invariant.
ZONES_TOKEN_NAME = 'kluster-zones'

#: The stack that manages the estate's DNS records, and therefore the slot the
#: zones token is delivered into.
ZONES_STACK = 'dns'

#: Where the Cloudflare provider reads its credential, and where the program
#: reads the account that owns the zones. The provider's key is a secret; the
#: account id is an identifier the committed file may carry in plain text.
#:
#: The provider key is namespaced to the provider, as the provider requires.
#: The account id belongs to this project's own namespace, and is therefore
#: written bare: `pulumi config set` prefixes an unqualified key with the
#: project's name, which is the same name `pulumi.Config()` resolves against
#: inside the program. Spelling that prefix out here would be a second place
#: for the project name to live, and the place it could be wrong.
API_TOKEN_KEY = 'cloudflare:apiToken'
ACCOUNT_KEY = 'cloudflareAccountId'

#: The stack that runs on the cloud account and the backup account, and
#: therefore the slot the OCI and B2 credentials below are delivered into.
#:
#: Not an argument, unlike the zones token's stack. What each of those two
#: rows mints is named after the row -- one IAM user, one B2 key name -- and
#: the mint retires every other credential of that name, so a delivery aimed
#: somewhere else would revoke this stack's live credential on its way to
#: filling another stack's slot. Identity is fixed, so delivery is too.
PHYSICAL_STACK = 'physical'

#: Where the OCI provider reads its credential, namespaced to the provider as
#: the provider requires. The compartment is this project's own key rather
#: than the provider's, so it carries no namespace at all, for the reason
#: `cloudflareAccountId` carries none: `pulumi config set` and
#: `pulumi.Config()` resolve an unqualified key against the same project name,
#: which then lives in `Pulumi.yaml` alone. It is also the one key here whose
#: name a stack program already spells (`stacks/physical.py`).
OCI_TENANCY_KEY = 'oci:tenancyOcid'
OCI_USER_KEY = 'oci:userOcid'
OCI_FINGERPRINT_KEY = 'oci:fingerprint'
OCI_PRIVATE_KEY_KEY = 'oci:privateKey'
OCI_REGION_KEY = 'oci:region'
COMPARTMENT_KEY = 'compartmentId'

#: Where the B2 provider reads its credential.
B2_KEY_ID_KEY = 'b2:applicationKeyId'
B2_KEY_KEY = 'b2:applicationKey'

CLOUDFLARE_SEED_ENTRY = entries.SEEDS['cloudflare'].entry
OCI_SEED_ENTRY = entries.SEEDS['oci'].entry
B2_SEED_ENTRY = entries.SEEDS['b2'].entry


def cloudflare_zones(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = CLOUDFLARE_SEED_ENTRY) -> str:
    """Mint the zones token from the seed and install it in a stack's config.

    The scope is the estate's zones as `conventions` lists them, so adding a
    zone there and re-running is the whole procedure for widening it. Returns
    the account id it discovered, which the same push writes beside the token.
    """
    zones = conventions.ALL_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    minted = cloudflare.mint_zone_token(session, name=ZONES_TOKEN_NAME, zones=zones)

    stack.ensure()
    stack.set_secret(API_TOKEN_KEY, minted.value)
    stack.set(ACCOUNT_KEY, minted.account_id)
    log.info(
        'the %s stack holds a token scoped to %s; commit Pulumi.%s.yaml to publish the slot',
        stack.name,
        ', '.join(zones),
        stack.name,
    )
    return minted.account_id


def _push_api_key(stack: pulumi_config.Stack, key: oci_iam.ApiKey, *, compartment_id: str) -> None:
    """Write one OCI signing configuration into a stack's committed config.

    The four keys the provider signs with are secrets. The key obviously is;
    the tenancy and user OCIDs are account identifiers, which the kit itself
    keeps as protected attributes (§2.1) and a public repository has no reason
    to publish. The fingerprint is written here although §2.1 declines to store
    one, because the provider takes it as a separate input rather than deriving
    it — and it is computed from the key at push time, so the two cannot
    disagree.

    The other two are plain, and each for its own reason. The region is a
    constant of this estate (`conventions`).

    The compartment is plain because the program reads it plain:
    `stacks/physical.py` takes it with `require`, and a value pushed as a
    secret and read that way agrees only by way of an upstream defect
    (pulumi/pulumi#7127) — the two sides have to say the same thing. Reading
    it with `require_secret` instead would type-check, every component here
    taking an `Input[str]`, but secretness propagates: the compartment field
    of every resource in the stack would become encrypted state and every
    preview line naming one would read `[secret]`. That is a poor trade for an
    identifier that names a container inside the tenancy rather than the
    account that owns it.
    """
    stack.ensure()
    stack.set_secret(OCI_TENANCY_KEY, key.tenancy)
    stack.set_secret(OCI_USER_KEY, key.user)
    stack.set_secret(OCI_FINGERPRINT_KEY, key.fingerprint)
    # The PEM goes in without its trailing newline: a push proves itself by
    # reading the value back through `pulumi config get`, which is
    # line-oriented, so a value ending in a newline can never compare equal to
    # what was written. The end marker is the end of a PEM to every reader of
    # one, this provider included.
    stack.set_secret(OCI_PRIVATE_KEY_KEY, key.private_key.strip())
    stack.set(COMPARTMENT_KEY, compartment_id)
    stack.set(OCI_REGION_KEY, key.region)


def oci_physical(
    kit: KdbxStore,
    *,
    stack: pulumi_config.Stack,
    compartment_id: str,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> str:
    """Mint the `physical` stack's OCI user and key into its config. Returns the user OCID.

    The compartment is pushed beside the credential because a signing
    configuration does not say where the stack may act, and the two are one
    decision: the user is an administrator of that compartment and a stranger
    outside it, so a key delivered without it would be a credential the stack
    cannot aim.
    """
    identity = oci_iam.Identity.for_consumer(PHYSICAL_STACK, compartment_id=compartment_id)
    minted = oci_iam.mint_api_key(kit, identity=identity, seed_entry=seed_entry, connect=connect)

    _push_api_key(stack, minted, compartment_id=compartment_id)
    log.info(
        'the %s stack holds a key for %s; commit Pulumi.%s.yaml to publish the slot',
        stack.name,
        identity.name,
        stack.name,
    )
    return minted.user


def oci_state_backend(
    kit: KdbxStore,
    *,
    compartment_id: str,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> Path:
    """Mint the appliance's own OCI key into its workstation slot. Returns the slot's path.

    The one §3 OCI row that is not pushed to a stack: `state-backend provision`
    is workstation-only by design — bring-up and rebuild, never CI — and it
    runs before there is a Pulumi backend to hold a secret at all, so the slot
    is what a non-interactive reader can be pointed at (`oci_slot.py`).
    """
    identity = oci_iam.Identity.for_consumer(conventions.STATE_BACKEND, compartment_id=compartment_id)
    minted = oci_iam.mint_api_key(kit, identity=identity, seed_entry=seed_entry, connect=connect)

    written = oci_slot.write(minted, compartment_id=compartment_id)
    log.info('`state-backend provision` signs as %s from now on, reading %s', identity.name, written)
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
    key_id, key = b2.mint_management(kit, seed_entry=seed_entry)

    stack.ensure()
    stack.set_secret(B2_KEY_ID_KEY, key_id)
    stack.set_secret(B2_KEY_KEY, key)
    log.info(
        'the %s stack holds %s (%s); commit Pulumi.%s.yaml to publish the slot',
        stack.name,
        b2.MANAGEMENT_KEY_NAME,
        key_id,
        stack.name,
    )
    return key_id
