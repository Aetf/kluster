"""The slot map: every §3 credential, where its value comes from, and every slot it lands in.

`docs/credentials.md` §3 is the register's human-readable half -- a table of
credentials, each naming the channel its consumer reads. This is the same table
in machine-readable form, and the two are held equal by a test that reads the
document: a §3 row with no entry here, or an entry naming a credential §3 does
not, fails there rather than in an operator's head.

**A row is a credential, a source and a set of targets.** The targets are the
closed set of storage channels §1 rule 6 names, one dataclass each, and three of
that set collapse into one here: a CI Environment secret, an ops-repository
secret and the `kluster` repository secret are all a `Slot` in
`github_secrets.py`, differing in which repository they sit in and whether they
name an Environment.

**Every value comes from one of five places**, which is the row's source class.
Four of them hold a value that can be obtained again, and `derived sync` copies
those into their GitHub slots; the remaining one is born into its slot and is no
business of that command:

-   **derived** -- obtainable again from the kit alone, with no provider
    involved. Most such rows are an escrow label (§2.2): the value is
    recovered with the recovery key and pushed, so a first fill and a re-fill
    are the same command and neither rotates anything. The state-backend
    client bundle is the one that is *issued* rather than recovered -- the CA
    is escrowed, the leaf under it is generated at issuance and kept nowhere
    -- so a re-fill there hands CI a certificate it did not have before.
-   **minted** -- created by the run that delivers it: a provider mint by the
    row's own `credentials derived <row> mint`, or a secret a program generates
    for its own resource. The value is disclosed once, to the code that made
    it, so this map cannot re-push one -- obtaining it again means minting
    again, which is a rotation. Such a row names its producer instead of
    pretending it can be filled from here.
-   **state-read** -- generated inside a Pulumi program, and read back out of
    the stack it belongs to. Not derivable and not re-mintable: the value
    exists because a stack ran. What separates this from *minted* is whether
    anything has to read the value again: a credential whose every slot the
    creating run fills is minted, and one that a second consumer needs a copy
    of is read back out of state.
-   **manual** -- a value this system does not produce. Some are pasted from a
    console (the Home Assistant webhook, whose slot is its only storage), some
    are made in the console that checks them and delivered by a command of
    their own (the UniFi key, the AdGuard login and the ZeroTier Central token,
    `devices.py`), and some are installed by another tracker's automation
    entirely (the UDM and libvirt SSH identities, §3).
-   **decided** -- not a credential at all, but a constant this repository
    holds in `conventions` that a continuous-integration job needs beside one.
    There is one: the overlay network's id, which a workflow can only pass as a
    secret (rfc-002 §11).

**A target has to be an address, not an intention.** Where §3 names a channel
that nothing has given a name yet -- an Environment secret no workflow reads, a
SealedSecret whose manifest does not exist -- the row records that in `pending`
rather than inventing a name a future workflow would have to guess right.
`derived ls` prints it, `derived sync` skips the row saying so, and `derived
sync --only <row>` refuses by name. That is the discipline the seed layer already
uses: a register row with no implementation is a command that refuses, not a
command that is missing.

Pushing is resolve, push, verify, every run, and it retires nothing. One row
issues rather than copies -- the client bundle, whose leaf key exists only in
the run that made it -- and that is still not the *minted* class above: this
PKI revokes nothing, the appliance authenticates the CA rather than a
particular leaf, and so the certificate CI was holding keeps working until it
expires. Nothing is disclosed once and nothing is revoked, and the run after
this one issues another. The verification is what the channel allows -- a
GitHub secret is never readable again once written, so what is checked is that
the name is in the listing and its timestamp moved (`github_secrets.py`).
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from ... import conventions
from ...lib import config
from ..state_backend import config as appliance
from ..state_backend import settings as appliance_settings
from . import derived, devices, escrow, pki, pulumi_config
from .github_secrets import Forge, Slot
from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: The two repositories, `owner/name` as the API spells them. The `github`
#: stack declares both; a test holds the two equal rather than importing that
#: module, which would drag the Pulumi provider SDKs into `credentials --help`.
REPOSITORY = 'Aetf/kluster'
OPS_REPOSITORY = 'Aetf/kluster-ops'

#: The deployment Environments, in the order the merge chain runs them (ci.md
#: §3). `physical` is two of them because its plan is ungated and its apply is
#: reviewer-gated; both hold the same credentials.
ENVIRONMENTS = ('physical-plan', 'physical', 'dns', 'k8s-base', 'apps')

#: The ops repository's own Environment, which carries the unattended drills'
#: credentials and is ungated because its scope is the gate (§4).
DRILL_ENVIRONMENT = 'drill'

#: The Environments whose jobs join ZeroTier, one identity domain each
#: (physical/gateway.md §2.1). `k8s-base` and `apps` join nothing: the
#: LAN-touching work is the AdGuard rewrites, which `dns` applies.
ZEROTIER_PHYSICAL = ('physical-plan', 'physical')
ZEROTIER_DNS = ('dns',)

#: The four secrets that carry one state-backend client bundle: the connection
#: string, and the three files it authenticates with. They are **file contents
#: rather than variables a job reads** -- `.github/actions/state-backend` writes
#: each of them into the checkout's `.credentials/state-backend/` slot, and
#: `mise.toml` then resolves `PULUMI_BACKEND_URL` and the three `PGSSL*`
#: variables out of those files exactly as it does on a workstation. So the
#: names below are carriers, and no job has an environment of its own.
BACKEND_URL = 'PULUMI_BACKEND_URL'
BACKEND_CA = 'PULUMI_BACKEND_CA'
BACKEND_CERT = 'PULUMI_BACKEND_CERT'
BACKEND_KEY = 'PULUMI_BACKEND_KEY'

PHYSICAL_STACK = derived.PHYSICAL_STACK
DNS_STACK = derived.ZONES_STACK


# --------------------------------------------------------------------------
# The channels: §1 rule 6's closed set, one type each.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PulumiConfig:
    """A key in `Pulumi.<stack>.yaml`, committed -- the provider-credential channel."""

    stack: str
    key: str
    #: Plain keys are identifiers the committed file may carry in the clear
    #: (§4): the account id, the ZeroTier network.
    secret: bool = True

    def __str__(self) -> str:
        return f'Pulumi config {"secret" if self.secret else "value"} {self.stack}: {self.key}'


@dataclass(frozen=True)
class PulumiState:
    """A value that lives in the state backend, never in git.

    The stronger of the two Pulumi channels and the home of what a program
    generates rather than what it needs to start (§1 rule 6).
    """

    stack: str
    what: str

    def __str__(self) -> str:
        return f'Pulumi state {self.stack}: {self.what}'


@dataclass(frozen=True)
class EscrowCopy:
    """A committed age ciphertext under `escrow/<label>/` (§2.2).

    A recovery copy rather than a delivery: nothing reads it at runtime, and
    opening one needs the recovery key that exists only in the kit.
    """

    label: str

    def __str__(self) -> str:
        return f'escrow ciphertext {escrow.DIRECTORY}/{self.label}/'


@dataclass(frozen=True)
class SealedSecret:
    """A `kubeseal`-encrypted manifest committed to this repository."""

    what: str

    def __str__(self) -> str:
        return f'SealedSecret: {self.what}'


@dataclass(frozen=True)
class OnBox:
    """Delivered by provisioning -- embedded in Butane, or written by a run."""

    what: str

    def __str__(self) -> str:
        return f'on-box: {self.what}'


@dataclass(frozen=True)
class WorkstationSlot:
    """A file under the checkout's git-ignored `.credentials/` (§4.4)."""

    name: str

    def __str__(self) -> str:
        return f'workstation slot .credentials/{self.name}'


@dataclass(frozen=True)
class GwConfigSecret:
    """A secret pushed to the gateway beside its nspawn units (physical/gateway.md §1).

    Part of §1 rule 6's closed set, and addressed by no row: the gateway's own
    secrets travel in the `physical` stack's configuration, and the provider
    that owns the device writes them onto it.
    """

    what: str

    def __str__(self) -> str:
        return f'gw-config device secret: {self.what}'


#: Everything a row may be delivered into. `Slot` is the GitHub one, and the
#: only kind this module can fill.
Channel = Slot | PulumiConfig | PulumiState | EscrowCopy | SealedSecret | OnBox | WorkstationSlot | GwConfigSecret


# --------------------------------------------------------------------------
# The sources: where a push obtains the value, if it can obtain it at all.
# --------------------------------------------------------------------------


def _no_environment() -> pulumi_config.BackendEnvironment:
    """The default: nothing recovered, for a run that reads no state."""
    return pulumi_config.BackendEnvironment()


@dataclass
class Context:
    """What a push may reach for, each part opened only when a row needs it.

    Everything here is lazy on purpose: pushing the one manual row asks for no
    kit, and pushing an escrowed row opens no state backend.
    """

    forge: Forge
    #: The kit's escrow, for a derived row. Called at most once.
    open_vault: Callable[[], escrow.Vault]
    #: The backend URL and the state passphrase, for a state read.
    open_environment: Callable[[], pulumi_config.BackendEnvironment] = _no_environment
    #: The checkout holding `Pulumi.yaml`; a state read runs `pulumi` there.
    project: Path = field(default_factory=pulumi_config.project_dir)
    #: How that `pulumi` is invoked. A seam, so a state read is testable
    #: without a backend, exactly as the config slot's is.
    runner: pulumi_config.Runner = pulumi_config.run_pulumi
    #: How a manual row asks. `getpass`, so a typed value never echoes.
    ask: Callable[[str], str] = getpass.getpass
    _vault: escrow.Vault | None = field(default=None, init=False, repr=False)
    _environment: pulumi_config.BackendEnvironment | None = field(default=None, init=False, repr=False)

    @property
    def vault(self) -> escrow.Vault:
        if self._vault is None:
            self._vault = self.open_vault()
        return self._vault

    @property
    def environment(self) -> pulumi_config.BackendEnvironment:
        """What a `pulumi` run needs here, opened at most once.

        Two rows read it for two things. A state read runs `pulumi` with it.
        The client bundle takes the appliance's address out of the backend
        URL, because the one place a workstation records which box it talks to
        is the URL beside its own certificates.
        """
        if self._environment is None:
            self._environment = self.open_environment()
        return self._environment

    def stack(self, name: str) -> pulumi_config.Stack:
        return pulumi_config.Stack(name=name, directory=self.project, env=self.environment.variables(), run=self.runner)


class Source(Protocol):
    """Where one row's value comes from, and what to call that in a listing."""

    #: One word for `derived ls`: derived, minted, state-read, manual, decided.
    kind: ClassVar[str]

    def describe(self) -> str:
        """The origin in the operator's words, printed beside the row."""
        ...

    def value(self, context: Context) -> str:
        """The value itself, or `SlotRefused` naming what stands in the way."""
        ...


@dataclass(frozen=True)
class Derived:
    """An escrowed secret (§2.2), recovered with the kit's recovery key.

    Re-running a push is not a rotation: the plaintext is whatever generation
    the registry currently holds, and producing a new one is `credentials
    derived <row> generate`, which this map deliberately cannot do.
    """

    kind: ClassVar[str] = 'derived'

    label: str

    def describe(self) -> str:
        return f'escrow label `{self.label}`, recovered with the kit'

    def value(self, context: Context) -> str:
        return context.vault.recover(self.label)


def _appliance_address(context: Context) -> str:
    """Which state-backend box a certificate is being issued for.

    Read off this workstation's own bundle rather than asked for. The address
    is a fact of the appliance, the URL beside the operator certificates is
    where a workstation already records it, and a typed-in one is a way to hand
    CI a certificate for a box that is not the one anybody is using.
    """
    url = context.environment.url
    if not url:
        raise SlotRefused(
            'this workstation has no client bundle, so which appliance to issue for is unknown; '
            '`state-backend bundle operator --address <ip>` writes one'
        )
    address = urlsplit(url).hostname
    if address is None:
        raise SlotRefused(f"the connection string beside this workstation's bundle names no host: {url!r}")
    return address


@dataclass(frozen=True)
class Issued:
    """A client bundle issued under the escrowed CA: one credential, four secrets.

    Derived, because the CA is: `state-backend/ca` opens with the kit's
    recovery key and nothing else, and everything below follows from it without
    a provider being asked anything. What is *not* derived is the leaf -- its
    key is random at issuance and escrowed nowhere (`pki.py`) -- so this cannot
    re-push the certificate CI is holding. It issues another one.

    **That costs nothing, which is why it is allowed to happen on every run.**
    This PKI has no revocation and the appliance authenticates the CA rather
    than a particular leaf, so the predecessor keeps working until it expires
    and no live consumer is broken by a run that replaces it. What the run does
    buy is convergence: after it, the bundle CI holds names the box this
    workstation talks to, whatever the box's address was when it was last
    filled.

    **The four secrets are one credential, which is why they are one row.** A
    certificate and the key that opens it come from a single issuance
    (`pki.py`), so splitting them across rows would let two runs deliver halves
    of two different bundles. Each sink takes the part its own name asks for.
    """

    kind: ClassVar[str] = 'derived'

    #: The Postgres role the certificate authenticates as, which is also the
    #: bundle's name. The appliance's roles are its own (`state_backend`).
    role: str

    def describe(self) -> str:
        return (
            f'issued with the kit from escrow label `{escrow.CA}`; the server and `operator` halves come from '
            f'`state-backend provision`, and a push here issues a fresh `{self.role}` key, so re-running it '
            f'replaces the bundle CI holds'
        )

    def parts(self, context: Context) -> Mapping[str, str]:
        """The bundle, keyed by the secret name each part is pushed under.

        Each PEM goes in without its trailing newline. A secret is stored
        exactly as it is piped in (`github_secrets.py`) and the composite
        action writes it out with a `printf` that appends one, so stripping
        here is what makes the file in a runner's slot byte-identical to the
        one `state-backend bundle` writes on a workstation.
        """
        address = _appliance_address(context)
        log.info(
            'issuing a fresh `%s` certificate for %s; what CI holds now is replaced, and stays valid until it expires',
            self.role,
            address,
        )
        bundle = appliance.client_bundle(
            pki.Authority.from_pem(context.vault.recover(escrow.CA)), name=self.role, address=address
        )
        return {
            BACKEND_URL: bundle.url(),
            BACKEND_CA: bundle.ca_cert.decode().strip(),
            BACKEND_CERT: bundle.cert.decode().strip(),
            BACKEND_KEY: bundle.key.decode().strip(),
        }


@dataclass(frozen=True)
class Minted:
    """Created by the run that delivers it, and disclosed to nothing afterwards.

    Nothing can push one of these later: a minted secret is returned once, to
    the call that created it, and asking again produces a *different*
    credential. So the slot is filled at mint time (§4) and this map's job for
    such a row is to record where that is.
    """

    kind: ClassVar[str] = 'minted'

    #: What mints and delivers it, named in every refusal. Usually a command;
    #: for two rows it is a program, because the mint happens inside a stack.
    command: str
    #: What stands between the register and that producer existing. Empty when
    #: it is built.
    unbuilt: str = ''

    def describe(self) -> str:
        built = '' if not self.unbuilt else f' (unbuilt: {self.unbuilt})'
        return f'minted and delivered by {self.command}{built}'

    def value(self, context: Context) -> str:
        _ = context
        raise SlotRefused(
            f'a minted credential is disclosed only to the call that creates it, so it cannot be pushed from '
            f'here; `{self.command}` mints it and fills its slot in one run'
        )


@dataclass(frozen=True)
class StateRead:
    """A stack output, read back out of the state the kit's passphrase opens.

    The output name is this map's half of a contract: the producing program has
    to export under it, and a map that named nothing would leave that agreement
    implicit until the day the push found nothing to read.
    """

    kind: ClassVar[str] = 'state-read'

    stack: str
    output: str

    def describe(self) -> str:
        return f'`{self.stack}` stack state, output `{self.output}`'

    def value(self, context: Context) -> str:
        stack = context.stack(self.stack)
        if not stack.exists():
            raise SlotRefused(f'the `{self.stack}` stack has no state, so `{self.output}` does not exist to be pushed')
        outputs = stack.outputs()
        if self.output not in outputs:
            raise SlotRefused(f'the `{self.stack}` stack exports no `{self.output}`')
        # The boundary between a stack's exported state and a secret this
        # pushes: every row read this way exports a string, and an output that
        # stopped being one must refuse rather than be coerced into the four
        # characters `null` or into its own JSON.
        try:
            return config.text(outputs[self.output], f'the `{self.stack}` stack output `{self.output}`')
        except TypeError as exc:
            raise SlotRefused(str(exc)) from exc


@dataclass(frozen=True)
class Decided:
    """A constant this repository holds, copied into a slot that can only take a secret.

    Not a credential and not a secret, which is why it is its own class rather
    than a `Manual` an operator would be asked to re-type: the value is in
    `conventions`, a checkout is the whole of what it takes to obtain it, and
    nothing about it rotates. It exists for the one such value CI needs beside
    a credential -- the overlay network's id, which a workflow input can only
    be a secret.
    """

    kind: ClassVar[str] = 'decided'

    #: Where the constant lives, spelled as a reader would import it.
    where: str
    constant: str

    def describe(self) -> str:
        return f'`{self.where}`, a decision of this repository'

    def value(self, context: Context) -> str:
        _ = context
        return self.constant


@dataclass(frozen=True)
class Manual:
    """A value from outside this system; nothing here mints or derives it.

    Asked for when the slot is empty, and asked for again only when the operator
    names the row: a full run must not stop to re-type a value that is already
    in place, and a rotation must not be silently skipped.
    """

    kind: ClassVar[str] = 'manual'

    describes: str
    #: Where the operator gets it, printed at the moment they are asked.
    console: str
    #: What takes it from them and delivers it, where anything does. Empty for
    #: a row whose only delivery is this map's own sink.
    command: str = ''

    def describe(self) -> str:
        by = f', delivered by `{self.command}`' if self.command else ''
        return f'typed in: {self.describes}{by}'

    def value(self, context: Context) -> str:
        log.warning('%s is neither minted nor derived; it comes from here:', self.describes)
        for line in self.console.splitlines():
            log.warning('  %s', line)
        secret = context.ask(f'{self.describes}: ').strip()
        if not secret:
            raise SlotRefused(f'{self.describes} is required')
        return secret


# --------------------------------------------------------------------------
# The map itself.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One line of the map: a §3 credential, its origin, and its slots."""

    #: The §3 "Credential" cell this row implements, verbatim. More than one
    #: row may name the same cell -- one credential can be several secrets, as
    #: the ZeroTier identities are one per identity domain.
    register: str
    #: `Issued` is spelled out beside the protocol because it is the one source
    #: that does not resolve to a single value: its row is a bundle, and each
    #: sink takes a different part of it (`resolve`).
    source: Source | Issued
    targets: tuple[Channel, ...] = ()
    #: A channel §3 names for this row that has no address yet, and why. Empty
    #: when every slot the register promises is written down above.
    pending: str = ''

    @property
    def sinks(self) -> tuple[Slot, ...]:
        """The targets this module can fill: the GitHub secrets, and only those."""
        return tuple(target for target in self.targets if isinstance(target, Slot))

    def resolve(self, context: Context) -> dict[Slot, str]:
        """What the push writes into each of this row's sinks.

        Almost every row is one credential fanned out into every slot that needs
        a copy of it, so one resolution answers for all of them. The client
        bundle is the exception, and resolving it once is what keeps its parts a
        set: a certificate and the key that opens it come from a single
        issuance, so asking twice would deliver halves of two bundles.
        """
        if isinstance(self.source, Issued):
            parts = self.source.parts(context)
            return {slot: parts[slot.name] for slot in self.sinks}
        return dict.fromkeys(self.sinks, self.source.value(context))


def _github(name: str, environments: tuple[str, ...], repository: str = REPOSITORY) -> tuple[Slot, ...]:
    return tuple(Slot(repository=repository, name=name, environment=environment) for environment in environments)


def _device(member: str) -> Row:
    """A §3 row whose credential is made in a console and typed in (`devices.py`).

    Built from that module's table rather than restated here, so the keys this
    map advertises are the keys the command writes -- the same rule the minted
    rows follow by importing their key names.
    """
    device = devices.DEVICES[member]
    return Row(
        register=device.register,
        source=Manual(device.title, device.console, command=f'credentials derived {device.member} record'),
        targets=tuple(PulumiConfig(device.stack, field.key, secret=field.secret) for field in device.fields),
        pending=_IN_STACK_CONFIG,
    )


#: Why a provider credential CI *uses* is nonetheless not a CI secret today: it
#: reaches the job through the stack's committed configuration, which the
#: program reads for itself.
_IN_STACK_CONFIG = (
    '§3 names a CI Environment secret beside the config secret; no workflow reads one, because the stack '
    'takes the credential from its committed configuration (ci.md §3)'
)

#: Why an ops-repository row has no address: `kluster-ops` carries the issues
#: this register cites and no workflow, so nothing there names a secret yet
#: (ci.md §3).
_OPS_UNBUILT = 'the ops-repository workflow that would read it is not built, so no secret there names it'

#: Why an in-cluster row has no address: sealing needs the controller, and the
#: controller arrives with `k8s-base`.
_CLUSTER_UNBUILT = 'the sealed-secrets controller and its consumer arrive with `k8s-base`, so no manifest path exists'

#: Sinks this map used to carry, and where the fact each one delivered lives
#: now. Retiring a sink moves a fact rather than deleting it, so the name that
#: addressed the old home answers with the new one instead of with "no such
#: row" -- the same courtesy `pending` pays a slot that does not exist yet.
#: Keyed by the name the sink was addressed as, which for a Pulumi config sink
#: is its key.
RETIRED: Mapping[str, str] = {
    'ociTenancyOcid': (
        "the tenancy OCID is no longer delivered anywhere: it names this program's account rather than "
        'authenticating to it, so it is `conventions.OCI_TENANCY.tenancy_ocid` and `credentials derived '
        'oci-physical mint` verifies the key it issues against that instead of writing a copy beside it'
    ),
}

#: The map, in §3's own order. Keys are the `credentials derived` row names:
#: the same string the command tree gives the row, so a row has one spelling.
ROWS: dict[str, Row] = {
    derived.OCI_PHYSICAL_ROW: Row(
        register='OCI API key (`physical`)',
        source=Minted(f'credentials derived {derived.OCI_PHYSICAL_ROW} mint'),
        targets=(
            PulumiConfig(PHYSICAL_STACK, derived.OCI_USER_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_FINGERPRINT_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_PRIVATE_KEY_KEY),
        ),
        pending=_IN_STACK_CONFIG,
    ),
    derived.OCI_STATE_BACKEND_ROW: Row(
        register='OCI API key (state backend)',
        source=Minted(f'credentials derived {derived.OCI_STATE_BACKEND_ROW} mint'),
        targets=(WorkstationSlot(f'oci/{conventions.STATE_BACKEND}/'),),
    ),
    derived.ZONES_ROW: Row(
        register='Cloudflare token (zones)',
        source=Minted(f'credentials derived {derived.ZONES_ROW} mint'),
        targets=(
            PulumiConfig(DNS_STACK, derived.API_TOKEN_KEY),
            PulumiConfig(DNS_STACK, derived.ACCOUNT_KEY, secret=False),
        ),
        pending=_IN_STACK_CONFIG,
    ),
    'cloudflare-dns01': Row(
        register='Cloudflare token (DNS-01)',
        source=Minted(
            'credentials derived cloudflare-dns01 mint', unbuilt='cert-manager has no slot to be sealed into'
        ),
        targets=(SealedSecret("cert-manager's DNS-01 solver token"),),
        pending=_CLUSTER_UNBUILT,
    ),
    derived.GATEWAY_ACME_ROW: Row(
        register='Cloudflare token (gateway ACME)',
        source=Minted(f'credentials derived {derived.GATEWAY_ACME_ROW} mint'),
        # One key, and no CI Environment secret beside it: this is not a
        # credential a job authenticates with but a value the `physical`
        # program writes onto the device, and the program reads it out of the
        # committed configuration wherever it runs.
        targets=(PulumiConfig(PHYSICAL_STACK, derived.GATEWAY_ACME_KEY),),
    ),
    derived.B2_MANAGEMENT_ROW: Row(
        register='B2 management key',
        source=Minted(f'credentials derived {derived.B2_MANAGEMENT_ROW} mint'),
        targets=(
            PulumiConfig(PHYSICAL_STACK, derived.B2_KEY_ID_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.B2_KEY_KEY),
        ),
        pending=_IN_STACK_CONFIG,
    ),
    'b2-writer': Row(
        register='B2 writer keys',
        source=Minted('the `physical` stack, from the B2 seed', unbuilt='the prefix-scoped keys are not declared'),
        targets=(
            SealedSecret('the VolSync, CNPG barman and etcd-snapshot repository keys'),
            OnBox("the micro cron's key"),
        ),
        pending=_OPS_UNBUILT,
    ),
    'b2-dump': Row(
        register='B2 dump key (micro)',
        source=Minted('state-backend provision'),
        targets=(OnBox("the appliance's Ignition"),),
    ),
    'github-installation-tokens': Row(
        register='GitHub installation tokens',
        source=Minted('the alert producer and the drift trigger, per run', unbuilt='neither producer exists'),
        # Deliberately empty: an 8-hour token minted per run is stored nowhere,
        # which is the row's whole point, so it has no slot to map.
        pending='never stored: minted in-run from an App private key and used within the same job',
    ),
    'zerotier-identity-physical': Row(
        register='ZT CI member identities (`ci-physical`, `ci-dns`)',
        source=StateRead(PHYSICAL_STACK, 'ci_zerotier_identity_physical'),
        targets=_github('ZEROTIER_IDENTITY', ZEROTIER_PHYSICAL),
    ),
    'zerotier-identity-dns': Row(
        register='ZT CI member identities (`ci-physical`, `ci-dns`)',
        source=StateRead(PHYSICAL_STACK, 'ci_zerotier_identity_dns'),
        targets=_github('ZEROTIER_IDENTITY', ZEROTIER_DNS),
    ),
    'zerotier-network': Row(
        register='ZT CI member identities (`ci-physical`, `ci-dns`)',
        # Not a credential and not a secret: the workflows pass it beside the
        # identity, and a workflow input that is not a secret has nowhere else
        # to come from.
        source=Decided('conventions.overlay.NETWORK_ID', conventions.overlay.NETWORK_ID),
        targets=_github('ZEROTIER_NETWORK_ID', ZEROTIER_PHYSICAL + ZEROTIER_DNS),
    ),
    'pulumi-passphrase': Row(
        register='Pulumi state passphrase',
        source=Derived(escrow.PASSPHRASE),
        # Every Environment, because every job runs a `pulumi` command and both
        # Pulumi channels are encrypted under this one value.
        targets=(
            EscrowCopy(escrow.PASSPHRASE),
            WorkstationSlot('pulumi.passphrase'),
            *_github('PULUMI_CONFIG_PASSPHRASE', ENVIRONMENTS),
        ),
    ),
    'state-backend-ca': Row(
        register='State-backend CA',
        source=Derived(escrow.CA),
        targets=(EscrowCopy(escrow.CA), OnBox('the certificate every bundle carries')),
    ),
    'state-backend-certificates': Row(
        register='State-backend certificates (server, `ci`, `operator`)',
        source=Issued(appliance_settings.CI_ROLE),
        # Every Environment, for the same reason the passphrase reaches every
        # Environment: each one runs a `pulumi` command, and a `pulumi` command
        # that cannot log in to the state backend cannot start.
        targets=(
            OnBox("the appliance's server certificate"),
            WorkstationSlot('state-backend/'),
            *_github(BACKEND_URL, ENVIRONMENTS),
            *_github(BACKEND_CA, ENVIRONMENTS),
            *_github(BACKEND_CERT, ENVIRONMENTS),
            *_github(BACKEND_KEY, ENVIRONMENTS),
        ),
    ),
    'backup-age-identity': Row(
        register='age backup identity',
        source=Derived(f'{escrow.BACKUP}/<generation>'),
        targets=(
            EscrowCopy(f'{escrow.BACKUP}/<generation>'),
            OnBox('the public half, a Butane recipient'),
        ),
    ),
    'drill-age-identity': Row(
        register='Drill age identity',
        source=Minted('state-backend provision'),
        targets=(OnBox('the public half, the third Butane recipient'),),
        pending=_OPS_UNBUILT,
    ),
    'restic-passwords': Row(
        register='restic repo passwords',
        source=Minted(
            'the `backed_pvc` helper, one per volume',
            unbuilt='the helper is unwritten (declarative/workloads.md §3)',
        ),
        targets=(PulumiState('apps', 'one password per backed volume'), SealedSecret("VolSync's repository secret")),
        pending='`backed_pvc` generates and seals its own password, so no `credentials` command is involved',
    ),
    'talos': Row(
        register='Talos machine secrets + talosconfig',
        source=StateRead(PHYSICAL_STACK, 'talosconfig'),
        targets=(PulumiState(PHYSICAL_STACK, 'the cluster PKI roots'),),
        pending=_OPS_UNBUILT,
    ),
    'kubeconfig': Row(
        register='kubeconfig',
        source=StateRead(PHYSICAL_STACK, 'kubeconfig'),
        targets=(PulumiState(PHYSICAL_STACK, 'the cluster-admin credential'),),
        pending=(
            'the `k8s-base` and `apps` programs take it from the `physical` stack through a StackReference, '
            'so no workflow names a secret for it'
        ),
    ),
    'gateway-libvirt-identities': Row(
        register='UDM SSH key, libvirt SSH identity',
        source=Manual(
            'the gateway and libvirt SSH identities',
            'Neither is minted here: gw-config installs the gateway key, and aconfmgr\n'
            "  provisions the homelab host's service user with its key (physical/\n"
            '  homelab-host.md §4). The act on this side is the paste into the stack.',
        ),
        targets=(
            PulumiConfig(PHYSICAL_STACK, 'gatewayPrivateKey'),
            PulumiConfig(PHYSICAL_STACK, 'libvirtPrivateKey'),
        ),
        pending=_IN_STACK_CONFIG,
    ),
    'unifi': _device('unifi'),
    'adguard': _device('adguard'),
    'zerotier': _device('zerotier'),
    'alertmanager-read': Row(
        register='Alertmanager read token',
        source=Derived(escrow.ALERTMANAGER),
        targets=(EscrowCopy(escrow.ALERTMANAGER),),
        pending=(
            'the escrow copy is the only slot this row has today: the ops-repository secret and the config '
            'secret the HTTPRoute is rendered from wait on the issue-sync poller and on that route, neither '
            'of which is built'
        ),
    ),
    'haos-webhook': Row(
        register='HA webhook URL/ID',
        source=Manual(
            'the Home Assistant webhook URL a failed deploy posts to',
            'Home Assistant → Settings → Automations → the deploy-failure automation\n'
            '  → its webhook trigger, which shows the full URL. Nothing mints this and\n'
            '  nothing derives it, so this slot is where it lives: rotating it is a new\n'
            '  webhook id there and one more run of this command.',
        ),
        # A repository secret rather than an Environment one: the job that reads
        # it belongs to no stack, and the whole power of the value is to raise a
        # phone notification (ci.md §3).
        targets=(Slot(repository=REPOSITORY, name='HAOS_DEPLOY_WEBHOOK_URL'),),
        pending=(
            'the designed shape has CI hold no Home Assistant credential at all -- a `repository_dispatch` to '
            'the ops repository, which owns the alert (ci.md §3); the SealedSecret and ops-repo copies belong '
            'to that shape and arrive with it'
        ),
    ),
    'drill-credentials': Row(
        register='Drill-environment credentials',
        source=Minted(
            'credentials derived drill-credentials mint',
            unbuilt='the drill compartment and its keys are not declared',
        ),
        pending=_OPS_UNBUILT,
    ),
}


def describe(rows: Mapping[str, Row] | None = None) -> Iterator[str]:
    """`derived ls`: the whole map, one paragraph per row. No value is touched."""
    for name, row in (rows if rows is not None else ROWS).items():
        yield f'{name} ({row.source.kind}) - {row.register}'
        yield f'    from  {row.source.describe()}'
        for target in row.targets:
            yield f'    into  {target}'
        if row.pending:
            yield f'    open  {row.pending}'


def _verify(forge: Forge, slot: Slot, before: str | None) -> None:
    """Prove the push landed, as far as a write-only channel can be proven.

    The value cannot be read back, so what is checked is that the name is in the
    listing and its timestamp moved. Two runs inside the same second share a
    timestamp, which is worth a word rather than a failure: the name being there
    is the part that distinguishes a delivered secret from a refused one.
    """
    after = forge.listing(slot).get(slot.name)
    if after is None:
        raise SlotRefused(f'{slot}: pushed, but the secret listing does not show it')
    if before is not None and after == before:
        log.warning('%s: the listing still reads %s - a re-push inside one second looks like this', slot, after)
    else:
        log.info('%s: updated %s', slot, after)


def sync(context: Context, *, rows: Mapping[str, Row] | None = None, only: str | None = None) -> list[str]:
    """Copy into their GitHub slots the rows whose value lives elsewhere. Returns what was pushed.

    Resolve, push, verify -- per row, every run -- so a first fill and a refill
    after a channel is lost are the same command. A row with no GitHub slot is
    skipped with its reason; asking for one by name is an error instead, because
    a request for a specific row that quietly does nothing is worse than a
    refusal.

    **What this is for is a copy, not a delivery.** The source classes are set
    out in the module docstring; what decides whether a row belongs to this
    walk is whether its value can be obtained again. Four of them can, so all
    four are re-filled here. The *issued* row obtains a fresh certificate under
    the same authority instead, which has the same effect: each run hands CI
    one it did not have before, and the one it replaces keeps working until it
    expires.

    **A minted row is born into its slot and is out of scope.** Such a
    credential is disclosed once, to the call that creates it, so its own
    `credentials derived <row> mint` fills every slot it has in the same run --
    and obtaining it again means minting again, which is a rotation of a live
    credential rather than a synchronization. The walk passes over those rows;
    naming one is refused, so the operator hears which command owns it instead
    of watching a run do nothing.

    **A name this map used to carry answers with where the fact went**
    (`RETIRED`), because "no slot map row named that" is true and useless to
    someone reading a runbook written before the move.

    **A row that cannot produce its value does not stop the walk.** Most of the
    map is waiting on something -- a stack that has not run, a value nobody has
    typed in -- and a whole-map run is how an operator finds out what is left,
    so each such row is reported and the next one is attempted. The refusals are
    raised together at the end, after everything that could be delivered has
    been, which is what makes the run's exit status mean "the map is filled".
    A run naming one row raises immediately instead: there, the refusal *is* the
    answer.
    """
    table = rows if rows is not None else ROWS
    if only is not None and only not in table:
        if only in RETIRED:
            raise SlotRefused(f'{only}: {RETIRED[only]}')
        raise SlotRefused(f'no slot map row named {only!r}; `credentials derived ls` lists them')

    pushed: list[str] = []
    refused: list[str] = []
    for name, row in table.items():
        if only is not None and name != only:
            continue
        if isinstance(row.source, Minted):
            if only is not None:
                raise SlotRefused(
                    f'{name}: born into its slot, {row.source.describe()} -- there is nothing here to copy, '
                    'and obtaining the value again would mint a different credential'
                )
            continue
        if not row.sinks:
            reason = row.pending or 'the register names no GitHub secret for it'
            if only is not None:
                raise SlotRefused(f'{name}: no GitHub secret slot - {reason}')
            log.info('%s: no GitHub secret slot - %s', name, reason)
            continue

        # One listing per collection, read before the value is obtained: it is
        # what decides whether a manual row has to ask, and what the freshness
        # check afterwards is compared against.
        log.info('%s: reading what the forge already holds', name)
        before = {slot: context.forge.listing(slot).get(slot.name) for slot in row.sinks}
        # Naming a row is what turns a typed-in one from "leave what is there"
        # into "replace it": a full run must not stop to re-type a value that is
        # already in place, and a rotation must not be silently skipped.
        if isinstance(row.source, Manual) and only is None and all(before.values()):
            log.info('%s: already in every slot; `--only %s` replaces it', name, name)
            continue

        log.info('%s: %s', name, row.source.describe())
        try:
            values = row.resolve(context)
        except SlotRefused as exc:
            if only is not None:
                raise
            log.error('%s: %s', name, exc)
            refused.append(name)
            continue
        for slot in row.sinks:
            log.info('%s: pushing to %s (gh encrypts it on the way out)', name, slot)
            context.forge.put(slot, values[slot])
            _verify(context.forge, slot, before[slot])
            pushed.append(str(slot))

    # Deliberately after the walk rather than inside it: what is delivered is
    # delivered either way, and the exit status still says the map is not full.
    if refused:
        raise SlotRefused(f'{len(refused)} row(s) had no value to push: {", ".join(refused)}; each said why above')
    return pushed


__all__ = (
    'BACKEND_CA',
    'BACKEND_CERT',
    'BACKEND_KEY',
    'BACKEND_URL',
    'DRILL_ENVIRONMENT',
    'ENVIRONMENTS',
    'OPS_REPOSITORY',
    'REPOSITORY',
    'ROWS',
    'Context',
    'Decided',
    'Derived',
    'Issued',
    'Manual',
    'Minted',
    'Row',
    'Slot',
    'StateRead',
    'describe',
    'sync',
)
