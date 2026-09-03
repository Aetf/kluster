"""The gateway's persistence mechanism, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: which file lands where and with which mode, which command runs after
a write and after a delete, whose component a file asked for through the layer
belongs to, and what the two rendered boot-chain scripts say once the data is in
them.

**The unit converger is also run**, against a temporary tree with a `systemctl`
that records what it was asked to do, because what matters most about the
drop-in half of it is a property of the sequence rather than of any line the
file contains: a drop-in this program no longer declares has to leave the
device, and one belonging to somebody else has to stay.

The renderers are plain functions, so most cases read their output directly; the
component tree is declared once against mocks, which is where a wiring mistake
would surface.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import persistence
from kluster.components.gateway.persistence import DevicePersistence
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceDirectory, DeviceFile
from putils import Component

#: The two dynamic resource types this suite is about.
DEVICE_FILE = 'pulumi-python:dynamic/device:File'
DEVICE_DIRECTORY = 'pulumi-python:dynamic/device:Directory'

NAME = 'mechanism'
CONSUMER = 'consumer'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'

#: What the layer above this one requires of the device's package set, as the
#: gateway passes it. Restated rather than imported, so that a change to the set
#: has to be made twice — once where it is declared and once here.
PACKAGES = ('systemd-container', 'libnss-mymachines')

#: What a consumer asks the layer for, one of each kind. The drop-in is on a
#: unit this program does not declare at all, which is the case it exists for:
#: `TEMPLATE_UNIT` stands in for systemd's own `systemd-nspawn@.service`.
SCRIPT = '30-example.sh'
PROGRAM = 'example-watchdog.sh'
UNIT = 'example.service'
TEMPLATE_UNIT = 'example@instance.service'
DROPIN = '10-example.conf'
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
        self.dropin: DeviceFile = mechanism.dropin(
            TEMPLATE_UNIT, DROPIN, '[Service]\nRestart=always\n', opts=self.child_opts()
        )
        self.directory: DeviceDirectory = mechanism.skeleton_dir(DIRECTORY, opts=self.child_opts())
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


def test_the_custom_root_is_declared_as_directories_at_the_paths_it_names(monitor: Recorder) -> None:
    """A directory is desired state like a file is, and a resource of its own.

    What the device is asked for is the directory itself, so one removed by hand
    is a change the next preview reports. The order between a directory and what
    goes in it is a separate claim, asserted below.
    """
    for directory in persistence.SKELETON:
        declaration = monitor.one(f'{NAME}-skeleton-{directory}')

        assert declaration.typ == DEVICE_DIRECTORY
        assert declaration.inputs['path'] == f'{conventions.gateway.CUSTOM_ROOT}/{directory}'
        assert declaration.inputs['mode'] == persistence.DIRECTORY_MODE
        assert declaration.inputs['owner'] == conventions.gateway.SSH_USER

    assert set(persistence.SKELETON) == {'bin', 'dpkg', 'units'}


def test_no_file_stands_in_for_a_directory_anywhere_under_the_custom_root(monitor: Recorder) -> None:
    """A directory is declared by asking for a directory, and by nothing else.

    A file that stood for one would be a second way of declaring the same thing,
    and a blind one: what the device says about the file is no answer about the
    directory.
    """
    declared = [str(declaration.inputs.get('path', '')) for declaration in monitor.of_type(DEVICE_FILE)]

    assert [path for path in declared if '.skeleton' in path] == []


def test_nothing_is_ever_written_inside_the_offline_package_cache(monitor: Recorder) -> None:
    """The cache is `10-packages.sh`'s alone, directory and contents.

    The script refreshes it by replacing the whole directory in one rename, so a
    file this program wrote in it would be deleted by the next refresh and
    reported as drift on every preview afterwards. The directory itself is still
    declared, and a directory resource says nothing about what is inside it.
    """
    written = [
        declaration.inputs['path']
        for declaration in monitor.of_type(DEVICE_FILE)
        if str(declaration.inputs.get('path', '')).startswith(f'{persistence.DPKG_DIR}/')
    ]

    assert written == []
    assert monitor.inputs_of(f'{NAME}-skeleton-dpkg')['path'] == persistence.DPKG_DIR


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


@pytest.mark.asyncio
async def test_a_file_waits_for_the_directory_it_lands_in(monitor: Recorder, mechanism: DevicePersistence) -> None:
    """Create order is free; delete order is not, and only an edge declares it.

    A destroy deletes in reverse dependency order, and a directory still holding
    files refuses to go — so without this edge the run would fail on a directory
    Pulumi tried to remove before its contents. It is the same edge that keeps a
    file from being written into a directory that is not there yet.
    """
    bin_dir = str(await mechanism.skeleton[persistence.BIN].urn.future())
    unit_dir = str(await mechanism.skeleton[persistence.UNITS].urn.future())

    assert bin_dir in monitor.depends_on(f'{NAME}-bin-{PROGRAM}')
    assert unit_dir in monitor.depends_on(f'{NAME}-unit-{UNIT}')
    assert unit_dir in monitor.depends_on(f'{NAME}-unit-{persistence.UDM_BOOT_UNIT}')


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


def test_a_directory_arriving_is_not_an_event_anything_is_told_about(monitor: Recorder) -> None:
    """The layer that fills a directory is the one that knows what to run.

    Making the directory is the resource's own business, so the mechanism has no
    command to attach: a directory appearing is not an event anything on the
    device waits for. Refusing to remove one somebody filled is the provider's,
    and is asserted there.
    """
    assert monitor.inputs_of(f'{NAME}-skeleton-{DIRECTORY}').get('hook') is None


##
## What the rendered scripts say
##


def test_the_package_set_is_data_and_the_transaction_is_mechanism() -> None:
    """Which packages is the installation's; how they are installed is the script's.

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


##
## Drop-ins: what a unit says that its own file cannot
##


def test_a_drop_in_lands_beside_the_unit_it_amends_and_belongs_to_the_caller(monitor: Recorder) -> None:
    """A statement about a unit this program never writes still has a home.

    The unit being amended may be systemd's own — a machine runs on an instance
    of `systemd-nspawn@.service` — so `unit` has nothing to offer it. The
    drop-in goes under the unit sources in the directory systemd names after
    the unit, and comes back as a resource of the component that asked, exactly
    as every other kind does.
    """
    inputs = monitor.inputs_of(f'{NAME}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')

    assert inputs['path'] == f'{persistence.UNIT_SOURCE_DIR}/{TEMPLATE_UNIT}.d/{DROPIN}'
    assert inputs['mode'] == persistence.FILE_MODE
    assert inputs['owner'] == conventions.gateway.SSH_USER
    assert monitor.options_of(f'{NAME}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}').parent.endswith(f'::{CONSUMER}')


@pytest.mark.asyncio
async def test_a_drop_in_waits_for_the_converger_that_installs_it(
    monitor: Recorder, mechanism: DevicePersistence
) -> None:
    """The same two edges a unit has, and for the same two reasons.

    Its hook is `20-units.sh`, so the script has to be on the device first; and
    it lands under the unit source directory, which a destroy would otherwise
    try to remove while the file was still in it.
    """
    converger = str(await mechanism.units.urn.future())
    unit_dir = str(await mechanism.skeleton[persistence.UNITS].urn.future())
    edges = monitor.depends_on(f'{NAME}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')

    assert converger in edges
    assert unit_dir in edges


def test_a_drop_in_comes_off_the_unit_by_the_delete_that_stops_declaring_it(monitor: Recorder) -> None:
    """The converger only ever walks sources, so the delete cannot go through it.

    A drop-in this program has stopped declaring has no source left to be
    noticed by, so the hook removes the live copy itself and reloads systemd —
    what the unit loses is the statement, not the unit, which is why nothing
    here disables or stops anything. The `<unit>.d` directories go with the last
    drop-in in them, on both sides, because nothing else would ever take them
    away.
    """
    hook = monitor.inputs_of(f'{NAME}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')['hook']
    source = persistence.dropin_source(TEMPLATE_UNIT, DROPIN)
    live = f'{persistence.live_dropin_dir(TEMPLATE_UNIT)}/{DROPIN}'

    assert f'if [ -e {source} ]' in hook
    assert persistence.on_boot_path(persistence.UNITS_SCRIPT) in hook
    assert f'rm -f {live}' in hook
    assert f'rmdir {persistence.live_dropin_dir(TEMPLATE_UNIT)} {persistence.dropin_dir(TEMPLATE_UNIT)}' in hook
    assert 'systemctl daemon-reload' in hook
    assert 'disable' not in hook


def test_a_drop_in_the_device_would_read_by_nothing_is_refused(mechanism: DevicePersistence) -> None:
    """Two ways to land a file nobody opens, and both are refused where they start.

    The converger walks the drop-in directories of one unit kind, and systemd
    opens one file suffix inside such a directory. A source outside either would
    reach the device, run the hook, and mean nothing — with no error anywhere.
    """
    content = '[Service]\nRestart=always\n'

    with pytest.raises(ValueError, match=persistence.UNITS_SCRIPT):
        _ = mechanism.dropin('example.timer', DROPIN, content, opts=pulumi.ResourceOptions())
    with pytest.raises(ValueError, match=persistence.DROPIN_SUFFIX):
        _ = mechanism.dropin(UNIT, '10-example.txt', content, opts=pulumi.ResourceOptions())


##
## What the unit converger does to a device, drop-in by drop-in
##


@final
@dataclass(frozen=True)
class _UnitsRendering:
    """The unit converger's parameters, pointed at a temporary tree."""

    cluster: str
    unit_source_dir: str
    live_unit_dir: str
    unit_suffix: str
    dropin_dir_suffix: str
    dropin_suffix: str


@final
@dataclass(frozen=True)
class _Device:
    """One temporary stand-in for the device the converger runs on."""

    script: Path
    source: Path
    live: Path
    #: Where the `systemctl` stand-in records what it was asked to do.
    commands: Path
    #: What that stand-in records at the moment it is asked to restart a unit:
    #: whether the drop-in below had already been installed by then. It is the
    #: only way to see an ordering from outside, because both halves of the
    #: script leave the same tree behind whichever ran first.
    witness: Path
    watched: Path
    #: What `PATH` must start with for that stand-in to be the one found.
    tools: Path


def _device(tmp_path: Path) -> _Device:
    """The converger rendered against a temporary tree, ready to run.

    Rendered here rather than through `units_script` because the paths that
    function carries are the device's absolute ones; everything else about the
    file — which globs it walks, what it copies, what it removes — is what the
    component ships.
    """
    live = tmp_path / 'live'
    device = _Device(
        script=tmp_path / persistence.UNITS_SCRIPT,
        source=tmp_path / 'source',
        live=live,
        commands=tmp_path / 'commands',
        witness=tmp_path / 'witness',
        watched=live / f'{UNIT}{persistence.DROPIN_DIR_SUFFIX}' / DROPIN,
        tools=tmp_path / 'tools',
    )
    device.source.mkdir()
    device.live.mkdir()
    device.tools.mkdir()

    systemctl = device.tools / 'systemctl'
    _ = systemctl.write_text(
        f'#!/bin/sh\n'
        f'echo "$*" >>{device.commands}\n'
        f'if [ "$1" = restart ]; then\n'
        f'  if [ -e {device.watched} ]; then echo present >>{device.witness}; '
        f'else echo absent >>{device.witness}; fi\n'
        f'fi\n'
        f'exit 0\n'
    )
    systemctl.chmod(0o755)

    _ = device.script.write_text(
        templates.render(
            persistence.TEMPLATE_PACKAGE,
            f'templates/{persistence.UNITS_SCRIPT}.j2',
            _UnitsRendering(
                cluster=conventions.CLUSTER_NAME,
                unit_source_dir=str(device.source),
                live_unit_dir=str(device.live),
                unit_suffix=persistence.UNIT_SUFFIX,
                dropin_dir_suffix=persistence.DROPIN_DIR_SUFFIX,
                dropin_suffix=persistence.DROPIN_SUFFIX,
            ),
        )
    )
    return device


def _converge(device: _Device) -> tuple[int, list[str]]:
    """Run the converger once, and read back what it asked of systemd."""
    device.commands.unlink(missing_ok=True)
    completed = subprocess.run(  # noqa: S603 -- a rendered script of this repository's own
        ['/bin/bash', str(device.script)],  # noqa: S607 -- the shell the device's own scripts name
        env={'PATH': f'{device.tools}:/usr/bin:/bin'},
        capture_output=True,
        check=False,
    )
    recorded = device.commands.read_text().split('\n') if device.commands.exists() else []
    return completed.returncode, [line for line in recorded if line]


def _dropin(root: Path, unit: str, name: str, content: str) -> Path:
    """One drop-in in a tree, source side or live side."""
    directory = root / f'{unit}{persistence.DROPIN_DIR_SUFFIX}'
    directory.mkdir(exist_ok=True)
    written = directory / name
    _ = written.write_text(content)
    return written


def test_the_converger_installs_a_drop_in_and_reloads_for_it(tmp_path: Path) -> None:
    """The device is given the statement, and systemd is told to read it again.

    Nothing is enabled or started: a drop-in directory is not a unit, and the
    unit it names is one this program may never have written. What makes the
    statement apply to the unit as it stands is the reload, and one covers
    however many drop-ins moved.
    """
    device = _device(tmp_path)
    _ = _dropin(device.source, TEMPLATE_UNIT, DROPIN, '[Service]\nRestart=always\n')

    status, commands = _converge(device)

    assert status == 0
    assert (device.live / f'{TEMPLATE_UNIT}.d' / DROPIN).read_text() == '[Service]\nRestart=always\n'
    assert commands == ['daemon-reload']


def test_a_drop_in_nothing_declares_is_removed_from_the_device(tmp_path: Path) -> None:
    """Inside a drop-in directory this program has a source for, everything is its.

    So the mirror removes, unlike the one over the unit files — a live drop-in
    with no source is one this program stopped declaring on a device it did not
    push the removal to, which is every recovery boot after a firmware update.
    """
    device = _device(tmp_path)
    _ = _dropin(device.source, TEMPLATE_UNIT, DROPIN, '[Service]\nRestart=always\n')
    stale = _dropin(device.live, TEMPLATE_UNIT, '20-stale.conf', '[Service]\nRestart=no\n')

    status, commands = _converge(device)

    assert status == 0
    assert not stale.exists()
    assert (device.live / f'{TEMPLATE_UNIT}.d' / DROPIN).exists()
    assert commands == ['daemon-reload']


def test_a_drop_in_directory_this_program_has_no_source_for_is_left_alone(tmp_path: Path) -> None:
    """The unit store holds units that are not this program's, and so do their drop-ins.

    The firmware is free to amend its own units, and the mirror reaches only
    into the directories this program has a source directory for.
    """
    device = _device(tmp_path)
    somebody_else = _dropin(device.live, 'firmware.service', '10-vendor.conf', '[Service]\nNice=5\n')

    status, commands = _converge(device)

    assert status == 0
    assert somebody_else.exists()
    assert commands == []


def test_a_unit_is_restarted_onto_the_drop_ins_it_is_meant_to_have(tmp_path: Path) -> None:
    """Which half of the script runs first is the whole of this claim.

    A unit whose file changed is restarted, and a restart is what makes a
    statement that only applies at start take effect. Converging the drop-ins
    afterwards would restart the unit onto the configuration it had before,
    leaving the new statement waiting for a restart nothing else is going to
    do — so the stand-in is asked, at the moment of the restart, whether the
    drop-in is already on the device.
    """
    device = _device(tmp_path)
    _ = (device.source / UNIT).write_text('[Service]\nExecStart=/bin/true\n')
    _ = (device.live / UNIT).write_text('[Service]\nExecStart=/bin/false\n')
    _ = _dropin(device.source, UNIT, DROPIN, '[Service]\nRestart=always\n')

    status, commands = _converge(device)

    assert status == 0
    assert f'restart {UNIT}' in commands
    assert device.witness.read_text().split() == ['present']


def test_a_run_that_moved_no_drop_in_succeeds(tmp_path: Path) -> None:
    """A run that had nothing to do is not a failure, and the push reads the status.

    Every file of a machine runs this script as its hook and carries the exit
    status out to the apply, and the boot where nothing changed is the common
    case — here with no unit source to walk afterwards, which makes the
    drop-ins' own reload the last decision the script makes.
    """
    device = _device(tmp_path)
    _ = _dropin(device.source, TEMPLATE_UNIT, DROPIN, '[Service]\nRestart=always\n')
    _ = _dropin(device.live, TEMPLATE_UNIT, DROPIN, '[Service]\nRestart=always\n')

    status, commands = _converge(device)

    assert status == 0
    assert commands == []
