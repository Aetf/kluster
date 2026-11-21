"""The `k8s-base` and `apps` programs, declared against mocks.

What is held here is where each program's providers' credentials come from --
the kubeconfig and the zones token, both out of the stack's own configuration,
the kubeconfig read so that anything but a kubeconfig stops the run -- what
each reads across a StackReference, with every sealed value configured, as the
stack files hold them, read so that anything but the machine fact it names
stops the run, that what `apps` reads reaches
the application it wires, and that each committed stack file turns a missed
provider into an error. What `k8s-base` installs is `test_cilium.py`'s and
`test_standing_set.py`'s; what an application's component declares is
`test_apps.py`'s.

Every run here is under the parent backstop `kluster.main` installs before a
real run declares anything, so a resource the program leaves unparented fails
the run here rather than in `pulumi preview`.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from client_kubeconfig import KUBECONFIG
from committed_stack_files import StackFiles
from k8s_base_installation import (
    PUBLISHED,
    VERSIONS_CONFIG,
    ZONE_IDS,
    Physical,
    release_assets,
    sealed_values,
    sealed_without,
)
from mock_monitor import declaring, run_under_backstop
from pulumi.output import UNKNOWN
from pulumi.runtime.rpc import UNKNOWN as UNKNOWN_SENTINEL
from referenced_stack import reading_back

from kluster import conventions, stacks
from kluster.lib.k8s import KUBECONFIG_KEY, AbsentSealedValue, UnusableKubeconfig
from kluster.lib.stack_addresses import Held, UnusableAddressOutput
from kluster.stacks import apps

#: A stand-in for the zones token, which says what it is if it ever reaches a
#: diff.
ZONES_TOKEN = 'a-fake-zones-token-that-opens-nothing'

KUBERNETES_PROVIDER = 'pulumi:providers:kubernetes'
CLOUDFLARE_PROVIDER = 'pulumi:providers:cloudflare'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'

#: The prefix every provider resource's type carries; what follows it is the
#: package the provider serves.
PROVIDER_PREFIX = 'pulumi:providers:'

#: The outputs of `physical` the pools and the raw ports' fronts are made of
#: (rfc-007 §4.4, rfc-008 §4.1): the `internet` pool's members and the
#: worker's GUA, which both programs read across a StackReference.
POOL_OUTPUTS = frozenset(
    {
        conventions.PHYSICAL_OUTPUTS.node_private_ips,
        conventions.PHYSICAL_OUTPUTS.node_guas,
        conventions.PHYSICAL_OUTPUTS.vip1_private,
        conventions.PHYSICAL_OUTPUTS.cluster_endpoint,
        conventions.PHYSICAL_OUTPUTS.cluster_endpoint_v6,
        conventions.PHYSICAL_OUTPUTS.worker_gua,
    }
)

#: Type prefixes that name no provider package: the engine's own (the stack,
#: every provider resource), the dynamic resources' (whose default provider
#: must stay enabled -- rfc-002 §8.1), and this repository's components, which
#: no provider serves.
NOT_PACKAGES = frozenset({'pulumi', 'pulumi-python', 'kluster'})

#: The checkout this file sits in, where the stack files are committed.
CHECKOUT = Path(__file__).resolve().parents[1]


def _extensions() -> dict[str, str]:
    """Extension package → the provider that serves it, read off each generated SDK's plugin record.

    The record is the SDK generator's, so a package added under `sdks/` is
    counted under the provider it says serves it without anyone listing it.
    """
    served: dict[str, str] = {}
    for record in sorted((CHECKOUT / 'sdks').glob('*/pulumi_*/pulumi-plugin.json')):
        plugin = json.loads(record.read_text())
        extension = plugin.get('extensionParameterization')
        if extension is not None:
            served[extension['name']] = plugin['name']
    return served


#: Extension package → the provider that serves it: the CRD SDK's `crds`,
#: which the `kubernetes` provider serves.
EXTENSIONS = _extensions()

#: What each program reads across a StackReference, by stack: the pools'
#: outputs; for `apps`, the `dns` stack's zone identifiers its records are
#: declared in; and for `k8s-base`, the volumes' OCIDs the pull of OCI's
#: block-volume series is configured with.
READS: dict[str, frozenset[str]] = {
    conventions.STACK_NAMES.k8s_base: POOL_OUTPUTS | {conventions.PHYSICAL_OUTPUTS.volume_ids},
    conventions.STACK_NAMES.apps: POOL_OUTPUTS | {apps.DNS_ZONE_IDS},
}

#: Stack name → the program it runs, read off the dispatch table, so a case
#: parametrized over it asks about the program `pulumi up -s <name>` runs.
PROGRAMS: dict[str, Callable[[], Awaitable[None]]] = {
    name: stacks.STACKS[name] for name in (conventions.STACK_NAMES.k8s_base, conventions.STACK_NAMES.apps)
}

#: A configured kubeconfig that is present and no kubeconfig, and the part of
#: the refusal that names it.
UNUSABLE: dict[str, tuple[str, str]] = {
    'empty': ('', 'a blank string'),
    'blank': (' \n', 'a blank string'),
    # The unknown sentinel a targeted apply of `physical` exports, as a copy
    # of that output would carry it: an ordinary string.
    'sentinel': (UNKNOWN_SENTINEL, 'unknown sentinel'),
}

#: The command a refusal sends the operator to, which fills the key: the
#: copy, confined to that key's row, as `filled_by` reads it.
FILLED_BY = ('derived', 'sync', KUBECONFIG_KEY)


def filled_by(message: str) -> list[tuple[str, str, str | None]]:
    """Each `credentials` command `message` names, as the parser reads it: subject, row, and the row `--only` names.

    The `--only` value is held as well as the leaf: `derived sync` alone
    copies every row it can, which reads the admin token, and the copy this
    key needs is the one that reads none.
    """
    from credentials_command_tree import named_commands

    return [(args['subject'], args['member'], args.get('only')) for _, args in named_commands(message)]


#: What `physical` publishes as its kubeconfig in this suite: a string no other
#: fixture holds, so a registration carrying it carries the reference's
#: kubeconfig, however the program reached it.
PUBLISHED_KUBECONFIG = 'the-kubeconfig-physical-published-which-no-registration-may-carry'


class Run(Physical):
    """One program's run against a fully applied `physical`, a `dns` that publishes `dns_outputs`, and every output it read across a StackReference by name."""

    def __init__(self, dns_outputs: dict[str, object] | None = None) -> None:
        super().__init__(PUBLISHED | {conventions.PHYSICAL_OUTPUTS.kubeconfig: PUBLISHED_KUBECONFIG}, dns_outputs)
        #: The output names asked of any StackReference, in the order asked.
        self.read_across: list[str] = []


async def declare(
    name: str,
    *,
    kubeconfig: str | None = KUBECONFIG,
    preview: bool = False,
    sealed: dict[str, object] | None = None,
    dns_outputs: dict[str, object] | None = None,
) -> Run:
    """One program, declared once, with `kubeconfig` in its configuration, or none at all.

    The configuration holds every sealed value the census has the stack
    declare (`sealed_values`), as the committed stack files do, unless
    `sealed` is given.
    `dns_outputs` is what the `dns` stack publishes, its zone identifiers
    unless given.

    The reads through a StackReference's by-name methods -- `get_output`,
    `require_output`, `get_output_details` -- are recorded on the way through.
    The reference's `outputs` mapping is no by-name read and is not recorded,
    which is why the kubeconfig is also held by value: `physical` publishes it
    here as `PUBLISHED_KUBECONFIG`.
    """
    secrets = {f'kluster:{apps.CLOUDFLARE_API_TOKEN}': ZONES_TOKEN}
    if kubeconfig is not None:
        secrets[f'kluster:{KUBECONFIG_KEY}'] = kubeconfig
    plain = {f'kluster:{conventions.sealed.CONFIG_KEY}': json.dumps(sealed_values(name) if sealed is None else sealed)}
    pulumi.runtime.set_all_config(secrets | plain | VERSIONS_CONFIG, secret_keys=list(secrets))
    monitor = await run_under_backstop(Run(dns_outputs), stack=name, preview=preview)

    def recording(read: Callable[..., Any]) -> Callable[..., Any]:
        def read_by_name(reference: pulumi.StackReference, output: str) -> Any:
            monitor.read_across.append(output)
            return read(reference, output)

        return read_by_name

    with pytest.MonkeyPatch.context() as patch, release_assets():
        for method in ('get_output', 'require_output', 'get_output_details'):
            patch.setattr(pulumi.StackReference, method, recording(getattr(pulumi.StackReference, method)))
        async with declaring():
            await PROGRAMS[name]()
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def applied() -> dict[str, Run]:
    """Each program, declared against a `physical` that has published.

    Its configuration holds a kubeconfig and every sealed value its stack
    declares.
    """
    return {name: await declare(name) for name in PROGRAMS}


@pytest.mark.parametrize('name', PROGRAMS)
def test_each_program_builds_one_kubernetes_provider_from_its_configured_kubeconfig(
    applied: dict[str, Run], name: str
) -> None:
    """One provider, opened with the kubeconfig under the key the copy fills, kept secret.

    One, because every component of the stack is declared through it and a
    second would be a cluster some of them reach and others do not. The value
    is compared rather than its presence, so a program reading some other key
    fails here; and it reaches the provider as a secret, since it is the
    cluster-admin credential.
    """
    built = applied[name].of_type(KUBERNETES_PROVIDER)

    assert len(built) == 1
    assert built[0].inputs['kubeconfig']['value'] == KUBECONFIG
    assert ':' not in KUBECONFIG_KEY


@pytest.mark.parametrize('name', PROGRAMS)
def test_no_program_reads_the_kubeconfig_across_a_stack_reference(applied: dict[str, Run], name: str) -> None:
    """Each program reads only its machine facts from `physical`, and no kubeconfig crosses.

    `physical` is encrypted under a passphrase of its own (rfc-005 §5.1), and a
    StackReference from a stack that cannot decrypt it hands back `{}` for the
    kubeconfig; the copy in the stack's own configuration is the only route.
    What does cross is plain machine facts a program declares something
    from: the pools' members, which `k8s-base` builds its pools from and
    `apps` hands a raw port's Service (rfc-007 §3.1, §4.4; rfc-008 §4.1);
    the `dns` stack's zone identifiers, which `apps` declares its records
    in (rfc-007 §3.1); and the volumes' OCIDs, which `k8s-base` configures
    the pull of OCI's block-volume series with (`READS`).

    Held two ways. By name, the by-name reads are the program's machine facts
    and nothing else. By value, the kubeconfig `physical` publishes here
    reaches no registration's inputs, providers included, which a read
    through the reference's `outputs` mapping would otherwise get past.
    """
    run = applied[name]
    read = set(run.read_across)
    carrying = [
        f'{request.type}::{request.name}' for request in run.requested if PUBLISHED_KUBECONFIG in str(request.object)
    ]

    assert carrying == []
    assert conventions.PHYSICAL_OUTPUTS.kubeconfig not in read
    assert read == READS[name]


@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.asyncio
async def test_a_stack_with_no_kubeconfig_configured_stops_the_run_naming_the_copy(name: str) -> None:
    """Absent, it is refused at the read, sending the operator to the command that fills it.

    Not `require_secret`'s refusal, which tells the operator to `pulumi config
    set` a value by hand: the copy is a command, and the refusal names it.
    """
    with pytest.raises(UnusableKubeconfig) as refused:
        _ = await declare(name, kubeconfig=None)

    assert FILLED_BY in filled_by(str(refused.value))


@pytest.mark.parametrize('found', UNUSABLE)
@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.asyncio
async def test_a_configured_kubeconfig_that_is_not_one_stops_the_run(name: str, found: str) -> None:
    """Anything but a non-blank string that is not the sentinel is refused by name, and opens no provider.

    The provider reads a kubeconfig it cannot load as an unreachable cluster,
    so the run would go on against something other than this installation's
    cluster. The refusal says what it found, and names the copy that replaces
    it. Only the sentinel sends the operator to apply `physical` first: it is
    what a targeted apply leaves, while a blank copy is replaced by the copy
    alone.
    """
    value, named = UNUSABLE[found]

    with pytest.raises(UnusableKubeconfig, match=named) as refused:
        _ = await declare(name, kubeconfig=value)

    assert FILLED_BY in filled_by(str(refused.value))
    assert ('apply physical in full' in str(refused.value).lower()) == (found == 'sentinel')


#: Every sealed value the census has `k8s-base` declare.
K8S_BASE_VALUES: list[conventions.sealed.SealedValue] = [
    value for value in conventions.sealed.VALUES.values() if value.stack == conventions.STACK_NAMES.k8s_base
]


@pytest.mark.parametrize('value', K8S_BASE_VALUES, ids=[value.name for value in K8S_BASE_VALUES])
@pytest.mark.asyncio
async def test_a_sealed_value_the_stack_does_not_hold_stops_k8s_base_at_its_read(
    value: conventions.sealed.SealedValue,
) -> None:
    """Every value the program declares reaches the read that refuses an absent one, and no component reads around it.

    The stack holds every other value the census has it declare, so the run
    stops at this one's read and at no other: a value read some other way,
    or read only where something else is present, would let the run go on
    and declare a consumer of a Secret that is not there.
    """
    with pytest.raises(AbsentSealedValue) as refused:
        _ = await declare(conventions.STACK_NAMES.k8s_base, sealed=sealed_without(value))

    assert refused.value.value == value


def test_the_kubeconfig_is_copied_under_the_key_both_programs_read() -> None:
    """The slot map's targets for the kubeconfig are both stacks, under the programs' key.

    `sync` writes the key the map names, so a key renamed in the programs alone
    leaves the copy filling a slot nothing reads while both stacks refuse for a
    value that is there under its old name; and a stack dropped from the map is
    one the copy never reaches.
    """
    from kluster.scripts.credentials import slots

    copied = {(target.stack, target.key) for target in slots.ROWS['kubeconfig'].config_sinks}

    assert copied == {(name, KUBECONFIG_KEY) for name in PROGRAMS}
    assert slots.KUBECONFIG_KEY == KUBECONFIG_KEY


def test_apps_builds_its_cloudflare_provider_from_its_own_configuration(applied: dict[str, Run]) -> None:
    """The zones token is read at the line that builds the provider it opens.

    One provider for every zone, opened with the value under this project's
    key rather than with anything the provider package would find for itself.
    """
    built = applied[conventions.STACK_NAMES.apps].of_type(CLOUDFLARE_PROVIDER)

    assert len(built) == 1
    assert built[0].inputs['apiToken']['value'] == ZONES_TOKEN
    assert ':' not in apps.CLOUDFLARE_API_TOKEN


def test_the_zones_token_is_delivered_under_the_key_apps_reads() -> None:
    """The slot map's `apps` target and the program's key are one key.

    The value is delivered by the zones row's mint, so a key renamed in the
    program alone leaves the delivery filling a slot nothing reads while the
    stack refuses for a value present under its old name.
    """
    from kluster.scripts.credentials import derived, slots

    delivered = {
        target.key
        for target in slots.ROWS[derived.ZONES_ROW].targets
        if isinstance(target, slots.PulumiConfig) and target.stack == conventions.STACK_NAMES.apps
    }

    assert delivered == {apps.CLOUDFLARE_API_TOKEN}


@pytest.mark.parametrize('name', PROGRAMS)
def test_each_stack_file_disables_the_defaults_of_every_package_its_program_uses(
    applied: dict[str, Run], name: str, committed_stack_files: StackFiles
) -> None:
    """The committed configuration turns a missed provider into an error.

    A resource that misses its explicit provider falls back to the package's
    default, which configures itself from ambient namespaces. Disabling it is
    decided per package, so the list names every package the program uses:
    those it builds a provider for, those it declares a resource of, and those
    it calls through. An extension package's resources are served by the
    provider it extends, and a missed one asks for that provider's default, so
    it counts as that provider (`EXTENSIONS`). The equality runs the other way
    too, so a package the program no longer uses is a stale entry.
    """
    run = applied[name]

    def serving(package: str) -> str:
        return EXTENSIONS.get(package, package)

    built = {typ.removeprefix(PROVIDER_PREFIX) for typ in run.types if typ.startswith(PROVIDER_PREFIX)}
    used = {serving(typ.partition(':')[0]) for typ in run.types} | {
        serving(token.partition(':')[0]) for token, _ in run.called
    }
    config = committed_stack_files.config(name)

    assert set(config.get('pulumi:disable-default-providers', ())) == built | (used - NOT_PACKAGES)


@pytest.mark.parametrize(
    ('output', 'value', 'held'),
    [
        (conventions.PHYSICAL_OUTPUTS.node_private_ips, UNKNOWN, Held.UNKNOWN),
        (conventions.PHYSICAL_OUTPUTS.worker_gua, None, Held.ABSENT),
    ],
    ids=['node-ips-unknown', 'worker-gua-absent'],
)
@pytest.mark.asyncio
async def test_a_front_apps_reads_that_is_not_an_address_stops_its_run_by_name(
    output: str, value: object, held: Held
) -> None:
    """`apps` reads the fronts' addresses so that anything but an address stops the run, naming the output.

    The run stops whether or not a component it wires declares a raw port:
    a Service declared from what a targeted apply of `physical` left behind
    would carry `"None"` as its address.
    """
    with reading_back({output: value}), pytest.raises(UnusableAddressOutput) as refused:
        _ = await declare(conventions.STACK_NAMES.apps, preview=value is UNKNOWN)

    assert repr(output) in str(refused.value)
    assert (refused.value.output, refused.value.held) == (output, held)


def test_apps_declares_the_echo_servers_record_in_the_zone_identifier_dns_publishes(applied: dict[str, Run]) -> None:
    """The record goes in the zone the `dns` stack holds, by the identifier that stack published for it."""
    found = {
        inputs['name']: inputs['zoneId'] for inputs in applied[conventions.STACK_NAMES.apps].by_name(RECORD).values()
    }

    assert found == {f'echo.{conventions.ZONE_PRIMARY}': ZONE_IDS[conventions.ZONE_PRIMARY]}


def test_apps_runs_the_echo_server_from_its_pin(applied: dict[str, Run]) -> None:
    (deployment,) = applied[conventions.STACK_NAMES.apps].by_name(DEPLOYMENT).values()
    (container,) = deployment['spec']['template']['spec']['containers']

    assert container['image'] == VERSIONS_CONFIG['versions:image-echo']


@pytest.mark.asyncio
async def test_apps_with_no_zone_identifiers_published_stops_its_run_naming_the_output() -> None:
    """A `dns` stack that publishes none has not run, and a record declared without its zone goes nowhere."""
    with pytest.raises(KeyError, match=apps.DNS_ZONE_IDS):
        _ = await declare(conventions.STACK_NAMES.apps, dns_outputs={})


@pytest.mark.asyncio
async def test_the_dns_stack_publishes_each_zones_identifier_under_the_name_apps_reads() -> None:
    """The producer and the reader of one output name it alike, and every zone a record of `apps` is in is there."""
    from kluster.stacks import dns

    exported: dict[str, Any] = {}
    pulumi.runtime.set_all_config({f'kluster:{dns.CLOUDFLARE_API_TOKEN}': ZONES_TOKEN})
    _ = await run_under_backstop(Physical(), stack=conventions.STACK_NAMES.dns)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi, 'export', exported.__setitem__)
        async with declaring():
            await dns.main()

    zone_ids = await pulumi.Output.from_input(exported[apps.DNS_ZONE_IDS]).future()
    assert zone_ids is not None
    assert {name.zone for name in conventions.routes.public_names()} <= set(zone_ids)
