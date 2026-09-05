"""The rewrites: which rows a route census implies, and how they are declared.

Two subjects, in the order the module puts them. The derivation is plain data
and needs no runtime: what a route implies is a function of the row. The
component is the declaration — which instance a row says it belongs to, where
that instance is reached, and what a resource is called. The provider's own
behaviour when any of those moves is `test_dns_adguard.py`.
"""

import importlib

import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.dns.rewrites import ResolverRewrites, Rewrite, rewrites
from kluster.providers import configured

ROUTE = conventions.routes.Route(host='photos', exposure=conventions.routes.Exposure.SPLIT, zones=('ucw.phd',))

ALICE = f'rewrites-{conventions.gateway.ADGUARD_ALICE.name}'
BOB = f'rewrites-{conventions.gateway.ADGUARD_BOB.name}'


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> Recorder:
    """One split-horizon route, declared once against each instance."""
    monitor = await run_with(Recorder(), stack='dns')
    async with declaring():
        for resolver in conventions.gateway.RESOLVERS:
            _ = ResolverRewrites(f'rewrites-{resolver.name}', resolver=resolver, entries=rewrites([ROUTE]))
    return monitor


def test_both_instances_are_written_to_directly(stack: Recorder) -> None:
    """Dual-writing is what retires adguardhome-sync.

    A synchronizer would overwrite whichever instance Pulumi wrote second, so
    the pair is two components rather than one plus a copier — and an instance
    that is down fails its own resources and leaves the other's converged.
    """
    names = {declaration.name for declaration in stack.declared if declaration.typ.startswith('pulumi-python:dynamic')}

    assert names == {
        f'{ALICE}-photos.ucw.phd-v4',
        f'{ALICE}-photos.ucw.phd-v6',
        f'{BOB}-photos.ucw.phd-v4',
        f'{BOB}-photos.ucw.phd-v6',
    }


def test_a_resource_is_named_after_the_instance_and_never_after_its_address(stack: Recorder) -> None:
    """A logical name is half of the URN state is keyed by (style/pulumi.md).

    Naming a rewrite after the address it is written at would make moving an
    instance a delete and a create of every rewrite on it. The name is built
    from the census entry, so neither the address nor any label spelled out of
    it appears — an address with its dots swapped for hyphens is still an
    address.
    """
    address = str(conventions.gateway.ADGUARD_ALICE.address)
    octets = address.split('.')

    for name in stack.names_declared:
        assert name.startswith((ALICE, BOB)), name
        assert address not in name
        assert '-'.join(octets) not in name


def test_a_rewrite_says_which_instance_it_belongs_to_and_where_that_instance_is(stack: Recorder) -> None:
    """Two properties, because they answer two questions.

    The instance is the resolver's census name and identifies the row; the
    endpoint is where this run reaches it, derived from the same census entry
    so that nothing can point a rewrite somewhere the flow rule does not admit.
    """
    inputs = stack.inputs_of(f'{ALICE}-photos.ucw.phd-v4')

    assert inputs['instance'] == conventions.gateway.ADGUARD_ALICE.name
    assert inputs['endpoint'] == conventions.gateway.resolver_api_url(conventions.gateway.ADGUARD_ALICE)


def test_a_rewrite_carries_the_vip_its_route_implies(stack: Recorder) -> None:
    inputs = stack.inputs_of(f'{ALICE}-photos.ucw.phd-v4')

    assert inputs['domain'] == 'photos.ucw.phd'
    assert inputs['answer'] == str(conventions.LAN_POOL.default_vip.v4)


def test_a_rewrite_declares_no_credential_and_no_stamp(stack: Recorder) -> None:
    """What the caller declares, and no more.

    The login is the provider's, read in `configure`; the two stamps are added
    by `check` in the plugin's process. A rewrite that carried either would put
    it in state on every row, on both instances, for every name.
    """
    inputs = stack.inputs_of(f'{ALICE}-photos.ucw.phd-v4')

    assert 'username' not in inputs
    assert 'password' not in inputs
    assert configured.SESSION not in inputs
    assert configured.PROVIDER_VERSION not in inputs


def test_the_package_does_not_shadow_this_module_with_the_function_it_holds() -> None:
    """`from kluster.components.dns import rewrites` is the module, not the function.

    The package exports `Rewrite` and `ResolverRewrites` and deliberately not
    the derivation: a package attribute of that name shadows the module that
    defines it. Nothing raises when it does — the import succeeds and binds the
    function, and the failure surfaces at the first attribute lookup on it,
    arbitrarily far away. It has already happened once, so the reason recorded
    in the package docstring is backed by this case rather than by care.

    Asked at runtime because that is where the binding is made: the attribute
    exists as a side effect of the package importing from the submodule, which
    a type checker does not see and this case therefore cannot ask statically.
    """
    package = importlib.import_module('kluster.components.dns')
    module = importlib.import_module('kluster.components.dns.rewrites')

    assert getattr(package, 'rewrites') is module
    assert 'rewrites' not in package.__all__


def test_a_public_route_needs_no_rewrite() -> None:
    # LAN clients take the cloud path for it, which is the whole difference.
    assert rewrites([conventions.routes.Route(host='www', exposure=conventions.routes.Exposure.PUBLIC)]) == ()


def test_a_split_route_is_rewritten_in_every_zone_it_is_published_in() -> None:
    route = conventions.routes.Route(
        host='photos', exposure=conventions.routes.Exposure.SPLIT, zones=('ucw.phd', 'peifeng.phd')
    )

    assert rewrites([route]) == (
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v6),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v6),
    )


def test_both_families_are_rewritten() -> None:
    """A LAN client that prefers IPv6 must not fall through to the public answer.

    AdGuard answers a rewrite only for the family of its answer, so a v4-only
    rewrite leaves AAAA resolving to the cloud path (RFC 6724).
    """
    entries = rewrites(
        [conventions.routes.Route(host='tube', exposure=conventions.routes.Exposure.SPLIT, zones=('ucw.phd',))]
    )

    assert {entry.answer for entry in entries} == {
        conventions.LAN_POOL.default_vip.v4,
        conventions.LAN_POOL.default_vip.v6,
    }
    assert {entry.answer.version for entry in entries} == {4, 6}


def test_an_iot_route_is_answered_by_the_media_vip() -> None:
    # Attaching to the media gateway *is* the "IoT may reach this" decision.
    route = conventions.routes.Route(host='tube', exposure=conventions.routes.Exposure.IOT, zones=('ucw.phd',))

    assert {entry.answer for entry in rewrites([route])} == {
        conventions.LAN_POOL.media_vip.v4,
        conventions.LAN_POOL.media_vip.v6,
    }


def test_a_lan_only_route_is_rewrite_only() -> None:
    """No public record, but the name still has to resolve on the LAN.

    Publishing nothing is what keeps the LAN service census out of public
    resolvers; the rewrite is the only thing that makes the name work.
    """
    route = conventions.routes.Route(host='golinks', exposure=conventions.routes.Exposure.LAN_ONLY, zones=('ucw.phd',))

    assert route.public is False
    assert len(rewrites([route])) == 2
