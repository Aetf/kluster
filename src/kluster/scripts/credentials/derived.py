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

A row appears here when it has a slot to be delivered into: a mint with no
slot would be exactly the parked secret the register rules out.

**A row that mints into a stack finds that stack's passphrase and backend
first**: the environment finds a passphrase only when a stack asks for it,
and a mint that went first would leave its credential live at the provider
when the passphrase is then refused, or when the fill finds no backend to
write to (`_stack_in_reach`; a seal's declaring stack, and `physical` for the
certificate, are held to the same before they are read).

Which slot a row is pushed into follows from what consumes it. A stack's
credential goes into that stack's committed configuration, where the program
reads it before it can run -- the state-backend appliance's OCI key included,
which the `state-backend` stack builds its OCI provider from. A credential the
cluster consumes is sealed to the cluster instead and its ciphertext written
in the clear into the configuration of the stack that declares it
(`sealing.py`): the DNS-01 token cert-manager answers challenges with, the
OCI key the block-volume metrics pull reads with (`oci_metrics`), and
the backup bucket's writer keys, which `physical` mints and this side seals
out of that stack's state into `apps`, or into `k8s-base` for a namespace
the platform holds and for the etcd snapshots (`b2_writer_seal`), and the
etcd snapshots' read-only key the same way into `k8s-base`
(`b2_reader_seal`); Authelia's
storage encryption key, which is escrowed and sealed from the escrow
(`authelia_storage_key_seal`); the client bundle the state table's daily
upkeep reaches the state backend with, issued from the escrowed CA
(`state_backend_upkeep`); the state dumps' freshness key, which its mint
also pushes to the ops repository (`b2_freshness_dumps`); and the etcd
restore-verify's age identity, drawn here (below).

The drill age identity is drawn here rather than minted anywhere, as the
appliance's SSH host key below is; its consumer is the ops repository's
rebuild drill. It is `generate` in the
command tree because no provider issues it, and it is not an escrow label
because every dump it opens is also encrypted to an escrowed generation
(credentials.md §2.2) — so its private half goes to the ops-repo Environment
through the same GitHub sink every other GitHub secret takes, and its public
half to a committed file the appliance's recipient list appends.

The etcd restore-verify's age identity is drawn here too, and is the drill
identity's counterpart for the cluster: its own key, so that neither drill
opens what the other's contract leaves out (credentials.md §1 rule 4). Its
consumer is in the cluster, so its private half is sealed into `k8s-base`
rather than pushed to a forge, and its public half goes to a committed file
for the etcd snapshots to be encrypted to, by a read of that file which
lands after it (`etcd_drill_age_identity`; credentials.md §1 rule 6). One run
writes both, and reaches `physical` for the cluster's certificate and
`k8s-base` for the write, each under its passphrase's workstation slot, and
nothing in the kit.

The drill's other row is one command over two mints: the OCI key that
administers the drill compartment and the B2 key that reads the dump prefix
are minted by the same two shapes the `physical` rows use, and every carrier
lands in that same Environment through the sink. One row, because the census
keys map rows by the command that produces them; two mints, because the two
platforms' seeds are two kit entries and a failure on one must leave the
other's predecessor live.

**The `state-backend` stack's rows fill the configuration its program reads.**
The stack keeps its state committed (framework/pulumi.md §3.3) and its
program is rfc-006 §4's, so its rows are its providers' credentials and the
stable keys that program renders the box from: its own OCI key and B2
management key, minted like `physical`'s under names of their own; the server
key and certificate, issued from the escrowed CA; and the SSH host key, drawn
here, whose public half is committed beside the Butane template. Each refuses before anything is minted while the stack has
no checkpoint, since the stack's first `stack init` goes through the
`operator-stack` driver and nothing here creates it. Beside them, a backup
generation's `generate` writes that generation's public half into the
committed recipients file, which is how the box's recipients stop needing the
kit (`backup_age_recipient`).

The freshness key's row is the B2 half of that on its own, narrowed to a
listing, and it has two consumers, each reading the dump prefix's age: the
ops repository's probe, which takes it as a **repository** secret rather than
from the `drill` Environment, because its job belongs to no Environment; and
the cluster's dump-age check, which takes it sealed, as every credential the
cluster consumes is, and which lands after the ciphertext (credentials.md §1
rule 6). One mint delivers both, and retires the key it replaces
only once both hold the new one (`b2_freshness_dumps`). The value's whole
power is a listing of names. The names the ops repository's copy is
delivered under are the probe's own (`state_backend.probe`), imported here,
so the slot the mint fills and the variable the probe reads cannot drift.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pulumi.runtime import rpc

from kluster.conventions.backup import ETCD_DRILL_RECIPIENT
from kluster.lib import bundle, stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.lib.state_backend import committed
from kluster.lib.state_backend import settings as appliance_settings

from ... import conventions
from ..state_backend import config as appliance
from ..state_backend import probe
from . import age, b2, cloudflare, entries, escrow, oci_iam, pki, pulumi_config, sealing
from .github_secrets import Forge, Slot
from .kdbx import KdbxStore

log = logging.getLogger(__name__)


class NoZoneServed(pulumi_config.SlotRefused):
    """The route census serves no zone, so a token scoped to the served zones would be scoped to nothing."""


class BucketMissing(pulumi_config.SlotRefused):
    """A B2 bucket a row is confined to that the account does not hold."""

    def __init__(self, message: str, *, bucket: str) -> None:
        super().__init__(message)
        self.bucket: str = bucket


#: The stack that manages the installation's DNS records: the zones token's own
#: stack, which the row's first mint creates. The token is delivered into every
#: stack the slot map's zones row names, and this is the one of them that may
#: not exist yet.
ZONES_STACK = conventions.STACK_NAMES.dns

#: The stack that declares the forge itself, and therefore the slot the GitHub
#: admin token is delivered into. Nothing here mints that token -- it is made by
#: hand in the GitHub UI (`devices.py`) -- but the stack it is delivered to is
#: named here beside the others, so every row that delivers into a stack reads
#: it from one module, under the role that stack plays for the row.
GITHUB_STACK = conventions.STACK_NAMES.github

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
PHYSICAL_STACK = conventions.STACK_NAMES.physical

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

#: The operator stack that declares the state-backend appliance, and therefore
#: the slot its own B2 management key, its server key and certificate and its
#: SSH host key are delivered into. Fixed, for the reason `PHYSICAL_STACK` is.
STATE_BACKEND_STACK = conventions.STACK_NAMES.state_backend

#: Where the `state-backend` stack reads the server's TLS key, its certificate
#: and the CA's certificate, and the box's SSH host key. Bare, in this
#: project's namespace, for the reason the zones token's key is. The key and
#: the host key are secrets; the two certificates are public, and sit in the
#: clear so that a reviewer reads what a reissue changed (rfc-006 §5).
SERVER_KEY_KEY = 'serverKey'
SERVER_CERT_KEY = 'serverCertificate'
CA_CERT_KEY = 'caCertificate'
HOST_KEY_KEY = 'sshHostKey'

#: The files committed beside the Butane template that the rows below write,
#: where the stack reads them (`kluster.lib.state_backend.committed`, which
#: holds their readers). The box's SSH host key, public half: what
#: `state-backend ssh` pins, and what a reader compares a fingerprint against
#: without the passphrase.
HOST_KEY_FILE = committed.HOST_KEY
#: The backup generations' public halves, one line per generation the box
#: encrypts its dumps to: the escrow label, then the recipient.
BACKUP_RECIPIENTS_FILE = committed.BACKUP_RECIPIENTS
#: The etcd restore-verify's age identity, public half, committed for the
#: cluster's etcd snapshots to be encrypted to; its row is
#: `ETCD_DRILL_AGE_IDENTITY_ROW`.
ETCD_DRILL_RECIPIENT_FILE = ETCD_DRILL_RECIPIENT

#: The names these rows carry on the command line and in the slot map. One
#: string per row, defined here because this is where the mint lives: the map
#: imports them (`slots.py`), so a row cannot be spelled one way in the tree
#: and another way in the register's machine-readable half. Each is the
#: function below it with `-` where the identifier has `_`, which is the whole
#: of the convention.
ZONES_ROW = 'cloudflare-zones'
GATEWAY_ACME_ROW = 'cloudflare-gateway-acme'
DNS01_ROW = 'cloudflare-dns01'
B2_WRITER_ROW = 'b2-writer'
B2_READER_ROW = 'b2-reader'
OCI_PHYSICAL_ROW = 'oci-physical'
OCI_STATE_BACKEND_ROW = f'oci-{conventions.STATE_BACKEND}'
OCI_METRICS_ROW = 'oci-metrics'
B2_MANAGEMENT_ROW = 'b2-management'
DRILL_AGE_IDENTITY_ROW = f'{conventions.DRILL}-age-identity'
DRILL_CREDENTIALS_ROW = f'{conventions.DRILL}-credentials'
ETCD_DRILL_AGE_IDENTITY_ROW = f'{conventions.sealed.ETCD_DRILL_IDENTITY.name}-age-identity'
B2_FRESHNESS_DUMPS_ROW = 'b2-freshness-dumps'
B2_STATE_BACKEND_MANAGEMENT_ROW = f'b2-{conventions.STATE_BACKEND}-management'
STATE_BACKEND_SERVER_ROW = f'{conventions.STATE_BACKEND}-server'
STATE_BACKEND_HOST_KEY_ROW = f'{conventions.STATE_BACKEND}-host-key'
STATE_BACKEND_UPKEEP_ROW = f'{conventions.STATE_BACKEND}-upkeep'
#: The escrowed row whose value the cluster consumes, sealed from the escrow
#: by the two functions named after it (`authelia_storage_key_import`,
#: `authelia_storage_key_seal`). Its name is its escrow label's
#: (`escrow.row_name`), as every escrowed row's is.
AUTHELIA_STORAGE_KEY_ROW = escrow.row_name(escrow.AUTHELIA_STORAGE_KEY)


def _drill_slot(name: str) -> Slot:
    """One secret in the ops repository's `drill` Environment, read by the rebuild drill and by nothing else (ci.md §3).

    The name carries the Environment as a prefix. Inside a job an Environment
    secret shadows a repository secret of the same name, and the ops
    repository is to hold both kinds (credentials.md §3), so an ops-repo
    Environment secret is named `<ENVIRONMENT>_<what>`; a test holds every
    such slot in the map to it.
    """
    return Slot(repository=conventions.forge.OPS.full_name, name=name, environment=conventions.forge.DRILL.name)


#: Where the drill age identity's private half lands. One value, imported by
#: the slot map, so the sink the generator pushes to and the target the
#: register advertises cannot be two addresses; the five below are the same
#: arrangement for the drill's OCI and B2 keys.
DRILL_AGE_IDENTITY_SLOT = _drill_slot('DRILL_AGE_IDENTITY')

#: The drill's OCI signing configuration, the three carriers `_push_api_key`
#: writes for a stack, as Environment secrets: the user OCID the key signs
#: as, the fingerprint computed from the key at push time so the two cannot
#: disagree, and the PEM. The tenancy, the region and the compartment are not
#: among them, for the reason they are not config keys either: all three are
#: `conventions`, and the workflow that assembles a signing configuration out
#: of these reads them from the checkout it pins.
DRILL_OCI_USER_SLOT = _drill_slot('DRILL_OCI_USER_OCID')
DRILL_OCI_FINGERPRINT_SLOT = _drill_slot('DRILL_OCI_FINGERPRINT')
DRILL_OCI_PRIVATE_KEY_SLOT = _drill_slot('DRILL_OCI_PRIVATE_KEY')
DRILL_OCI_SLOTS = (DRILL_OCI_USER_SLOT, DRILL_OCI_FINGERPRINT_SLOT, DRILL_OCI_PRIVATE_KEY_SLOT)

#: The drill's B2 key, both halves: the id names the one key of that name the
#: account holds, and the pair is one credential.
DRILL_B2_KEY_ID_SLOT = _drill_slot('DRILL_B2_KEY_ID')
DRILL_B2_KEY_SLOT = _drill_slot('DRILL_B2_KEY')
DRILL_B2_SLOTS = (DRILL_B2_KEY_ID_SLOT, DRILL_B2_KEY_SLOT)

#: Where the ops repository's copy of the freshness key lands: two repository
#: secrets, no Environment, under the names its probe reads them by. The pair
#: is one credential, as the drill's B2 pair is. The cluster's copy is sealed
#: (`conventions.sealed.B2_FRESHNESS_DUMPS`).
B2_FRESHNESS_DUMPS_SLOTS = (
    Slot(repository=conventions.forge.OPS.full_name, name=probe.KEY_ID_ENV),
    Slot(repository=conventions.forge.OPS.full_name, name=probe.KEY_ENV),
)

#: The two halves of the drill's credentials, as `--only` names them: the
#: platform each key is minted at.
DRILL_OCI_HALF = 'oci'
DRILL_B2_HALF = 'b2'
DRILL_HALVES = (DRILL_OCI_HALF, DRILL_B2_HALF)

CLOUDFLARE_SEED_ENTRY = entries.SEEDS['cloudflare'].entry
OCI_SEED_ENTRY = entries.SEEDS['oci'].entry
B2_SEED_ENTRY = entries.SEEDS['b2'].entry


def _stack_in_reach(stack: pulumi_config.Stack) -> None:
    """Find the passphrase and the backend `stack` is opened with, refusing as the stack itself would, before a mint.

    The environment looks for a stack's passphrase only when that stack is
    first asked for (`lifecycle.environment`), so whatever it refuses for that
    stack is refused there: today, a workstation slot that cannot be read, a
    kit with no recovery key, a label with nothing filed under it, or a
    machine that cannot give the operator passphrase. For a row that mints
    and then writes its stack, that is after the mint, with the new
    credential live at its provider and in no slot. Asked first, every such
    refusal fires before anything is minted, and a successful run finds the
    passphrase once: the environment keeps what it found for the run. The
    backend is held the same way (`pulumi_config.Stack.require_backend`): on
    a machine with no client bundle a stack whose state is in the state
    backend has none, and every `pulumi` run against it is refused, the
    fill's included, which comes after the mint.

    A passphrase found is not yet one that opens the stack: a workstation
    slot written before a rotation filed on another workstation gives the
    earlier generation, which the fill would meet as `incorrect passphrase`.
    So the passphrase is held to the stack file's salt as well
    (`pulumi_config.Stack.require_passphrase`), which asks the backend
    whether it holds the stack and nothing of a stack it does not, so a stack
    the row's first mint creates is not refused for being absent.
    """
    log.info('finding the passphrase and the backend the %s stack is opened with', stack.name)
    stack.require_backend()
    stack.require_passphrase()


def _deliverable(stack: pulumi_config.Stack, *, own: str) -> None:
    """Refuse a delivery aimed anywhere but a stack of this project that is there to take it.

    The zones row is the one row delivered into more than one stack, and the
    mint retires every other token of the row's name once the new one is in
    all of them: a stack that cannot take it would fail the run part-way, and
    one created by mistake would be filled with a credential nothing reads. So
    the name has to be one the project declares (`conventions.STACK_NAMES`),
    and any stack but the row's own has to exist already -- a mint fills
    configuration, and creating a stack is that stack's own bring-up rather
    than a side effect of delivering a token to it. The row's own stack is the
    exception because its first mint *is* its bring-up (credentials.md §4.1):
    nothing else creates it, so the push cannot assume it exists.

    Each stack's passphrase and backend are found here too
    (`_stack_in_reach`), the row's own stack's included. All of it runs before
    the seed is read and before anything is minted, so a refusal here leaves
    no token live at the provider and no stack in the backend.
    """
    if stack.name not in conventions.STACK_NAMES.names():
        raise pulumi_config.SlotRefused(
            f'{stack.name!r} is no stack of this project, so nothing would read a token delivered there; '
            f'the stacks are {", ".join(sorted(conventions.STACK_NAMES.names()))}'
        )
    _stack_in_reach(stack)
    if stack.name != own and not stack.exists():
        raise pulumi_config.StackMissing(
            f'the {stack.name} stack does not exist in the state backend, and a mint creates no stack but '
            f"this row's own ({own}): a delivery fills a stack that is already there, and bringing "
            f'{stack.name} into being is its own bring-up',
            stack=stack.name,
        )


def cloudflare_zones(
    kit: KdbxStore, *, stacks: Sequence[pulumi_config.Stack], seed_entry: str = CLOUDFLARE_SEED_ENTRY
) -> None:
    """Mint the zones token from the seed and install it in every stack that reads it.

    The scope is the installation's zones as `conventions` lists them, so adding
    a zone there and re-running is the whole procedure for widening it.

    The token is the whole of the delivery. Which account those zones live in
    is a fact this program already holds (`conventions.CLOUDFLARE_ACCOUNT`),
    and a fact with one home is not copied into a second — so the mint proves
    the account it is about to issue into is that one, and refuses before
    anything exists if it is not (`cloudflare.verify_account`).

    **One token, every stack, one retirement.** `stacks` are the zones row's
    slots (the slot map's `PulumiConfig` targets, which the command builds them
    from), and the one minted token is written into each of them before the
    token it supersedes is retired. Retirement matches on the token's name, so
    a run per stack would revoke the first stack's token while filling the
    second; and a write that fails part-way retires nothing, leaving every
    stack it had not reached on the token that still works. Each stack is held
    to `_deliverable` before anything is minted.
    """
    if not stacks:
        raise pulumi_config.SlotRefused(f'the {ZONES_ROW} row names no stack to deliver its token into')
    for stack in stacks:
        _deliverable(stack, own=ZONES_STACK)
    zones = conventions.ALL_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    pending = cloudflare.mint_zone_token(session, role=cloudflare.ZONES, zones=zones)

    def fill_every_stack(token: cloudflare.ZoneToken) -> None:
        for stack in stacks:
            stack.fill(
                secret={API_TOKEN_KEY: token.value},
                plain={},
                holds=f'a token scoped to {", ".join(zones)}',
            )

    _ = pending.deliver(fill_every_stack)


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
    The stack's passphrase and backend are found before the seed is read
    (`_stack_in_reach`).
    """
    _stack_in_reach(stack)
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


def cloudflare_dns01(kit: KdbxStore, *, sealer: sealing.Sealer, seed_entry: str = CLOUDFLARE_SEED_ENTRY) -> str:
    """Mint cert-manager's DNS-01 token from the seed and seal it into the stack that declares it. Returns its id.

    The scope is the zones the cluster serves (`conventions.routes.served_zones`),
    the same derivation `k8s-base` declares one certificate per zone from, so
    a route in a new zone widens the token on the next run of this command and
    the two never disagree about which zones are served. Cloudflare scopes a
    token to zones and not to records within one, so the token carries the
    permissions of the gateway's own (`cloudflare.DNS01`) on those zones.

    The delivery is a seal (`sealing.py`): the token is sealed strict to the
    cluster's certificate under the name and namespace its row in
    `conventions.sealed` gives it, and the ciphertext written in the clear at
    the path the row derives, in the stack the row names. A third token from
    the same seed, apart from the gateway's on purpose: two issuers that have
    to survive each other's outage do not share a credential.

    **Every refusal the run can know before minting fires before anything is
    minted**: a scope with no zone in it, a declaring stack that does not
    exist or that this machine has no backend to reach, and -- by the time
    this is called -- a cluster whose certificate could not be fetched, which
    is what building `sealer` does. The account
    is held to `conventions.CLOUDFLARE_ACCOUNT` before the token is created,
    as for every Cloudflare row.
    """
    value = conventions.sealed.DNS01_TOKEN
    zones = conventions.routes.served_zones()
    if not zones:
        raise NoZoneServed(
            f'the cluster serves no zone, so the {DNS01_ROW} token would be scoped to nothing; '
            'the primary zone is served by definition, so the route census or its derivation is broken'
        )
    _ = sealer.stack(value)
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    pending = cloudflare.mint_zone_token(session, role=cloudflare.DNS01, zones=zones)

    (key,) = value.keys
    delivered, _ = pending.deliver(lambda token: sealer.deliver(value, {key: token.value}))
    log.info(
        'cert-manager answers DNS-01 challenges in %s as %s from the next apply', ', '.join(zones), delivered.token_id
    )
    return delivered.token_id


class BucketKeyMissing(pulumi_config.SlotRefused):
    """A scope whose backup bucket key `physical`'s state does not hold, or holds in no usable shape."""

    def __init__(self, message: str, *, scope: str) -> None:
        super().__init__(message)
        self.scope: str = scope


#: Which half of a minted B2 key each field of a sealed bucket key carries:
#: the fields are a mover's (`conventions.WRITER_KEY_FIELDS`), the halves are
#: the names `physical` exports each key under. A field the row does not
#: carry is refused by the seal itself, so the two cannot drift apart unseen.
WRITER_KEY_HALVES: dict[str, str] = {'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'secret'}


def _bucket_key(keys: object, scope: str, *, physical: str) -> dict[str, str]:
    """One scope's key out of `physical`'s `backup_keys` export, by field; `BucketKeyMissing` where it is not there.

    Refused by its scope and the half that is wrong, never by its content.
    """
    output = conventions.PHYSICAL_OUTPUTS.backup_keys
    held = cast('dict[str, object]', keys).get(scope) if isinstance(keys, dict) else None
    if not isinstance(held, dict):
        raise BucketKeyMissing(
            f'the `{physical}` stack exports no key for `{scope}` in `{output}`; '
            f"`{physical}`'s apply after the scope is declared mints it",
            scope=scope,
        )
    halves = cast('dict[str, object]', held)
    fields: dict[str, str] = {}
    for field, half in WRITER_KEY_HALVES.items():
        value = halves.get(half)
        if value == rpc.UNKNOWN:
            raise pulumi_config.UnknownOutput(
                f"the `{physical}` stack exports the {half} of `{scope}` as Pulumi's unknown sentinel, which an "
                f'apply restricted by `--target` leaves behind; apply `{physical}` again with no targets',
                stack=physical,
                output=output,
            )
        if not isinstance(value, str) or not value:
            raise BucketKeyMissing(
                f'the `{physical}` stack exports the {half} of `{scope}` as no key half', scope=scope
            )
        fields[field] = value
    return fields


class UnknownBucketKey(ValueError):
    """A sealed value a bucket key's `seal --only` was asked for, which is no key that command seals."""

    def __init__(self, name: str, *, row: str, values: Mapping[str, conventions.sealed.SealedValue]) -> None:
        super().__init__(
            f'{name!r} is no key `{row} seal` seals; it seals {", ".join(value.name for value in values.values())}'
        )
        self.name: str = name
        self.row: str = row


def _seal_bucket_keys(
    values: Mapping[str, conventions.sealed.SealedValue], *, row: str, sealer: sealing.Sealer, only: str | None
) -> list[str]:
    """Seal the backup bucket keys `values` names, by scope, out of `physical`'s state; returns what was written.

    The one mechanism behind `b2_writer_seal` and `b2_reader_seal`, which
    differ in the rows they seal and nothing else. `only` names one row's
    sealed value, and a name of none refuses (`UnknownBucketKey`) before
    anything is read. Each declaring stack is held to exist, then `physical`
    is opened through `sealer` and every key read out of its `backup_keys`
    export before any is sealed, so a key `physical` does not export yet
    refuses the run before anything is written (`BucketKeyMissing`).
    """
    rows = [(scope, value) for scope, value in values.items() if only is None or value.name == only]
    if only is not None and not rows:
        raise UnknownBucketKey(only, row=row, values=values)
    # Each declaring stack once, before `physical` is read.
    for value in {value.stack: value for _, value in rows}.values():
        _ = sealer.stack(value)
    physical = sealer.open_stack(PHYSICAL_STACK)
    log.info("reading the backup bucket's keys for `%s` out of the %s stack's state", row, physical.name)
    keys = physical.outputs().get(conventions.PHYSICAL_OUTPUTS.backup_keys)
    held = [(value, _bucket_key(keys, scope, physical=physical.name)) for scope, value in rows]
    sealed = [(value, sealer.seal(value, fields)) for value, fields in held]
    for value, ciphertext in sealed:
        sealer.write(value, ciphertext)
    for stack in sorted({value.stack for value, _ in sealed}):
        log.info(
            'sealed %d backup bucket keys into the %s stack; commit its file, and the Secrets it declares carry '
            'their key from its next apply',
            sum(1 for value, _ in sealed if value.stack == stack),
            stack,
        )
    return [value.name for value, _ in sealed]


def b2_writer_seal(*, sealer: sealing.Sealer, only: str | None = None) -> list[str]:
    """Seal the backup bucket's writer keys out of `physical`'s state, each into its consumers' stack.

    Returns the names of the sealed values written: every row of
    `conventions.sealed.WRITER_KEYS_BY_SCOPE`, or the one named `only`, which a name of none of them
    refuses (`UnknownBucketKey`) before anything is read.

    `physical` mints each key from the B2 management key, one per scope on the
    backup bucket, and exports them under `backup_keys` in its state, which
    opens under its own passphrase (credentials.md §1). A StackReference
    cannot carry them across, and a copy as a configuration secret of the
    stack that reads it would reach every pull request's preview; so each is
    sealed, the shape `derived sync` has for the kubeconfig with a seal in
    place of the copy. Each key is written into the stack its row names,
    which declares what reads it: `apps` for an application's namespace,
    `k8s-base` for the platform's (`conventions.sealed.WRITER_KEY_STACKS`).
    Each is sealed **namespace-wide** (`conventions.sealed.writer_key`): one
    ciphertext, which every Secret of its namespace that carries it carries
    under its own name.

    `physical` is opened through `sealer`, by its name, so it is read under
    its own passphrase. Its state exists by the time this runs: building
    `sealer` read the cluster's kubeconfig out of it, and refuses a stack
    with none (`sealing.cluster_kubeconfig`).

    The etcd snapshots' writer key is the exception to both: one Secret reads it,
    so it is sealed **strict** to that Secret's name, and its consumer is the
    platform's, so it goes into `k8s-base` (`conventions.sealed.ETCD_WRITER_KEY`).
    `only` seals one key alone, which is how this one is sealed at the
    sitting that seals the cluster's own values (credentials.md §4.1); a run
    without it seals it again with the rest, which changes nothing it reads.

    Every refusal comes before anything is written: a cluster whose
    certificate could not be fetched, which building `sealer` does; a
    declaring stack that does not exist; and a scope whose key `physical` does not
    export yet, which a census row added since `physical`'s last apply is,
    or exports with a half missing or empty.
    Every run seals afresh, so it rewrites every value whatever the file
    held (`sealing.py`).
    """
    return _seal_bucket_keys(conventions.sealed.WRITER_KEYS_BY_SCOPE, row=B2_WRITER_ROW, sealer=sealer, only=only)


def b2_reader_seal(*, sealer: sealing.Sealer, only: str | None = None) -> list[str]:
    """Seal the backup bucket's read-only keys out of `physical`'s state, each strict into `k8s-base`.

    Returns the names of the sealed values written: every row of
    `conventions.sealed.READER_KEYS_BY_SCOPE`, or the one named `only`, which
    a name of none of them refuses (`UnknownBucketKey`) before anything is
    read. Today that is the etcd snapshots' reader, `reader-etcd`, which the
    restore-verify lists and downloads the newest snapshot with.

    The mechanism is `b2_writer_seal`'s: `physical` mints each key with the
    bucket and exports it under `backup_keys`, which opens under that stack's
    own passphrase, and nothing here opens the kit. A key that only reads is
    sealed apart from the writer keys because it is not one, and a command or
    a row map named for writers would say otherwise. Each is sealed strict,
    since one Secret reads it under one name, into the stack its row names.

    Every refusal comes before anything is written: a cluster whose
    certificate could not be fetched, which building `sealer` does; a
    declaring stack that does not exist; and a scope whose key `physical`
    does not export yet, which a reader declared since `physical`'s last
    apply is (`BucketKeyMissing`). Every run seals afresh.
    """
    return _seal_bucket_keys(conventions.sealed.READER_KEYS_BY_SCOPE, row=B2_READER_ROW, sealer=sealer, only=only)


#: Where the legacy cluster holds Authelia's storage key, and the pipe that
#: carries it from there into `import` without a file or a terminal between.
AUTHELIA_STORAGE_KEY_SOURCE = f"""The legacy cluster holds it: the Secret authelia in serving-system, under
  STORAGE_ENCRYPTION_KEY, generated there. Piped straight from it, so it
  touches no file and no terminal:
    kubectl --context <legacy> -n serving-system get secret authelia \\
      -o jsonpath='{{.data.STORAGE_ENCRYPTION_KEY}}' | base64 -d \\
      | credentials derived {AUTHELIA_STORAGE_KEY_ROW} import
  Once, at a kit sitting before Wave A1's head, while the legacy cluster still
  holds it. The value is escrowed without the whitespace around it, as every
  piped value is; Wave A1's sign-in with a second factor against the migrated
  database is what proves the escrowed key is the one Authelia encrypted with."""


def _seal_storage_key(vault: escrow.Vault, *, sealer: sealing.Sealer) -> int:
    """Seal the newest escrowed generation of Authelia's storage key into `apps`; returns that generation.

    What is sealed is what the registry returns, read back out of the
    ciphertext, so the Secret the server reads and the value a recovery opens
    are one value by construction rather than by two writes agreeing.
    """
    label = escrow.AUTHELIA_STORAGE_KEY
    value = conventions.sealed.AUTHELIA_STORAGE_KEY
    generation = vault.registry.latest(label)
    log.info('opening generation %d of %s with the kit', generation, label)
    (key,) = value.keys
    sealer.deliver(value, {key: vault.recover(label, generation)})
    return generation


def authelia_storage_key_announce() -> None:
    """Print where Authelia's storage key comes from, and the pipe that carries it, before it is asked for.

    Always, and ahead of the read, as `escrow.announce` and
    `devices.record_sealed` print their steps: a run started bare to learn
    the pipe is then answered before it waits on standard input, and a run
    already fed by the pipe shows the operator what it was fed by.
    """
    log.warning(
        '%s is carried from the legacy cluster, which generated it; it comes from here:', escrow.AUTHELIA_STORAGE_KEY
    )
    for line in AUTHELIA_STORAGE_KEY_SOURCE.splitlines():
        log.warning('  %s', line)


def authelia_storage_key_import(vault: escrow.Vault, secret: str, *, sealer: sealing.Sealer) -> int:
    """Escrow the storage key a predecessor hands over, then seal what the registry holds; returns its generation.

    The import credentials.md §2.2 prescribes for a value that predates the
    model: the key becomes the label's next generation as it stands, checked
    against what Authelia accepts before anything is written
    (`escrow.AUTHELIA_KEY`). The declaring stack is held to exist first, so a
    machine that could not write the ciphertext refuses before anything is
    escrowed; the cluster's certificate was fetched in building `sealer`.

    A seal that fails after the escrow leaves the key filed, and the repair is
    `seal`, which seals that generation: a second `import` of the same key would
    file it again as the generation after it. Where the key comes from is
    printed before it is read (`authelia_storage_key_announce`).
    """
    label = escrow.AUTHELIA_STORAGE_KEY
    _ = sealer.stack(conventions.sealed.AUTHELIA_STORAGE_KEY)
    path = escrow.adopt(vault, label, secret)
    log.info('escrowed %s in %s; commit it with the stack file', label, path)
    try:
        return _seal_storage_key(vault, sealer=sealer)
    except pulumi_config.SlotRefused as exc:
        raise pulumi_config.SlotRefused(
            f'{exc}; {label} is escrowed in {path}, so the repair is '
            f'`credentials derived {AUTHELIA_STORAGE_KEY_ROW} seal`, which seals it, rather than a second '
            '`import`, which would file the same key again as the next generation'
        ) from exc


def authelia_storage_key_seal(vault: escrow.Vault, *, sealer: sealing.Sealer) -> int:
    """Seal the newest escrowed generation of Authelia's storage key into `apps`; returns that generation.

    The whole of the recovery after a rebuild: the escrow holds the key, and
    the new cluster's controller seals it afresh. It is also the adoption of a
    generation `generate` drew, on an installation whose Authelia has no
    database yet; a generation drawn over a database already encrypted is one
    Authelia opens only after the database is re-keyed to it, which nothing
    here does. Refused by name, with nothing written, while the escrow holds
    no generation.
    """
    label = escrow.AUTHELIA_STORAGE_KEY
    if not vault.registry.generations(label):
        raise escrow.EscrowRefused(
            f'nothing escrowed for {label}, so there is nothing to seal: '
            f'`credentials derived {AUTHELIA_STORAGE_KEY_ROW} import` takes the key the legacy cluster holds, and '
            f'`credentials derived {AUTHELIA_STORAGE_KEY_ROW} generate` draws one for an Authelia with no database yet',
            refusal=escrow.Refusal.NOTHING_ESCROWED,
            label=label,
        )
    _ = sealer.stack(conventions.sealed.AUTHELIA_STORAGE_KEY)
    return _seal_storage_key(vault, sealer=sealer)


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

    The stack's passphrase and backend are found before the seed is read
    (`_stack_in_reach`).
    """
    _stack_in_reach(stack)
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
    stack: pulumi_config.Stack,
    compartment_id: str | None = None,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> str:
    """Mint the appliance's own OCI user and key into the `state-backend` stack's config. Returns the user OCID.

    The shape `oci_physical` delivers, confined to the compartment
    `conventions` names for the appliance: the stack program builds its OCI
    provider from these three values (rfc-006 §4.1). Refused before anything
    is minted while the stack has no checkpoint (`_require_initialized`) or
    while this machine cannot give its passphrase (`_stack_in_reach`).

    The account is held against `conventions.OCI_TENANCY` here as it is for the
    row above: this key builds the appliance the whole installation's state
    lives on, and one minted in the wrong tenancy would build that appliance
    in an account nothing here manages. A run given `compartment_id` is
    pointed at a drill tenancy and is not held to it, exactly as the row above
    is not.
    """
    _require_initialized(stack)
    _stack_in_reach(stack)
    pending = oci_iam.mint_api_key(
        kit,
        consumer=conventions.STATE_BACKEND,
        compartment_id=compartment_id,
        seed_entry=seed_entry,
        connect=connect,
    )

    delivered, _ = pending.deliver(
        lambda key: _push_api_key(stack, key, holds=f'a key for {oci_iam.Identity.name_for(conventions.STATE_BACKEND)}')
    )
    return delivered.user


def _reader_data(key: oci_iam.ApiKey) -> dict[str, str]:
    """Which part of a minted key each data key of the sealed reader carries.

    A data key the census row does not name is refused by the seal itself
    (`sealing.Sealer.seal`), so the two cannot drift apart unseen.
    """
    return {'user': key.user, 'fingerprint': key.fingerprint, 'key_content': key.private_key}


def oci_metrics(
    kit: KdbxStore,
    *,
    sealer: sealing.Sealer,
    seed_entry: str = OCI_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> str:
    """Mint the metrics reader's OCI user and key, and seal them for the pull. Returns the user OCID.

    The key reads the block-volume metrics of `physical`'s compartment and
    nothing else (`oci_iam.mint_metrics_reader`). Its consumer is in the
    cluster, so the delivery is a seal, as `cloudflare_dns01`'s is: the user,
    the fingerprint and the key sealed strict for the Secret the census row
    names (`conventions.sealed.OCI_METRICS`), in the monitoring namespace,
    and the ciphertext written in the clear into `k8s-base`'s configuration.
    The tenancy, the region and the compartment are not sealed with it:
    each is a constant `conventions` names.

    **Every refusal the run can know before minting fires before anything is
    minted**: a declaring stack that does not exist or that this machine has
    no backend to reach, and -- by the time this is called -- a cluster whose
    certificate could not be fetched, which is what building `sealer` does;
    then a compartment `conventions` records no OCID for, and a seed in an
    account other than the one it records. A live key on the reader's user is
    retired only once the ciphertext is written, so a run that fails leaves
    the working key alone.
    """
    value = conventions.sealed.OCI_METRICS
    _ = sealer.stack(value)
    pending = oci_iam.mint_metrics_reader(kit, seed_entry=seed_entry, connect=connect)

    delivered, _ = pending.deliver(lambda key: sealer.deliver(value, _reader_data(key)))
    log.info(
        'the block-volume metrics pull reads as %s from the apply that hands it the sealed %s',
        delivered.user,
        value.name,
    )
    return delivered.user


def b2_management(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = B2_SEED_ENTRY) -> str:
    """Mint the B2 management key into a stack's config. Returns its key id.

    Bucket, key and lifecycle administration and no file capability at all
    (`b2.py`): the credential that manages the backup buckets cannot read a
    byte out of them. Both halves are secrets — the id is not the key, but it
    names the one key of that name the account holds, and the pair is one
    credential. The stack's passphrase and backend are found before the seed
    is read (`_stack_in_reach`).
    """
    _stack_in_reach(stack)
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


# --------------------------------------------------------------------------
# The `state-backend` stack's configuration, and the files committed beside
# the Butane template.
# --------------------------------------------------------------------------


def _require_initialized(stack: pulumi_config.Stack) -> None:
    """Refuse a delivery into the `state-backend` stack before its first checkpoint exists.

    The stack keeps its state committed, so its existence is a file of the
    checkout rather than an answer from a backend, and it is created by a
    `stack init` through the driver, which checks the working copy first and
    leaves the checkpoint for the operator to land (framework/pulumi.md
    §3.3). A `pulumi config set` here would otherwise meet no stack, or,
    through `Stack.fill`, create one past the driver's checks. Before anything
    is minted, so a refusal leaves nothing live at a provider.
    """
    if stack_environment.checkpoint(stack.directory, stack.name) is None:
        raise pulumi_config.SlotRefused(
            f'the {stack.name} stack has no checkpoint in {stack.directory / stack_environment.CHECKPOINTS}, so '
            f'there is no stack to fill: `operator-stack {stack.name} pulumi stack init` creates it, and its '
            'checkpoint is landed like any change before its configuration is'
        )


def _fill_state_backend(
    stack: pulumi_config.Stack, *, secret: dict[str, str], plain: dict[str, str], holds: str
) -> None:
    """`Stack.fill` for the `state-backend` stack, refused before its first checkpoint (`_require_initialized`)."""
    _require_initialized(stack)
    stack.fill(secret=secret, plain=plain, holds=holds)


def b2_state_backend_management(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = B2_SEED_ENTRY) -> str:
    """Mint the `state-backend` stack's own B2 management key into its config. Returns its key id.

    The shape `b2_management` delivers to `physical`, under a role of its own
    (`b2.STATE_BACKEND_MANAGEMENT`): retirement matches a role's name, so one
    name for both would have each mint revoke the other stack's key. The
    stack declares the dump bucket and the dump key with it (rfc-006 §4.1).
    Its checkpoint and its passphrase are asked for before the seed is read,
    as for the appliance's OCI key.
    """
    _require_initialized(stack)
    _stack_in_reach(stack)
    log.info('opening the B2 seed from the kit')
    pending = b2.mint_management(kit, seed_entry=seed_entry, role=b2.STATE_BACKEND_MANAGEMENT)

    delivered, _ = pending.deliver(
        lambda key: _fill_state_backend(
            stack,
            secret={B2_KEY_ID_KEY: key.key_id, B2_KEY_KEY: key.key},
            plain={},
            holds=f'{b2.STATE_BACKEND_MANAGEMENT.name} ({key.key_id})',
        )
    )
    return delivered.key_id


def state_backend_server(
    vault: escrow.Vault, *, stack: pulumi_config.Stack, now: dt.datetime | None = None
) -> pki.Credential:
    """Issue the appliance's server key and certificate into the `state-backend` stack's config. Returns them.

    For the address the box is reached at (`settings.ADDRESS`), which is the
    certificate's name and its only one (`pki.issue_server`). The key is a
    secret; the certificate and the CA's certificate, which the box hands its
    clients, are public and written in the clear. One issuance, both halves:
    a second call would mint a second key.

    The CA stays in the escrow, which is why issuing needs the kit and nothing
    else does (rfc-006 §5). Nothing is retired: the box authenticates to
    clients by the CA, and the certificate it serves stays valid until the
    replacement that carries this one.
    """
    _require_initialized(stack)
    log.info('recovering the state-backend CA from the escrow')
    authority = pki.Authority.from_pem(vault.recover(escrow.CA))
    server = authority.issue_server(appliance_settings.ADDRESS, now=now)
    ca = authority.certificate(now=now)
    expires = pki.not_valid_after(server.cert_pem)
    _fill_state_backend(
        stack,
        secret={SERVER_KEY_KEY: server.key_pem.decode().strip()},
        plain={SERVER_CERT_KEY: server.cert_pem.decode().strip(), CA_CERT_KEY: ca.cert_pem.decode().strip()},
        holds=f'a server certificate for {appliance_settings.ADDRESS}, valid until {expires.date().isoformat()}',
    )
    log.info('the box serves it from the replacement that carries it')
    return server


def upkeep_bundle(authority: pki.Authority, *, address: str) -> dict[str, str]:
    """A fresh client bundle for the role `upkeep` at `address`, by the file each part is in a bundle directory.

    The parts `appliance.write_client_bundle` writes for a workstation's
    bundle, under `kluster.lib.bundle`'s file names, each as that function
    writes it: the URL with its newline, and each PEM whole. A directory
    holding them is a bundle `bundle.read` and `bundle.ssl_env` read. Every
    call issues a new key (`appliance.client_bundle`).
    """
    issued = appliance.client_bundle(authority, name=appliance_settings.UPKEEP_ROLE, address=address)
    return {
        bundle.URL_FILE: f'{issued.url()}\n',
        bundle.CA_FILE: issued.ca_cert.decode(),
        bundle.CERT_FILE: issued.cert.decode(),
        bundle.KEY_FILE: issued.key.decode(),
    }


def state_backend_upkeep(open_vault: Callable[[], escrow.Vault], *, sealer: sealing.Sealer) -> dt.datetime:
    """Issue a fresh `upkeep` client bundle from the escrowed CA and seal it into `k8s-base`. Returns its leaf's expiry.

    For the address the box is reached at (`settings.ADDRESS`), which the
    bundle's connection string names. Its consumer is the state table's
    daily upkeep in the cluster, so the delivery is a seal, strict, for the
    Secret the census row names (`conventions.sealed.STATE_UPKEEP_BUNDLE`),
    whose data keys are the bundle's file names; the seal refuses a part the
    row does not carry.

    No other slot holds the leaf, so a fresh seal is the rotation. Nothing is
    retired: the box authenticates the CA rather than a leaf, so the leaf
    sealed before stays valid until it expires. The escrow is opened by
    `open_vault`, which reads the kit, only once the declaring stack is held
    to exist; the cluster's certificate was fetched in building `sealer`.
    So a refusal of either costs no kit prompt.
    """
    value = conventions.sealed.STATE_UPKEEP_BUNDLE
    _ = sealer.stack(value)
    log.info('recovering the state-backend CA from the escrow')
    authority = pki.Authority.from_pem(open_vault().recover(escrow.CA))
    parts = upkeep_bundle(authority, address=appliance_settings.ADDRESS)
    sealer.deliver(value, parts)
    expires = pki.not_valid_after(parts[bundle.CERT_FILE].encode())
    log.info(
        "the state table's upkeep reads as `%s` from the apply that hands it the sealed %s; its leaf is valid until %s",
        appliance_settings.UPKEEP_ROLE,
        value.name,
        expires.date().isoformat(),
    )
    return expires


def committed_target(path: Path) -> Path:
    """`path`, refused unless it is in the checkout this package runs from.

    A file a command writes for the operator to commit is written only by a
    package running from the checkout it belongs to: an installed one
    resolves the path inside that installation, where no commit picks the
    file up, while the value it wrote beside the file lands for real.
    """
    root = lib_workstation.repo_root()
    if not path.resolve().is_relative_to(root.resolve()):
        raise lib_workstation.WorkstationError(
            f'{path} is not in the checkout at {root}: it is a file to commit, so it is written only by a '
            'package running from the checkout it belongs to'
        )
    return path


def state_backend_host_key(*, stack: pulumi_config.Stack, public_file: Path) -> str:
    """Draw the box's SSH host key: the private half into the stack's config, the public half into `public_file`.

    Returns the public line. Ed25519, which is the one algorithm the client
    is told to accept (`state-backend ssh`). Drawn here rather than on the box, so
    the key is stable across replacements and the pin is known before a box
    boots with it; no provider issues it and nothing escrows it, since a lost
    one costs a fresh draw and a replacement (rfc-006 §5).

    **Config before file.** The configuration is written and read back
    first, and the public half is derived from what was written: a file that
    named a key the configuration does not hold would pin a box nobody can
    reach, where a configuration whose file was not written yet is repaired
    by running this again. Re-running is the rotation, which the box adopts at
    its next replacement.
    """
    _require_initialized(stack)
    private = (
        Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
        .strip()
    )
    public = committed.public_host_key(private)
    _fill_state_backend(stack, secret={HOST_KEY_KEY: private}, plain={}, holds='an ed25519 SSH host key')
    _ = public_file.write_text(
        "# The state-backend appliance's SSH host key, public half (docs/credentials.md §3). The\n"
        f'# private half is `{HOST_KEY_KEY}` in Pulumi.{stack.name}.yaml, under the operator passphrase.\n'
        f'# Written by `credentials derived {STATE_BACKEND_HOST_KEY_ROW} generate`; replaced by running it again.\n'
        f'{public}\n'
    )
    log.info(
        'commit %s and Pulumi.%s.yaml together; the box carries the key from its next replacement',
        public_file,
        stack.name,
    )
    return public


def _tool_check(value: str, name: str) -> None:
    """The pinned `age`'s parse of one recipient (`age.check_recipient`), refusing as an `AgeError`."""
    age.check_recipient(value, name=name)


def backup_recipients(path: Path) -> dict[str, str]:
    """The recipients file as label → recipient, each recipient one the pinned `age` parses; empty while no file exists.

    The grammar is the file's reader (`committed.backup_recipients`); what
    this adds is the tool's own parse of every recipient, which a script can
    ask and the stack does not (`age.check_recipient`), and the writer's
    reading of an absent file as nothing on file yet. A refusal is an
    `age.AgeError`, which is what `check` prints, and it names a line by its
    number and never quotes it.
    """
    if not path.is_file():
        return {}
    try:
        return committed.backup_recipients(path, check=_tool_check)
    except committed.Refused as exc:
        raise age.AgeError(str(exc)) from None


def _write_backup_recipients(path: Path, recipients: dict[str, str]) -> None:
    lines = ''.join(
        f'{label} {recipients[label]}\n' for label in sorted(recipients, key=lambda label: int(label.rsplit('/', 1)[1]))
    )
    _ = path.write_text(
        "# The age backup identities' public halves (docs/credentials.md §3): one line per\n"
        '# escrowed generation the appliance encrypts its dumps to, the escrow label and then\n'
        '# the recipient. Written by `credentials derived backup-age-<N> generate`;\n'
        '# `credentials derived check` holds the labels to the escrow.\n'
        f'{lines}'
    )


def backup_age_recipient(vault: escrow.Vault, label: str, *, recipients_file: Path) -> str:
    """Put one backup generation's public half in the recipients file, escrowing the generation first where it is not. Returns the recipient.

    A generation the escrow does not hold yet is drawn and escrowed
    (`escrow.generate`); one it holds is recovered, which is how the file is
    written once for the generations escrowed before it existed: a backup
    label holds one identity for its lifetime (`escrow.Label.single`), so
    there is nothing to draw. Either way the recipient is computed from the
    escrowed identity by the tool, so the file cannot name a key the escrow
    does not hold.

    The file keeps the generations the box encrypts to (`escrow.backup_labels`)
    and no other: a generation the window has left is dropped, and a line
    that disagrees with the escrow is replaced, with a warning, since the
    escrow is what every dump is opened with.
    """
    window = escrow.backup_labels()
    if label not in window:
        raise escrow.EscrowError(
            f'{label} is not a generation the appliance encrypts to; the window is {", ".join(window)}'
        )
    # The file is read before anything is drawn, so a line that refuses
    # stops the run with the escrow as it was.
    on_file = backup_recipients(recipients_file)
    if vault.registry.generations(label):
        log.info('%s is escrowed already; recovering it for its public half', label)
        secret = vault.recover(label)
    else:
        secret = escrow.generate(vault, label)
    public = age.recipient(secret)

    kept = {held: value for held, value in on_file.items() if held in window}
    for dropped in sorted(set(on_file) - set(kept)):
        log.info('%s has left the window; dropping its line from %s', dropped, recipients_file)
    if kept.get(label) == public and kept == on_file:
        log.info('%s already names %s for %s', recipients_file, public, label)
        return public
    if label in kept and kept[label] != public:
        log.warning('%s named another recipient for %s than the escrow holds; replacing it', recipients_file, label)
    kept[label] = public
    _write_backup_recipients(recipients_file, kept)
    log.info('%s names %s for %s; commit it', recipients_file, public, label)
    return public


def host_key_problems(path: Path) -> list[str]:
    """What is wrong with the committed host key's public half. No kit, no key.

    The file holds one `ssh-ed25519` line (`committed.host_key`); an absent
    one is a problem, since the `state-backend` stack refuses to plan without
    it and `state-backend ssh` pins the box to it. Whether it is the public
    half of the key in the stack's configuration is what only the operator
    passphrase can answer; the stack's component holds the two to each other
    before it plans (`refuse_out_of_step`).
    """
    try:
        _ = committed.host_key(path)
    except committed.Refused as exc:
        return [str(exc)]
    return []


def backup_recipients_problems(registry: escrow.Registry, path: Path) -> list[str]:
    """What is wrong with the recipients file, held to the escrow. No kit, no key.

    The file names exactly the generations of the box's window the escrow
    holds, each once, each a native recipient. Whether each recipient is the
    public half of the identity escrowed under its label is what only the kit
    can answer; `backup_age_recipient` computes it from that identity when it
    writes. An absent file is a problem too: the `state-backend` stack renders
    the box's recipients from it and refuses to plan without it
    (`committed.age_recipients`).
    """
    if not path.is_file():
        rows = ', '.join(f'`credentials derived {escrow.row_name(label)} generate`' for label in escrow.backup_labels())
        return [f'no {path}: the state-backend stack renders the box from it; {rows} writes it, and it is committed']
    try:
        on_file = backup_recipients(path)
    except age.AgeMissing:
        raise
    except age.AgeError as exc:
        return [str(exc)]
    escrowed = [label for label in escrow.backup_labels() if registry.generations(label)]
    problems = [
        f'{path}: names no recipient for {label}, which the escrow holds; '
        f'`credentials derived {escrow.row_name(label)} generate` writes it'
        for label in escrowed
        if label not in on_file
    ]
    problems.extend(
        f'{path}: names a recipient for {label}, which the escrow does not hold in the window the box encrypts to'
        for label in sorted(set(on_file) - set(escrowed))
    )
    return problems


def _push(forge: Forge, slot: Slot, value: str) -> None:
    """Push one GitHub secret through the sink and prove it landed, as far as the channel can.

    The value is never readable again, so what is checked is what the map's
    sink checks (`slots.py`): the name is in the listing afterwards and its
    timestamp moved. The name separates a push that happened from one that
    was refused, which is the failure guarded against; on a rotation the name
    was there before, and the timestamp is what says this run's push is the
    one listed -- two pushes inside one second share one, which is worth a
    word rather than a failure. A push that does not show there ends the
    run, and what that leaves behind is the caller's to say.
    """
    before = forge.listing(slot).get(slot.name)
    log.info('pushing %s (gh encrypts it on the way out)', slot)
    forge.put(slot, value)
    after = forge.listing(slot).get(slot.name)
    if after is None:
        raise pulumi_config.SlotRefused(f'{slot}: pushed, but the secret listing does not show it')
    if before is not None and after == before:
        log.warning('%s: the listing still reads %s - a re-push inside one second looks like this', slot, after)
    else:
        log.info('%s: updated %s', slot, after)


def drill_age_identity(forge: Forge, *, recipient_file: Path, rotate: bool) -> str:
    """Draw the drill age identity: private half to the ops-repo Environment, public half to a file.

    Returns the recipient written. The identity exists in this process, then
    on `gh`'s standard input, then in the Environment: no file, no kit row,
    no escrow ciphertext ever holds the private half, and `age.Identity`
    keeps it out of every repr. Losing the Environment secret costs a
    `--rotate` and the replacement that follows, never a byte of data, because
    every dump it opens is also encrypted to an escrowed generation.

    **The recipient on file is what refuses a second generation.** The row is
    no escrow label, so nothing counts its generations; the committed public
    half is the one durable trace of a key already in service, and drawing
    over it would leave the appliance encrypting to a key the Environment
    no longer holds until the next replacement. `rotate` says that is the
    intent -- and is refused when there is nothing on file to rotate, since
    an operator who believes a key exists where none does is about to skip
    the replacement that installs it.

    **`rotate` asks only whether the file is there, and never reads it.** A
    recipient on file that `appliance.drill_recipient` refuses -- a hand edit
    or a truncated write, a second line -- is repaired by drawing a
    successor over it, since the public half cannot be recomputed from an
    Environment secret nobody can read back. Without `rotate` the file is
    read, and such a refusal names `--rotate` as the repair.

    **Push before write, because the file is the durable half.** An
    Environment secret with no recipient on file costs a re-run; a committed
    recipient whose private half never landed is a dump encrypted to a key
    nobody holds -- harmless to the data, and a drill failure found a
    quarter later. The push is verified through the listing as every GitHub
    push is (`slots.py`), and a push that does not show there ends the run
    with the file untouched.

    What follows the write is the operator's: commit the file, then
    `operator-stack state-backend up --force` -- the recipient list is part
    of the box's bill of materials, so the plain run names the replacement
    and stops -- which dumps the box, replaces it and restores into the new
    one. The first object the drill can open is the first dump written after
    that replacement.
    """
    if rotate:
        if not recipient_file.is_file():
            raise pulumi_config.SlotRefused(
                f'--rotate, but {recipient_file} names no drill recipient to rotate; the first generation is '
                'drawn without it'
            )
    else:
        try:
            on_file = appliance.drill_recipient(recipient_file)
        except age.AgeMissing:
            raise
        except age.AgeError as exc:
            # No refusal of `drill_recipient` carries the line, so this one
            # does not either.
            raise pulumi_config.SlotRefused(f'{exc}; `--rotate` draws a successor over it') from exc
        if on_file is not None:
            raise pulumi_config.SlotRefused(
                f'{recipient_file} already names a drill recipient, so a drill key is in service; '
                '`--rotate` draws its successor, and the sequence it starts is: commit the file, '
                '`operator-stack state-backend up --force`, then a fresh dump for the drill to open'
            )
    slot = DRILL_AGE_IDENTITY_SLOT
    log.info('drawing the drill age identity with %s', age.KEYGEN)
    identity = age.generate()
    # The secret line alone, as `age-keygen` prints it minus its comment
    # lines and with no trailing newline: a GitHub secret is stored exactly
    # as it is piped in, and the drill writes the value to a file it hands
    # `state-backend restore --identity-file`, which reads it line by line.
    _push(forge, slot, identity.secret)
    log.info('%s holds the private half; writing the public half to %s', slot, recipient_file)
    _ = recipient_file.write_text(
        '# The drill age identity, public half (docs/credentials.md §3). The private\n'
        f'# half is the {slot}; nothing on disk holds it.\n'
        f'# Written by `credentials derived {DRILL_AGE_IDENTITY_ROW} generate`; replaced by `--rotate`.\n'
        f'{identity.public}\n'
    )
    log.info('commit %s; the appliance encrypts to it from the replacement that carries it:', recipient_file)
    log.info('    operator-stack state-backend up --force    # dumps the box, replaces it, restores into the new one')
    return identity.public


def etcd_drill_age_identity(*, sealer: sealing.Sealer, recipient_file: Path) -> str:
    """Draw the etcd restore-verify's age identity: private half sealed into `k8s-base`, public half to a file.

    Returns the recipient written. The identity exists in this process, on
    `kubeseal`'s standard input and as ciphertext only the cluster opens: no
    file, no kit row and no escrow ciphertext holds the private half in the
    clear, and `age.Identity` keeps it out of every repr. It is escrowed
    nowhere, because every etcd snapshot it opens also opens with an escrowed
    backup generation; losing it costs a fresh draw, never a snapshot. The
    seal is strict, for the Secret the census row names
    (`conventions.sealed.ETCD_DRILL_IDENTITY`), and its value is the identity
    line as an identity file holds it.

    **Re-running is the rotation**, and it is the seal again after a rebuild
    of the cluster, whose new sealing key no earlier ciphertext opens under:
    the private half cannot be read back out of the cluster, so each run
    draws a fresh identity over both halves. The run writes both, and they
    are committed together, so `main` never holds one half without the
    other.

    **The stack first, then the seal, then the file.** The declaring stack is
    held to exist before anything is drawn, so a refusal costs nothing; the
    cluster's certificate was fetched in building `sealer`. The file is
    written only once the ciphertext is written and read back, since a
    recipient on file whose private half was never sealed is, once the
    snapshots read the file, a snapshot encrypted to a key nothing holds --
    harmless to the data, a verify failure found a month later -- where a
    seal with no file yet is repaired by running this again.
    """
    value = conventions.sealed.ETCD_DRILL_IDENTITY
    _ = sealer.stack(value)
    log.info('drawing the etcd restore-verify age identity with %s', age.KEYGEN)
    identity = age.generate()
    (key,) = value.keys
    sealer.deliver(value, {key: f'{identity.secret}\n'})
    log.info('%s holds the sealed private half; writing the public half to %s', value.stack, recipient_file)
    _ = recipient_file.write_text(
        "# The etcd restore-verify's age identity, public half (docs/credentials.md §3). The\n"
        f'# private half is sealed into Pulumi.{value.stack}.yaml at {value.path(key)}; nothing\n'
        '# on disk holds it in the clear.\n'
        f'# Written by `credentials derived {ETCD_DRILL_AGE_IDENTITY_ROW} generate`; replaced by running it again.\n'
        f'{identity.public}\n'
    )
    log.info('commit %s and Pulumi.%s.yaml together', recipient_file, value.stack)
    return identity.public


def _push_drill_api_key(forge: Forge, key: oci_iam.ApiKey) -> None:
    """Write the drill's OCI signing configuration into its three Environment secrets.

    The same three values `_push_api_key` writes into a stack's config, for
    the same reasons -- the fingerprint computed from the key at push time, the
    PEM without its trailing newline -- landing as three secrets because a
    GitHub secret is one string and a workflow assembles the configuration
    file out of them. Each push is verified through the listing before the
    next one starts, so a run that stops part way leaves a set the drill
    cannot sign with rather than one that signs as the wrong user.
    """
    _push(forge, DRILL_OCI_USER_SLOT, key.user)
    _push(forge, DRILL_OCI_FINGERPRINT_SLOT, key.fingerprint)
    _push(forge, DRILL_OCI_PRIVATE_KEY_SLOT, key.private_key.strip())


def _dump_bucket(session: b2.Session) -> str:
    """The id of the bucket the appliance dumps into, which is what confines the drill's key.

    Looked up rather than created: the `state-backend` stack declares the
    bucket and its retention, and a drill that finds no bucket has nothing to
    read -- creating one here would mint a reader over an empty prefix and
    report success.
    """
    name = appliance_settings.B2_BUCKET
    found = session.buckets(name)
    if not found:
        raise BucketMissing(
            f'the B2 account holds no bucket named {name}, so there is nothing for a drill to read; '
            'the state-backend stack declares it',
            bucket=name,
        )
    return found[0].bucket_id


def drill_credentials(
    kit: KdbxStore,
    forge: Forge,
    *,
    only: str | None = None,
    oci_seed_entry: str = OCI_SEED_ENTRY,
    b2_seed_entry: str = B2_SEED_ENTRY,
    connect: oci_iam.Connect = oci_iam.identity_client,
) -> dict[str, str]:
    """Mint the rebuild drill's OCI and B2 keys into the ops repository's `drill` Environment.

    Returns the identifiers delivered, by half: the OCI user OCID and the B2
    key id, which is what an operator matches against a console listing.

    Two mints the `physical` rows already run, aimed at the drill's own
    boundaries. The OCI half is `oci_iam.mint_api_key` for the `drill`
    consumer: its user, group and policy under one name, confined to the
    compartment `conventions` names for it and created there on the first
    run, which prints the OCID to record. **No `--compartment` override on
    this row.** That flag exists for rehearsing a bring-up in a tenancy that
    is not this installation's, where no recorded name applies; the drill
    compartment is a recorded name in the recorded tenancy, and the mint is
    held to it (`oci_iam.verify_tenancy`) -- a drill key confined to a
    compartment named on the command line is a key whose bound no test holds.
    The B2 half is `b2.mint_drill_read_key` over the bucket the appliance
    dumps into: list and read on the dump prefix, verified by listing it as
    the new key.

    Each half goes through `Delivery`: mint, push its carriers, verify each
    through the listing, and only then retire what it supersedes -- so a push
    that fails leaves the predecessor live, and the halves are independent of
    each other's failure. Re-running is the rotation of both; `only` rotates
    one.

    **Every refusal the run can know before minting fires before anything is
    minted.** The B2 half's preconditions -- the seed opens, the account is
    the recorded one, the dump bucket exists -- are resolved ahead of the OCI
    half whenever the B2 half runs, so a run against an account with no
    bucket creates nothing on either platform, rather than rotating the OCI
    key and then refusing with nothing said about it.

    Nothing here writes a file: the carriers exist in this process, on `gh`'s
    standard input and in the Environment. Assembling a signing configuration
    and a B2 credential out of them is the workflow's step (state-backend.md
    §7.3), which reads the tenancy, the region and the compartment from the
    checkout it pins rather than from a secret.
    """
    if only is not None and only not in DRILL_HALVES:
        raise pulumi_config.SlotRefused(
            f'{only!r} is no half of the drill credentials; the halves are {", ".join(DRILL_HALVES)}'
        )
    halves = DRILL_HALVES if only is None else (only,)
    delivered: dict[str, str] = {}

    reader_scope: tuple[b2.Session, str] | None = None
    if DRILL_B2_HALF in halves:
        log.info('opening the B2 seed from the kit')
        session = b2.Session.from_entry(kit, b2_seed_entry)
        b2.verify_account(session.account_id)
        reader_scope = (session, _dump_bucket(session))

    if DRILL_OCI_HALF in halves:
        pending_key = oci_iam.mint_api_key(kit, consumer=conventions.DRILL, seed_entry=oci_seed_entry, connect=connect)
        key, _ = pending_key.deliver(lambda key: _push_drill_api_key(forge, key))
        log.info('the drill signs as %s (%s) from now on', oci_iam.Identity.name_for(conventions.DRILL), key.user)
        delivered[DRILL_OCI_HALF] = key.user

    if reader_scope is not None:
        session, bucket_id = reader_scope
        pending_read = b2.mint_drill_read_key(session, bucket_id=bucket_id)

        def push_read_key(read_key: b2.AppKey) -> None:
            _push(forge, DRILL_B2_KEY_ID_SLOT, read_key.key_id)
            _push(forge, DRILL_B2_KEY_SLOT, read_key.key)

        read_key, _ = pending_read.deliver(push_read_key)
        log.info('the drill reads the dump prefix as %s (%s) from now on', b2.DRILL_READ_NAME, read_key.key_id)
        delivered[DRILL_B2_HALF] = read_key.key_id

    return delivered


def _freshness_data(key: b2.AppKey) -> dict[str, str]:
    """Which half of a minted B2 key each data key of the sealed freshness key carries.

    A data key the census row does not name is refused by the seal itself
    (`sealing.Sealer.seal`), so the two cannot drift apart unseen.
    """
    return {'AWS_ACCESS_KEY_ID': key.key_id, 'AWS_SECRET_ACCESS_KEY': key.key}


def b2_freshness_dumps(kit: KdbxStore, forge: Forge, *, sealer: sealing.Sealer, seed_entry: str = B2_SEED_ENTRY) -> str:
    """Mint the dump-freshness key into the ops repository's secrets and seal it for the cluster. Returns its key id.

    `b2.mint_freshness_dumps_key` over the bucket the appliance dumps into --
    a listing of the dump prefix and nothing else, verified by listing it as
    the new key -- delivered to both of its consumers: the two repository
    secrets the ops repository's probe reads (`B2_FRESHNESS_DUMPS_SLOTS`),
    each verified through the listing, and the Secret the census row names
    (`conventions.sealed.B2_FRESHNESS_DUMPS`), sealed strict for the cluster's
    dump-age check in the monitoring namespace and written in the clear into
    `k8s-base`'s configuration. Re-running is the rotation of both.

    **The predecessor is retired only once every target holds the new key**,
    because the mint retires every other key of its name: a delivery that
    reached one consumer and not the other would leave that one holding a
    retired key. So the seal is required rather than taken where a cluster
    happens to answer, and the steps run in the order that changes least on
    a failure: the key sealed, which writes nothing; both pushes; the
    ciphertext written; and only then the retirement. A failure at any step
    leaves the key both consumers held live.

    **Every refusal the run can know before minting fires before anything is
    minted**: a declaring stack that does not exist or that this machine has
    no backend to reach, and -- by the time this is called -- a cluster whose
    certificate could not be fetched, which is what building `sealer` does;
    then a seed in an account other than the one `conventions` records, and
    a dump bucket the account does not hold. The bucket is looked up rather
    than converged, for the reason the drill's mint looks it up: a check over
    a bucket that does not exist has nothing to measure, and creating one
    here would mint a key over an empty prefix the check then reports as
    never dumped into.
    """
    value = conventions.sealed.B2_FRESHNESS_DUMPS
    _ = sealer.stack(value)
    log.info('opening the B2 seed from the kit')
    session = b2.Session.from_entry(kit, seed_entry)
    b2.verify_account(session.account_id)
    bucket_id = _dump_bucket(session)
    pending = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)

    def deliver(key: b2.AppKey) -> None:
        ciphertext = sealer.seal(value, _freshness_data(key))
        for slot, half in zip(B2_FRESHNESS_DUMPS_SLOTS, (key.key_id, key.key), strict=True):
            _push(forge, slot, half)
        sealer.write(value, ciphertext)

    delivered, _ = pending.deliver(deliver)
    log.info(
        "the dump-age checks list the dump prefix as %s (%s): the ops repository's probe from now on, and the "
        "cluster's from the apply that hands it the sealed %s",
        b2.FRESHNESS_DUMPS_NAME,
        delivered.key_id,
        value.name,
    )
    return delivered.key_id
