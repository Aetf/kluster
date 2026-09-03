"""The gateway's routing configuration, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: what the rendered configuration says about a peer that misbehaves,
which piece of the persistence layer each part of the component arrives
through, and what the converger does at boot, after a push, and after the
configuration is undeclared.

**The converger is also run**, against a temporary tree with a `systemctl` and
a `vtysh` that record what they were asked to do, because the properties that
matter most about it -- that a step which failed is retried, and that the
daemon is switched on again after a firmware update took the toggle away -- are
properties of the sequence rather than of any line the file contains.
"""

from __future__ import annotations

import os
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

#: The daemon list as the firmware ships it and as every firmware update
#: restores it: the protocol daemon switched off, which is why a device nobody
#: has declared a session on holds none.
STOCK_DAEMON_LIST = 'zebra=yes\nbgpd=no\nospfd=no\n'


@final
@dataclass(frozen=True)
class _Device:
    """One temporary stand-in for the device the converger runs on."""

    #: The rendered converger, and the four files it works between.
    script: Path
    source: Path
    live: Path
    stamp: Path
    daemons: Path
    #: Where the two stand-ins record what they were asked to do, and the flags
    #: that decide whether they agree to do it: `refusal` is the daemon that
    #: will not restart, `rejection` the parser that will not take the file.
    commands: Path
    checks: Path
    refusal: Path
    rejection: Path
    #: What `PATH` must start with for those stand-ins to be the ones found.
    tools: Path


@final
@dataclass(frozen=True)
class _Run:
    """What one run did: its status, what it asked of systemd, what it parsed."""

    status: int
    commands: list[str]
    checks: list[str]
    #: Whether the stamp existed when the run ended, which is the record that
    #: the daemon was restarted onto what was installed.
    stamped: bool


def _device(tmp_path: Path, *, configuration: str, daemons: str = STOCK_DAEMON_LIST) -> _Device:
    """The converger rendered against a temporary tree, ready to run.

    The template is rendered here rather than through `converger_script`
    because the paths it carries are the device's absolute ones, and because
    ownership on the device is a user this suite is not; everything else about
    the file — the toggle, the syntax check, the restart — is what the
    component ships.
    """
    device = _Device(
        script=tmp_path / 'frr-config.sh',
        source=tmp_path / 'source' / 'frr.conf',
        live=tmp_path / 'live' / 'frr.conf',
        stamp=tmp_path / 'live' / f'frr.conf.{conventions.CLUSTER_NAME}-applied',
        daemons=tmp_path / 'live' / 'daemons',
        commands=tmp_path / 'commands',
        checks=tmp_path / 'checks',
        refusal=tmp_path / 'refuses',
        rejection=tmp_path / 'rejects',
        tools=tmp_path / 'tools',
    )
    device.source.parent.mkdir()
    device.live.parent.mkdir()
    device.tools.mkdir()
    _ = device.source.write_text(configuration)
    _ = device.daemons.write_text(daemons)

    systemctl = device.tools / 'systemctl'
    # Records the verb and the unit, and refuses while the flag file exists —
    # which is the daemon that will not come back.
    _ = systemctl.write_text(f'#!/bin/sh\necho "$*" >>{device.commands}\n[ -e {device.refusal} ] && exit 1\nexit 0\n')
    systemctl.chmod(0o755)

    vtysh = device.tools / 'vtysh'
    # Records which file it was pointed at, and rejects while the flag file
    # exists — which is the parser that will not take one of these lines.
    _ = vtysh.write_text(f'#!/bin/sh\necho "$*" >>{device.checks}\n[ -e {device.rejection} ] && exit 1\nexit 0\n')
    vtysh.chmod(0o755)

    _ = device.script.write_text(
        templates.render(
            persistence.TEMPLATE_PACKAGE,
            f'templates/{routing.CONVERGER}.j2',
            _Rendering(
                cluster=conventions.CLUSTER_NAME,
                source=str(device.source),
                live=str(device.live),
                stamp=str(device.stamp),
                daemons=str(device.daemons),
                daemon=routing.BGP_DAEMON,
                # The only owner an unprivileged runner can install as.
                owner=str(os.getuid()),
                group=str(os.getgid()),
                mode=routing.FRR_MODE,
                check=routing.FRR_SYNTAX_CHECK,
                restart=routing.FRR_RESTART,
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
    daemons: str
    daemon: str
    owner: str
    group: str
    mode: str
    check: str
    restart: str


def _converge(device: _Device, *, daemon_answers: bool = True, syntax_accepted: bool = True) -> _Run:
    """Run the converger once, and read back what it did."""
    for flag, agrees in ((device.refusal, daemon_answers), (device.rejection, syntax_accepted)):
        if agrees:
            flag.unlink(missing_ok=True)
        else:
            _ = flag.write_text('')
    device.commands.unlink(missing_ok=True)
    device.checks.unlink(missing_ok=True)

    completed = subprocess.run(  # noqa: S603 -- a rendered script of this repository's own
        ['/bin/sh', str(device.script)],  # noqa: S607 -- the shell the device's own scripts name
        env={'PATH': f'{device.tools}:/usr/bin:/bin'},
        capture_output=True,
        check=False,
    )

    def _recorded(record: Path) -> list[str]:
        lines = record.read_text().split('\n') if record.exists() else []
        return [line for line in lines if line]

    return _Run(
        status=completed.returncode,
        commands=_recorded(device.commands),
        checks=_recorded(device.checks),
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
    directory = monitor.one(f'{MECHANISM}-skeleton-{routing.FRR_DIRECTORY}')

    assert directory.typ == 'pulumi-python:dynamic/device:Directory'
    assert directory.inputs['path'] == persistence.skeleton_path(routing.FRR_DIRECTORY)


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


def test_the_unit_starts_the_daemon_rather_than_anything_enabling_it(monitor: Recorder) -> None:
    """The daemon ships disabled, and this edge is the whole of what starts it.

    An enable would be a mutation of `/etc` that a boot script would have to
    re-assert and a retirement would have to undo; the edge is a line in a file
    this program already delivers and retires. It is ordered *after* the daemon
    because a converger ordered before it could not restart it synchronously.
    And it carries no condition on the configuration's presence: a failed
    condition still pulls in and orders `Wants=`, so it would gate nothing
    while implying that it does.
    """
    unit = monitor.inputs_of(f'{MECHANISM}-unit-{routing.CONVERGER_UNIT}')['content']

    assert f'Wants={routing.FRR_SERVICE}' in unit
    assert f'After={routing.FRR_SERVICE}' in unit
    assert f'Before={routing.FRR_SERVICE}' not in unit
    assert '\nCondition' not in unit, 'a condition here would gate nothing while implying that it does'


@pytest.mark.asyncio
async def test_the_unit_waits_for_the_configuration_it_is_about_to_start_a_daemon_on(
    monitor: Recorder, site: SiteRouting
) -> None:
    """So a first push lands the file and the toggle before the daemon is started.

    The unit is what pulls the daemon in, and the unit converger starts it the
    moment its source lands. Started before the configuration exists, the
    daemon comes up with the protocol switched off and is restarted a moment
    later — correct, and a restart nobody needed.
    """
    depends = monitor.depends_on(f'{MECHANISM}-unit-{routing.CONVERGER_UNIT}')

    assert str(await site.config.urn.future()) in depends
    assert str(await site.converger.urn.future()) in depends


##
## What the converger does
##


def test_the_converger_restarts_the_daemon_because_this_firmware_cannot_reload_it() -> None:
    """The reload verb needs a helper script the image does not ship.

    So the reload half of a `reload || restart` pair would fail on every single
    run and only the fallback would ever execute. A restart costs what a reload
    would have saved only where the daemon holds other sessions, and on this
    device it holds none. Nothing is silenced: a converger that cannot make the
    file take effect has failed, and the hook's non-zero exit fails the apply
    that ran it.
    """
    script = routing.converger_script()

    assert routing.FRR_RESTART in script
    assert 'systemctl reload' not in script
    assert '|| true' not in script


def test_the_daemons_copy_is_installed_with_the_ownership_the_daemon_suite_uses() -> None:
    """Which is convention rather than a requirement, and cheap to match.

    The supervisor that pushes the file into the daemons keeps root, so a
    root-owned file would be read fine; what the ownership buys is that an
    operator writing the configuration out from a running daemon overwrites
    this file rather than failing on it.
    """
    script = routing.converger_script()

    assert f'install -o {routing.FRR_OWNER} -g {routing.FRR_GROUP} -m {routing.FRR_MODE}' in script


def test_a_run_that_finds_the_daemon_switched_on_and_holding_the_configuration_does_nothing(tmp_path: Path) -> None:
    """The converger runs at every boot and after every push of the file.

    A restart the daemon did not need drops the session for no reason, so a run
    that finds its own stamp beside a live copy that matches, with the toggle
    already switched on, leaves the daemon alone.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')

    first = _converge(device)
    second = _converge(device)

    assert first.commands == [f'restart {routing.FRR_SERVICE}']
    assert second.commands == []
    assert second.checks == [], 'nothing to install is nothing to parse'
    assert device.live.read_text() == device.source.read_text()
    assert f'{routing.BGP_DAEMON}=yes' in device.daemons.read_text()


def test_a_daemon_list_a_firmware_update_restored_is_switched_on_again(tmp_path: Path) -> None:
    """The list is the firmware's own file, so the toggle is a converged fact.

    An update puts the stock list back while leaving the daemon's copy of the
    configuration in place, so the run that repairs it is exactly the run whose
    file comparison says there is nothing to do. The toggle is therefore
    checked before that comparison, and a switch that had to be flipped is
    itself a reason to restart.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')
    _ = _converge(device)

    _ = device.daemons.write_text(STOCK_DAEMON_LIST)
    repaired = _converge(device)

    assert repaired.status == 0
    assert repaired.commands == [f'restart {routing.FRR_SERVICE}']
    assert f'{routing.BGP_DAEMON}=yes' in device.daemons.read_text()


def test_a_daemon_list_carrying_neither_toggle_line_fails_the_run(tmp_path: Path) -> None:
    """A file reshaped past what this program understands must not pass silently.

    The substitution matches nothing there, so a converger that only edited
    would install the configuration, restart, and report success over a daemon
    that never starts the protocol. Asserting the line afterwards is what turns
    that into a failed apply an operator sees.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n', daemons='zebra=yes\n')

    run = _converge(device)

    assert run.status != 0
    assert run.commands == [], 'nothing is restarted onto a daemon list this program cannot read'
    assert not run.stamped


def test_a_configuration_the_parser_rejects_is_never_installed_and_is_retried(tmp_path: Path) -> None:
    """The supervisor pushes the file into the daemons after the unit returns.

    Its rejection of a line fails nothing, so an installed file and a stamp
    prove installed rather than accepted. Parsing the candidate first is what
    turns a firmware update whose parser no longer likes one of these lines
    into a failed converge instead of a peer that never establishes — and
    because the failure lands before the write, the retry is a full attempt.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')

    rejected = _converge(device, syntax_accepted=False)
    nothing_installed = not device.live.exists()
    retried = _converge(device)

    assert rejected.status != 0
    assert rejected.checks == [f'-C -f {device.source}'], 'the candidate is parsed, not the installed copy'
    assert rejected.commands == [], 'nothing is restarted onto a file the parser refused'
    assert nothing_installed, 'the parse comes before the write, so a refusal leaves the daemon on what it had'
    assert not rejected.stamped
    assert retried.status == 0
    assert retried.commands == [f'restart {routing.FRR_SERVICE}']
    assert retried.stamped


def test_a_restart_that_failed_is_retried_by_the_next_run(tmp_path: Path) -> None:
    """The installed file cannot say whether the daemon came back onto it.

    The write succeeds before the restart is attempted, so a run that fails at
    the restart leaves two identical copies behind. Deciding on those alone,
    the retry — the one the operator makes with the daemon healthy — would exit
    successfully having told the daemon nothing, and the session would stay on
    the old configuration under a green apply.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')

    failed = _converge(device, daemon_answers=False)
    retried = _converge(device)

    assert failed.status != 0, 'a converger that could not make the file take effect must fail its apply'
    assert failed.commands == [f'restart {routing.FRR_SERVICE}']
    assert not failed.stamped, 'nothing may record an effect that did not happen'
    assert retried.commands == [f'restart {routing.FRR_SERVICE}']
    assert retried.status == 0
    assert retried.stamped


def test_undeclaring_the_configuration_does_not_take_the_daemon_away(tmp_path: Path) -> None:
    """The hook runs after the delete too, and what it must not do then is act.

    The daemon is the device's own and it is running on the configuration it
    has; this program ceasing to declare one is not a reason to leave the
    router with none, nor to switch the protocol off again.
    """
    device = _device(tmp_path, configuration='router bgp 65000\n')
    _ = _converge(device)
    device.source.unlink()

    undeclared = _converge(device)

    assert undeclared.status == 0
    assert undeclared.commands == []
    assert device.live.read_text() == 'router bgp 65000\n'
    assert f'{routing.BGP_DAEMON}=yes' in device.daemons.read_text()
    assert routing.FRR_LIVE_CONFIG not in routing.converger_hook()
