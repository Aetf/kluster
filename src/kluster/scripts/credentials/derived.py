"""The derived credentials (docs/credentials.md §3): minted from a seed, pushed to a slot.

One function per register row, each of them account check -> mint -> push ->
verify -> retire inside a single run, and therefore idempotent: rotating a row
is re-running it. The account check is there where `conventions` records that
platform's account, which `docs/credentials.md` §4 states along with the rest
of the shape. Nothing here ever writes to the kit — a derived credential in the
offline store would be the staging area §1 rule 2 forbids.

**No row spells the last two steps in the right order, because no row spells
them at all.** A mint returns a `Delivery` (`delivery.py`), whose `deliver`
pushes and only then retires, and which keeps the credential behind that call —
so reaching for the value directly is a type error rather than a shortcut. What
that buys, and what a failed push costs instead, is the register's to say.

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

One row is drawn here rather than minted anywhere: the drill age identity,
whose consumer is the ops repository's rebuild drill. It is `generate` in the
command tree because no provider issues it, and it is not an escrow label
because every dump it opens is also encrypted to an escrowed generation
(credentials.md §2.2) — so its private half goes to the ops-repo Environment
through the same GitHub sink every other GitHub secret takes, and its public
half to a committed file the appliance's recipient list appends.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ... import conventions
from ..state_backend import config as appliance
from . import age, b2, cloudflare, entries, oci_iam, oci_slot, pulumi_config
from .github_secrets import Forge, Slot
from .kdbx import KdbxStore

log = logging.getLogger(__name__)

#: The stack that manages the installation's DNS records, and therefore the slot
#: the zones token is delivered into.
ZONES_STACK = 'dns'

#: The stack that declares the forge itself, and therefore the slot the GitHub
#: admin token is delivered into. Nothing here mints that token -- it is made by
#: hand in the GitHub UI (`devices.py`) -- but the stack it is delivered to is
#: named beside the others, because a stack name spelled in two modules is a
#: stack name that can drift. Taken from `pulumi_config`, which is where the
#: name has to be anyway: that module carries the census of stacks encrypted
#: apart from the estate passphrase, and this is the one on it.
GITHUB_STACK = pulumi_config.GITHUB_STACK

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
DRILL_AGE_IDENTITY_ROW = 'drill-age-identity'

#: Where the drill age identity's private half lands: the ops repository's
#: `drill` Environment, read by the rebuild drill and by nothing else
#: (ci.md §3). One value, imported by the slot map, so the sink this generator
#: pushes to and the target the register advertises cannot be two addresses.
#:
#: The name carries the Environment as a prefix. Inside a job an Environment
#: secret shadows a repository secret of the same name, and the ops
#: repository is to hold both kinds (credentials.md §3), so an ops-repo
#: Environment secret is named `<ENVIRONMENT>_<what>`; a test holds every
#: such slot in the map to it.
DRILL_AGE_IDENTITY_SLOT = Slot(
    repository=conventions.forge.OPS.full_name, name='DRILL_AGE_IDENTITY', environment=conventions.forge.DRILL.name
)

CLOUDFLARE_SEED_ENTRY = entries.SEEDS['cloudflare'].entry
OCI_SEED_ENTRY = entries.SEEDS['oci'].entry
B2_SEED_ENTRY = entries.SEEDS['b2'].entry


def _deliverable(stack: pulumi_config.Stack, *, own: str) -> None:
    """Refuse a delivery aimed anywhere but a stack of this project that is there to take it.

    The one row that takes a stack by name is the one row that can be aimed
    wrong, and the mint retires every other token of the row's name once the
    new one is verified: aimed at a misspelling, it would create that stack in
    the backend, fill it, and revoke the live credential of the stack that
    reads it. So the name has to be one the project declares
    (`pulumi_config.STACKS`), and any stack but the row's own has to exist
    already -- a mint fills configuration, and creating a stack is that
    stack's own bring-up rather than a side effect of delivering a token to
    it. The row's own stack is the exception because its first mint *is* its
    bring-up (credentials.md §4.1): nothing else creates it, so the push
    cannot assume it exists.

    Before the kit is opened and before anything is minted, so a refusal here
    leaves no token live at the provider and no stack in the backend.
    """
    if stack.name not in pulumi_config.STACKS:
        raise pulumi_config.SlotRefused(
            f'{stack.name!r} is no stack of this project, so nothing would read a token delivered there; '
            f'the stacks are {", ".join(sorted(pulumi_config.STACKS))}'
        )
    if stack.name != own and not stack.exists():
        raise pulumi_config.SlotRefused(
            f'the {stack.name} stack does not exist in the state backend, and a mint creates no stack but '
            f"this row's own ({own}): a delivery aimed elsewhere fills a stack that is already there, and "
            f'bringing {stack.name} into being is its own bring-up'
        )


def cloudflare_zones(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = CLOUDFLARE_SEED_ENTRY) -> None:
    """Mint the zones token from the seed and install it in a stack's config.

    The scope is the installation's zones as `conventions` lists them, so adding
    a zone there and re-running is the whole procedure for widening it.

    The token is the whole of the delivery. Which account those zones live in
    is a fact this program already holds (`conventions.CLOUDFLARE_ACCOUNT`),
    and a fact with one home is not copied into a second — so the mint proves
    the account it is about to issue into is that one, and refuses before
    anything exists if it is not (`cloudflare.verify_account`).

    Which stack takes it is the caller's, within limits `_deliverable` holds:
    a stack of this project, and one that exists unless it is `ZONES_STACK`.
    """
    _deliverable(stack, own=ZONES_STACK)
    zones = conventions.ALL_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    pending = cloudflare.mint_zone_token(session, role=cloudflare.ZONES, zones=zones)

    _ = pending.deliver(
        lambda token: stack.fill(
            secret={API_TOKEN_KEY: token.value},
            plain={},
            holds=f'a token scoped to {", ".join(zones)}',
        )
    )


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

    Only the token is delivered: the consumer is caddy, which signs with the
    token and never names an account. The account is still proven, because
    every mint holds the account it is about to issue into against
    `conventions.CLOUDFLARE_ACCOUNT` before it creates anything
    (`cloudflare.verify_account`) — a check that ran per row would be one a
    second row could forget, and forgetting it here would put a live token in
    a foreign account that nothing records and nobody revokes.

    Which stack takes it is not a choice, for the reason `PHYSICAL_STACK`
    states: the token is named after this row and the mint retires every other
    token of that name, so a delivery aimed elsewhere would revoke the
    gateway's live credential on its way to filling a different stack's slot.
    """
    zones = GATEWAY_ACME_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    pending = cloudflare.mint_zone_token(session, role=cloudflare.GATEWAY_ACME, zones=zones)

    delivered, _ = pending.deliver(
        lambda token: stack.fill(
            secret={GATEWAY_ACME_KEY: token.value},
            plain={},
            holds=f'{cloudflare.GATEWAY_ACME.name} ({token.token_id}), scoped to {", ".join(zones)}',
        )
    )
    return delivered.token_id


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
    declares into, so the mint proves the seed it opened belongs to that
    account and refuses before it creates anything if it does not
    (`oci_iam.verify_tenancy`) — which is the check that catches a seed swapped
    for another tenancy's, while catching it still costs nothing. A run given
    `compartment_id` is pointed at a drill tenancy and is not held to it, for
    the reason `oci_iam.ensure_compartment` is not.
    """
    pending = oci_iam.mint_api_key(
        kit, consumer=PHYSICAL_STACK, compartment_id=compartment_id, seed_entry=seed_entry, connect=connect
    )

    delivered, _ = pending.deliver(
        lambda key: _push_api_key(stack, key, holds=f'a key for {oci_iam.Identity.name_for(PHYSICAL_STACK)}')
    )
    return delivered.user


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

    The account is held against `conventions.OCI_TENANCY` here as it is for the
    row above, because the mint holds it rather than the row does: this key
    provisions the appliance the whole installation's state lives on, and one
    minted in the wrong tenancy would build that appliance in an account
    nothing here manages. A run given `compartment_id` is pointed at a drill
    tenancy and is not held to it, exactly as the row above is not.
    """
    pending = oci_iam.mint_api_key(
        kit,
        consumer=conventions.STATE_BACKEND,
        compartment_id=compartment_id,
        seed_entry=seed_entry,
        connect=connect,
    )

    _, written = pending.deliver(oci_slot.write)
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
    pending = b2.mint_management(kit, seed_entry=seed_entry)

    delivered, _ = pending.deliver(
        lambda key: stack.fill(
            secret={B2_KEY_ID_KEY: key.key_id, B2_KEY_KEY: key.key},
            plain={},
            holds=f'{b2.MANAGEMENT.name} ({key.key_id})',
        )
    )
    return delivered.key_id


def drill_age_identity(forge: Forge, *, recipient_file: Path, rotate: bool) -> str:
    """Draw the drill age identity: private half to the ops-repo Environment, public half to a file.

    Returns the recipient written. The identity exists in this process, then
    on `gh`'s standard input, then in the Environment: no file, no kit row,
    no escrow ciphertext ever holds the private half, and `age.Identity`
    keeps it out of every repr. Losing the Environment secret costs a
    `--rotate` and the converge that follows, never a byte of data, because
    every dump it opens is also encrypted to an escrowed generation.

    **The recipient on file is what refuses a second generation.** The row is
    no escrow label, so nothing counts its generations; the committed public
    half is the one durable trace of a key already in service, and drawing
    over it would leave the appliance encrypting to a key the Environment
    no longer holds until the next converge. `rotate` says that is the
    intent -- and is refused when there is nothing on file to rotate, since
    an operator who believes a key exists where none does is about to skip
    the converge that installs it.

    **Push before write, because the file is the durable half.** An
    Environment secret with no recipient on file costs a re-run; a committed
    recipient whose private half never landed is a dump encrypted to a key
    nobody holds -- harmless to the data, and a drill failure found a
    quarter later. The push is verified through the listing as every GitHub
    push is (`slots.py`), and a push that does not show there ends the run
    with the file untouched.

    What follows the write is the operator's: commit the file, then
    `state-backend provision --force` -- the recipient list is digested, so
    the plain converge reports the drift and stops -- and `restore` of the
    dump that run takes. The first object the drill can open is the first
    dump written after that replace.
    """
    on_file = appliance.drill_recipient(recipient_file)
    if on_file is not None and not rotate:
        raise pulumi_config.SlotRefused(
            f'{recipient_file} already names a drill recipient, so a drill key is in service; '
            '`--rotate` draws its successor, and the sequence it starts is: commit the file, '
            '`state-backend provision --force`, `state-backend restore` of the dump that run takes, '
            'then a fresh dump for the drill to open'
        )
    if on_file is None and rotate:
        raise pulumi_config.SlotRefused(
            f'--rotate, but {recipient_file} names no drill recipient to rotate; the first generation is drawn '
            'without it'
        )
    slot = DRILL_AGE_IDENTITY_SLOT
    log.info('drawing the drill age identity with %s', age.KEYGEN)
    identity = age.generate()
    # The secret line alone, as `age-keygen` prints it minus its comment
    # lines and with no trailing newline: a GitHub secret is stored exactly
    # as it is piped in, and the drill writes the value to a file it hands
    # `state-backend restore --identity-file`, which reads it line by line.
    log.info('pushing the private half to %s (gh encrypts it on the way out)', slot)
    forge.put(slot, identity.secret)
    if slot.name not in forge.listing(slot):
        raise pulumi_config.SlotRefused(
            f'{slot}: pushed, but the secret listing does not show it; {recipient_file} is untouched'
        )
    log.info('%s holds the private half; writing the public half to %s', slot, recipient_file)
    _ = recipient_file.write_text(
        '# The drill age identity, public half (docs/credentials.md §3). The private\n'
        f'# half is the {slot}; nothing on disk holds it.\n'
        f'# Written by `credentials derived {DRILL_AGE_IDENTITY_ROW} generate`; replaced by `--rotate`.\n'
        f'{identity.public}\n'
    )
    log.info('commit %s; the appliance encrypts to it from the next converge on:', recipient_file)
    log.info('    state-backend provision --force    # dumps under the new recipients, replaces, names the file')
    log.info('    state-backend restore <that file>')
    return identity.public
