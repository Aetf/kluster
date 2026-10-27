"""The gateway's own wiring, asserted against Pulumi's mock provider.

What the component decides for itself rather than delegating: which layers it
builds and in which order they are allowed to actuate, what package set the
device is asked for, and the one file that answers to a daemon instead of to
the boot chain.

The run is under the parent backstop `kluster.main` installs before a real run
declares anything, so a child the component tree leaves unparented fails here
rather than in `pulumi preview`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import device_shell
import process_sessions
import pulumi
import pytest
import pytest_asyncio
from device_places import DEVICE_ARTIFACT, DEVICE_FILE
from fake_systemd import FakeSystemd
from mock_monitor import declaring, run_under_backstop
from unifi_controller import Controller

from kluster import conventions
from kluster.components.gateway import (
    BACKUP_PULL_PACKAGES,
    HEALTH_PROGRAM,
    PLAYBOOKS,
    Gateway,
    access,
    container,
    nspawn,
    persistence,
    routing,
    unifi,
)

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
#: The cutover runbook, which targets this component's subtree by URN.
RUNBOOK = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway-cutover.md'


def pin(service: conventions.gateway.ContainerService) -> container.Rootfs:
    return container.Rootfs(repository=f'registry.invalid/installation/{service.artifact}', tag='7', digest=DIGEST)


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def monitor() -> Controller:
    pulumi.runtime.set_all_config({f'kluster:{unifi.API_KEY}': API_KEY})
    return await run_under_backstop(Controller(site=SITE), stack='physical')


def declare(name: str, host: str) -> Gateway:
    """The whole device, the way the stack program declares it."""
    return Gateway(
        name,
        host=host,
        caddy=container.CaddyService(
            service=conventions.gateway.CADDY,
            pin=pin(conventions.gateway.CADDY),
            acme_token=ACME_TOKEN,
            vhosts=conventions.gateway.RESOLVERS,
            legacy=conventions.gateway.LEGACY_VHOSTS,
        ),
        resolvers=tuple(
            container.ResolverService(service=resolver, pin=pin(resolver)) for resolver in conventions.gateway.RESOLVERS
        ),
        overlay_daemon=container.OverlayDaemon(
            service=conventions.gateway.OVERLAY, pin=pin(conventions.gateway.OVERLAY)
        ),
        routing=routing.RoutingSession(neighbor=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD),
        keys=(CI_KEY,),
        site=SITE,
        worker_gua=None,
        static_hosts={},
    )


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def gateway(monitor: Controller) -> Gateway:
    """The device every case below reads, declared once."""
    async with declaring():
        device = declare(NAME, HOST)
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
        gateway.health,
    )
    expected = {str(await resource.urn.future()) for resource in elsewhere}

    for name in (f'{NAME}-zerotier-nspawn', f'{NAME}-zerotier-image'):
        assert expected <= set(monitor.depends_on(name)), name

    # And a workload that carries no session waits for none of it: the firewall
    # and the other machines are behind the overlay daemon, not ahead of it.
    assert str(await gateway.firewall.network.urn.future()) not in monitor.depends_on(f'{NAME}-caddy-nspawn')


def rendered_packages(monitor: Controller) -> list[str]:
    """The package set the device's boot chain reinstalls, as the script spells it."""
    script = str(monitor.inputs_of(f'{NAME}-persistence-on-boot-{persistence.PACKAGES_SCRIPT}')['content'])
    declared = re.search(r'^PACKAGES=\((.*)\)$', script, re.MULTILINE)
    assert declared is not None
    return declared[1].split()


def test_the_device_is_asked_for_what_the_layers_above_the_mechanism_require(monitor: Controller) -> None:
    """The package list is data the mechanism renders, not a set it decides.

    Each consumer states what it requires as a constant, and the union is
    what the gateway hands the mechanism rather than a set the mechanism holds.
    """
    packages = rendered_packages(monitor)

    for package in nspawn.NspawnRuntime.REQUIRED_PACKAGES:
        assert package in packages, package


def test_the_device_keeps_the_program_the_backup_pull_runs_on_its_end(monitor: Controller) -> None:
    """The controller's autobackups leave the device over `rsync`, which needs it on both ends.

    Nothing on the device uses it, so no layer here requires it; the pull on
    the homelab host does, and that transfer is the one it keeps for good
    (physical/gateway.md §1.6). A firmware update takes every package it did
    not ship, so a set without `rsync` breaks the pull at the first update
    after the window.
    """
    packages = rendered_packages(monitor)

    for package in BACKUP_PULL_PACKAGES:
        assert package in packages, package


def test_the_routing_configuration_carries_the_gateways_session_password_as_a_secret(monitor: Controller) -> None:
    """The gateway hands its routing session's password to the file, and the file is secret because of it.

    What the file is and how it is applied -- its path, its mode, the converger
    its hook runs -- are `test_site_routing`'s, which declares the component on
    its own; what is the gateway's is which password reaches it.
    """
    config = monitor.inputs_of(f'{NAME}-routing-config')

    assert f'password {BGP_PASSWORD}' in config['content']
    assert 'content' in monitor.options_of(f'{NAME}-routing-config').additionalSecretOutputs


def test_the_device_keeps_accepting_the_key_this_program_dials_with(monitor: Controller) -> None:
    """The door the push comes through is desired state like everything else.

    `/root` is off `/data`, so the key that authorizes every session is as
    perishable as the rest of the customization, and the component that keeps
    it there is a child of this one.
    """
    declared = monitor.inputs_of(f'{NAME}-access-key-{CI_KEY.name}')

    assert declared['content'].strip() == CI_KEY.key


def test_the_gateway_declares_one_machine_per_service_and_nothing_else(monitor: Controller) -> None:
    """A fifth service is a change to this component's signature.

    The converger is given no roll of machines, so the machines this device has
    are the directories under the machines root that this component fills —
    which makes the claim one about paths rather than about a line in a script.
    Both directions matter: a census entry with no directory is a service the
    device never runs, and a directory under there with no census entry behind
    it is a machine the converger would find and start.
    """
    root = f'{nspawn.MACHINES}/'
    # The module's recorder holds every gateway its cases declared, so this
    # reads the one the fixture declared, by the name each of its children
    # carries (style/pulumi.md): a machine missing from it would otherwise be
    # masked by another case's device that has one.
    ours = f'{NAME}-'
    trees = {str(image.inputs['root']) for image in monitor.of_type(DEVICE_ARTIFACT) if image.name.startswith(ours)}
    written = {
        str(declaration.inputs['path']).removeprefix(root).split('/')[0]
        for declaration in monitor.of_type(DEVICE_FILE)
        if declaration.name.startswith(ours) and str(declaration.inputs.get('path', '')).startswith(root)
    }

    assert trees == {nspawn.rootfs_path(service.name) for service in conventions.gateway.SERVICES}
    assert written == {service.name for service in conventions.gateway.SERVICES}


def test_the_health_reading_reads_every_machine_and_every_unit_the_program_delivers(
    monitor: Controller, tmp_path: Path
) -> None:
    """The executable the device is given reads what this program declares on it, all of it and nothing else.

    Its lists are rendered from the declaration, so a fifth machine or another
    unit delivered through the mechanism is read from the push that declares
    it. What it reads is taken from what it reports: the executable the program
    delivers runs against a systemd on which nothing is running, and every
    machine and unit it holds to running is then a finding.
    """
    ours = f'{NAME}-'
    machines = {
        str(image.inputs['root']).removeprefix(f'{nspawn.MACHINES}/').split('/')[0]
        for image in monitor.of_type(DEVICE_ARTIFACT)
        if image.name.startswith(ours)
    }
    unit_sources = {
        os.path.basename(path)
        for declaration in monitor.of_type(DEVICE_FILE)
        if declaration.name.startswith(ours)
        and os.path.dirname(path := str(declaration.inputs.get('path', ''))) == persistence.UNIT_SOURCE_DIR
        and path.endswith(persistence.UNIT_SUFFIX)
    }
    delivered = monitor.inputs_of(f'{NAME}-bin-{HEALTH_PROGRAM}')
    assert delivered['path'] == persistence.executable_path(HEALTH_PROGRAM)

    # The package script is the device's; a stand-in at its place answers that
    # the cache passes, so what remains to report is systemd's.
    packages = tmp_path / persistence.PACKAGES_SCRIPT
    _ = packages.write_text('#!/bin/sh\nexit 0\n')
    packages.chmod(0o755)
    program = tmp_path / HEALTH_PROGRAM
    _ = program.write_text(
        str(delivered['content']).replace(persistence.on_boot_path(persistence.PACKAGES_SCRIPT), str(packages))
    )
    systemd = FakeSystemd(tmp_path / 'systemd')
    reading = process_sessions.run(
        device_shell.argv(HEALTH_PROGRAM, program),
        env={'PATH': f'{systemd.tools}:/usr/bin:/bin'},
        text=True,
        timeout=30,
    )

    findings = [re.match(r'^([^:]+): .* "(.+)"$', line) for line in reading.stdout.splitlines()]
    assert all(findings), reading.stdout
    read = [(finding[1], finding[2]) for finding in findings if finding is not None]
    assert {subject for subject, title in read if title == PLAYBOOKS.machine} == {
        nspawn.machine_unit(machine) for machine in machines
    }
    assert {subject for subject, title in read if title in {PLAYBOOKS.unit, PLAYBOOKS.boot_chain}} == unit_sources


@pytest.mark.asyncio
async def test_an_ipv6_host_is_bracketed_in_the_controller_url_and_an_ipv4_host_is_not(monitor: Controller) -> None:
    """One value, two spellings: the URL brackets what the SSH session dials bare.

    The stack's `gatewayBootstrapHost` guard admits exactly what `ip_address`
    parses, which is a bare IPv6 literal and not a bracketed one, so the
    bracketing has to happen where the URL is built rather than in the value.
    Both directions are pinned: the brackets appear for a v6 literal, and
    nothing is added for a v4 one.
    """
    async with declaring():
        _ = declare('v6', '2001:db8::1')
        _ = declare('v4', '192.0.2.1')

    assert monitor.inputs_of('v6-firewall-unifi', 'pulumi:providers:unifi')['apiUrl'] == 'https://[2001:db8::1]'
    assert monitor.inputs_of('v4-firewall-unifi', 'pulumi:providers:unifi')['apiUrl'] == 'https://192.0.2.1'


def test_the_type_token_the_cutover_targets_by_is_the_one_the_runbook_spells() -> None:
    # A token is part of every URN, and this component's URN is written out
    # literally in docs/physical/gateway-cutover.md §3 and §4 — a `pulumi
    # preview` before the window and the `pulumi up` inside it, each targeting
    # the gateway alone by URN and by a `$**::**` glob below it. The operator
    # cannot derive those mid-window: `pulumi stack --show-urns` reads state,
    # and the stack has none at the moment they are needed. A glob naming a
    # type the program does not declare selects nothing without erroring at
    # all, so the runbook is read here, as the side of the seam this program
    # does not write, and held to the token the class states.
    targets = re.findall(r"-t '(urn:pulumi:[^']*)'", RUNBOOK.read_text())
    types = {target.split('::')[2] for target in targets}

    assert types == {Gateway.__pulumi_type__, f'{Gateway.__pulumi_type__}$**'}, targets
