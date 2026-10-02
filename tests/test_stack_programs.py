"""The `k8s-base` and `apps` programs, declared against mocks.

Both are wiring and nothing else yet: each builds the providers its components
will be declared through (rfc-007 §3.1). What is held here is where those
providers' credentials come from -- the kubeconfig and the zones token, both
out of the stack's own configuration, the kubeconfig read so that anything but
a kubeconfig stops the run -- and that each committed stack file turns a missed
provider into an error.

Every run here is under the parent backstop `kluster.main` installs before a
real run declares anything, so a resource the program leaves unparented fails
the run here rather than in `pulumi preview`.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import pulumi
import pytest
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_under_backstop
from pulumi.runtime.rpc import UNKNOWN as UNKNOWN_SENTINEL

from kluster import conventions, stacks
from kluster.lib.k8s import KUBECONFIG_KEY, UnusableKubeconfig
from kluster.stacks import apps

#: A stand-in for the cluster-admin kubeconfig, which says what it is if it ever
#: reaches a diff.
KUBECONFIG = 'a-fake-kubeconfig-that-reaches-no-cluster'

#: A stand-in for the zones token, likewise.
ZONES_TOKEN = 'a-fake-zones-token-that-opens-nothing'

KUBERNETES_PROVIDER = 'pulumi:providers:kubernetes'
CLOUDFLARE_PROVIDER = 'pulumi:providers:cloudflare'
STACK_REFERENCE = 'pulumi:pulumi:StackReference'

#: The prefix every provider resource's type carries; what follows it is the
#: package the provider serves.
PROVIDER_PREFIX = 'pulumi:providers:'

#: Type prefixes that name no provider package: the engine's own (the stack,
#: every provider resource), the dynamic resources' (whose default provider
#: must stay enabled -- rfc-002 §8.1), and this repository's components, which
#: no provider serves.
NOT_PACKAGES = frozenset({'pulumi', 'pulumi-python', 'kluster'})

#: The checkout this file sits in, where the stack files are committed.
CHECKOUT = Path(__file__).resolve().parents[1]

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

#: The command a refusal sends the operator to, which fills the key.
FILLED_BY = f'credentials derived sync --only {KUBECONFIG_KEY}'


async def declare(name: str, *, kubeconfig: str | None = KUBECONFIG) -> Recorder:
    """One program, declared once, with `kubeconfig` in its configuration, or none at all."""
    config = {f'kluster:{apps.CLOUDFLARE_API_TOKEN}': ZONES_TOKEN}
    if kubeconfig is not None:
        config[f'kluster:{KUBECONFIG_KEY}'] = kubeconfig
    pulumi.runtime.set_all_config(config, secret_keys=list(config))
    monitor = await run_under_backstop(Recorder(), stack=name)
    async with declaring():
        await PROGRAMS[name]()
    return monitor


@pytest_asyncio.fixture(scope='module')
async def applied() -> dict[str, Recorder]:
    """Each program, declared with a kubeconfig in its configuration."""
    return {name: await declare(name) for name in PROGRAMS}


@pytest.mark.parametrize('name', PROGRAMS)
def test_each_program_builds_one_kubernetes_provider_from_its_configured_kubeconfig(
    applied: dict[str, Recorder], name: str
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
def test_no_program_reads_the_kubeconfig_across_a_stack_reference(applied: dict[str, Recorder], name: str) -> None:
    """Nothing is read from `physical` by StackReference: across its passphrase, a secret reads back elided.

    `physical` is encrypted under a passphrase of its own (rfc-005 §5.1), and a
    StackReference from a stack that cannot decrypt it hands back `{}` for the
    kubeconfig; the copy in the stack's own configuration is the only route.
    """
    assert STACK_REFERENCE not in applied[name].types


@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.asyncio
async def test_a_stack_with_no_kubeconfig_configured_stops_the_run_naming_the_copy(name: str) -> None:
    """Absent, it is refused at the read, sending the operator to the command that fills it.

    Not `require_secret`'s refusal, which tells the operator to `pulumi config
    set` a value by hand: the copy is a command, and the refusal names it.
    """
    with pytest.raises(UnusableKubeconfig, match=FILLED_BY):
        _ = await declare(name, kubeconfig=None)


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

    assert FILLED_BY in str(refused.value)
    assert ('apply physical in full' in str(refused.value).lower()) == (found == 'sentinel')


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


def test_apps_builds_its_cloudflare_provider_from_its_own_configuration(applied: dict[str, Recorder]) -> None:
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
    applied: dict[str, Recorder], name: str
) -> None:
    """The committed configuration turns a missed provider into an error.

    A resource that misses its explicit provider falls back to the package's
    default, which configures itself from ambient namespaces. Disabling it is
    decided per package, so the list names every package the program uses:
    those it builds a provider for, those it declares a resource of, and those
    it calls through. The equality runs the other way too, so a package the
    program no longer uses is a stale entry.
    """
    committed = CHECKOUT / f'Pulumi.{name}.yaml'
    run = applied[name]

    built = {typ.removeprefix(PROVIDER_PREFIX) for typ in run.types if typ.startswith(PROVIDER_PREFIX)}
    used = {typ.partition(':')[0] for typ in run.types} | {token.partition(':')[0] for token, _ in run.called}
    config = yaml.safe_load(committed.read_text()).get('config', {})

    assert set(config.get('pulumi:disable-default-providers', ())) == built | (used - NOT_PACKAGES)
