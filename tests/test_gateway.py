"""The gateway's own wiring, asserted against Pulumi's mock provider.

What the component decides for itself rather than delegating: which layers it
builds and in which order they are allowed to actuate, what package set the
device is asked for, and the one file that answers to a daemon instead of to
the boot chain.
"""

from __future__ import annotations

from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import Gateway, access, container, nspawn, persistence, routing, unifi

NAME = 'kluster'
HOST = 'gateway.invalid'
SITE = 'default'
API_KEY = 'unifi-api-key'
BGP_PASSWORD = 'a-session-password'
ACME_TOKEN = 'a-zone-scoped-token'
#: The one key the stack declares, as the device holds it. Invented here; the
#: shape of an `authorized_keys` line is all this suite needs of it.
CI_KEY = access.PublicKey(name='kluster-physical', key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIci kluster-physical@gw')
DIGEST = f'sha256:{"e" * 64}'


class Controller(Recorder):
    """A monitor that answers the firewall's zone lookups; every zone exists."""

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'unifi:index/getFirewallZone:getFirewallZone':
            name = str(cast('dict[str, Any]', args.args)['name'])
            return {'id': f'zone-{name}', 'name': name, 'networks': [], 'site': SITE}
        return {}


def pin(service: conventions.gateway.ContainerService) -> container.Rootfs:
    return container.Rootfs(repository=f'registry.invalid/installation/{service.artifact}', tag='7', digest=DIGEST)


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Controller:
    pulumi.runtime.set_all_config({f'kluster:{unifi.API_KEY}': API_KEY})
    return await run_with(Controller(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def gateway(monitor: Controller) -> Gateway:
    """The whole device, declared once the way the stack program declares it."""
    async with declaring():
        device = Gateway(
            NAME,
            host=HOST,
            caddy=container.CaddyService(
                service=conventions.gateway.CADDY,
                pin=pin(conventions.gateway.CADDY),
                acme_token=ACME_TOKEN,
                vhosts=conventions.gateway.RESOLVERS,
                legacy=conventions.gateway.LEGACY_VHOSTS,
            ),
            resolvers=tuple(
                container.ResolverService(service=resolver, pin=pin(resolver))
                for resolver in conventions.gateway.RESOLVERS
            ),
            overlay_daemon=container.OverlayDaemon(
                service=conventions.gateway.OVERLAY, pin=pin(conventions.gateway.OVERLAY)
            ),
            routing=routing.RoutingSession(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD),
            keys=(CI_KEY,),
            site=SITE,
            worker_gua=None,
        )
    return device


@pytest.mark.asyncio
async def test_the_machine_carrying_the_session_actuates_after_every_other_child(
    gateway: Gateway, monitor: Controller
) -> None:
    """Restarting the overlay daemon drops the session the apply is riding.

    Once the device is a member, *any* resource's session may ride the tunnel
    that one container carries — not only the other containers' — so the
    dependency covers every other child of this component and not merely its
    siblings among the workloads. An apply that dies on that last restart has
    already done the rest of its work, and the retry finds it done.
    """
    # A dependency on a component reaches the engine as its children, so what
    # every other child contributes is read back through one resource of each.
    elsewhere = (
        gateway.persistence.units,
        gateway.runtime.machines,
        *(workload.settings for workload in gateway.containers),
        gateway.routing.config,
        gateway.access.keys[CI_KEY.name],
        gateway.firewall.network,
    )
    expected = {str(await resource.urn.future()) for resource in elsewhere}

    for name in (f'{NAME}-zerotier-nspawn', f'{NAME}-zerotier-image'):
        assert expected <= set(monitor.depends_on(name)), name

    # And a workload that carries no session waits for none of it: the firewall
    # and the other machines are behind the overlay daemon, not ahead of it.
    assert str(await gateway.firewall.network.urn.future()) not in monitor.depends_on(f'{NAME}-caddy-nspawn')


def test_the_device_is_asked_for_what_the_layers_above_the_mechanism_require(monitor: Controller) -> None:
    """The package list is data the mechanism renders, not a set it decides.

    One layer requires anything today, so the union is that layer's constant;
    what makes it a union is that the mechanism is handed it rather than
    holding it.
    """
    script = str(monitor.inputs_of(f'{NAME}-persistence-on-boot-{persistence.PACKAGES_SCRIPT}')['content'])

    for package in nspawn.NspawnRuntime.REQUIRED_PACKAGES:
        assert package in script, package


def test_the_routing_configuration_answers_to_its_daemon_and_not_to_the_boot_chain(monitor: Controller) -> None:
    """It is the one piece of desired state on this device with no machine behind it.

    So what applies it is a converger of its own — an executable the file's
    hook runs and a unit runs at boot — rather than one of the boot chain's
    scripts, and it is secret because the session password is in it.
    """
    config = monitor.inputs_of(f'{NAME}-routing-config')

    assert config['path'] == routing.FRR_CONFIG
    assert config['hook'] == routing.converger_hook()
    assert config['mode'] == routing.FRR_MODE
    assert f'password {BGP_PASSWORD}' in config['content']


def test_the_device_keeps_accepting_the_key_this_program_dials_with(monitor: Controller) -> None:
    """The door the push comes through is desired state like everything else.

    `/root` is off `/data`, so the key that authorizes every session is as
    perishable as the rest of the customization, and the component that keeps
    it there is a child of this one.
    """
    declared = monitor.inputs_of(f'{NAME}-access-key-{CI_KEY.name}')

    assert declared['path'] == access.key_path(CI_KEY.name)
    assert declared['content'].strip() == CI_KEY.key


def test_the_gateway_declares_one_machine_per_service_and_nothing_else(monitor: Controller) -> None:
    """A fifth service is a change to this component's signature.

    The set the runtime converges and the set of components that fill those
    machines are the same set, taken from the same declarations, so neither can
    name a machine the other does not.
    """
    trees = {str(image.inputs['root']) for image in monitor.of_type('pulumi-python:dynamic/device:Artifact')}  # noqa: S105 -- a type, not a credential
    declared = str(monitor.inputs_of(f'{NAME}-persistence-on-boot-{nspawn.MACHINES_SCRIPT}')['content'])
    machines = next(line for line in declared.splitlines() if line.startswith('DECLARED='))

    assert trees == {nspawn.rootfs_path(service.name) for service in conventions.gateway.SERVICES}
    assert set(machines.removeprefix('DECLARED="').rstrip('"').split()) == {
        service.name for service in conventions.gateway.SERVICES
    }
