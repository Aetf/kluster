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

**Every value comes from one of four places**, which is the row's source class:

-   **derived** -- obtainable again from the kit alone, with no provider
    involved. Today every such row is an escrow label (§2.2): the value is
    recovered with the recovery key and pushed, so a first fill and a re-fill
    are the same command and neither rotates anything.
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
    from a kit row the platform publishes no API for (the ZeroTier Central
    token), some are made on a device of the estate and delivered by a command
    of their own (the UniFi key and the AdGuard login, `devices.py`), and some
    are installed by another tracker's automation entirely (the UDM and libvirt
    SSH identities, §3).

**A target has to be an address, not an intention.** Where §3 names a channel
that nothing has given a name yet -- an Environment secret no workflow reads, a
SealedSecret whose manifest does not exist -- the row records that in `pending`
rather than inventing a name a future workflow would have to guess right.
`derived ls` prints it, `derived sync` skips the row saying so, and `derived
sync --only <row>` refuses by name. That is the discipline the seed layer already
uses: a register row with no implementation is a command that refuses, not a
command that is missing.

Pushing is mint-free by construction: resolve, push, verify, every run. The
verification is what the channel allows -- a GitHub secret is never readable
again once written, so what is checked is that the name is in the listing and
its timestamp moved (`github_secrets.py`).
"""

from __future__ import annotations

import getpass
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ... import conventions
from . import derived, devices, escrow, pulumi_config
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
    #: (§4): the region, the compartment, the account id.
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
    """A secret pushed to the gateway beside its nspawn units (physical/gateway.md §1)."""

    what: str

    def __str__(self) -> str:
        return f'gw-config device secret: {self.what}'


#: Everything a row may be delivered into. `Slot` is the GitHub one, and the
#: only kind this module can fill.
Channel = Slot | PulumiConfig | PulumiState | EscrowCopy | SealedSecret | OnBox | WorkstationSlot | GwConfigSecret


# --------------------------------------------------------------------------
# The sources: where a push obtains the value, if it can obtain it at all.
# --------------------------------------------------------------------------


def _no_environment() -> Mapping[str, str]:
    """The default state-backend environment: none, for a run that reads no state."""
    return {}


@dataclass
class Context:
    """What a push may reach for, each part opened only when a row needs it.

    Everything here is lazy on purpose. Pushing the one manual row asks for no
    kit, pushing an escrowed row opens no state backend, and a machine holding
    neither can still run `derived ls`.
    """

    forge: Forge
    #: The kit's escrow, for a derived row. Called at most once.
    open_vault: Callable[[], escrow.Vault]
    #: `PULUMI_BACKEND_URL` and `PULUMI_CONFIG_PASSPHRASE`, for a state read.
    open_environment: Callable[[], Mapping[str, str]] = _no_environment
    #: The checkout holding `Pulumi.yaml`; a state read runs `pulumi` there.
    project: Path = field(default_factory=pulumi_config.project_dir)
    #: How that `pulumi` is invoked. A seam, so a state read is testable
    #: without a backend, exactly as the config slot's is.
    runner: pulumi_config.Runner = pulumi_config.run_pulumi
    #: How a manual row asks. `getpass`, so a typed value never echoes.
    ask: Callable[[str], str] = getpass.getpass
    _vault: escrow.Vault | None = field(default=None, init=False, repr=False)
    _environment: Mapping[str, str] | None = field(default=None, init=False, repr=False)

    @property
    def vault(self) -> escrow.Vault:
        if self._vault is None:
            self._vault = self.open_vault()
        return self._vault

    def stack(self, name: str) -> pulumi_config.Stack:
        if self._environment is None:
            self._environment = self.open_environment()
        return pulumi_config.Stack(name=name, directory=self.project, env=self._environment, run=self.runner)


class Source(Protocol):
    """Where one row's value comes from, and what to call that in a listing."""

    #: One word for `derived ls`: derived, minted, state-read, manual.
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
        return _text(outputs[self.output])


@dataclass(frozen=True)
class ConfigRead:
    """A key of a stack's committed configuration, read back for a second consumer.

    The same class of act as a state read -- the value is a fact the stack
    already carries, and this map copies it rather than deciding it -- so it
    lists as `state-read`. It exists for the one value CI needs that is not a
    credential at all: the ZeroTier network id, which the `physical` stack takes
    in plain text and a workflow can only pass as a secret.
    """

    kind: ClassVar[str] = 'state-read'

    stack: str
    key: str

    def describe(self) -> str:
        return f'`{self.stack}` stack configuration, key `{self.key}`'

    def value(self, context: Context) -> str:
        return context.stack(self.stack).get(self.key)


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


def _text(value: Any) -> str:
    """One stack output as the string a secret is: JSON for anything structured."""
    return value if isinstance(value, str) else json.dumps(value)


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
    source: Source
    targets: tuple[Channel, ...] = ()
    #: A channel §3 names for this row that has no address yet, and why. Empty
    #: when every slot the register promises is written down above.
    pending: str = ''

    @property
    def sinks(self) -> tuple[Slot, ...]:
        """The targets this module can fill: the GitHub secrets, and only those."""
        return tuple(target for target in self.targets if isinstance(target, Slot))


def _github(name: str, environments: tuple[str, ...], repository: str = REPOSITORY) -> tuple[Slot, ...]:
    return tuple(Slot(repository=repository, name=name, environment=environment) for environment in environments)


def _device(member: str) -> Row:
    """A §3 row whose credential is made on a device and typed in (`devices.py`).

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

#: The map, in §3's own order. Keys are the `credentials derived` row names:
#: the same string the command tree gives the row, so a row has one spelling.
ROWS: dict[str, Row] = {
    derived.OCI_PHYSICAL_ROW: Row(
        register='OCI API key (`physical`)',
        source=Minted(f'credentials derived {derived.OCI_PHYSICAL_ROW} mint'),
        targets=(
            PulumiConfig(PHYSICAL_STACK, derived.OCI_TENANCY_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_USER_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_FINGERPRINT_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_PRIVATE_KEY_KEY),
            PulumiConfig(PHYSICAL_STACK, derived.OCI_REGION_KEY, secret=False),
            PulumiConfig(PHYSICAL_STACK, derived.COMPARTMENT_KEY, secret=False),
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
    'cloudflare-gateway-acme': Row(
        register='Cloudflare token (gateway ACME)',
        source=Minted(
            'credentials derived cloudflare-gateway-acme mint', unbuilt='the gateway push is another tracker'
        ),
        targets=(GwConfigSecret("the UDM caddy's ACME token"),),
        pending="the gw-config push is the estate's other automation, so this side has no command",
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
        source=ConfigRead(PHYSICAL_STACK, 'zerotierNetworkId'),
        targets=_github('ZEROTIER_NETWORK_ID', ZEROTIER_PHYSICAL + ZEROTIER_DNS),
    ),
    'zerotier-central-token': Row(
        register='ZeroTier Central token, as delivered',
        source=Manual(
            'the ZeroTier Central API token',
            'It is the §2 kit row itself -- Central publishes no token API, so there is\n'
            '  nothing smaller to mint and nothing to rotate from here. `credentials kit\n'
            "  show seeds/zerotier` names it; the paste into the stack's config is the\n"
            '  whole of the delivery (§3).',
        ),
        targets=(
            PulumiConfig(PHYSICAL_STACK, 'zerotierApiToken'),
            PulumiConfig(PHYSICAL_STACK, 'zerotierNetworkId', secret=False),
        ),
        pending=_IN_STACK_CONFIG,
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
        source=Minted('state-backend bundle'),
        targets=(OnBox("the appliance's server certificate"), WorkstationSlot('state-backend/')),
        pending=(
            'the `ci` bundle has no sink: the workflows name `PULUMI_BACKEND_URL` alone, and a backend URL is '
            'unusable without the three certificate files it points at, which no workflow step materializes'
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
            PulumiConfig(PHYSICAL_STACK, 'gatewayHostKey'),
            PulumiConfig(PHYSICAL_STACK, 'libvirtPrivateKey'),
        ),
        pending=_IN_STACK_CONFIG,
    ),
    'unifi': _device('unifi'),
    'adguard': _device('adguard'),
    'alertmanager-read': Row(
        register='Alertmanager read token',
        source=Derived(escrow.ALERTMANAGER),
        targets=(EscrowCopy(escrow.ALERTMANAGER),),
        pending=(
            'neither the issue-sync poller nor the HTTPRoute that matches its header exists, so generating '
            'it now would park a secret (§1 rule 2)'
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
    """Fill every GitHub secret in the map that can be filled. Returns what was pushed.

    Resolve, push, verify -- per row, every run -- so a first fill and a refill
    are the same command. A row with no GitHub slot is skipped with its reason;
    asking for one by name is an error instead, because a request for a specific
    row that quietly does nothing is worse than a refusal.

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
        raise SlotRefused(f'no slot map row named {only!r}; `credentials derived ls` lists them')

    pushed: list[str] = []
    refused: list[str] = []
    for name, row in table.items():
        if only is not None and name != only:
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
            value = row.source.value(context)
        except SlotRefused as exc:
            if only is not None:
                raise
            log.error('%s: %s', name, exc)
            refused.append(name)
            continue
        for slot in row.sinks:
            log.info('%s: pushing to %s (gh encrypts it on the way out)', name, slot)
            context.forge.put(slot, value)
            _verify(context.forge, slot, before[slot])
            pushed.append(str(slot))

    # Deliberately after the walk rather than inside it: what is delivered is
    # delivered either way, and the exit status still says the map is not full.
    if refused:
        raise SlotRefused(f'{len(refused)} row(s) had no value to push: {", ".join(refused)}; each said why above')
    return pushed


__all__ = (
    'DRILL_ENVIRONMENT',
    'ENVIRONMENTS',
    'OPS_REPOSITORY',
    'REPOSITORY',
    'ROWS',
    'ConfigRead',
    'Context',
    'Derived',
    'Manual',
    'Minted',
    'Row',
    'Slot',
    'StateRead',
    'describe',
    'sync',
)
