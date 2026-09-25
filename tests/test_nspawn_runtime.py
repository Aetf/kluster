"""The nspawn runtime: what it declares, and what its convergers do to a device.

Nothing here contacts a device, and nothing here knows what a container
service is: the runtime is the framework, so what is exercised is what it does
for *a* machine. Which file lands where, what runs after one lands, and what a
rollback moves.

The two convergers are exercised by **running them**, against a directory
tree this module builds and, for `40-machines.sh`, a `systemctl` it can read
back. That is the tier a script handed no machines needs: every claim about
which machines it acts on is a claim about what it finds on a disk, and reading
the rendered text back would only restate the template. The cases therefore
disagree with the declaration on purpose — a tree with no settings, a settings
file that went away under a running machine, a half-written file a push
abandoned, a named pipe where a file was expected — because that is the device
state the operator meets and no declaration describes it.

The other renderers are plain functions over plain data, so those cases read
their output directly; the component is declared once against mocks, which is
where a wiring mistake would surface.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import nspawn, persistence
from kluster.components.gateway.nspawn import NspawnRuntime
from kluster.components.gateway.persistence import DevicePersistence
from kluster.lib import templates
from kluster.providers.device_files.provider import SUPERSEDED_SUFFIX, Connection, DeviceFile, marker_path
from kluster.providers.device_files.ssh import STAGING_SUFFIX
from putils import Component

NAME = 'runtime'
#: The mechanism the runtime asks. The path discipline is layer one's, and the
#: resource that comes back is the runtime's: parented on it and named for it,
#: so nothing below keys on this name but the mechanism's own files.
MECHANISM = 'mechanism'
#: A component that has a machine, whose drop-in is therefore named for it.
WORKLOAD = 'workload'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'

#: A machine to declare files for, named so that it is not a service of this
#: repository's own: the runtime knows no service and neither does this suite.
MACHINE = 'plain'


class Workload(Component, pulumi_type='test:gateway:Workload'):
    """A component that has a machine, which is what declares that machine's drop-in.

    It stands in for `container.Container`: the runtime is the framework and
    knows no service, so the file that carries one machine's restart policy and
    bridge binding is the workload's, like every other file of that machine.
    """

    def __init__(self, name: str, *, runtime: NspawnRuntime, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.dropin: DeviceFile = runtime.dropin(MACHINE, bridge='br5', opts=self.child_opts())
        self.register_outputs({})


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def mechanism(monitor: Recorder) -> DevicePersistence:
    """The layer this one is built on, declared the way `Gateway` declares it."""
    async with declaring():
        layer_one = DevicePersistence(
            MECHANISM,
            connection=Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER),
            packages=NspawnRuntime.REQUIRED_PACKAGES,
        )
    return layer_one


@pytest_asyncio.fixture(scope='module', autouse=True)
async def runtime(mechanism: DevicePersistence) -> NspawnRuntime:
    """The runtime on that mechanism, declared once."""
    async with declaring():
        framework = NspawnRuntime(NAME, mechanism=mechanism)
    return framework


##
## What the runtime puts on the device
##


def test_the_framework_reaches_the_device_only_through_the_mechanism(monitor: Recorder) -> None:
    """Every piece is a file layer one placed, and each belongs to this layer.

    The runtime decides what it needs on the device; where a boot-chain script,
    an executable, a unit or a directory goes and what runs once it lands is
    layer one's, and the resource comes back as a child of the component that
    asked.
    """
    for name in (
        f'{NAME}-on-boot-{nspawn.NSPAWN_UNITS_SCRIPT}',
        f'{NAME}-on-boot-{nspawn.MACHINES_SCRIPT}',
        f'{NAME}-bin-{nspawn.ROLLBACK_PROGRAM}',
        f'{NAME}-bin-{nspawn.WATCHDOG_WORKER}',
        f'{NAME}-unit-{nspawn.WATCHDOG_UNIT}',
        f'{NAME}-skeleton-{nspawn.SKELETON}',
    ):
        assert monitor.options_of(name).parent.endswith(f'::{NAME}'), name

    assert monitor.inputs_of(f'{NAME}-skeleton-{nspawn.SKELETON}')['path'] == nspawn.MACHINES
    assert nspawn.MACHINES == f'{conventions.gateway.CUSTOM_ROOT}/machines'


def test_the_device_is_asked_for_the_tooling_the_push_needs_as_well() -> None:
    """Two of the four packages are the push's, not the runtime's.

    The device pulls and unpacks its own root filesystems, and the provider
    that drives it declares no packages — nothing in a dynamic provider can. So
    the layer whose machines those trees are is what puts `skopeo` and `umoci`
    on the device's path, beside the tooling that boots a directory as a
    machine.
    """
    assert set(NspawnRuntime.REQUIRED_PACKAGES) == {'systemd-container', 'libnss-mymachines', 'skopeo', 'umoci'}


def test_the_watchdog_is_a_unit_and_an_executable_rather_than_a_boot_script() -> None:
    """The local rule: it configures nothing of systemd's, so it is not a script.

    What it needs of the device is a place its unit can find it and the
    directory whose files say which bridge a container belongs to — both of
    them paths another layer decides, so the pair is only correct as long as it
    agrees with them.
    """
    unit = nspawn.watchdog_unit()
    worker = nspawn.watchdog_worker()

    assert f'ExecStart={persistence.executable_path(nspawn.WATCHDOG_WORKER)}' in unit
    assert 'Restart=always' in unit
    assert nspawn.LIVE_NSPAWN_DIR in worker


##
## What a machine's unit says that its settings file cannot
##


def test_every_machine_is_kept_running_by_a_policy_the_template_unit_has_none_of() -> None:
    """Without it a machine that died stays down until the next boot or push.

    The policy is on the machine's own instance of the template unit, which is
    the only place it can be: `systemd-nspawn@.service` is shared by every
    machine on the device, and a settings file describes the container rather
    than the unit.
    """
    for bridge in (None, 'br5'):
        rendered = nspawn.machine_dropin('plain', bridge=bridge)

        assert f'Restart={nspawn.RESTART_POLICY}' in rendered, bridge
        assert f'RestartSec={nspawn.RESTART_DELAY}' in rendered, bridge

    assert nspawn.MACHINE_DROPIN.endswith(persistence.DROPIN_SUFFIX)


def test_a_machine_on_a_bridge_is_ordered_after_that_bridge_and_bound_to_nothing() -> None:
    """Ordering, and deliberately no binding on the same device unit.

    The ordering holds a start systemd queues while the bridge is coming up.
    A binding would additionally stop the machine when the bridge went away,
    and nothing would start it back up: a dependency stop forbids the restart
    policy from acting, so the machine would sit stopped until the next boot or
    the next push. Its absence is the statement being pinned here.

    The assertions read rendered directive lines and ignore comments. A
    whole-file negative check is unusable — the template's comment names
    `BindsTo=` while explaining its absence, so it is red on a correct
    template — and a whole-file positive check on `After=` would be satisfied
    by comment text alone. Editing either the comment or these assertions
    touches both.
    """
    rendered = nspawn.machine_dropin('plain', bridge='br5')
    unit = nspawn.interface_device_unit('br5')

    directives = [line for line in rendered.splitlines() if not line.startswith('#')]

    assert unit == 'sys-subsystem-net-devices-br5.device'
    assert f'After={unit}' in directives
    assert not [line for line in directives if line.startswith(('BindsTo', 'Requires'))]


def test_a_machine_in_the_hosts_namespace_names_no_bridge_at_all() -> None:
    """It has no bridge to be ordered after, and the file says nothing about one.

    The overlay daemon runs in the host's network namespace, so the section an
    ordering would go in is absent rather than empty — a machine that named a
    device unit nothing creates would name an ordering against something that
    never activates, which holds nothing back and leaves a start that fails on
    the missing bridge.
    """
    rendered = nspawn.machine_dropin('plain', bridge=None)

    assert '[Unit]' not in rendered
    assert not [line for line in rendered.splitlines() if line.startswith(('After', 'BindsTo', 'Requires'))]


def test_an_interfaces_device_unit_is_its_escaped_sysfs_path() -> None:
    """systemd names a device unit after its path, and escapes what a path may not carry.

    A bridge whose name carries a hyphen is the case that separates the escaped
    name from the plain one: unescaped, the unit would name a device that does
    not exist, and the ordering would be against something that never activates.
    """
    assert nspawn.interface_device_unit('br-lan') == 'sys-subsystem-net-devices-br\\x2dlan.device'


@pytest.mark.asyncio
async def test_a_machines_drop_in_is_a_resource_of_the_component_that_has_the_machine(
    monitor: Recorder, runtime: NspawnRuntime
) -> None:
    """The runtime decides what the unit must say; the file belongs to the caller.

    A machine's drop-in is declared when that machine is, by the component that
    declares the machine, so the statement comes off the unit in the session
    that stops declaring it — while where such a file goes and what runs once it
    lands stays layer one's.
    """
    unit = nspawn.machine_unit('plain')
    async with declaring():
        workload = Workload(WORKLOAD, runtime=runtime)

    inputs = monitor.inputs_of(f'{WORKLOAD}-dropin-{unit}.d/{nspawn.MACHINE_DROPIN}')

    assert inputs['path'] == persistence.dropin_source(unit, nspawn.MACHINE_DROPIN)
    assert f'Restart={nspawn.RESTART_POLICY}' in str(inputs['content'])
    assert str(await workload.dropin.urn.future()).endswith(f'{nspawn.MACHINE_DROPIN}')
    assert monitor.options_of(f'{WORKLOAD}-dropin-{unit}.d/{nspawn.MACHINE_DROPIN}').parent.endswith(f'::{WORKLOAD}')


##
## What runs after a machine's file lands
##


def test_a_machine_that_did_not_come_up_is_rolled_back_and_the_push_fails() -> None:
    """Converging is not evidence that the machine runs, so systemd is asked.

    A machine that did not reach active leaves the device on the tree this push
    displaced and the operation non-zero: the resource is not recorded as
    applied, so the next preview still has the work to do, and the operator
    finds a red apply rather than a resolver that has been down since a push
    that reported success.
    """
    hook = nspawn.machine_hook('plain', nspawn.rootfs_path('plain'), rollback=True)

    assert persistence.on_boot_path(nspawn.NSPAWN_UNITS_SCRIPT) in hook
    assert persistence.on_boot_path(nspawn.MACHINES_SCRIPT) in hook
    assert 'systemctl is-active --quiet systemd-nspawn@plain.service' in hook
    assert f'{persistence.executable_path(nspawn.ROLLBACK_PROGRAM)} plain' in hook
    assert 'exit 1' in hook


def test_only_the_root_filesystems_hook_rolls_anything_back() -> None:
    """The tree is the only piece of a machine with a displaced copy beside it.

    A configuration file's hook that swapped it would replace a tree that had
    nothing to do with the failure and leave the bad configuration in place —
    so the next push would deliver it again, fail again, and swap again. Such a
    hook fails without touching the tree.
    """
    configuration = nspawn.machine_hook('plain', nspawn.machine_file('plain', 'Caddyfile'), rollback=False)

    assert nspawn.ROLLBACK_PROGRAM not in configuration
    assert 'exit 1' in configuration


def test_the_health_gate_holds_only_a_machine_that_could_have_started() -> None:
    """Two cases the push produces, and the gate has to survive both.

    The same command runs after a delete: a machine being retired is *supposed*
    not to be active, and holding the delete to an active unit would fail the
    delete that was removing it. And on the push that creates a machine its
    configuration lands before its root filesystem does, so the converger skips
    it — a gate that fired then would fail every file of every new machine.
    """
    path = nspawn.machine_file('plain', 'Caddyfile')
    hook = nspawn.machine_hook('plain', path, rollback=False)

    assert f'if [ -e {path} ] && [ -d {nspawn.rootfs_path("plain")} ]; then' in hook
    # The convergers run either way: a file that has just gone is a change the
    # device still has to be told about.
    assert hook.index(persistence.on_boot_path(nspawn.MACHINES_SCRIPT)) < hook.index(f'if [ -e {path} ]')


def test_the_convergers_exit_status_reaches_the_apply() -> None:
    """It reports what only it learns, and a hook that dropped it would lie.

    A machine of this push's set that failed to start, or a live directory the
    script refused to touch, is a failure no `is-active` of *this* machine
    would see — and a green apply over a device that printed a failure is the
    one outcome this mechanism must not produce.
    """
    hook = nspawn.machine_hook('plain', nspawn.rootfs_path('plain'), rollback=True)

    assert 'rc=$?' in hook
    assert hook.endswith('exit $rc')


def test_the_settings_are_mirrored_before_the_machines_are_started() -> None:
    """A machine started against settings not yet mirrored is on the wrong network.

    The hook runs the two scripts in the order the boot chain runs them, which
    is what the numeric prefixes are for — and it is the hook itself that is
    read, because that string is what the device executes.
    """
    hook = nspawn.machine_hook('plain', nspawn.nspawn_path('plain'), rollback=False)

    assert hook.index(nspawn.NSPAWN_UNITS_SCRIPT) < hook.index(nspawn.MACHINES_SCRIPT)


def test_a_machine_keeps_no_file_whose_name_the_runtime_already_uses() -> None:
    """Two things at one path is the machine directory's one failure mode.

    Everything a machine is lives in one directory, so a mounted file called
    `rootfs` or `stamp` would be the tree or the content stamp — and whichever
    was written last would decide what the machine booted. `initial-state` is
    the one whose collision is silent rather than loud: the converger would
    copy a mounted file into a state directory that has never held any and go
    on running.
    """
    assert nspawn.machine_file('plain', 'Caddyfile') == f'{nspawn.machine_path("plain")}/Caddyfile'

    for reserved in ('rootfs', 'rootfs.digest', 'state', 'stamp', 'initial-state', 'plain.nspawn'):
        with pytest.raises(ValueError, match='nspawn runtime keeps'):
            _ = nspawn.machine_file('plain', reserved)

    # Any settings name and not only this machine's: the mirror keys the live
    # directory by machine name, so a second one here would be installed as
    # another machine's settings and removed as stale in the same run.
    with pytest.raises(ValueError, match='nspawn runtime keeps'):
        _ = nspawn.machine_file('plain', f'other{nspawn.NSPAWN_SUFFIX}')


def test_a_machine_keeps_no_file_the_content_stamp_cannot_see() -> None:
    """A hidden name is delivered, read by the container, and never stamped.

    The stamp covers the machine's directory as the shell globs it, and a glob
    skips leading-dot names — so a mounted file called `.env` would be pushed,
    bound in, and changing it would restart nothing. That failure is silent
    where the others here are loud, which is why the name is refused at the one
    place a caller can be held to a name at all.
    """
    with pytest.raises(ValueError, match='content stamp cannot see'):
        _ = nspawn.machine_file('plain', '.env')

    assert nspawn.machine_file('plain', 'resolv.conf') == f'{nspawn.machine_path("plain")}/resolv.conf'


##
## What the convergers say
##


@final
@dataclass(frozen=True)
class _MachinesRendering:
    """What `40-machines.sh.j2` reads, spelled again so a test can aim it at a tree.

    The production renderer points every one of these at the device's own
    paths; a case here points them at a temporary directory instead, which is
    what makes running the real script possible without a device.
    """

    cluster: str
    machines_root: str
    live_machines_dir: str
    unit_template: str
    rootfs: str
    state: str
    stamp: str
    initial_state: str
    nspawn_suffix: str
    marker_suffix: str
    staging_suffix: str


@final
@dataclass(frozen=True)
class _Device:
    """A device the converger can be run against: its two directories and a systemd.

    `commands` is what the fake `systemctl` appends every invocation to, and
    `active` is the set of units it believes are running — a directory, so the
    script's own `systemctl restart` is what puts a unit in it and the case can
    read afterwards whether a machine was bounced. `failing` holds the units
    whose start is to fail.
    """

    script: Path
    machines: Path
    live: Path
    commands: Path
    complaints: Path
    active: Path
    failing: Path
    tools: Path


SYSTEMCTL = """#!/bin/sh
echo "$*" >>{commands}
verb=$1
shift
case "$verb" in
    is-active)
        [ "$1" = --quiet ] && shift
        [ -e {active}/"$1" ]
        exit $?
        ;;
    is-enabled)
        exit 1
        ;;
    restart | start)
        [ ! -e {failing}/"$1" ] || exit 1
        : >{active}/"$1"
        exit 0
        ;;
    disable)
        [ "$1" = --now ] && shift
        rm -f {active}/"$1"
        exit 0
        ;;
esac
exit 0
"""


@pytest.fixture
def device(tmp_path: Path) -> _Device:
    """The converger rendered against a tree, with a systemd that can be read back."""
    box = _Device(
        script=tmp_path / nspawn.MACHINES_SCRIPT,
        machines=tmp_path / 'machines',
        live=tmp_path / 'live',
        commands=tmp_path / 'commands',
        complaints=tmp_path / 'complaints',
        active=tmp_path / 'active',
        failing=tmp_path / 'failing',
        tools=tmp_path / 'tools',
    )
    for directory in (box.machines, box.active, box.failing, box.tools):
        directory.mkdir()

    systemctl = box.tools / 'systemctl'
    _ = systemctl.write_text(
        SYSTEMCTL.format(commands=box.commands, active=box.active, failing=box.failing), encoding='utf-8'
    )
    systemctl.chmod(0o755)

    _ = box.script.write_text(
        templates.render(
            persistence.TEMPLATE_PACKAGE,
            f'templates/{nspawn.MACHINES_SCRIPT}.j2',
            _MachinesRendering(
                cluster=conventions.CLUSTER_NAME,
                machines_root=str(box.machines),
                live_machines_dir=str(box.live),
                unit_template=nspawn.UNIT_TEMPLATE,
                rootfs=nspawn.ROOTFS,
                state=nspawn.STATE,
                stamp=nspawn.STAMP,
                initial_state=nspawn.INITIAL_STATE,
                nspawn_suffix=nspawn.NSPAWN_SUFFIX,
                marker_suffix=nspawn.MARKER_SUFFIX,
                staging_suffix=STAGING_SUFFIX,
            ),
        ),
        encoding='utf-8',
    )
    return box


def declare(
    device: _Device,
    machine: str,
    *,
    settings: str | None = 'machine\n',
    tree: bool = True,
    files: dict[str, str] | None = None,
    initial_state: dict[str, str] | None = None,
) -> Path:
    """Put a machine on the device, the way a push leaves one.

    Every part is optional because the states a push passes through are what
    the converger has to survive: a directory whose settings have not landed
    yet, one whose tree has not, one whose settings were taken away by the
    delete that retired it.
    """
    directory = device.machines / machine
    directory.mkdir(exist_ok=True)
    if settings is not None:
        _ = (directory / f'{machine}{nspawn.NSPAWN_SUFFIX}').write_text(settings, encoding='utf-8')
    if tree:
        (directory / nspawn.ROOTFS).mkdir(exist_ok=True)
        _ = (directory / f'{nspawn.ROOTFS}{nspawn.MARKER_SUFFIX}').write_text('sha256:one\n', encoding='utf-8')
    for name, content in (files or {}).items():
        _ = (directory / name).write_text(content, encoding='utf-8')
    for name, content in (initial_state or {}).items():
        initial = directory / nspawn.INITIAL_STATE / name
        initial.parent.mkdir(parents=True, exist_ok=True)
        _ = initial.write_text(content, encoding='utf-8')
    return directory


def converge(device: _Device, *, environment: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """Run the converger once, and read back what it asked of systemd.

    What it wrote to the boot log's error stream is kept beside that, because
    on this device the log is the whole of what an unattended boot reports.
    """
    device.commands.unlink(missing_ok=True)
    completed = subprocess.run(  # noqa: S603 -- a rendered script of this repository's own
        ['/bin/bash', str(device.script)],  # noqa: S607 -- the shell the device's own scripts name
        env={'PATH': f'{device.tools}:/usr/bin:/bin', **(environment or {})},
        capture_output=True,
        check=False,
        # A converger that does not return is the failure this bounds: the
        # device runs it from the boot chain, where waiting forever and doing
        # nothing look the same from outside.
        timeout=30,
    )
    _ = device.complaints.write_bytes(completed.stderr)
    recorded = device.commands.read_text(encoding='utf-8').split('\n') if device.commands.exists() else []
    return completed.returncode, [line for line in recorded if line]


def started(commands: list[str]) -> set[str]:
    """The machines the converger restarted, by name."""
    return {
        line.removeprefix(f'restart {nspawn.UNIT_TEMPLATE}').removesuffix('.service')
        for line in commands
        if line.startswith('restart ')
    }


def test_the_converger_is_told_no_machines_and_finds_them_anyway(device: _Device) -> None:
    """The whole of the change: what exists is what is on the disk.

    The rendered script names no machine — there is no list in it to fall out
    of step with the declarations, and adding a service rewrites none of it —
    and two machines that were never mentioned to it are linked, enabled and
    started because their directories are there.
    """
    _ = declare(device, 'alice')
    _ = declare(device, 'bob')

    status, commands = converge(device)

    assert 'alice' not in device.script.read_text(encoding='utf-8')
    assert status == 0
    assert started(commands) == {'alice', 'bob'}
    for machine in ('alice', 'bob'):
        assert (device.live / machine).resolve() == device.machines / machine / nspawn.ROOTFS
        assert f'enable {nspawn.UNIT_TEMPLATE}{machine}.service' in commands


def test_a_device_with_no_machines_on_it_converges_to_nothing(device: _Device) -> None:
    """The first push of a device delivers the framework before anything runs on it.

    So an empty machines root is a legitimate state and not an error — the
    converger is on the box before any service is, and it is also the state a
    factory reset leaves.
    """
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()
    assert device.live.is_dir()


def test_a_directory_with_no_settings_file_is_not_a_machine(device: _Device) -> None:
    """A tree alone cannot make one, which is what bounds what a leftover can do.

    The settings file is what `30-nspawn-units.sh` mirrors on, so making it the
    same test here keeps the two convergers from disagreeing about what exists.
    A directory holding only a tree is a machine whose first push has not
    finished or one whose delete has — and starting either would boot a
    container on the template unit's defaults, which for a bridged machine is a
    virtual ethernet pair attached to nothing.
    """
    _ = declare(device, 'partial', settings=None)

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()
    assert not (device.live / 'partial').exists()


def test_a_machine_whose_settings_went_away_is_retired_and_its_tree_cannot_bring_it_back(
    device: _Device,
) -> None:
    """The delete that stops declaring a machine is what retires it on the device.

    Removing a `Container` deletes its files, and the settings file going is
    the event: the machine is disabled and unlinked on that hook, and stays
    that way on every boot afterwards even though the tree its artifact left
    behind is still on the disk. A converger that took a tree for a machine
    would restart a service the declaration no longer holds, once, at the next
    firmware update — with nothing to connect the two.
    """
    directory = declare(device, 'alice')
    _ = converge(device)

    (directory / f'alice{nspawn.NSPAWN_SUFFIX}').unlink()
    status, commands = converge(device)

    assert status == 0
    assert f'disable --now {nspawn.UNIT_TEMPLATE}alice.service' in commands
    assert not (device.live / 'alice').exists()
    assert (directory / nspawn.ROOTFS).is_dir(), 'the tree is exactly what is left behind'

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()
    assert not (device.live / 'alice').exists()


def test_a_machine_whose_tree_has_not_landed_is_skipped_and_its_siblings_are_not(device: _Device) -> None:
    """Every file of a machine runs this script, and the tree lands last.

    So the settings of a machine being created arrive at a converger that
    cannot start it yet. That is not an error and it is not a reason to leave
    the rest of the device unconverged: the tree's own delivery is what starts
    that machine, and until then its sibling converges normally.
    """
    _ = declare(device, 'arriving', tree=False)
    _ = declare(device, 'settled')

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'settled'}
    assert not (device.live / 'arriving').exists()


def test_a_converged_machine_is_left_alone_by_the_next_run(device: _Device) -> None:
    """Otherwise every push would restart the machine carrying its own session.

    The converger runs as the hook of every file of every machine, so a run
    that found work where there is none would bounce the whole device on each
    of them.
    """
    _ = declare(device, 'alice', files={'Caddyfile': 'one\n'})
    _ = converge(device)

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()


def test_a_machine_is_restarted_when_a_file_it_mounts_changes(device: _Device) -> None:
    """The content stamp covers the machine's directory, so the change is seen.

    Nothing tells the converger which files those are — a machine that mounts
    one more file needs no push of this script — and the file's content is what
    decides, so a file rewritten to what it already said restarts nothing.
    """
    directory = declare(device, 'alice', files={'Caddyfile': 'one\n'})
    _ = converge(device)

    _ = (directory / 'Caddyfile').write_text('two\n', encoding='utf-8')
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'alice'}


def test_a_stamp_covers_the_settings_and_the_pin_as_well_as_what_is_mounted(device: _Device) -> None:
    """Those two are what every machine has, and each is a reason to restart one.

    The digest marker is how a new root filesystem reaches the stamp at all —
    the tree is represented by the marker beside it, because walking a root
    filesystem to learn it has not changed costs more than the restart it would
    save — so a machine that skipped it would take a new image and go on
    running the old one until something else moved.
    """
    directory = declare(device, 'alice', settings='left\n')
    _ = converge(device)

    _ = (directory / f'alice{nspawn.NSPAWN_SUFFIX}').write_text('right\n', encoding='utf-8')
    _, commands = converge(device)
    assert started(commands) == {'alice'}

    _ = (directory / f'{nspawn.ROOTFS}{nspawn.MARKER_SUFFIX}').write_text('sha256:two\n', encoding='utf-8')
    _, commands = converge(device)
    assert started(commands) == {'alice'}


def test_a_stamp_is_a_sequence_and_not_a_bag(device: _Device) -> None:
    """Two files that traded contents are a machine that changed, and it says so.

    A checksum over a set that ignored order would call this machine unchanged
    — which is why the order is fixed in the script rather than left to
    whatever a walk returns.
    """
    directory = declare(device, 'alice', settings='left\n', files={'Caddyfile': 'right\n'})
    _ = converge(device)

    _ = (directory / f'alice{nspawn.NSPAWN_SUFFIX}').write_text('right\n', encoding='utf-8')
    _ = (directory / 'Caddyfile').write_text('left\n', encoding='utf-8')
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'alice'}


def test_the_converger_reads_what_is_a_file_and_walks_past_what_is_not(device: _Device) -> None:
    """It runs at boot, unattended, so it may not meet anything that waits.

    The stamp is taken over the machine's directory, and a converger that read
    whatever it found there would open a named pipe and block until a writer
    that is not coming arrives — leaving a device that boots, starts nothing,
    and reports nothing. Anything that is not a regular file is skipped, which
    keeps it out of the stamp as well.
    """
    directory = declare(device, 'alice', files={'Caddyfile': 'one\n'})
    _ = converge(device)

    os.mkfifo(directory / 'nobody-writes-here')
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()


def test_the_two_files_every_machine_has_are_read_by_kind_like_the_rest(device: _Device) -> None:
    """Naming a file is not knowing what is at that path.

    The settings file and the digest marker are the two the converger reaches
    for by name rather than by walking, and a converger that read them because
    it knew their names would wait forever on a named pipe at either — the same
    hazard the walk is already guarded against, at the two paths most likely to
    be mid-delivery.
    """
    directory = declare(device, 'alice')
    marker = directory / f'{nspawn.ROOTFS}{nspawn.MARKER_SUFFIX}'
    marker.unlink()
    os.mkfifo(marker)

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'alice'}, 'a machine whose marker is unreadable still converges'


def test_a_named_pipe_wearing_the_stamps_name_is_taken_away_rather_than_read(device: _Device) -> None:
    """The stamp is the one name under a machine that is the script's own.

    Nothing declares it and nothing but this script writes it, so whatever else
    is at that path is debris. Reading around it would not be enough — the
    write that follows a restart blocks on a named pipe exactly as the read
    does — so it is removed, and the run ends with a stamp the next run can
    compare against.
    """
    directory = declare(device, 'alice')
    os.mkfifo(directory / nspawn.STAMP)

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'alice'}
    assert (directory / nspawn.STAMP).is_file()

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set(), 'the stamp it wrote is the one it reads back'


def test_a_directory_wearing_the_stamps_name_is_taken_away_too(device: _Device) -> None:
    """Otherwise every write fails and the machine is restarted on every run.

    A converger that removed only the kinds it could read past would leave this
    one to bounce a machine forever — including, on a push, the machine
    carrying the deployment's own session.
    """
    directory = declare(device, 'alice')
    (directory / nspawn.STAMP).mkdir()

    _ = converge(device)
    status, commands = converge(device)

    assert status == 0
    assert (directory / nspawn.STAMP).is_file()
    assert started(commands) == set()


def test_a_link_wearing_the_stamps_name_is_taken_away_and_its_target_left_alone(device: _Device) -> None:
    """A link is the kind that answers for a file that is somewhere else.

    `-e` and `-f` both follow one, so a link is what would pass a kind test and
    still send the write it guards to a path this script never named. It goes
    the way the other debris goes, and what it pointed at is not this script's
    to touch.
    """
    directory = declare(device, 'alice')
    elsewhere = device.machines.parent / 'not-ours'
    _ = elsewhere.write_text('somebody else\n', encoding='utf-8')
    (directory / nspawn.STAMP).symlink_to(elsewhere)

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == {'alice'}
    assert not (directory / nspawn.STAMP).is_symlink()
    assert elsewhere.read_text(encoding='utf-8') == 'somebody else\n'


def test_a_machine_is_a_settings_file_and_not_a_path_that_answers(device: _Device) -> None:
    """What sits at the settings path decides whether there is a machine there.

    Anything that is not a regular file there is a machine the push has not
    finished with or never wrote, so it is not started and its unit is not
    enabled — and the converger does not read it on the way to deciding that.
    """
    directory = declare(device, 'alice')
    (directory / f'alice{nspawn.NSPAWN_SUFFIX}').unlink()
    os.mkfifo(directory / f'alice{nspawn.NSPAWN_SUFFIX}')

    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()
    assert not (device.live / 'alice').exists()


def test_a_healthy_device_gives_the_boot_log_nothing_to_read(device: _Device) -> None:
    """Nobody watches this box, so what it prints is what a later reader has.

    Every part of a machine's directory that is not a file — the tree, the
    trees a push parks beside it, the state, the initial state — is walked past
    rather than read, so a converged device is silent and anything on that
    stream is a device that needs somebody.
    """
    _ = declare(device, 'alice', files={'Caddyfile': 'one\n'}, initial_state={'AdGuardHome.yaml': 'listen\n'})
    (device.machines / 'alice' / f'{nspawn.ROOTFS}{SUPERSEDED_SUFFIX}').mkdir()

    status, _ = converge(device)

    assert status == 0
    assert device.complaints.read_text(encoding='utf-8') == ''

    status, _ = converge(device)

    assert status == 0
    assert device.complaints.read_text(encoding='utf-8') == ''


@pytest.mark.skipif(
    not Path('/usr/lib/locale/en_US.utf8').exists() and not Path('/usr/lib/locale/locale-archive').exists(),
    reason='no second locale on this box to collate against',
)
def test_the_stamp_does_not_move_with_the_locale_the_script_was_started_in(device: _Device) -> None:
    """The device runs this from systemd at boot and over a session at push time.

    Those two environments carry different locales, and collation is what
    decides the order a glob comes back in: `_beta` sorts after `Caddyfile` in
    one and before it in the other. A stamp that moved with it would bounce
    every machine on the device on each alternation between the two.
    """
    _ = declare(device, 'alice', files={'Caddyfile': 'one\n', '_beta': 'two\n'})
    _ = converge(device, environment={'LC_ALL': 'C'})

    status, commands = converge(device, environment={'LC_ALL': 'en_US.utf8'})

    assert status == 0
    assert started(commands) == set()


def test_a_stamp_ignores_a_write_a_push_has_in_flight_or_abandoned(device: _Device) -> None:
    """A push writes to a staged name and renames; the machines are pushed in parallel.

    So one machine's converger hook runs while another machine's file is
    part-written, and a run that counted the staged name would stamp a machine
    differently depending on when it was taken — restarting services for a
    neighbour's write. The same file is what a push that died mid-write leaves
    behind, and it must not become a reason to bounce anything either.
    """
    directory = declare(device, 'alice', files={'Caddyfile': 'one\n'})
    _ = converge(device)

    _ = (directory / f'Caddyfile{STAGING_SUFFIX}').write_text('half\n', encoding='utf-8')
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()


def test_a_machine_that_has_never_run_is_given_what_its_initial_state_holds(device: _Device) -> None:
    """The initial state lands where the declaration put it, and nothing carries a mapping.

    What the converger copies is the shape of the machine's initial-state
    directory onto its state directory, so a nested path is delivered as one —
    and the state directory exists either way, because it is a bind source and
    nspawn refuses to start a machine whose bind source is missing.
    """
    directory = declare(device, 'alice', initial_state={'AdGuardHome.yaml': 'listen\n', 'nested/other.yaml': 'deep\n'})
    _ = declare(device, 'bob')

    status, _ = converge(device)

    assert status == 0
    assert (directory / nspawn.STATE / 'AdGuardHome.yaml').read_text(encoding='utf-8') == 'listen\n'
    assert (directory / nspawn.STATE / 'nested' / 'other.yaml').read_text(encoding='utf-8') == 'deep\n'
    assert (device.machines / 'bob' / nspawn.STATE).is_dir(), (
        'a machine with no initial state still gets the bind source'
    )


def test_a_machine_that_has_run_keeps_its_own_state_and_is_not_bounced_for_its_initial_state(device: _Device) -> None:
    """The software behind it rewrites those files the moment it accepts a change.

    Emptiness of the state directory is the test rather than the absence of a
    file: a resolver that took a rewrite through its API owns the file it
    rewrote. And the initial state is out of the content stamp by where it
    sits, so re-declaring it — a listen address that moved, a template that
    changed — is not a reason to restart an instance that has already made it
    its own.
    """
    directory = declare(device, 'alice', initial_state={'AdGuardHome.yaml': 'listen\n'})
    _ = converge(device)
    _ = (directory / nspawn.STATE / 'AdGuardHome.yaml').write_text('rewritten by the instance\n', encoding='utf-8')

    _ = (directory / nspawn.INITIAL_STATE / 'AdGuardHome.yaml').write_text('listen elsewhere\n', encoding='utf-8')
    status, commands = converge(device)

    assert status == 0
    assert started(commands) == set()
    assert (directory / nspawn.STATE / 'AdGuardHome.yaml').read_text(encoding='utf-8') == (
        'rewritten by the instance\n'
    )


def test_something_at_the_live_name_that_is_not_this_programs_link_is_refused(device: _Device) -> None:
    """A machine of that name already exists on the box, and it is not ours.

    Replacing it would take a container away from whoever put it there, so the
    converger says so and fails the run instead — and the failure reaches the
    apply, because the exit status is carried out to the hook.
    """
    _ = declare(device, 'alice')
    device.live.mkdir()
    (device.live / 'alice').mkdir()

    status, commands = converge(device)

    assert status == 1
    assert started(commands) == set()


def test_a_live_link_that_points_outside_the_machines_root_is_not_retired(device: _Device) -> None:
    """Anything under the live directory that is not ours stays where it is.

    The retirement pass is the one place this script deletes something, so what
    it may consider is bounded by where the link points rather than by what it
    is called.
    """
    device.live.mkdir()
    elsewhere = device.live.parent / 'elsewhere'
    elsewhere.mkdir()
    (device.live / 'stranger').symlink_to(elsewhere)

    status, commands = converge(device)

    assert status == 0
    assert (device.live / 'stranger').is_symlink()
    assert not [line for line in commands if line.startswith('disable')]


def test_a_machine_that_failed_to_start_fails_the_run_and_leaves_its_stamp_unwritten(device: _Device) -> None:
    """The converger is a hook as well as a boot script, and a hook reports.

    A machine that could not be started is the one thing this script learns
    that nothing else on the device would report. Its stamp stays unwritten, so
    the next run has the work to do again rather than believing it done — and
    the machines beside it are converged regardless, because one broken service
    is not a reason to leave the device half configured.
    """
    broken = declare(device, 'alice')
    _ = declare(device, 'bob')
    _ = (device.failing / nspawn.machine_unit('alice')).write_text('', encoding='utf-8')

    status, commands = converge(device)

    assert status == 1
    assert started(commands) == {'alice', 'bob'}
    assert not (broken / nspawn.STAMP).exists()
    assert (device.machines / 'bob' / nspawn.STAMP).exists()


def test_the_settings_mirror_is_rendered_against_the_devices_own_directories() -> None:
    """The production rendering points at the device; the cases below point at a tree."""
    script = nspawn.nspawn_units_script()

    assert f'src={nspawn.MACHINES}' in script
    assert f'live={nspawn.LIVE_NSPAWN_DIR}' in script


@final
@dataclass(frozen=True)
class _NspawnUnitsRendering:
    """What `30-nspawn-units.sh.j2` reads, spelled again so a test can aim it at a tree."""

    cluster: str
    machines_root: str
    live_nspawn_dir: str
    suffix: str


@final
@dataclass(frozen=True)
class _Mirror:
    """The settings mirror rendered against two directories of a temporary tree."""

    script: Path
    machines: Path
    live: Path


@pytest.fixture
def mirror(tmp_path: Path) -> _Mirror:
    box = _Mirror(script=tmp_path / nspawn.NSPAWN_UNITS_SCRIPT, machines=tmp_path / 'machines', live=tmp_path / 'live')
    box.machines.mkdir()
    _ = box.script.write_text(
        templates.render(
            persistence.TEMPLATE_PACKAGE,
            f'templates/{nspawn.NSPAWN_UNITS_SCRIPT}.j2',
            _NspawnUnitsRendering(
                cluster=conventions.CLUSTER_NAME,
                machines_root=str(box.machines),
                live_nspawn_dir=str(box.live),
                suffix=nspawn.NSPAWN_SUFFIX,
            ),
        ),
        encoding='utf-8',
    )
    return box


def settings_of(mirror: _Mirror, machine: str) -> Path:
    """Where the push puts a machine's settings, whatever is actually there."""
    (mirror.machines / machine).mkdir(exist_ok=True)
    return mirror.machines / machine / f'{machine}{nspawn.NSPAWN_SUFFIX}'


def installed_as(mirror: _Mirror, machine: str) -> Path:
    """Where the mirror installs them, whatever is actually there."""
    return mirror.live / f'{machine}{nspawn.NSPAWN_SUFFIX}'


def mirror_once(mirror: _Mirror) -> tuple[int, str]:
    """Run the mirror once, bounded the way `converge` is: hanging is the failure."""
    completed = subprocess.run(  # noqa: S603 -- a rendered script of this repository's own
        ['/bin/bash', str(mirror.script)],  # noqa: S607 -- the shell the device's own scripts name
        env={'PATH': '/usr/bin:/bin'},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return completed.returncode, completed.stdout


def test_the_mirror_installs_a_settings_file_once_and_removes_it_with_its_source(mirror: _Mirror) -> None:
    """A true mirror, both ways: what the cases after this one are measured against."""
    _ = settings_of(mirror, 'alice').write_text('one\n', encoding='utf-8')

    status, log = mirror_once(mirror)
    assert (status, installed_as(mirror, 'alice').read_text(encoding='utf-8')) == (0, 'one\n')
    assert 'installing' in log

    status, log = mirror_once(mirror)
    assert (status, log) == (0, ''), 'an installed copy that matches its source is left alone'

    settings_of(mirror, 'alice').unlink()
    status, log = mirror_once(mirror)
    assert status == 0
    assert 'removing stale' in log
    assert not installed_as(mirror, 'alice').exists(), 'a retired machine is retired on a recovery boot too'


def test_a_settings_path_that_is_not_a_regular_file_is_neither_read_nor_a_machine(mirror: _Mirror) -> None:
    """The mirror runs at boot, unattended, so it may not open anything that waits.

    A named pipe where the settings should be is a machine the push has not
    finished with or never wrote; `cmp` on it would block until a writer that
    is not coming arrived, and take the rest of the boot chain with it. The
    kind is the test, and it is the same test `40-machines.sh` starts a machine
    by — so a copy installed while the file was real goes as stale, and the
    two convergers agree that there is no machine here.
    """
    _ = settings_of(mirror, 'alice').write_text('one\n', encoding='utf-8')
    _ = settings_of(mirror, 'bob').write_text('two\n', encoding='utf-8')
    _ = mirror_once(mirror)

    settings_of(mirror, 'alice').unlink()
    os.mkfifo(settings_of(mirror, 'alice'))

    status, _ = mirror_once(mirror)

    assert status == 0
    assert not installed_as(mirror, 'alice').exists()
    assert installed_as(mirror, 'bob').read_text(encoding='utf-8') == 'two\n', 'its siblings are still mirrored'


def test_debris_at_the_live_name_is_taken_away_and_the_settings_installed_over_it(mirror: _Mirror) -> None:
    """The live name is the mirror's own, so what wears it is not read around.

    `cmp` opens both of its arguments, so a named pipe on the live side wedges
    exactly as one on the source side does; and `cp` would write through a link
    to wherever it pointed. Either is removed first, and the link's target is
    left alone. Debris with no source goes the same way: a dangling link, which
    `-e` cannot see, and a directory, which a plain `rm` refuses.
    """
    _ = settings_of(mirror, 'alice').write_text('one\n', encoding='utf-8')
    _ = settings_of(mirror, 'bob').write_text('two\n', encoding='utf-8')
    mirror.live.mkdir()
    os.mkfifo(installed_as(mirror, 'alice'))
    elsewhere = mirror.live.parent / 'elsewhere'
    _ = elsewhere.write_text('not the settings\n', encoding='utf-8')
    installed_as(mirror, 'bob').symlink_to(elsewhere)
    installed_as(mirror, 'carol').symlink_to(mirror.live.parent / 'nowhere')
    installed_as(mirror, 'dave').mkdir()

    status, _ = mirror_once(mirror)

    assert status == 0
    assert installed_as(mirror, 'alice').read_text(encoding='utf-8') == 'one\n'
    assert not installed_as(mirror, 'bob').is_symlink()
    assert installed_as(mirror, 'bob').read_text(encoding='utf-8') == 'two\n'
    assert elsewhere.read_text(encoding='utf-8') == 'not the settings\n'
    assert not installed_as(mirror, 'carol').is_symlink(), 'a dangling link with no source is taken away'
    assert not installed_as(mirror, 'dave').exists(), 'a directory with no source is taken away'


##
## The rollback
##


def test_a_rollback_swaps_the_trees_and_withdraws_the_claim_about_them() -> None:
    """The marker says which published artifact the live tree came from.

    After a swap that claim is false, and a marker left in place would make the
    next preview see a device that already holds the pin — so the rollback
    takes it away, leaving a tree of unknown provenance, which is work the next
    push does.
    """
    program = nspawn.rollback_program()

    assert f'live={nspawn.MACHINES}/$machine/{nspawn.ROOTFS}' in program
    assert f'superseded=$live{SUPERSEDED_SUFFIX}' in program
    assert f'rm -f "$live{marker_path("")}"' in program
    assert 'systemctl stop "$unit"' in program
    assert 'systemctl start "$unit"' in program


def test_a_rollback_with_nothing_to_roll_back_to_refuses() -> None:
    """The push leaves the displaced tree beside the live one until the next push.

    Outside that window there is nothing to swap in, and swapping in something
    else would be worse than failing: the health gate that calls this reports
    the failure either way.
    """
    program = nspawn.rollback_program()

    assert 'if [ ! -d "$superseded" ]; then' in program
    assert 'nothing to roll back to' in program
    # A failed rename must not let the next two run: a half-swapped machine is
    # a machine on neither tree.
    assert 'set -eu' in program


@pytest.mark.asyncio
async def test_a_machines_file_waits_for_the_convergers_that_act_on_it(
    runtime: NspawnRuntime, mechanism: DevicePersistence
) -> None:
    """A hook that runs a script the device has not been given fails its apply.

    So what a machine's files must be behind is the two convergers, the
    rollback the health gate reaches for, the directory they all work in — and
    the package script, because the two programs the device pulls and unpacks a
    root filesystem with are not on the box until it has run.
    """
    urns = {str(await resource.urn.future()) for resource in runtime.convergers}

    assert urns == {
        str(await mechanism.packages.urn.future()),
        str(await runtime.skeleton.urn.future()),
        str(await runtime.nspawn_units.urn.future()),
        str(await runtime.machines.urn.future()),
        str(await runtime.rollback.urn.future()),
    }


def test_the_watchdog_unit_is_installed_by_the_layer_below(monitor: Recorder) -> None:
    """It is a unit source like any other, so `20-units.sh` is what installs it.

    Which is layer one's decision and not this one's: what the runtime states
    is that the watchdog is a unit, not where a unit goes.
    """
    inputs = monitor.inputs_of(f'{NAME}-unit-{nspawn.WATCHDOG_UNIT}')

    assert inputs['path'] == persistence.unit_source(nspawn.WATCHDOG_UNIT)
    assert persistence.on_boot_path(persistence.UNITS_SCRIPT) in inputs['hook']


def test_the_rollback_is_delivered_with_nothing_told_about_it(monitor: Recorder) -> None:
    """It converges nothing: it is a command something else takes when it must.

    An executable with a hook would run at delivery, and running a rollback
    because a rollback was installed is the opposite of what it is for.
    """
    inputs = monitor.inputs_of(f'{NAME}-bin-{nspawn.ROLLBACK_PROGRAM}')

    assert inputs['path'] == persistence.executable_path(nspawn.ROLLBACK_PROGRAM)
    assert inputs['mode'] == persistence.SCRIPT_MODE
    assert inputs.get('hook') is None


def test_the_convergers_run_themselves_once_they_land(monitor: Recorder) -> None:
    """The recovery path is the push path, for the scripts as for everything else."""
    for script in (nspawn.NSPAWN_UNITS_SCRIPT, nspawn.MACHINES_SCRIPT):
        inputs = monitor.inputs_of(f'{NAME}-on-boot-{script}')

        assert inputs['path'] == persistence.on_boot_path(script)
        assert inputs['hook'] == persistence.on_boot_hook(script)


def test_no_machine_is_declared_a_unit_of_its_own(monitor: Recorder) -> None:
    """systemd's template unit is what runs a machine, and it is systemd's.

    A unit per machine would be a second place a machine says what it is, and
    the one this program wrote would be the one `machinectl` did not read.
    """
    units = [
        declaration.name
        for declaration in monitor.declared
        if str(declaration.inputs.get('path', '')).startswith(persistence.UNIT_SOURCE_DIR)
        and monitor.options_of(declaration.name).parent.endswith(f'::{NAME}')
    ]

    assert units == [f'{NAME}-unit-{nspawn.WATCHDOG_UNIT}']
    assert nspawn.machine_unit('plain') == 'systemd-nspawn@plain.service'


def test_the_runtime_declares_no_machine_of_its_own(monitor: Recorder) -> None:
    """The framework is not a workload: it fills nothing under a machine.

    What a machine holds is the workload's, and the runtime is told nothing
    about which workloads there are — so a device running one service and a
    device running five get the same files from this component.
    """
    written = [
        str(declaration.inputs['path'])
        for declaration in monitor.declared
        if str(declaration.inputs.get('path', '')).startswith(f'{nspawn.MACHINES}/')
    ]

    assert written == []


def test_the_settings_converger_is_declared_before_the_machine_converger(monitor: Recorder) -> None:
    """Numeric order in the boot directory is what expresses the dependency.

    udm-boot runs the directory in name order and this layer's two scripts are
    numbered for it, so nothing else has to say which comes first.
    """
    assert nspawn.NSPAWN_UNITS_SCRIPT < nspawn.MACHINES_SCRIPT
    assert monitor.inputs_of(f'{NAME}-on-boot-{nspawn.NSPAWN_UNITS_SCRIPT}')['mode'] == persistence.SCRIPT_MODE


def test_the_pieces_of_one_machine_are_all_under_its_own_directory() -> None:
    """A machine can be inspected, moved or deleted whole, which is the point.

    Everything the runtime keeps for a machine derives from one directory, so
    there is no second place a piece of it could be left behind.
    """
    directory = nspawn.machine_path('plain')

    assert nspawn.rootfs_path('plain') == f'{directory}/rootfs'
    assert nspawn.state_path('plain') == f'{directory}/state'
    assert nspawn.nspawn_path('plain') == f'{directory}/plain.nspawn'
    assert nspawn.stamp_path('plain') == f'{directory}/stamp'
    assert marker_path(nspawn.rootfs_path('plain')) == f'{directory}/rootfs.digest'
    assert nspawn.initial_state_path('plain', 'AdGuardHome.yaml') == f'{directory}/initial-state/AdGuardHome.yaml'


@pytest.mark.asyncio
async def test_the_runtime_is_not_a_second_place_the_layout_is_decided(
    runtime: NspawnRuntime, monitor: Recorder
) -> None:
    """Every path the scripts act on is the one the declaration puts a file at.

    The scripts are rendered from the same constants the paths are built from,
    so a machine's directory cannot mean one thing to the converger and another
    to the push.
    """
    script = str(monitor.inputs_of(f'{NAME}-on-boot-{nspawn.MACHINES_SCRIPT}')['content'])

    assert f'MACHINES={nspawn.MACHINES}' in script
    assert nspawn.MACHINES == persistence.skeleton_path(nspawn.SKELETON)
    assert str(await runtime.machines.path.future()) == persistence.on_boot_path(nspawn.MACHINES_SCRIPT)
