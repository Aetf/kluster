"""The gateway's persistence mechanism, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: which file lands where and with which mode, which command runs after
a write and after a delete, whose component a file asked for through the layer
belongs to, and what the two rendered boot-chain scripts say once the data is in
them.

The renderers are plain functions, so most cases read their output directly; the
component tree is declared once against mocks, which is where a wiring mistake
would surface.
"""

from __future__ import annotations

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import persistence
from kluster.components.gateway.persistence import DevicePersistence
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

#: The one dynamic resource type this suite is about.
DEVICE_FILE = 'pulumi-python:dynamic/device:File'

NAME = 'mechanism'
CONSUMER = 'consumer'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'

#: What the layer above this one requires of the device's package set, as the
#: gateway passes it. Restated rather than imported, so that a change to the set
#: has to be made twice — once where it is declared and once here.
PACKAGES = ('systemd-container', 'libnss-mymachines')

#: What a consumer asks the layer for, one of each kind.
SCRIPT = '30-example.sh'
PROGRAM = 'example-watchdog.sh'
UNIT = 'example.service'
DIRECTORY = 'machines'


class Layer(Component, pulumi_type='test:gateway:Layer'):
    """A layer above the mechanism, asking for one file of each kind.

    It stands in for the components the design puts on top of the persistence
    layer: it knows what it needs on the device, and nothing about where such a
    file goes or what has to run once it is there.
    """

    def __init__(self, name: str, *, mechanism: DevicePersistence, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.script: DeviceFile = mechanism.on_boot_script(SCRIPT, '#!/bin/sh\nexit 0\n', opts=self.child_opts())
        self.program: DeviceFile = mechanism.executable(PROGRAM, '#!/bin/sh\nexit 0\n', opts=self.child_opts())
        self.unit: DeviceFile = mechanism.unit(UNIT, '[Unit]\nDescription=Example\n', opts=self.child_opts())
        self.directory: DeviceFile = mechanism.skeleton_dir(DIRECTORY, opts=self.child_opts())
        self.register_outputs({})


class Neighbours(Component, pulumi_type='test:gateway:Neighbours'):
    """Two executables whose names differ only after the dot."""

    def __init__(self, name: str, *, mechanism: DevicePersistence, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.shell: DeviceFile = mechanism.executable('example.sh', '#!/bin/sh\nexit 0\n', opts=self.child_opts())
        self.python: DeviceFile = mechanism.executable('example.py', 'raise SystemExit(0)\n', opts=self.child_opts())
        self.register_outputs({})


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def mechanism(monitor: Recorder) -> DevicePersistence:
    """The mechanism and one consumer of it, declared once."""
    async with declaring():
        layer_one = DevicePersistence(
            NAME,
            connection=Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER),
            packages=PACKAGES,
        )
        _ = Layer(CONSUMER, mechanism=layer_one)
    return layer_one


##
## What the layer puts on the device
##


def test_the_boot_chain_is_delivered_where_the_vendored_oneshot_looks(monitor: Recorder) -> None:
    """`udm-boot.service` runs `/data/on_boot.d`, so that is where a script goes.

    The directory is not this program's choice: it is compiled into the
    `ExecStart` of the unit vendored beside this module, so a script delivered
    anywhere else is a script nothing runs.
    """
    packages = monitor.inputs_of(f'{NAME}-on-boot-{persistence.PACKAGES_SCRIPT}')
    units = monitor.inputs_of(f'{NAME}-on-boot-{persistence.UNITS_SCRIPT}')

    assert packages['path'] == f'{conventions.gateway.ON_BOOT_D}/{persistence.PACKAGES_SCRIPT}'
    assert units['path'] == f'{conventions.gateway.ON_BOOT_D}/{persistence.UNITS_SCRIPT}'
    for inputs in (packages, units):
        assert inputs['mode'] == persistence.SCRIPT_MODE
        assert inputs['owner'] == conventions.gateway.SSH_USER

    assert conventions.gateway.ON_BOOT_D in persistence.udm_boot_unit()


def test_the_anchor_of_the_chain_is_delivered_as_a_unit_source(monitor: Recorder) -> None:
    """A wiped `/etc` is recoverable because the device still holds the unit.

    The vendored oneshot goes under the custom root like any other unit source,
    which is what makes `20-units.sh` able to put it back — and what makes the
    runbook's recovery one copy of a file the device already has. Its header
    carries the upstream pin, because bytes vendored from somebody else are
    only reviewable if the reader can find where they came from.
    """
    inputs = monitor.inputs_of(f'{NAME}-unit-{persistence.UDM_BOOT_UNIT}')

    assert inputs['path'] == persistence.unit_source(persistence.UDM_BOOT_UNIT)
    assert inputs['mode'] == persistence.FILE_MODE

    header = persistence.udm_boot_unit().splitlines()[0:2]
    assert any('unifi-utilities' in line for line in header), header
    assert any(len(word) == 40 for line in header for word in line.split()), header


def test_the_custom_root_has_its_shape_before_anything_fills_it(monitor: Recorder) -> None:
    """The skeleton is declared, and declared beside itself rather than inside.

    A directory is desired state like a file is, but the provider writes files —
    so what is declared is a marker whose hook creates the directory. That is
    the only shape available for the one directory nothing may write in.
    """
    for directory in persistence.SKELETON:
        inputs = monitor.inputs_of(f'{NAME}-skeleton-{directory}')

        assert inputs['path'] == f'{persistence.SKELETON_DIR}/{directory}'
        assert inputs['content'].strip() == f'{conventions.gateway.CUSTOM_ROOT}/{directory}'
        assert f'mkdir -p {conventions.gateway.CUSTOM_ROOT}/{directory}' in inputs['hook']

    assert set(persistence.SKELETON) == {'bin', 'dpkg', 'units'}


def test_nothing_is_ever_written_inside_the_offline_package_cache(monitor: Recorder) -> None:
    """The cache is `10-packages.sh`'s alone, directory and contents.

    The script refreshes it by replacing the whole directory in one rename, so a
    file this program wrote in it would be deleted by the next refresh and
    reported as drift on every preview afterwards. The directory is still
    declared — by a marker outside it.
    """
    written = [
        declaration.inputs['path']
        for declaration in monitor.of_type(DEVICE_FILE)
        if str(declaration.inputs.get('path', '')).startswith(f'{persistence.DPKG_DIR}/')
    ]

    assert written == []
    assert monitor.inputs_of(f'{NAME}-skeleton-dpkg')['path'] == f'{persistence.SKELETON_DIR}/dpkg'


##
## The interface the layers above use
##


def test_a_file_asked_for_through_the_layer_belongs_to_the_layer_that_asked(monitor: Recorder) -> None:
    """The path discipline is layer one's; the resource is the caller's.

    A component that needs a file on the device gets a child of its own back,
    so the file is deleted when that component stops declaring it and shows up
    under it in a preview — while nothing above this layer has to know which
    directory the file goes in or what runs afterwards. The mechanism's own
    files stay the mechanism's.
    """
    for name in (f'{NAME}-on-boot-{SCRIPT}', f'{NAME}-bin-{PROGRAM}', f'{NAME}-unit-{UNIT}'):
        assert monitor.options_of(name).parent.endswith(f'::{CONSUMER}'), name

    assert monitor.options_of(f'{NAME}-skeleton-{DIRECTORY}').parent.endswith(f'::{CONSUMER}')
    assert monitor.options_of(f'{NAME}-on-boot-{persistence.UNITS_SCRIPT}').parent.endswith(f'::{NAME}')


def test_an_executable_is_delivered_and_nothing_is_told_about_it(monitor: Recorder) -> None:
    """A program in `bin/` is run by a unit, so installing one is not an event.

    Only the named file is managed: an executable placed on the device by hand
    beside it is a neighbour this program never looks at, which is what makes
    the directory shared rather than owned.
    """
    inputs = monitor.inputs_of(f'{NAME}-bin-{PROGRAM}')

    assert inputs['path'] == f'{persistence.BIN_DIR}/{PROGRAM}'
    assert inputs['mode'] == persistence.SCRIPT_MODE
    assert inputs.get('hook') is None


@pytest.mark.asyncio
async def test_two_files_whose_names_share_a_stem_are_two_resources(
    monitor: Recorder, mechanism: DevicePersistence
) -> None:
    """The file's whole name decides the resource, because it decides the path.

    `example.sh` and `example.py` are two files on the device, so they are two
    resources; a name that dropped the suffix would put one URN where the device
    has two files, and the second declaration would silently replace the first.
    """
    async with declaring():
        _ = Neighbours('neighbours', mechanism=mechanism)

    shell = monitor.inputs_of(f'{NAME}-bin-example.sh')
    python = monitor.inputs_of(f'{NAME}-bin-example.py')

    assert shell['path'] == f'{persistence.BIN_DIR}/example.sh'
    assert python['path'] == f'{persistence.BIN_DIR}/example.py'


def test_a_unit_of_a_kind_the_converger_never_walks_is_refused(mechanism: DevicePersistence) -> None:
    """The one failure this mechanism could not report, refused where it starts.

    `20-units.sh` walks the `.service` sources and nothing else, so a timer or a
    mount delivered through this method would land on the device, run the hook,
    and be installed by nobody — with no error anywhere. The limit is stated
    instead, naming the script that holds it.
    """
    with pytest.raises(ValueError, match=persistence.UNITS_SCRIPT):
        _ = mechanism.unit('example.timer', '[Timer]\nOnCalendar=daily\n', opts=pulumi.ResourceOptions())


def test_a_script_of_the_chain_runs_itself_once_it_lands(monitor: Recorder) -> None:
    """Delivering a converger converges: the recovery path is the apply path.

    The guard is what makes the same command survive the delete, which runs the
    hook too — a script this program no longer declares is gone from the device,
    and there is nothing left to run.
    """
    hook = monitor.inputs_of(f'{NAME}-on-boot-{SCRIPT}')['hook']

    assert hook == f'if [ -x {persistence.on_boot_path(SCRIPT)} ]; then {persistence.on_boot_path(SCRIPT)}; fi'


def test_the_command_that_runs_an_executable_is_this_layers_to_write() -> None:
    """A file elsewhere may name a `bin/` program as its hook, and asks for it here.

    Where the program sits and how a hook survives the delete that removes it
    are the same two decisions this layer already makes for a script of the
    boot chain, so a component that needs them does not write the shell for
    itself and cannot get the guard subtly wrong.
    """
    hook = persistence.executable_hook(PROGRAM)
    path = persistence.executable_path(PROGRAM)

    assert hook == f'if [ -x {path} ]; then {path}; fi'


@pytest.mark.asyncio
async def test_a_unit_waits_for_the_converger_that_installs_it(monitor: Recorder, mechanism: DevicePersistence) -> None:
    """A hook that runs a script the device has not been given fails its apply.

    So every unit delivered through this layer depends on `20-units.sh`,
    wherever the component that asked for it sits in the tree.
    """
    converger = str(await mechanism.units.urn.future())

    assert converger in monitor.depends_on(f'{NAME}-unit-{UNIT}')
    assert converger in monitor.depends_on(f'{NAME}-unit-{persistence.UDM_BOOT_UNIT}')


def test_a_unit_is_retired_by_the_delete_that_stops_declaring_it(monitor: Recorder) -> None:
    """One command, two meanings, and the device says which one applies.

    A hook runs after a write and after a delete alike, so it asks whether the
    source is still there. Gone means this program stopped declaring the unit,
    and the same session disables it and removes the live copy — otherwise a
    retired unit would keep running until somebody found it on the device.
    """
    hook = monitor.inputs_of(f'{NAME}-unit-{UNIT}')['hook']
    live = f'{persistence.LIVE_UNIT_DIR}/{UNIT}'

    assert f'if [ -e {persistence.unit_source(UNIT)} ]' in hook
    assert persistence.on_boot_path(persistence.UNITS_SCRIPT) in hook
    assert f'systemctl disable --now {UNIT}' in hook
    assert f'rm -f {live}' in hook
    assert 'systemctl daemon-reload' in hook


def test_a_skeleton_directory_is_taken_away_only_while_it_is_empty() -> None:
    """Undeclaring a directory is not a licence to delete what is in it.

    The delete branch is `rmdir`, so a directory something on the device still
    fills stays, and one nothing filled goes with the marker that declared it.
    """
    hook = persistence.skeleton_hook(DIRECTORY)

    assert f'rmdir {persistence.skeleton_path(DIRECTORY)}' in hook
    assert 'rm -r' not in hook


##
## What the rendered scripts say
##


def test_the_package_set_is_data_and_the_transaction_is_mechanism() -> None:
    """Which packages is the estate's; how they are installed is the script's.

    The set is rendered into one line and nothing else about the file changes
    with it: one apt transaction, because packages version-locked to each other
    cannot be resolved one at a time, and an offline cache for the boot where
    apt cannot be reached.
    """
    script = persistence.packages_script(PACKAGES)

    assert 'PACKAGES=(libnss-mymachines systemd-container)' in script
    assert f'CACHE={persistence.DPKG_DIR}' in script
    assert script.count('apt-get install -y "${PACKAGES[@]}"') == 1
    assert 'dpkg -i "$CACHE"/*.deb' in script
    # The transport this repository used before it owned the mechanism is not a
    # package the device needs: the device-file provider carries its own.
    assert 'rsync' not in script


def test_the_package_set_is_a_set() -> None:
    """The file is a function of what is required, not of how it was listed.

    Two layers asking for the same package, or asking in a different order, must
    not produce two different files for a preview to report as a change.
    """
    assert persistence.packages_script(('b', 'a', 'b')) == persistence.packages_script(('a', 'b'))
    assert 'PACKAGES=(a b)' in persistence.packages_script(('b', 'a'))


def test_a_package_name_is_one_word_however_it_was_written() -> None:
    """The array is shell, and a caller's string is not.

    A name carrying a space or a metacharacter would otherwise split into
    entries the device cannot install, or run as something else entirely.
    """
    assert "PACKAGES=('two words')" in persistence.packages_script(('two words',))


@pytest.mark.asyncio
async def test_a_mechanism_with_no_packages_is_refused() -> None:
    """An empty set renders a script that fails the boot it exists to repair.

    `set -u` and an empty array expansion is a `apt-get install` with no
    arguments at best and an unbound variable at worst, on the one boot where
    the device has nothing.
    """
    async with declaring():
        with pytest.raises(ValueError, match='at least one package'):
            _ = DevicePersistence(
                'empty',
                connection=Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER),
                packages=(),
            )


def test_the_unit_converger_never_restarts_the_oneshot_running_it() -> None:
    """It is the script `udm-boot.service` is executing at that moment.

    Everything else it installs is enabled, and restarted when its file changed;
    the anchor is converged and enabled and left running, because restarting it
    would kill the boot chain halfway through.
    """
    script = persistence.units_script()

    assert f'srcdir={persistence.UNIT_SOURCE_DIR}' in script
    assert f'dest={persistence.LIVE_UNIT_DIR}/$unit' in script
    assert f'[ "$unit" = {persistence.UDM_BOOT_UNIT} ] && continue' in script
    assert 'systemctl restart "$unit"' in script
    # The glob and the kind `unit` accepts are one decision, so a source that
    # would be installed by nothing cannot be declared in the first place.
    assert f'for src in "$srcdir"/*{persistence.UNIT_SUFFIX}; do' in script
