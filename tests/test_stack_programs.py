"""The `k8s-base` and `apps` programs, declared against mocks.

Both are wiring and nothing else yet: each builds the providers its components
will be declared through (rfc-007 §3.1). What is held here is where those
providers' credentials come from -- the kubeconfig across the StackReference to
`physical`, read so that anything but a kubeconfig stops the run, and the zones
token out of `apps`'s own configuration -- and that each committed stack file
turns a missed provider into an error.

Every run here is under the parent backstop `kluster.main` installs before a
real run declares anything, so a resource the program leaves unparented fails
the run here rather than in `pulumi preview`.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pulumi
import pytest
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_under_backstop
from pulumi.output import UNKNOWN
from pulumi.runtime.rpc import UNKNOWN as UNKNOWN_SENTINEL

from kluster import conventions, stacks
from kluster.lib.k8s import UnusableKubeconfig
from kluster.stacks import apps

#: A stand-in for the cluster-admin kubeconfig, which says what it is if it ever
#: reaches a diff.
KUBECONFIG = 'a-fake-kubeconfig-that-reaches-no-cluster'

#: A stand-in for the zones token, likewise.
ZONES_TOKEN = 'a-fake-zones-token-that-opens-nothing'

KUBERNETES_PROVIDER = 'pulumi:providers:kubernetes'
CLOUDFLARE_PROVIDER = 'pulumi:providers:cloudflare'

#: The prefix every provider resource's type carries; what follows it is the
#: package the provider serves.
PROVIDER_PREFIX = 'pulumi:providers:'

#: Type prefixes that name no provider package: the engine's own (the stack,
#: a StackReference, every provider resource), the dynamic resources' (whose
#: default provider must stay enabled -- rfc-002 §8.1), and this repository's
#: components, which no provider serves.
NOT_PACKAGES = frozenset({'pulumi', 'pulumi-python', 'kluster'})

#: The checkout this file sits in, where the stack files are committed.
CHECKOUT = Path(__file__).resolve().parents[1]

#: Stack name → the program it runs, read off the dispatch table, so a case
#: parametrized over it asks about the program `pulumi up -s <name>` runs.
PROGRAMS: dict[str, Callable[[], Awaitable[None]]] = {
    name: stacks.STACKS[name] for name in (conventions.STACK_NAMES.k8s_base, conventions.STACK_NAMES.apps)
}

#: What a StackReference to a `physical` that publishes a kubeconfig output
#: can read back in place of one, and the part of the refusal that names it.
UNUSABLE: dict[str, tuple[object, str]] = {
    # A secret the reader cannot decrypt, elided by the engine (rfc-005 §5.1).
    'elided-secret': ({}, 'cannot open'),
    # The unknown sentinel a targeted apply exports, as the SDK reads it back.
    'unknown': (UNKNOWN, 'unknown'),
    # The sentinel as a string, should it reach the reader undeserialized.
    'sentinel': (UNKNOWN_SENTINEL, 'unknown'),
    # The sentinel stored encrypted, which reads back as nothing.
    'none': (None, 'unknown'),
    'empty': ('', 'an empty string'),
}


class Physical(Recorder):
    """A `physical` stack whose outputs are `published`, as its StackReference reads them."""

    def __init__(self, published: dict[str, Any]) -> None:
        super().__init__()
        self.published = published

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': self.published}
        return {}


async def declare(name: str, published: dict[str, Any]) -> Physical:
    """One program, declared once against a `physical` that published `published`."""
    pulumi.runtime.set_all_config({f'kluster:{apps.CLOUDFLARE_API_TOKEN}': ZONES_TOKEN})
    monitor = await run_under_backstop(Physical(published), stack=name)
    async with declaring():
        await PROGRAMS[name]()
    return monitor


@pytest_asyncio.fixture(scope='module')
async def applied() -> dict[str, Physical]:
    """Each program, declared against a `physical` that has published its kubeconfig."""
    published = {conventions.PHYSICAL_OUTPUTS.kubeconfig: KUBECONFIG}
    return {name: await declare(name, published) for name in PROGRAMS}


@pytest.mark.parametrize('name', PROGRAMS)
def test_each_program_builds_one_kubernetes_provider_from_the_published_kubeconfig(
    applied: dict[str, Physical], name: str
) -> None:
    """One provider, opened with the output `physical` publishes under the census's name.

    One, because every component of the stack is declared through it and a
    second would be a cluster some of them reach and others do not. The value
    is compared rather than its presence, so a program reading some other
    output of `physical` fails here.
    """
    built = applied[name].of_type(KUBERNETES_PROVIDER)

    assert len(built) == 1
    assert built[0].inputs['kubeconfig'] == KUBECONFIG


@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.asyncio
async def test_a_physical_stack_with_no_kubeconfig_stops_the_run(name: str) -> None:
    """Read with `require_output`, so a missing kubeconfig is an error rather than an unknown.

    An unknown kubeconfig would make the provider treat the cluster as
    unreachable: plain resources would preview green by echoing their inputs,
    and every chart would refuse with a message that names no output
    (rfc-007 §3.1). The error names the output instead.
    """
    with pytest.raises(KeyError, match=conventions.PHYSICAL_OUTPUTS.kubeconfig):
        _ = await declare(name, {})


@pytest.mark.parametrize('found', UNUSABLE)
@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.asyncio
async def test_a_kubeconfig_output_that_is_not_one_stops_the_run(name: str, found: str) -> None:
    """Anything but a non-empty string is refused by name, and no provider is opened with it.

    The provider reads a kubeconfig it cannot load as an unreachable cluster,
    and one handed nothing falls back to `$KUBECONFIG`; either way the run
    would go on against something other than this installation's cluster.
    The refusal says what it found and never quotes it.

    The read is answered here rather than by the mock monitor, which drops an
    output that is `None` or unknown on its way to the reader and so can only
    present the absence `require_output` already refuses.
    """
    value, named = UNUSABLE[found]

    def read_back(_reference: pulumi.StackReference, output: pulumi.Input[str]) -> pulumi.Output[Any]:
        assert output == conventions.PHYSICAL_OUTPUTS.kubeconfig
        return pulumi.Output.from_input(value)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'require_output', read_back)
        with pytest.raises(UnusableKubeconfig, match=named):
            _ = await declare(name, {conventions.PHYSICAL_OUTPUTS.kubeconfig: KUBECONFIG})


def test_apps_builds_its_cloudflare_provider_from_its_own_configuration(applied: dict[str, Physical]) -> None:
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
    applied: dict[str, Physical], name: str
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
