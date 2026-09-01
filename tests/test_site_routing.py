"""The gateway's routing configuration, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: what the rendered configuration says about a peer that misbehaves,
which piece of the persistence layer each part of the component arrives
through, and what the converger does at boot, after a push, and after the
configuration is undeclared.

**The converger is also run**, against a temporary tree and a `systemctl` that
records what it was asked to do, because the property that matters most about
it -- that a failed reload is retried -- is a property of the sequence rather
than of any line the file contains.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import persistence, routing
from kluster.components.gateway.persistence import DevicePersistence
from kluster.components.gateway.routing import RoutingSession, SiteRouting
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection

MECHANISM = 'mechanism'
NAME = 'routing'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'
BGP_PASSWORD = 'a-session-password'
PACKAGES = ('systemd-container',)


@final
@dataclass(frozen=True)
class _Device:
    """One temporary stand-in for the device the converger runs on."""

    #: The rendered converger, and the three files it works between.
    script: Path
    source: Path
    live: Path
    stamp: Path
    #: Where the `systemctl` stand-in records what it was asked to do, and the
    #: flag that decides whether it agrees to do it.
    commands: Path
    refusal: Path
    #: What `PATH` must start with for that stand-in to be the one found.
    tools: Path


@final
@dataclass(frozen=True)
class _Run:
    """What one run of the converger did: its exit status, and what it asked of systemd."""

    status: int
    commands: list[str]
    #: Whether the stamp existed when the run ended, which is the record that
    #: the daemon accepted what was installed.
    stamped: bool


def _device(tmp_path: Path, *, configuration: str) -> _Device:
    """The converger rendered against a temporary tree, ready to run.

    The template is rendered here rather than through `converger_script`
    because the paths it carries are the device's absolute ones; everything
    else about the file — including the reload command — is what the component
    ships.
    """
    device = _Device(
        script=tmp_path / 'frr-config.sh',
        source=tmp_path / 'source' / 'frr.conf',
        live=tmp_path / 'live' / 'frr.conf',
        stamp=tmp_path / 'live' / f'frr.conf.{conventions.CLUSTER_NAME}-applied',
        commands=tmp_path / 'commands',
        refusal=tmp_path / 'refuses',
        tools=tmp_path / 'tools',
    )
    device.source.parent.mkdir()
    device.live.parent.mkdir()
    device.tools.mkdir()
    _ = device.source.write_text(configuration)

    systemctl = device.tools / 'systemctl'
    # Records the verb and the unit, and refuses while the flag file exists —
    # which is the daemon that is not up yet, or is up and unhappy.
    _ = systemctl.write_text(f'#!/bin/sh\necho "$*" >>{device.commands}\n[ -e {device.refusal} ] && exit 1\nexit 0\n')
    systemctl.chmod(0o755)

    _ = device.script.write_text(
        templates.render(
            persistence.TEMPLATE_PACKAGE,
            f'templates/{routing.CONVERGER}.j2',
            _Rendering(
                cluster=conventions.CLUSTER_NAME,
                source=str(device.source),
                live=str(device.live),
                stamp=str(device.stamp),
                mode=routing.FRR_MODE,
                reload=routing.FRR_RELOAD,
            ),
        )
    )
    return device


@final
@dataclass(frozen=True)
class _Rendering:
    """The converger template's parameters, pointed at a temporary tree."""

    cluster: str
    source: str
    live: str
    stamp: str
    mode: str
    reload: str


def _converge(device: _Device, *, daemon_answers: bool = True) -> _Run:
    """Run the converger once, and read back what it did."""
    if daemon_answers:
        device.refusal.unlink(missing_ok=True)
    else:
        _ = device.refusal.write_text('')
    device.commands.unlink(missing_ok=True)

    completed = subprocess.run(  # noqa: S603 -- a rendered script of this repository's own
        ['/bin/sh', str(device.script)],  # noqa: S607 -- the shell the device's own scripts name
        env={'PATH': f'{device.tools}:/usr/bin:/bin'},
        capture_output=True,
        check=False,
    )
    asked = device.commands.read_text().split('\n') if device.commands.exists() else []
    return _Run(
        status=completed.returncode,
        commands=[command for command in asked if command],
        stamped=device.stamp.exists(),
    )


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def site(monitor: Recorder) -> SiteRouting:
    """The component declared once, the way `Gateway` declares it."""
    connection = Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER)
    async with declaring():
        mechanism = DevicePersistence(MECHANISM, connection=connection, packages=PACKAGES)
        declared = SiteRouting(
            NAME,
            connection=connection,
            mechanism=mechanism,
            session=RoutingSession(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD),
        )
    return declared


##
## What the daemon is told
##


def test_the_routing_configuration_confines_what_the_peer_may_announce() -> None:
    """Three defences, and each of them is a line a reviewer can find.

    Without the prefix-list, anything holding the worker's address could
    announce the resolvers' own /32s and take the LAN's name service with it;
    without the cap, it could announce the pool one address at a time until the
    table gave out; without the password, holding the address would be enough
    to be the peer.
    """
    rendered = routing.frr_config(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD)
    peer = str(conventions.HOMELAB_NODE_IPV4)

    assert f'neighbor {peer} remote-as {conventions.CLUSTER_ASN}' in rendered
    assert f'router bgp {conventions.UDM_ASN}' in rendered
    assert f'neighbor {peer} password {BGP_PASSWORD}' in rendered
    assert f'neighbor {peer} maximum-prefix {routing.MAX_PREFIXES}' in rendered
    assert f'permit {conventions.LAN_POOL.v4} le 32' in rendered
    assert f'permit {conventions.LAN_POOL.v6} le 128' in rendered
    assert rendered.count('deny any') == 2, 'each family admits the pool and refuses the rest'
    # Both families ride the one session, so both have to be activated on it.
    assert rendered.count(f'neighbor {peer} activate') == 2


##
## What the device holds
##


def test_the_configuration_survives_a_firmware_update_and_the_daemons_copy_does_not(monitor: Recorder) -> None:
    """Which is the whole reason there are two of them, and a converger between.

    The source lands under the custom root, in a directory declared through the
    layer below rather than assumed; what the daemon reads is off `/data` and
    is gone after an update, so it is written by the converger and never
    declared here.
    """
    config = monitor.inputs_of(f'{NAME}-config')

    assert config['path'] == f'{conventions.gateway.CUSTOM_ROOT}/{routing.FRR_DIRECTORY}/frr.conf'
    assert config['path'] == routing.FRR_CONFIG
    assert routing.FRR_LIVE_CONFIG.startswith('/etc/')
    assert monitor.inputs_of(f'{MECHANISM}-skeleton-{routing.FRR_DIRECTORY}')['hook'].endswith(
        f'rmdir {persistence.skeleton_path(routing.FRR_DIRECTORY)} 2>/dev/null || true; fi'
    )


def test_the_session_password_is_in_the_file_and_the_file_is_a_secret(monitor: Recorder) -> None:
    """A configuration nobody may read in a preview, and nobody else may read on the box.

    Holding the peer's address is not enough to become the peer *because* the
    password is in this file, which is the same reason it is neither rendered
    into a diff nor left world-readable where it lands.
    """
    config = monitor.inputs_of(f'{NAME}-config')

    assert f'password {BGP_PASSWORD}' in config['content']
    assert config['mode'] == routing.FRR_MODE
    assert config['mode'] != persistence.FILE_MODE, 'the daemon-readable mode is not the ordinary one'


def test_the_converger_is_a_unit_and_an_executable_rather_than_a_boot_chain_script(monitor: Recorder) -> None:
    """Installing a configuration file manipulates nothing of systemd's own.

    So the local rule puts it on the unit side: a journal, a failure visible in
    `systemctl status`, and a run that can be repeated outside boot — none of
    which a script in the numeric chain gets.
    """
    executable = monitor.inputs_of(f'{MECHANISM}-bin-{routing.CONVERGER}')
    unit = monitor.inputs_of(f'{MECHANISM}-unit-{routing.CONVERGER_UNIT}')

    assert executable['path'] == persistence.executable_path(routing.CONVERGER)
    assert executable['mode'] == persistence.SCRIPT_MODE
    assert unit['path'] == persistence.unit_source(routing.CONVERGER_UNIT)
    assert 'WantedBy=multi-user.target' in unit['content']
    assert 'Type=oneshot' in unit['content']
    # The unit converger starts what it finds inactive, and a oneshot without
    # this looks inactive the moment it succeeds.
    assert 'RemainAfterExit=yes' in unit['content']
    assert f'ExecStart={persistence.executable_path(routing.CONVERGER)}' in unit['content']

    on_boot = [name for name in monitor.names_declared if name.startswith(f'{NAME}-on-boot-')]
    assert on_boot == [], 'the component put nothing in the boot chain'


def test_the_boot_path_and_the_push_path_are_one_converger(monitor: Recorder) -> None:
    """The file's hook is the executable the unit runs, not a command beside it.

    A separate apply command would be a second way of doing the same thing, and
    the one that only runs after a firmware update is the one that would rot
    unnoticed.
    """
    hook = monitor.inputs_of(f'{NAME}-config')['hook']
    unit = monitor.inputs_of(f'{MECHANISM}-unit-{routing.CONVERGER_UNIT}')['content']

    assert persistence.executable_path(routing.CONVERGER) in hook
    assert f'ExecStart={persistence.executable_path(routing.CONVERGER)}' in unit


@pytest.mark.asyncio
async def test_the_configuration_waits_for_the_executable_that_applies_it(monitor: Recorder, site: SiteRouting) -> None:
    """A hook that runs a program the device does not have fails its own write.

    So the file that carries the hook is declared after the file that is the
    hook, and after the directory it lands in — neither of which the engine
    could infer from the content.
    """
    depends = monitor.depends_on(f'{NAME}-config')

    assert str(await site.converger.urn.future()) in depends
    assert str(await site.directory.urn.future()) in depends


##
## What the converger does
##


def test_the_converger_reloads_the_daemon_rather_than_restarting_it() -> None:
    """A restart drops every session the device holds, including ones not ours.

    Reload is what the daemon offers for a configuration change; the restart is
    the fallback for the boot where the daemon is not up to be reloaded, and
    neither is silenced — a converger that cannot make the file take effect has
    failed, and the hook's non-zero exit fails the apply that ran it.
    """
    script = routing.converger_script()

    assert routing.FRR_RELOAD in script
    assert '|| true' not in script


def test_a_run_that_finds_the_daemon_already_holding_the_configuration_does_nothing(tmp_path: Path) -> None:
    """The converger runs at every boot and after every push of the file.

    A reload the daemon did not ask for is a reload that can fail for reasons
    this program did not cause, so a run that finds its own stamp beside a live
    copy that matches leaves the daemon alone.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')

    first = _converge(device)
    second = _converge(device)

    assert first.commands == ['reload frr']
    assert second.commands == []
    assert device.live.read_text() == device.source.read_text()


def test_a_reload_that_failed_is_retried_by_the_next_run(tmp_path: Path) -> None:
    """The installed file cannot say whether the daemon accepted it.

    The write succeeds before the reload is attempted, so a run that fails at
    the reload leaves two identical copies behind. Deciding on those alone, the
    retry — the one the operator makes with the daemon healthy — would exit
    successfully having told the daemon nothing, and the session would stay on
    the old configuration under a green apply.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')

    failed = _converge(device, daemon_answers=False)
    retried = _converge(device)

    assert failed.status != 0, 'a converger that could not make the file take effect must fail its apply'
    assert failed.commands == ['reload frr', 'restart frr']
    assert not failed.stamped, 'nothing may record an effect that did not happen'
    assert retried.commands == ['reload frr']
    assert retried.status == 0
    assert retried.stamped


def test_undeclaring_the_configuration_does_not_take_the_daemons_away() -> None:
    """The hook runs after the delete too, and what it must not do then is act.

    The daemon is the device's own and it is running on the configuration it
    has; this program ceasing to declare one is not a reason to leave the
    router with none.
    """
    script = routing.converger_script()
    unit = routing.converger_unit()

    assert '[ -e "$source" ] || exit 0' in script
    assert f'ConditionPathExists={routing.FRR_CONFIG}' in unit
    assert routing.FRR_LIVE_CONFIG not in routing.converger_hook()
