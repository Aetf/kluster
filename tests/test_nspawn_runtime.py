"""The nspawn runtime, asserted against Pulumi's mock provider.

Nothing here contacts a device, and nothing here knows what a container
service is: the runtime is the framework, so what is exercised is what it does
for *a* machine. Which file lands where, what runs after one lands, what the
two rendered convergers do to a device that has nothing on it, and what a
rollback moves.

The renderers are plain functions over plain data, so most cases read their
output directly; the component is declared once against mocks, which is where a
wiring mistake would surface.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import nspawn, persistence
from kluster.components.gateway.nspawn import Machine, NspawnRuntime, Placement
from kluster.components.gateway.persistence import DevicePersistence
from kluster.providers.device_files.provider import SUPERSEDED_SUFFIX, Connection, marker_path

NAME = 'runtime'
#: The mechanism the runtime asks, whose own name is what its `_declare`
#: builds a resource name out of: the path discipline is layer one's, so the
#: resource is named for the layer that decided the path and parented to the
#: layer that needed the file.
MECHANISM = 'mechanism'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'

#: Two machines, one of them with an initial state: enough to render every
#: branch of the converger, and named so that neither is a service of this
#: repository's own.
PLAIN = Machine(
    name='plain',
    stamped=(nspawn.nspawn_path('plain'), marker_path(nspawn.rootfs_path('plain'))),
    initial_state=None,
)
GIVEN_STATE = Machine(
    name='stateful',
    stamped=(nspawn.nspawn_path('stateful'), marker_path(nspawn.rootfs_path('stateful'))),
    initial_state=Placement(
        source=nspawn.machine_file('stateful', 'initial.yaml'),
        destination=f'{nspawn.state_path("stateful")}/live.yaml',
    ),
)
MACHINES = (GIVEN_STATE, PLAIN)


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
        framework = NspawnRuntime(NAME, mechanism=mechanism, machines=MACHINES)
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
        f'{MECHANISM}-on-boot-{nspawn.NSPAWN_UNITS_SCRIPT}',
        f'{MECHANISM}-on-boot-{nspawn.MACHINES_SCRIPT}',
        f'{MECHANISM}-bin-{nspawn.ROLLBACK_PROGRAM}',
        f'{MECHANISM}-bin-{nspawn.WATCHDOG_WORKER}',
        f'{MECHANISM}-unit-{nspawn.WATCHDOG_UNIT}',
        f'{MECHANISM}-skeleton-{nspawn.SKELETON}',
    ):
        assert monitor.options_of(name).parent.endswith(f'::{NAME}'), name

    assert monitor.inputs_of(f'{MECHANISM}-skeleton-{nspawn.SKELETON}')['path'] == nspawn.MACHINES
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
    was written last would decide what the machine booted.
    """
    assert nspawn.machine_file('plain', 'Caddyfile') == f'{nspawn.machine_path("plain")}/Caddyfile'

    for reserved in ('rootfs', 'rootfs.digest', 'state', 'stamp', 'plain.nspawn'):
        with pytest.raises(ValueError, match='nspawn runtime keeps'):
            _ = nspawn.machine_file('plain', reserved)

    # Any settings name and not only this machine's: the mirror keys the live
    # directory by machine name, so a second one here would be installed as
    # another machine's settings and removed as stale in the same run.
    with pytest.raises(ValueError, match='nspawn runtime keeps'):
        _ = nspawn.machine_file('plain', f'other{nspawn.NSPAWN_SUFFIX}')


##
## What the convergers say
##


def test_the_machine_set_carries_no_order() -> None:
    """Which machine moves when is a fact about a push, not about the device.

    At boot no apply is in flight and no session rides anything the device
    runs, so there is nothing for a start order to protect. The set is sorted
    so that the file is a function of which machines are declared rather than
    of the order a caller listed them in.
    """
    script = nspawn.machines_script(MACHINES)
    declared = next(line for line in script.splitlines() if line.startswith('DECLARED='))

    assert declared == 'DECLARED="plain stateful"'
    assert nspawn.machines_script(MACHINES) == nspawn.machines_script((PLAIN, GIVEN_STATE))


def test_the_link_names_the_root_filesystem_and_not_the_machine_directory() -> None:
    """What systemd boots is a tree, and the machine's directory is not one.

    Linking the directory would hand nspawn a root filesystem containing the
    machine's settings, its state and its stamp — the layout would be visible
    inside the container, and the state would be inside the tree the next push
    replaces whole.
    """
    script = nspawn.machines_script(MACHINES)

    assert f'root=$MACHINES/$machine/{nspawn.ROOTFS}' in script
    assert f'LIVE={nspawn.LIVE_MACHINES_DIR}' in script
    assert 'ln -s "$root" "$link"' in script


def test_a_machine_is_restarted_only_when_something_that_defines_it_changed() -> None:
    """Otherwise every push would restart the machine carrying its own session.

    The content stamp is a checksum over the machine's stamped set, and the
    converger compares before acting; an unchanged stamp and an active unit
    mean there is nothing to do.
    """
    script = nspawn.machines_script(MACHINES)

    for path in GIVEN_STATE.stamped:
        assert path in script
    assert f'stamp=$MACHINES/$machine/{nspawn.STAMP}' in script
    assert 'cksum' in script
    assert 'systemctl is-active --quiet "$unit"' in script
    assert 'systemctl restart "$unit"' in script


def test_every_declared_machine_gets_a_state_directory() -> None:
    """A bind whose source is missing is a machine that refuses to start.

    The writable state is bound into every machine here, and nothing else on
    the device creates that directory: a machine that has never run would
    otherwise fail its first start, and the machine that has never run is the
    last one of the cutover push.
    """
    script = nspawn.machines_script(MACHINES)

    assert f'mkdir -p "$MACHINES/$machine/{nspawn.STATE}"' in script
    assert script.index(f'mkdir -p "$MACHINES/$machine/{nspawn.STATE}"') < script.index(
        'install_initial_state "$machine"'
    )


def test_a_machine_is_given_its_initial_state_only_while_its_state_is_empty() -> None:
    """The software behind it rewrites the file the moment it accepts a change.

    So the initial state is delivered under a name the software does not read,
    and it is copied in only when the machine has never run. Emptiness is the
    test rather than the absence of the file: a machine that has run once owns
    everything in there.
    """
    script = nspawn.machines_script(MACHINES)

    assert GIVEN_STATE.initial_state is not None
    assert GIVEN_STATE.initial_state.source in script
    assert GIVEN_STATE.initial_state.destination in script
    assert f'[ -z "$(ls -A "$MACHINES/$1/{nspawn.STATE}" 2>/dev/null)" ] || return 0' in script
    assert PLAIN.name not in script.split('install_initial_state() {')[1].split('}')[0]


def test_the_converger_converges_a_device_that_has_nothing_on_it() -> None:
    """This is the firmware-update case, and the first-push case.

    The script is written before the trees it describes, so a machine whose
    root filesystem has not landed yet is skipped rather than fatal — and a
    machine no longer declared is disabled and unlinked, which is what keeps
    the device from accumulating every service it ever ran.
    """
    script = nspawn.machines_script(MACHINES)

    assert '[ -d "$root" ] || continue' in script
    # Both halves: a machine started without the settings 30 mirrors would come
    # up on the template unit's defaults, which for a bridged machine is an
    # interface attached to nothing — and the gate would then pass on it.
    assert f'[ -e "$MACHINES/$machine/$machine{nspawn.NSPAWN_SUFFIX}" ] || continue' in script
    assert 'systemctl disable --now' in script
    assert 'machines: retiring $machine' in script
    # Only links into the machines root are candidates for retirement: anything
    # else under the live directory is somebody else's container.
    assert 'case "$(readlink "$link")" in' in script


def test_a_machine_that_failed_to_start_fails_the_script() -> None:
    """The converger is a hook as well as a boot script, and a hook reports.

    A machine that could not be started is the one thing this script learns
    that nothing else on the device would report, so its exit status carries
    it out to the apply.
    """
    script = nspawn.machines_script(MACHINES)

    assert 'failed=1' in script
    assert script.rstrip().endswith('exit "$failed"')


def test_the_settings_mirror_removes_what_has_no_source() -> None:
    """The live directory is wholly this program's, unlike the unit store.

    So a machine retired here is retired on a recovery boot too, rather than
    only on the push that retired it.
    """
    script = nspawn.nspawn_units_script()

    assert f'src={nspawn.MACHINES}' in script
    assert f'live={nspawn.LIVE_NSPAWN_DIR}' in script
    assert 'for dir in "$src"/*/; do' in script
    assert f'f=$dir$machine{nspawn.NSPAWN_SUFFIX}' in script
    assert 'removing stale' in script


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
    inputs = monitor.inputs_of(f'{MECHANISM}-unit-{nspawn.WATCHDOG_UNIT}')

    assert inputs['path'] == persistence.unit_source(nspawn.WATCHDOG_UNIT)
    assert persistence.on_boot_path(persistence.UNITS_SCRIPT) in inputs['hook']


def test_the_rollback_is_delivered_with_nothing_told_about_it(monitor: Recorder) -> None:
    """It converges nothing: it is a command something else takes when it must.

    An executable with a hook would run at delivery, and running a rollback
    because a rollback was installed is the opposite of what it is for.
    """
    inputs = monitor.inputs_of(f'{MECHANISM}-bin-{nspawn.ROLLBACK_PROGRAM}')

    assert inputs['path'] == persistence.executable_path(nspawn.ROLLBACK_PROGRAM)
    assert inputs['mode'] == persistence.SCRIPT_MODE
    assert inputs.get('hook') is None


def test_the_convergers_run_themselves_once_they_land(monitor: Recorder) -> None:
    """The recovery path is the push path, for the scripts as for everything else."""
    for script in (nspawn.NSPAWN_UNITS_SCRIPT, nspawn.MACHINES_SCRIPT):
        inputs = monitor.inputs_of(f'{MECHANISM}-on-boot-{script}')

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

    assert units == [f'{MECHANISM}-unit-{nspawn.WATCHDOG_UNIT}']
    assert nspawn.machine_unit('plain') == 'systemd-nspawn@plain.service'


def test_the_runtime_declares_no_machine_of_its_own(monitor: Recorder) -> None:
    """The framework is not a workload: it fills nothing under a machine.

    What a machine holds is the workload's, which is why the runtime is handed
    a set of machines rather than the components that own them.
    """
    written = [
        str(declaration.inputs['path'])
        for declaration in monitor.declared
        if str(declaration.inputs.get('path', '')).startswith(f'{nspawn.MACHINES}/')
    ]

    assert written == []


def test_the_declared_machines_are_the_ones_the_runtime_was_given(monitor: Recorder) -> None:
    """The converger acts on the set it was handed and invents nothing.

    A machine in the script that no component declares is a machine the device
    would try to start and never find a tree for; one missing from it is a
    machine nothing ever starts.
    """
    script = monitor.inputs_of(f'{MECHANISM}-on-boot-{nspawn.MACHINES_SCRIPT}')['content']
    declared = next(line for line in str(script).splitlines() if line.startswith('DECLARED='))

    assert set(declared.removeprefix('DECLARED="').rstrip('"').split()) == {machine.name for machine in MACHINES}


def test_the_settings_converger_is_declared_before_the_machine_converger(monitor: Recorder) -> None:
    """Numeric order in the boot directory is what expresses the dependency.

    udm-boot runs the directory in name order and this layer's two scripts are
    numbered for it, so nothing else has to say which comes first.
    """
    assert nspawn.NSPAWN_UNITS_SCRIPT < nspawn.MACHINES_SCRIPT
    assert monitor.inputs_of(f'{MECHANISM}-on-boot-{nspawn.NSPAWN_UNITS_SCRIPT}')['mode'] == persistence.SCRIPT_MODE


@pytest.mark.asyncio
async def test_a_machine_named_by_nobody_is_not_declared() -> None:
    """The runtime with no machines is a device with a framework and no workloads.

    That is a legitimate state — the first push of a device delivers the
    framework before anything runs on it — so it renders rather than refuses.
    """
    script = nspawn.machines_script(())

    assert 'DECLARED=""' in script
    assert 'no machine on this device owns its own configuration' in script


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


@pytest.mark.asyncio
async def test_the_runtime_is_not_a_second_place_the_layout_is_decided(
    runtime: NspawnRuntime, monitor: Recorder
) -> None:
    """Every path the scripts act on is the one the declaration puts a file at.

    The scripts are rendered from the same constants the paths are built from,
    so a machine's directory cannot mean one thing to the converger and another
    to the push.
    """
    script = str(monitor.inputs_of(f'{MECHANISM}-on-boot-{nspawn.MACHINES_SCRIPT}')['content'])

    assert f'MACHINES={nspawn.MACHINES}' in script
    assert nspawn.MACHINES == persistence.skeleton_path(nspawn.SKELETON)
    assert str(await runtime.machines.path.future()) == persistence.on_boot_path(nspawn.MACHINES_SCRIPT)
