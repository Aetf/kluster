"""The gateway's persistence mechanism, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: which file lands where and with which mode, which command runs after
a write and after a delete, whose component a file asked for through the layer
belongs to, and what the two rendered boot-chain scripts say once the data is in
them.

**Both convergers are also run**, against temporary trees with stand-ins for
what they call — the shared `systemctl` for the unit converger, a `dpkg` and an
`apt-get` for the package converger — because what matters most about each is
a property of the sequence rather than of any line the file contains: a drop-in
this program no longer declares has to leave the device and one belonging to
somebody else has to stay, a unit that is not enabled or not running is made
so, and a device missing any one package is given the whole set.

The renderers are plain functions, so most cases read their output directly; the
component tree is declared once against mocks, which is where a wiring mistake
would surface.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pytest
import pytest_asyncio
from device_places import DEVICE_DIRECTORY, DEVICE_FILE, PLACES
from fake_systemd import INSTALLABLE, FakeSystemd
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import persistence
from kluster.components.gateway.persistence import DevicePersistence
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceDirectory, DeviceFile
from putils import Component

NAME = 'mechanism'
CONSUMER = 'consumer'
NEIGHBORS = 'neighbors'
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


class Neighbors(Component, pulumi_type='test:gateway:Neighbors'):
    """Two executables whose names differ only after the dot."""

    def __init__(self, name: str, *, mechanism: DevicePersistence, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.shell: DeviceFile = mechanism.executable('example.sh', '#!/bin/sh\nexit 0\n', opts=self.child_opts())
        self.python: DeviceFile = mechanism.executable('example.py', 'raise SystemExit(0)\n', opts=self.child_opts())
        self.register_outputs({})


#: An executable two components of different types both ask for.
CONTESTED = 'contested.sh'


class Claimant(Component, pulumi_type='test:gateway:Claimant'):
    """A component asking for `CONTESTED`, of one type."""

    def __init__(self, name: str, *, mechanism: DevicePersistence, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.program: DeviceFile = mechanism.executable(CONTESTED, '#!/bin/sh\nexit 0\n', opts=self.child_opts())
        self.register_outputs({})


class OtherClaimant(Component, pulumi_type='test:gateway:OtherClaimant'):
    """A component asking for `CONTESTED`, of another type."""

    def __init__(self, name: str, *, mechanism: DevicePersistence, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.program: DeviceFile = mechanism.executable(CONTESTED, '#!/bin/sh\nexit 0\n', opts=self.child_opts())
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
    for name in (f'{CONSUMER}-on-boot-{SCRIPT}', f'{CONSUMER}-bin-{PROGRAM}', f'{CONSUMER}-unit-{UNIT}'):
        assert monitor.options_of(name).parent.endswith(f'::{CONSUMER}'), name

    assert monitor.options_of(f'{CONSUMER}-skeleton-{DIRECTORY}').parent.endswith(f'::{CONSUMER}')
    assert monitor.options_of(f'{NAME}-on-boot-{persistence.UNITS_SCRIPT}').parent.endswith(f'::{NAME}')


def test_an_executable_is_delivered_and_nothing_is_told_about_it(monitor: Recorder) -> None:
    """A program in `bin/` is run by a unit, so installing one is not an event.

    Only the named file is managed: an executable placed on the device by hand
    beside it is a neighbor this program never looks at, which is what makes
    the directory shared rather than owned.
    """
    inputs = monitor.inputs_of(f'{CONSUMER}-bin-{PROGRAM}')

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
        _ = Neighbors(NEIGHBORS, mechanism=mechanism)

    shell = monitor.inputs_of(f'{NEIGHBORS}-bin-example.sh')
    python = monitor.inputs_of(f'{NEIGHBORS}-bin-example.py')

    assert shell['path'] == f'{persistence.BIN_DIR}/example.sh'
    assert python['path'] == f'{persistence.BIN_DIR}/example.py'


def test_a_file_is_named_for_the_component_that_asked(monitor: Recorder) -> None:
    """The name is read off the parent the URN places the file under, so the two agree by construction.

    A child's logical name carries its component's (style/pulumi.md), and the
    component a file asked for through the mechanism belongs to is the asker
    -- so the asker's name is what it carries, with the kind and the file's
    whole name after it, and the mechanism's own files carry the mechanism's.
    Nothing here is named for the mechanism on the asker's behalf.
    """
    for kind, file in (('on-boot', SCRIPT), ('bin', PROGRAM), ('unit', UNIT), ('skeleton', DIRECTORY)):
        assert monitor.options_of(f'{CONSUMER}-{kind}-{file}').parent.endswith(f'::{CONSUMER}')
        assert f'{NAME}-{kind}-{file}' not in monitor.names_declared


@pytest.mark.asyncio
async def test_two_components_asking_for_one_path_are_listed_by_the_path_census(
    monitor: Recorder, mechanism: DevicePersistence
) -> None:
    """Two askers of different types are two URNs at one place, and the run is accepted.

    A URN qualifies a name by parent types, and each of these files is named
    for its own asker besides, so nothing about the registration collides: the
    engine would `create` both, the second write would overwrite the first,
    and a delete of either would take the file from under the other. The
    census over the declared places is what says so, naming the path and both
    claimants.
    """
    async with declaring():
        first = Claimant('first', mechanism=mechanism)
        second = OtherClaimant('second', mechanism=mechanism)

    contested = persistence.executable_path(CONTESTED)
    listed = monitor.places_claimed_more_than_once(PLACES)

    assert set(listed) == {contested}
    assert listed[contested] == sorted([str(await first.program.urn.future()), str(await second.program.urn.future())])


@pytest.mark.asyncio
async def test_an_asker_that_is_not_a_component_is_refused_before_anything_is_registered(
    monitor: Recorder, mechanism: DevicePersistence
) -> None:
    """The name comes from the parent, so an `opts` with no component to read it off is refused by the rule.

    Both shapes: no parent at all, and a custom resource as the parent -- a
    file parented on another file has a URN under a type and no component's
    name to carry. The refusal is the rule's, by name, and it is before the
    registration: the run has no such file afterwards.
    """
    before = set(monitor.registrations)
    for opts in (pulumi.ResourceOptions(), pulumi.ResourceOptions(parent=mechanism.units)):
        with pytest.raises(ValueError, match='named for the component the URN places it under'):
            _ = mechanism.executable('orphan.sh', '#!/bin/sh\nexit 0\n', opts=opts)

    async with declaring():
        pass
    assert set(monitor.registrations) == before


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
    hook = monitor.inputs_of(f'{CONSUMER}-on-boot-{SCRIPT}')['hook']

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

    assert converger in monitor.depends_on(f'{CONSUMER}-unit-{UNIT}')
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

    assert bin_dir in monitor.depends_on(f'{CONSUMER}-bin-{PROGRAM}')
    assert unit_dir in monitor.depends_on(f'{CONSUMER}-unit-{UNIT}')
    assert unit_dir in monitor.depends_on(f'{NAME}-unit-{persistence.UDM_BOOT_UNIT}')


def test_a_unit_is_retired_by_the_delete_that_stops_declaring_it(monitor: Recorder) -> None:
    """One command, two meanings, and the device says which one applies.

    A hook runs after a write and after a delete alike, so it asks whether the
    source is still there. Gone means this program stopped declaring the unit,
    and the same session disables it and removes the live copy — otherwise a
    retired unit would keep running until somebody found it on the device.
    """
    hook = monitor.inputs_of(f'{CONSUMER}-unit-{UNIT}')['hook']
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
    assert monitor.inputs_of(f'{CONSUMER}-skeleton-{DIRECTORY}').get('hook') is None


##
## What the rendered scripts say, and what the package converger does
##


#: The dpkg database both stand-ins read and write, in dpkg's own format — a
#: `Package:` stanza per package dpkg holds a record of, with its `Status:` and
#: `Version:` —
#: which is the file the converger saves as the firmware base. A record is not
#: an installation: `install ok unpacked` and `install ok half-configured` are
#: what a dpkg run stopped part-way leaves, and `deinstall ok config-files` is
#: what a removal that was not a purge leaves.
DATABASE_SH = """
state() {{
    awk -v p="Package: $1" 'BEGIN {{ RS = "" }} {{ split($0, l, "\\n") }} l[1] == p {{ sub(/^Status: /, "", l[2]); print l[2] }}' "$2"
}}
put() {{
    awk -v p="Package: $1" 'BEGIN {{ RS = ""; ORS = "\\n\\n" }} {{ split($0, l, "\\n") }} l[1] != p' "$3" >"$3.new"
    printf 'Package: %s\\nStatus: %s\\nVersion: %s\\n\\n' "$1" "$2" "$4" >>"$3.new"
    mv "$3.new" "$3"
}}
"""

#: Debian's version order, as dpkg's `verrevcmp` defines it: an epoch first,
#: then the upstream version and the revision, each compared as alternating
#: runs of non-digits — where `~` sorts before everything, even the end of the
#: string, and letters before other characters — and of digits, compared as
#: numbers. Run as `<a> <op> <b>` with dpkg's operators; the exit status is
#: the answer.
COMPARE_VERSIONS = """
import sys


def order(c):
    if c == '' or c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == '~':
        return -1
    return ord(c) + 256


def verrevcmp(a, b):
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = order(a[i] if i < len(a) else '')
            bc = order(b[j] if j < len(b) else '')
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == '0':
            i += 1
        while j < len(b) and b[j] == '0':
            j += 1
        first = 0
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            first = first or ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first:
            return first
    return 0


def parse(version):
    epoch, _, rest = version.partition(':') if ':' in version else ('0', '', version)
    upstream, _, revision = rest.rpartition('-') if '-' in rest else (rest, '', '')
    return int(epoch), upstream, revision


def compare(a, b):
    (ea, ua, ra), (eb, ub, rb) = parse(a), parse(b)
    return (ea > eb) - (ea < eb) or verrevcmp(ua, ub) or verrevcmp(ra, rb)


a, op, b = sys.argv[1:]
result = compare(a, b)
holds = {'lt': result < 0, 'le': result <= 0, 'eq': result == 0, 'ne': result != 0, 'ge': result >= 0, 'gt': result > 0}
sys.exit(0 if holds[op] else 1)
"""

#: `dpkg` as the package converger asks it. `-s` prints the record of a package
#: and exits 0 whenever there is one, whatever its status — as `dpkg` does,
#: so a converger that trusted the exit status would take a removed package for
#: an installed one — and refuses a package it holds no record of.
#:
#: `--compare-versions <a> <op> <b>` compares two versions in Debian's order
#: (`COMPARE_VERSIONS`).
#:
#: `-i` unpacks every deb it is handed, named for its package the way a deb's
#: file is, refusing an archive that is not there, and then configures what it
#: unpacked, in dpkg 1.20's words throughout. A deb older than a package dpkg
#: holds installed, unpacked or half-configured downgrades it, or, under
#: `--refuse-downgrade`, is skipped and leaves the exit status alone. Under
#: `--skip-same-version`, a deb at the version of such a package is skipped and
#: not configured.
#: Two dependencies of `DEPENDENT` are checked when it is configured: `PINNED`
#: at `DEPENDENT`'s own version, wherever the database holds `PINNED` —
#: `systemd-container` pins `systemd` exactly — and `DEPENDENCY` installed. A
#: failed check leaves `DEPENDENT` unpacked and dpkg exiting 1, with the reason
#: dpkg gives. `postinst-fails` names a package and a version at which its
#: maintainer script fails when it is configured, which leaves it
#: half-configured. Every call is recorded, beside `apt-get`'s.
DPKG = (
    """#!/bin/sh
echo "dpkg $*" >>{calls}
"""
    + DATABASE_SH
    + """
held() {{
    awk -v p="Package: $1" 'BEGIN {{ RS = "" }} {{ split($0, l, "\\n") }} l[1] == p {{ sub(/^Version: /, "", l[3]); print l[3] }}' "$2"
}}
newer() {{
    {python} {compare} "$1" gt "$2"
}}
case "$1" in
    --compare-versions)
        exec {python} {compare} "$2" "$3" "$4"
        ;;
    -s)
        if [ -n "$(state "$2" {status})" ]; then
            awk -v p="Package: $2" 'BEGIN {{ RS = "" }} {{ split($0, l, "\\n") }} l[1] == p' {status}
            exit 0
        fi
        echo "dpkg-query: package '$2' is not installed and no information is available" >&2
        exit 1
        ;;
    -i)
        shift
        refuse=0
        skip=0
        while :; do
            case "$1" in
                --refuse-downgrade) refuse=1 ;;
                --skip-same-version) skip=1 ;;
                *) break ;;
            esac
            shift
        done
        unpacked=
        for deb in "$@"; do
            [ -f "$deb" ] || {{ echo "dpkg: error: cannot access archive '$deb': No such file or directory" >&2; exit 1; }}
            name=${{deb##*/}}
            package=${{name%%_*}}
            version=${{name#*_}}
            version=${{version%_*}}
            s=$(state "$package" {status})
            installed=$(held "$package" {status})
            case "$s" in
                'install ok installed' | 'install ok unpacked' | 'install ok half-configured') ;;
                *) installed= ;;
            esac
            if [ -n "$installed" ] && newer "$installed" "$version"; then
                if [ "$refuse" -eq 1 ]; then
                    echo "dpkg: will not downgrade $package from $installed to $version, skipping" >&2
                    continue
                fi
                echo "dpkg: warning: downgrading $package from $installed to $version" >&2
            fi
            case "$s" in
                'install ok installed' | 'install ok unpacked' | 'install ok half-configured')
                    if [ "$skip" -eq 1 ] && [ "$installed" = "$version" ]; then
                        echo "dpkg: version $version of $package already installed, skipping" >&2
                        continue
                    fi
                    ;;
            esac
            put "$package" 'install ok unpacked' {status} "$version"
            unpacked="$unpacked $package"
        done
        failed=0
        for package in $unpacked; do
            [ "$package" != {dependent} ] || continue
            if [ "$package $(held "$package" {status})" = "$(cat {postinst_fails} 2>/dev/null)" ]; then
                put "$package" 'install ok half-configured' {status} "$(held "$package" {status})"
                printf 'dpkg: error processing package %s (--install):\\n installed %s package post-installation script subprocess returned error exit status 1\\n' \\
                    "$package" "$package" >&2
                failed=1
                continue
            fi
            put "$package" 'install ok installed' {status} "$(held "$package" {status})"
        done
        for package in $unpacked; do
            [ "$package" = {dependent} ] || continue
            version=$(held "$package" {status})
            pinned=$(held {pinned} {status})
            dependency=$(state {dependency} {status})
            reasons=
            if [ -n "$pinned" ] && [ "$pinned" != "$version" ]; then
                reasons="$reasons $package depends on {pinned} (= $version); however:\\n  Version of {pinned} on system is $pinned.\\n"
            fi
            if [ -z "$dependency" ]; then
                reasons="$reasons $package depends on {dependency}; however:\\n  Package {dependency} is not installed.\\n"
            elif [ "$dependency" != 'install ok installed' ]; then
                reasons="$reasons $package depends on {dependency}; however:\\n  Package {dependency} is not configured yet.\\n"
            fi
            if [ -z "$reasons" ]; then
                put "$package" 'install ok installed' {status} "$version"
                continue
            fi
            printf "dpkg: dependency problems prevent configuration of %s:\\n$reasons\\n" "$package" >&2
            printf 'dpkg: error processing package %s (--install):\\n dependency problems - leaving unconfigured\\n' "$package" >&2
            failed=1
        done
        exit $failed
        ;;
esac
echo "fake dpkg: $1 is not modeled" >&2
exit 2
"""
)

#: `apt-get` as the package converger asks it. Online, `install` resolves the
#: packages it is named against a dpkg database — the live one, or the file `-o
#: Dir::State::status=` names — and downloads a deb of each package that
#: database lacks, dependencies included, into the archives directory. A
#: package with an `unpacked` or `half-configured` record is one apt counts as
#: present: it configures it rather than fetching it. Without
#: `--download-only` it also installs what it resolved into that database,
#: leaving a package already installed at the version it has;
#: with `--reinstall` it downloads the packages it is named even where they are
#: installed, and not what they depend on (`TryToInstall::operator()`).
#: Offline — the boot after a firmware update with no network, its lists empty
#: and unfetchable — nothing it is asked for can be found, and `update` warns
#: and exits 0, as apt 2.2.4 does. `refresh-fails` is a
#: download-only run failing part-way on a boot the install itself worked: it
#: fetches the packages it was named and none of what they depend on, and exits
#: the way apt does when some of its downloads failed. `power-loss` is the power
#: going while dpkg unpacks an install: every dependency it resolved is left
#: `unpacked`, none of the packages it was named is, and the converger running
#: it is killed outright. `download` fetches each `<package>=<version>` it is
#: named into the current directory, as apt does, whatever the archive's
#: candidate, unless its own flag makes it fail. `candidate`, when present,
#: holds the version the archive offers, which `install` fetches and records.
#: When `postinst-fails` names a package it installed at that version, the
#: install's configuration of it fails: it is left half-configured — and
#: `DEPENDENT` unpacked, when it is `DEPENDENCY` — and apt exits the way it does
#: when dpkg returned an error.
#:
#: apt's own terms hold, as apt 2.2.4 states them: the archives directory is
#: its shared one unless `-o Dir::Cache::archives=` names another, it makes
#: `partial/` and its `lock` inside, and it refuses a directory that is not
#: there (`pkgAcquire::GetLock`); a status file that is not there is refused
#: too, in the words apt prints.
APT_GET = (
    """#!/bin/sh
echo "apt-get $*" >>{calls}
"""
    + DATABASE_SH
    + """
archives={archives}
database={status}
command=
download_only=0
reinstall=0
packages=
value=0
for word in "$@"; do
    if [ "$value" -eq 1 ]; then
        case "$word" in
            Dir::Cache::archives=*) archives=${{word#Dir::Cache::archives=}} ;;
            Dir::State::status=*) database=${{word#Dir::State::status=}} ;;
        esac
        value=0
        continue
    fi
    case "$word" in
        -o) value=1 ;;
        --download-only) download_only=1 ;;
        --reinstall) reinstall=1 ;;
        -*) ;;
        *) if [ -z "$command" ]; then command=$word; else packages="$packages $word"; fi ;;
    esac
done
if [ -e {offline} ]; then
    if [ "$command" = update ]; then
        echo "W: Failed to fetch the archive's indexes" >&2
        exit 0
    fi
    for package in $packages; do
        echo "E: Unable to locate package $package" >&2
    done
    exit 100
fi
[ "$command" = update ] && exit 0
candidate=$(cat {candidate} 2>/dev/null || echo 1.0)
if [ "$command" = download ]; then
    if [ -e {download_fails} ]; then
        echo "E: Failed to fetch the archives" >&2
        exit 100
    fi
    for wanted in $packages; do
        : >"${{wanted%%=*}}_${{wanted#*=}}_arm64.deb"
    done
    exit 0
fi
if [ ! -f "$database" ]; then
    echo "E: The package lists or status file could not be parsed or opened." >&2
    exit 100
fi
if [ ! -d "$archives" ]; then
    echo "E: Archives directory $archives/partial is missing. - Acquire (2: No such file or directory)" >&2
    exit 100
fi
mkdir -p "$archives/partial"
: >"$archives/lock"
failing=0
[ "$download_only" -eq 1 ] && [ -e {refresh_fails} ] && failing=1
power_loss=0
[ "$download_only" -eq 0 ] && [ -e {power_loss} ] && power_loss=1
for package in $packages; do
    wanted=$package
    [ "$package" != {dependent} ] || [ "$failing" -eq 1 ] || wanted="$package {dependencies}"
    for one in $wanted; do
        s=$(state "$one" "$database")
        case "$s" in
            'install ok installed') present=installed ;;
            'install ok unpacked' | 'install ok half-configured') present=unpacked ;;
            *) present= ;;
        esac
        if [ -z "$present" ] || {{ [ "$reinstall" -eq 1 ] && [ "$one" = "$package" ]; }}; then
            : >"$archives/${{one}}_${{candidate}}_arm64.deb"
        fi
        [ "$download_only" -eq 0 ] || continue
        if [ "$power_loss" -eq 1 ]; then
            [ "$one" = "$package" ] || [ "$present" = installed ] || put "$one" 'install ok unpacked' "$database" "$candidate"
        elif [ "$present" != installed ]; then
            put "$one" 'install ok installed' "$database" "$candidate"
        fi
    done
done
if [ "$power_loss" -eq 1 ]; then
    kill -KILL "$PPID"
    exit 137
fi
if [ "$download_only" -eq 0 ] && [ -s {postinst_fails} ]; then
    read -r broken broken_version <{postinst_fails}
    if [ "$broken_version" = "$candidate" ] && [ "$(state "$broken" "$database")" = 'install ok installed' ]; then
        put "$broken" 'install ok half-configured' "$database" "$candidate"
        if [ "$broken" = {dependency} ] && [ -n "$(state {dependent} "$database")" ]; then
            put {dependent} 'install ok unpacked' "$database" "$candidate"
        fi
        echo "E: Sub-process /usr/bin/dpkg returned an error code (1)" >&2
        exit 100
    fi
fi
if [ "$failing" -eq 1 ]; then
    echo "E: Some files failed to download" >&2
    exit 100
fi
exit 0
"""
)

#: The dependencies the stand-in `apt-get` knows, both of one package of the
#: set: one the firmware does not ship, so apt fetches it beside that package
#: whenever the database it resolves against lacks it, and one the firmware
#: does ship, which the cache therefore never holds.
DEPENDENT = 'systemd-container'
DEPENDENCY = 'libcurl3-gnutls'
FIRMWARE = 'libc6'

#: The package `DEPENDENT` pins to its own version, which a firmware can ship
#: at a newer one than the cache holds.
PINNED = 'systemd'

#: The statuses a record can carry that are not an installation, in dpkg's words.
UNPACKED = 'install ok unpacked'
CONFIG_FILES = 'deinstall ok config-files'


@final
@dataclass(frozen=True)
class _Packages:
    """A device the package converger can be run against, online or not."""

    script: Path
    #: The offline cache, in place of the device's own directory, and the
    #: directory beside it the converger has apt download into.
    cache: Path
    download: Path
    #: The directory the firmware base is saved in, the database it is, and the
    #: release it was saved under.
    base: Path
    base_status: Path
    base_release: Path
    #: apt's shared archives, where any other apt run leaves the debs it
    #: fetched, and which the converger never reads.
    archives: Path
    #: The live dpkg database — the firmware's package, and what is installed —
    #: and the running firmware's release.
    status: Path
    release: Path
    calls: Path
    #: The flags that make apt unreachable, make a download-only run fail, make
    #: `apt-get download` fail, cut the power during an install, and name the
    #: package and version whose maintainer script fails.
    offline: Path
    refresh_fails: Path
    download_fails: Path
    power_loss: Path
    postinst_fails: Path
    #: The version the archive offers, 1.0 while the file is absent.
    candidate: Path
    tools: Path


def deb(directory: Path, package: str, version: str = '1.0') -> Path:
    """The file a package's deb is, in the form apt and both stand-ins name it."""
    return directory / f'{package}_{version}_arm64.deb'


def _database(*packages: str, records: dict[str, str] | None = None, versions: dict[str, str] | None = None) -> str:
    """A dpkg database with `packages` installed and `records` in the statuses they name, as dpkg writes it.

    Every package is at version 1.0 unless `versions` names another.
    """
    stanzas = dict.fromkeys(packages, 'install ok installed') | (records or {})
    return ''.join(
        f'Package: {package}\nStatus: {status}\nVersion: {(versions or {}).get(package, "1.0")}\n\n'
        for package, status in stanzas.items()
    )


def _listed(database: Path) -> set[str]:
    """The packages a dpkg database holds as installed."""
    lines = database.read_text().splitlines()
    return {
        package.removeprefix('Package: ')
        for package, status in itertools.pairwise(lines)
        if package.startswith('Package: ') and status == 'Status: install ok installed'
    }


def _packages(
    tmp_path: Path,
    *,
    installed: tuple[str, ...],
    cached: tuple[str, ...],
    base: tuple[str, ...] | None = None,
    records: dict[str, str] | None = None,
    versions: dict[str, str] | None = None,
) -> _Packages:
    """The package converger, rendered by the production function against a temporary tree.

    The package set, its sorting and quoting, and every step are what the
    device runs; only the cache — and so the download directory beside it — the
    saved base, the live database and the release file are this tree's. The
    live database holds the firmware's package beside `installed`, and
    `records` in the statuses they name, each at 1.0 unless `versions` names
    another version; `base`, when given, is a firmware base
    already saved, holding exactly the packages it names, from a release other
    than the running one.
    """
    device = _Packages(
        script=tmp_path / persistence.PACKAGES_SCRIPT,
        cache=tmp_path / 'cache',
        download=tmp_path / 'cache.download',
        base=tmp_path / 'base',
        base_status=tmp_path / 'base' / 'status',
        base_release=tmp_path / 'base' / 'release',
        archives=tmp_path / 'archives',
        status=tmp_path / 'status',
        release=tmp_path / 'version',
        calls=tmp_path / 'calls',
        offline=tmp_path / 'offline',
        refresh_fails=tmp_path / 'refresh-fails',
        download_fails=tmp_path / 'download-fails',
        power_loss=tmp_path / 'power-loss',
        postinst_fails=tmp_path / 'postinst-fails',
        candidate=tmp_path / 'candidate',
        tools=tmp_path / 'tools',
    )
    for directory in (device.cache, device.archives, device.tools):
        directory.mkdir()
    _ = device.status.write_text(_database(FIRMWARE, *installed, records=records, versions=versions))
    _ = device.release.write_text('fw-1\n')
    if base is not None:
        device.base.mkdir()
        _ = device.base_status.write_text(_database(*base))
        _ = device.base_release.write_text('fw-0\n')
    for package in cached:
        _ = deb(device.cache, package).write_text('')
    compare = device.tools / 'compare-versions.py'
    _ = compare.write_text(COMPARE_VERSIONS)
    for name, stub in (('dpkg', DPKG), ('apt-get', APT_GET)):
        tool = device.tools / name
        _ = tool.write_text(
            stub.format(
                calls=device.calls,
                status=device.status,
                archives=device.archives,
                offline=device.offline,
                refresh_fails=device.refresh_fails,
                download_fails=device.download_fails,
                power_loss=device.power_loss,
                postinst_fails=device.postinst_fails,
                candidate=device.candidate,
                dependent=DEPENDENT,
                dependency=DEPENDENCY,
                pinned=PINNED,
                dependencies=f'{DEPENDENCY} {FIRMWARE}',
                python=sys.executable,
                compare=compare,
            )
        )
        tool.chmod(0o755)
    _ = device.script.write_text(
        persistence.packages_script(
            PACKAGES,
            cache=str(device.cache),
            base=str(device.base),
            status=str(device.status),
            release=str(device.release),
        )
    )
    return device


def _update_firmware(device: _Packages, shipping: dict[str, str] | None = None) -> None:
    """A firmware update: a new release, and a live database of the firmware's package with nothing of the set.

    `shipping` names further packages the new firmware ships, each at the version it names.
    """
    _ = device.status.write_text(_database(FIRMWARE, *(shipping or {}), versions=shipping))
    _ = device.release.write_text(f'{device.release.read_text().strip()}+1\n')


@final
@dataclass(frozen=True)
class _Run:
    """One run of the package converger: its exit, what it asked of apt and dpkg, and what it said."""

    status: int
    calls: list[str]
    output: str


def _install(
    device: _Packages,
    *,
    online: bool,
    refresh_fails: bool = False,
    download_fails: bool = False,
    power_loss: bool = False,
    postinst_fails: tuple[str, str] | None = None,
) -> _Run:
    """Run the package converger once, and read back what it asked of apt and dpkg."""
    for flag, raised in (
        (device.offline, not online),
        (device.refresh_fails, refresh_fails),
        (device.download_fails, download_fails),
        (device.power_loss, power_loss),
    ):
        if raised:
            _ = flag.write_text('')
        else:
            flag.unlink(missing_ok=True)
    if postinst_fails is None:
        device.postinst_fails.unlink(missing_ok=True)
    else:
        _ = device.postinst_fails.write_text(' '.join(postinst_fails))
    device.calls.unlink(missing_ok=True)
    completed = subprocess.run(
        ['/bin/bash', str(device.script)],
        env={'PATH': f'{device.tools}:/usr/bin:/bin'},
        capture_output=True,
        text=True,
        check=False,
    )
    calls = device.calls.read_text().splitlines() if device.calls.exists() else []
    return _Run(
        status=completed.returncode,
        calls=[call for call in calls if not call.startswith(('dpkg -s ', 'dpkg --compare-versions '))],
        output=completed.stdout + completed.stderr,
    )


def _apt(device: _Packages, *words: str) -> str:
    """One `apt-get` call as the converger makes it, downloading into its own directory."""
    return ' '.join(('apt-get', '-o', f'Dir::Cache::archives={device.download}', *words))


def _refresh(device: _Packages) -> str:
    """The download a refresh makes: the set, resolved against the saved base."""
    return _apt(
        device, '-o', f'Dir::State::status={device.base_status}', 'install', '-y', '--download-only', *sorted(PACKAGES)
    )


def _names_in(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def _debs(directory: Path, *packages: str) -> set[str]:
    """The names the debs of `packages` have in `directory`."""
    return {deb(directory, package).name for package in packages}


def _offline_install(device: _Packages, *packages: str) -> str:
    """The one `dpkg -i` an offline boot makes over a cache holding `packages`, in the order the glob gives."""
    return f'dpkg -i {" ".join(str(device.cache / name) for name in sorted(_debs(device.cache, *packages)))}'


def _dpkg_installs(run: _Run) -> list[str]:
    return [call for call in run.calls if call.startswith('dpkg -i ')]


def test_a_device_that_kept_every_package_is_asked_to_install_nothing(tmp_path: Path) -> None:
    """Every boot runs this, and on all but the first after an update there is nothing to do."""
    device = _packages(tmp_path, installed=PACKAGES, cached=())

    run = _install(device, online=True)

    assert run.status == 0
    assert run.calls == []


def test_a_package_removed_but_not_purged_is_installed_again(tmp_path: Path) -> None:
    """dpkg answers for a package it holds any record of, installed or not.

    A package removed without a purge leaves its configuration files and a
    record that says so, and `dpkg -s` prints that record and exits 0. A
    converger that took the exit status for an installation would call such a
    device done and install nothing.
    """
    kept = tuple(package for package in PACKAGES if package != DEPENDENT)
    device = _packages(tmp_path, installed=(*kept, DEPENDENCY), cached=(), records={DEPENDENT: CONFIG_FILES})

    run = _install(device, online=True)

    assert run.status == 0
    assert _apt(device, 'install', '-y', *sorted(PACKAGES)) in run.calls
    assert DEPENDENT in _listed(device.status)


def test_one_package_missing_is_the_whole_set_installed_in_one_transaction(tmp_path: Path) -> None:
    """Which packages is the installation's; how they are installed is the script's.

    A firmware update can take some of the set and leave the rest, and a
    converger that stopped at the first package it found would call that
    device done. The whole set goes to apt in one transaction, because
    packages version-locked to each other cannot be resolved one at a time;
    on the boot where apt cannot be reached, the offline cache is installed in
    one `dpkg` call for the same reason, and the device ends up with the set.
    """
    kept, lost = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(kept, DEPENDENCY), cached=PACKAGES)

    run = _install(device, online=False)

    assert run.status == 0
    assert run.calls == [
        'apt-get update',
        _apt(device, 'install', '-y', *sorted(PACKAGES)),
        _offline_install(device, *PACKAGES),
    ]
    assert _listed(device.status) == {FIRMWARE, kept, lost, DEPENDENCY}


def test_a_boot_with_neither_apt_nor_a_cache_fails_rather_than_reporting_the_set_installed(tmp_path: Path) -> None:
    """The one boot this script exists for is the one it cannot always win.

    With apt unreachable and no cache to fall back on, the package is still
    missing — and a script that exited successfully over that would leave the
    push, and the operator reading the boot log, believing the device whole.
    """
    kept, _ = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(kept,), cached=())

    run = _install(device, online=False)

    assert run.status == 1
    assert _listed(device.status) == {FIRMWARE, kept}


def test_an_offline_boot_whose_cache_lacks_part_of_the_set_fails(tmp_path: Path) -> None:
    """A cache from before the set last grew installs cleanly and still leaves a package missing.

    That is the cache a device without a saved base keeps, because a refresh
    there cannot say what the firmware lacks. dpkg succeeds over everything it
    was handed, so what decides the boot is the set installed afterwards.
    """
    kept, lost = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(), cached=(kept,))

    run = _install(device, online=False)

    assert run.status == 1
    assert _dpkg_installs(run) == [_offline_install(device, kept)]
    assert lost not in _listed(device.status)


def test_a_post_update_boot_records_the_firmware_base_before_installing_anything(tmp_path: Path) -> None:
    """The boot that finds none of the set installed is the one whose database is the firmware's.

    A firmware update wipes every package of the set with the dpkg records of
    them, so on the boot after it the live database is what the firmware
    shipped and nothing else — the base every refresh until the next update is
    resolved against. It is saved before apt installs anything, under the
    running release, and it replaces the base an earlier firmware left.
    """
    device = _packages(tmp_path, installed=(), cached=(), base=('from-an-older-firmware',))
    firmware = device.status.read_text()

    run = _install(device, online=True)

    assert run.status == 0
    assert device.base_status.read_text() == firmware
    assert _listed(device.base_status) == {FIRMWARE}
    assert device.base_release.read_text() == device.release.read_text()
    assert _names_in(device.base) == {'status', 'release'}, 'the new base replaced the old one by renames'
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


def test_a_post_update_boot_with_no_network_still_records_the_base(tmp_path: Path) -> None:
    """The database is the firmware's whether or not apt is reachable on that boot."""
    device = _packages(tmp_path, installed=(), cached=(*PACKAGES, DEPENDENCY))

    run = _install(device, online=False)

    assert run.status == 0
    assert _listed(device.base_status) == {FIRMWARE}


def test_a_post_update_boot_caches_what_the_firmware_lacks_for_the_set(tmp_path: Path) -> None:
    """The boot the cache exists for is the one after a firmware update took every package.

    What a networkless boot after the next update needs is the set with every
    dependency the firmware does not ship, and nothing the firmware does ship.
    So the refresh resolves the set against the base this boot saved, and the
    offline boot installs all of it in one `dpkg` call.
    """
    device = _packages(tmp_path, installed=(), cached=())

    run = _install(device, online=True)

    assert run.status == 0
    assert _refresh(device) in run.calls
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)

    _update_firmware(device)
    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


def test_a_post_update_boot_cut_short_leaves_the_base_it_recorded_for_the_boot_after_it(tmp_path: Path) -> None:
    """A boot that finds none of the set installed is not always the firmware's database.

    The power going while dpkg unpacks the first install after an update
    leaves the set's dependencies `unpacked` and nothing of the set installed,
    and the boot after it finds none of the set again. That database is not
    the firmware's: resolved against it, the refresh would fetch the set alone,
    and the networkless boot after the next update could not install it. The
    base that boot needs was recorded before the power went, under the release
    still running, so the boot after it records nothing and resolves against
    that one.
    """
    device = _packages(tmp_path, installed=(), cached=())
    assert _install(device, online=True).status == 0

    _update_firmware(device)
    run = _install(device, online=True, power_loss=True)

    assert run.status != 0
    assert DEPENDENCY not in _listed(device.status)
    assert f'Package: {DEPENDENCY}\nStatus: {UNPACKED}\n' in device.status.read_text()
    base = device.base_status.read_text()
    assert _listed(device.base_status) == {FIRMWARE}

    run = _install(device, online=True)

    assert run.status == 0
    assert device.base_status.read_text() == base
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)

    _update_firmware(device)
    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


def test_a_networkless_boot_after_an_install_cut_short_configures_what_it_left_unpacked(tmp_path: Path) -> None:
    """dpkg configures a package it holds unpacked only when it is handed the deb again and unpacks it.

    The power going during an install leaves the set's dependencies unpacked at
    the version the cache holds, and a networkless boot on the same firmware
    installs from the cache. Told to skip a deb at the version it already holds
    — `--skip-same-version` — dpkg skips the unpacked ones too, and they stay
    unconfigured with everything that depends on them.
    """
    device = _packages(tmp_path, installed=(), cached=())
    assert _install(device, online=True).status == 0
    _update_firmware(device)
    assert _install(device, online=True, power_loss=True).status != 0
    assert f'Package: {DEPENDENCY}\nStatus: {UNPACKED}\n' in device.status.read_text()

    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


def test_a_networkless_boot_after_an_install_cut_short_at_a_newer_version_repairs_it_from_the_cache(
    tmp_path: Path,
) -> None:
    """A package an interrupted run left unpacked is not the firmware's, at whatever version.

    The archive moves on between firmware updates, so the install a boot cut
    short had unpacked a dependency at a newer version than the cache holds.
    dpkg refuses to downgrade an unpacked package under `--refuse-downgrade` as
    it does an installed one, which would keep that leftover unpacked and the
    set unconfigured beside it. Nothing holds it back: dpkg replaces it from the
    cache and configures it, and the set comes up.
    """
    device = _packages(
        tmp_path,
        installed=(),
        cached=(*PACKAGES, DEPENDENCY),
        records={DEPENDENCY: UNPACKED},
        versions={DEPENDENCY: '1.1'},
    )

    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}
    assert _record(device.status, DEPENDENCY) == ('install ok installed', '1.0')
    assert not _held_back(run)


#: The cache a refresh on an older firmware leaves: the set, the dependency that
#: firmware lacks, and `PINNED` at the version the set's install upgraded that
#: firmware's to, which is the version `DEPENDENT` pins.
OLDER_FIRMWARES_CACHE = (*PACKAGES, DEPENDENCY, PINNED)

#: What a firmware update after that refresh ships of the cache: `PINNED`, at a
#: newer version than the cache holds.
NEWER_FIRMWARE = {PINNED: '2.0'}


def _record(database: Path, package: str) -> tuple[str, str] | None:
    """The status and version of the record a dpkg database holds of `package`, or None when it holds none."""
    for stanza in database.read_text().split('\n\n'):
        lines = stanza.splitlines()
        if lines and lines[0] == f'Package: {package}':
            return lines[1].removeprefix('Status: '), lines[2].removeprefix('Version: ')
    return None


def _held_back(run: _Run) -> list[str]:
    """The lines in which a run says it held a deb of the cache back."""
    return [line for line in run.output.splitlines() if line.startswith("packages: holding back the offline cache's ")]


def _holding_back(package: str, cached: str, shipped: str) -> str:
    return f"packages: holding back the offline cache's {package} {cached}: the firmware ships {shipped}"


def _not_installed(*packages: str) -> str:
    """The line in which a run names the members of the set it leaves not installed."""
    return (
        f'packages: FAILED -- {" ".join(packages)} not installed: neither apt nor the offline cache could install them'
    )


def test_a_networkless_boot_after_an_update_keeps_the_firmwares_newer_package_and_fails(tmp_path: Path) -> None:
    """The cache is resolved against the firmware that ran its last refresh (Aetf/kluster-ops#480).

    A refresh on the older firmware cached `systemd` at the version the set's
    install upgraded it to, since `systemd-container` pins it exactly. An
    update since ships a newer `systemd`, and a networkless boot on it installs
    from that cache: handed to dpkg, the cache would replace the firmware's own
    `systemd` with the older build. Its deb is held back and named with both
    versions, so `systemd-container`, pinned to the cache's version, stays
    unconfigured, and the boot fails and names it.
    """
    device = _packages(tmp_path, installed=(), cached=OLDER_FIRMWARES_CACHE)
    _update_firmware(device, shipping=NEWER_FIRMWARE)

    run = _install(device, online=False)

    assert run.status == 1
    assert _record(device.status, PINNED) == ('install ok installed', NEWER_FIRMWARE[PINNED])
    assert _held_back(run) == [_holding_back(PINNED, '1.0', NEWER_FIRMWARE[PINNED])]
    assert _not_installed(DEPENDENT) in run.output.splitlines()


def test_a_held_back_package_leaves_the_rest_of_the_cache_installed_and_the_next_online_boot_completes_the_set(
    tmp_path: Path,
) -> None:
    """dpkg installs every deb of the cache but the one held back.

    Every member of the set that does not depend on the held-back version is
    installed offline, and `systemd-container`, which pins the cache's
    `systemd`, is left unpacked. The next boot that reaches apt installs the
    set against the new firmware and succeeds, and the cache it refreshes is
    resolved against that firmware, so it no longer holds `systemd`.
    """
    rest = set(PACKAGES) - {DEPENDENT}
    device = _packages(tmp_path, installed=(), cached=OLDER_FIRMWARES_CACHE)
    _update_firmware(device, shipping=NEWER_FIRMWARE)

    run = _install(device, online=False)

    assert run.status == 1
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, PINNED, DEPENDENCY, *rest}
    assert _record(device.status, DEPENDENT) == (UNPACKED, '1.0')

    run = _install(device, online=True)

    assert run.status == 0
    assert _listed(device.status) == {FIRMWARE, PINNED, DEPENDENCY, *PACKAGES}
    assert _record(device.status, PINNED) == ('install ok installed', NEWER_FIRMWARE[PINNED])
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)


def test_an_install_whose_newer_dependency_failed_its_maintainer_script_is_repaired_from_the_cache(
    tmp_path: Path,
) -> None:
    """A package apt's own failed run left half-configured is not the firmware's.

    The archive's dependency, newer than the cache's, fails its maintainer
    script: apt leaves it half-configured, the package of the set that depends
    on it unpacked, and the rest of the set installed at the archive's version.
    None of that is the firmware's, so nothing is held back: the fallback
    replaces all of it from the cache, the set comes up as on `main`, and the
    boot succeeds.
    """
    device = _packages(tmp_path, installed=(), cached=(*PACKAGES, DEPENDENCY))
    _ = device.candidate.write_text('1.1')

    run = _install(device, online=True, postinst_fails=(DEPENDENCY, '1.1'))

    assert run.status == 0
    assert not _held_back(run)
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}
    assert all(
        _record(device.status, package) == ('install ok installed', '1.0') for package in (*PACKAGES, DEPENDENCY)
    )

    run = _install(device, online=True, postinst_fails=(DEPENDENCY, '1.1'))

    assert run.status == 0
    assert run.calls == []


def test_a_failed_install_beside_a_held_back_firmware_package_fails_the_boot(tmp_path: Path) -> None:
    """A boot fails when the set is not up, whatever apt could reach.

    The firmware ships a newer `systemd` than the cache, and the archive's
    `systemd-container`, pinned to it, fails its maintainer script. The
    fallback holds the firmware's `systemd` back and leaves the cache's
    `systemd-container` unconfigured against it, and the boot fails, on this
    boot and on every one like it.
    """
    device = _packages(tmp_path, installed=(), cached=OLDER_FIRMWARES_CACHE)
    _update_firmware(device, shipping=NEWER_FIRMWARE)
    _ = device.candidate.write_text('2.0')

    for _ in range(2):
        run = _install(device, online=True, postinst_fails=(DEPENDENT, '2.0'))

        assert run.status == 1
        assert _held_back(run) == [_holding_back(PINNED, '1.0', NEWER_FIRMWARE[PINNED])]
        assert _record(device.status, PINNED) == ('install ok installed', NEWER_FIRMWARE[PINNED])
        assert _not_installed(DEPENDENT) in run.output.splitlines()


def _base_is_this_firmwares(device: _Packages) -> None:
    """The base as the update's first boot recorded it: the firmware's package alone, under the running release."""
    device.base.mkdir(exist_ok=True)
    _ = device.base_status.write_text(_database(FIRMWARE))
    _ = device.base_release.write_text(device.release.read_text())


def test_a_pin_target_an_apt_run_installed_newer_is_replaced_from_the_cache(tmp_path: Path) -> None:
    """A package installed at a newer version than the cache's is the firmware's only when the base lists it.

    An apt run installed the package `DEPENDENT` pins at a newer version than
    the cache holds, and its own install of the set failed a maintainer
    script. The base does not list that package, so it is not held back: dpkg
    replaces it from the cache, the cache's `DEPENDENT` configures against it,
    and the set comes up, as on `main`.
    """
    device = _packages(tmp_path, installed=(PINNED,), cached=(*PACKAGES, DEPENDENCY, PINNED), versions={PINNED: '1.1'})
    _base_is_this_firmwares(device)
    _ = device.candidate.write_text('1.1')

    run = _install(device, online=True, postinst_fails=(DEPENDENT, '1.1'))

    assert run.status == 0
    assert not _held_back(run)
    assert _listed(device.status) == {FIRMWARE, PINNED, DEPENDENCY, *PACKAGES}
    assert _record(device.status, PINNED) == ('install ok installed', '1.0')


def test_an_install_cut_short_in_its_configure_phase_is_repaired_from_the_cache(tmp_path: Path) -> None:
    """A networkless boot repairs an install whose pin target configured and whose dependent did not.

    The power went after the online install configured the package
    `DEPENDENT` pins, at the archive's newer version, and before it configured
    `DEPENDENT`. The base does not list that package, so nothing is held back:
    dpkg replaces both from the cache, and the set comes up offline, as on
    `main`.
    """
    device = _packages(
        tmp_path,
        installed=(PINNED, DEPENDENCY, *(set(PACKAGES) - {DEPENDENT})),
        cached=(*PACKAGES, DEPENDENCY, PINNED),
        records={DEPENDENT: UNPACKED},
        versions={PINNED: '1.1', DEPENDENT: '1.1'},
    )
    _base_is_this_firmwares(device)

    run = _install(device, online=False)

    assert run.status == 0
    assert not _held_back(run)
    assert _listed(device.status) == {FIRMWARE, PINNED, DEPENDENCY, *PACKAGES}
    assert _record(device.status, DEPENDENT) == ('install ok installed', '1.0')


def test_an_epoch_makes_the_firmwares_version_the_newer_one(tmp_path: Path) -> None:
    """Versions are compared in Debian's order, where the epoch decides first.

    The firmware ships `systemd` at `1:1.0`, after an epoch bump, and the cache
    holds `2.0`, which is older in Debian's order however its digits read.
    """
    device = _packages(tmp_path, installed=(), cached=(*PACKAGES, DEPENDENCY))
    _ = deb(device.cache, PINNED, '2.0').write_text('')
    _update_firmware(device, shipping={PINNED: '1:1.0'})

    run = _install(device, online=False)

    assert _held_back(run) == [_holding_back(PINNED, '2.0', '1:1.0')]
    assert _record(device.status, PINNED) == ('install ok installed', '1:1.0')


def test_a_firmware_package_an_interrupted_upgrade_left_unpacked_is_still_held_back(tmp_path: Path) -> None:
    """The firmware base says which version is the firmware's when the live system cannot.

    An interrupted upgrade left the firmware's `systemd` unpacked at the
    archive's version, so it is not installed, and the cache holds it only at a
    version older than the firmware's. Handed to dpkg, that deb would downgrade
    the firmware's package; it is held back, `systemd` stays as it is, and with
    the set not up the boot fails.
    """
    device = _packages(tmp_path, installed=(), cached=OLDER_FIRMWARES_CACHE)
    _update_firmware(device, shipping=NEWER_FIRMWARE)
    _ = _install(device, online=False)
    _ = device.base_status.write_text(_database(FIRMWARE, PINNED, versions=NEWER_FIRMWARE))
    _ = device.status.write_text(_database(FIRMWARE, records={PINNED: UNPACKED}, versions={PINNED: '2.1'}))

    run = _install(device, online=False)

    assert run.status == 1
    assert _held_back(run) == [_holding_back(PINNED, '1.0', NEWER_FIRMWARE[PINNED])]
    assert _record(device.status, PINNED) == (UNPACKED, '2.1')


def test_a_boot_that_finds_part_of_the_set_and_a_networkless_boot_after_it_install_the_whole_set(
    tmp_path: Path,
) -> None:
    """What survived on the live system changes nothing about what is cached (Aetf/kluster-ops#436).

    A push that grows the set is a boot on which the rest of the set survived,
    with every dependency of it installed. Resolved against the live system,
    the refresh would fetch only the new package and what it lacks, and the
    cache would lose what the surviving packages depend on. Resolved against
    the saved base, it fetches the whole closure again, and the networkless
    boot after the next firmware update installs the whole set from it. The
    base is left as the post-update boot saved it.
    """
    grown = next(package for package in PACKAGES if package != DEPENDENT)
    device = _packages(tmp_path, installed=(), cached=())
    assert _install(device, online=True).status == 0
    base = device.base_status.read_text()

    _ = device.status.write_text(_database(FIRMWARE, *(set(PACKAGES) - {grown}), DEPENDENCY))
    run = _install(device, online=True)

    assert run.status == 0
    assert _refresh(device) in run.calls
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert device.base_status.read_text() == base

    _update_firmware(device)
    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


def test_the_cache_is_what_the_base_lacks_and_replaces_the_old_one_whole(tmp_path: Path) -> None:
    """The cache is a resolution against the base, never an accumulation and never the live system's.

    A deb the old cache held and this resolution does not name does not survive
    into the next offline boot. Nor does one the install fetched because the
    live system lacked it — a package of the firmware somebody removed by hand,
    here — when the base has it: the install downloads into the same directory,
    so the refresh empties it first.
    """
    kept, lost = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(kept,), cached=(), base=(FIRMWARE,))
    _ = device.status.write_text(_database(kept))
    _ = deb(device.cache, 'from-an-older-firmware').write_text('')

    run = _install(device, online=True)

    assert run.status == 0
    assert _refresh(device) in run.calls
    assert not _dpkg_installs(run)
    assert _listed(device.status) == {kept, lost, DEPENDENCY, FIRMWARE}
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(f'{device.cache.name}.')], (
        'nothing of the swap is left beside the cache'
    )


def test_a_boot_whose_refresh_download_failed_keeps_the_old_cache_whole(tmp_path: Path) -> None:
    """A cache is replaced only by a whole resolution, never by what a failed download left.

    A download that fails part-way leaves what it did fetch — here the whole
    set and none of what it depends on, so a deb of every package of the set is
    there — and a snapshot of it would replace a cache that could install
    everything with one that cannot, found out on the next boot without a
    network. apt's exit status is what says the download was incomplete.
    """
    kept, lost = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(kept,), cached=(*PACKAGES, DEPENDENCY), base=(FIRMWARE,))

    run = _install(device, online=True, refresh_fails=True)

    assert run.status == 0
    assert _refresh(device) in run.calls
    assert 'cache refresh download failed, keeping the old cache' in run.output
    assert _listed(device.status) == {FIRMWARE, kept, lost, DEPENDENCY}
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert not device.download.exists(), 'the download directory goes with the run'


def test_with_no_base_the_cache_keeps_every_old_package_the_download_did_not_carry(tmp_path: Path) -> None:
    """Without the firmware's database, the cache is a superset that never shrinks.

    What the firmware lacks is not known until a firmware update's boot has
    recorded the base, and the live system is no stand-in for it. So the
    refresh downloads the set by name, keeps what the install fetched, and
    carries over every package of the old cache the download did not — here the
    dependency of the package that survived, which the download alone would
    have lost, and a deb no resolution names, which is the price of not
    knowing. A package the download did carry is the download's copy.
    """
    grown = next(package for package in PACKAGES if package != DEPENDENT)
    device = _packages(tmp_path, installed=(DEPENDENT, DEPENDENCY), cached=())
    for package in (*PACKAGES, DEPENDENCY, 'from-an-older-firmware'):
        _ = deb(device.cache, package).write_text('old')

    run = _install(device, online=True)

    assert run.status == 0
    assert _listed(device.status) == {FIRMWARE, grown, DEPENDENT, DEPENDENCY}
    assert _apt(device, 'install', '-y', '--download-only', '--reinstall', *sorted(PACKAGES)) in run.calls
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY, 'from-an-older-firmware')
    assert {path.name for path in device.cache.iterdir() if path.read_text() == 'old'} == _debs(
        device.cache, DEPENDENCY, 'from-an-older-firmware'
    )
    assert f'no firmware base in {device.base_status} yet' in run.output
    assert not device.base.exists()

    _update_firmware(device)
    run = _install(device, online=False)

    assert run.status == 0
    assert _listed(device.status) >= {*PACKAGES, DEPENDENCY}


def test_with_no_base_a_package_upgraded_since_it_was_cached_is_cached_at_the_installed_version(
    tmp_path: Path,
) -> None:
    """The superset carries an old deb over only at the version the live system runs.

    An apt run outside this script — `apt-get install --only-upgrade` of the
    set against a newer archive — upgrades a package of the set and, through
    the exact pin, the dependency beside it. The refresh's download carries the
    set at the new version, and an old deb of the dependency kept by name would
    sit beside it at the version the set no longer accepts, which a networkless
    boot cannot configure. So the dependency is downloaded again, at the
    version installed; a package the live system does not run — one it never
    had, or one removed with its configuration files kept, whose record still
    names a version — is carried as it is.
    """
    grown = next(package for package in PACKAGES if package != DEPENDENT)
    device = _packages(tmp_path, installed=(), cached=())
    _ = device.status.write_text(
        _database(
            FIRMWARE,
            DEPENDENT,
            DEPENDENCY,
            records={'removed-by-hand': CONFIG_FILES},
            versions={DEPENDENT: '2.0', DEPENDENCY: '2.0', 'removed-by-hand': '2.0'},
        )
    )
    _ = device.candidate.write_text('2.0\n')
    for package in (*PACKAGES, DEPENDENCY, 'from-an-older-firmware', 'removed-by-hand'):
        _ = deb(device.cache, package).write_text('old')

    run = _install(device, online=True)

    assert run.status == 0
    assert grown in _listed(device.status)
    assert f'packages: downloading {DEPENDENCY}=2.0, installed at a version' in run.output
    assert _names_in(device.cache) == {
        *(deb(device.cache, package, '2.0').name for package in (*PACKAGES, DEPENDENCY)),
        *_debs(device.cache, 'from-an-older-firmware', 'removed-by-hand'),
    }


def test_with_no_base_a_failed_download_of_an_installed_version_keeps_the_old_cache(tmp_path: Path) -> None:
    """A package the superset cannot carry at the installed version keeps the whole old cache.

    The rule is the refresh's: a cache is replaced only by one that holds
    everything, and a deb left at a version the live system no longer runs is
    not everything.
    """
    device = _packages(tmp_path, installed=(), cached=())
    _ = device.status.write_text(_database(FIRMWARE, DEPENDENT, DEPENDENCY, versions={DEPENDENCY: '2.0'}))
    for package in (*PACKAGES, DEPENDENCY):
        _ = deb(device.cache, package).write_text('old')

    run = _install(device, online=True, download_fails=True)

    assert run.status == 0
    assert 'cache refresh download failed, keeping the old cache' in run.output
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert {path.read_text() for path in device.cache.iterdir()} == {'old'}


def test_with_no_base_the_cache_keeps_what_the_install_fetched(tmp_path: Path) -> None:
    """The dependencies a package new to the device needed are fetched by the install alone.

    `--reinstall` downloads the packages it is named and not what they depend
    on, so without a base the install's own download is the one place a new
    package's missing dependency is fetched, and it stays in the cache.
    """
    survivor = next(package for package in PACKAGES if package != DEPENDENT)
    device = _packages(tmp_path, installed=(survivor,), cached=())

    run = _install(device, online=True)

    assert run.status == 0
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)


def test_a_cache_swap_cut_short_between_its_renames_is_completed_before_the_cache_is_read(tmp_path: Path) -> None:
    """The swap is two renames, and the power can go between them.

    That leaves no cache and the whole new one beside it. The next boot puts
    it in place before anything reads the cache, so the networkless boot after
    a firmware update installs from it rather than reporting nothing to fall
    back on.
    """
    device = _packages(tmp_path, installed=(), cached=())
    device.cache.rmdir()
    new = device.cache.with_name(f'{device.cache.name}.new')
    new.mkdir()
    for package in (*PACKAGES, DEPENDENCY):
        _ = deb(new, package).write_text('')

    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert not new.exists()


def test_a_base_that_lists_part_of_the_set_keeps_the_old_cache_and_says_so(tmp_path: Path) -> None:
    """A base that lists a package of the set resolves to less than the set, and never replaces a cache.

    apt fetches nothing it finds installed in the database it is pointed at, so
    a package of the set that the base lists has no deb in the download — the
    cache it would make could not install the set. A firmware base never lists
    one; a database copied by hand onto the base from a device that holds the
    set does.
    """
    kept, _ = sorted(PACKAGES)
    device = _packages(tmp_path, installed=(kept,), cached=(*PACKAGES, DEPENDENCY), base=(FIRMWARE, DEPENDENT))

    run = _install(device, online=True)

    assert run.status == 0
    assert _refresh(device) in run.calls
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert f'the refresh fetched no deb of {DEPENDENT}' in run.output


def test_a_deb_another_apt_run_left_behind_is_neither_cached_nor_installed(tmp_path: Path) -> None:
    """The cache holds what apt fetched for the set, and the offline boot installs only that.

    apt's shared archives hold whatever any apt run on the device left there —
    a vendor's tooling, or somebody by hand — and an `frr` deb among them is the
    case that matters: installed by this script, it would change the routing
    daemon's parser under an unchanged firmware. So apt downloads the set into a
    directory of the converger's own, emptied before use because an
    interrupted run can leave debs in it too, and only that becomes the cache.
    The offline boot that follows installs the set and nothing else.
    """
    device = _packages(tmp_path, installed=(), cached=())
    foreign = deb(device.archives, 'frr')
    _ = foreign.write_text('')
    device.download.mkdir()
    _ = deb(device.download, 'frr-pythontools').write_text('')

    run = _install(device, online=True)

    assert run.status == 0
    assert _names_in(device.cache) == _debs(device.cache, *PACKAGES, DEPENDENCY)
    assert foreign.exists(), "the shared archives are not the converger's to empty either"

    _update_firmware(device)
    run = _install(device, online=False)

    assert run.status == 0
    assert _dpkg_installs(run) == [_offline_install(device, *PACKAGES, DEPENDENCY)]
    assert _listed(device.status) == {FIRMWARE, *PACKAGES, DEPENDENCY}


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
    inputs = monitor.inputs_of(f'{CONSUMER}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')

    assert inputs['path'] == f'{persistence.UNIT_SOURCE_DIR}/{TEMPLATE_UNIT}.d/{DROPIN}'
    assert inputs['mode'] == persistence.FILE_MODE
    assert inputs['owner'] == conventions.gateway.SSH_USER
    assert monitor.options_of(f'{CONSUMER}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}').parent.endswith(f'::{CONSUMER}')


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
    edges = monitor.depends_on(f'{CONSUMER}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')

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
    hook = monitor.inputs_of(f'{CONSUMER}-dropin-{TEMPLATE_UNIT}.d/{DROPIN}')['hook']
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
    #: The `systemctl` the script reaches, reading its unit files from `live`
    #: the way systemd reads them from the device's unit directory — so what a
    #: unit was started on is what that directory held when systemd last read
    #: it: at the unit's first use, or at the last reload.
    systemd: FakeSystemd
    #: What the last run wrote to its error stream, which on the device is the
    #: boot log.
    complaints: Path


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
        systemd=FakeSystemd(tmp_path / 'systemd', unit_path=(live,)),
        complaints=tmp_path / 'complaints',
    )
    device.source.mkdir()
    device.live.mkdir()

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
    _ = device.systemd.take_calls()
    completed = subprocess.run(
        ['/bin/bash', str(device.script)],
        env={'PATH': f'{device.systemd.tools}:/usr/bin:/bin'},
        capture_output=True,
        check=False,
    )
    _ = device.complaints.write_bytes(completed.stderr)
    return completed.returncode, device.systemd.take_calls()


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
    """A unit whose file changed is restarted onto that file and every drop-in it has.

    A restart is what makes a statement that only applies at start take
    effect. Converging the drop-ins after the units would restart the unit
    onto the configuration it had before, and so would a restart after the
    copy with no reload between them: systemd runs a unit on what it read at
    its first use or its last reload, not on what is on the disk. Either way
    the new statement waits for a restart nothing else is going to do — so
    what is read back is what systemd had loaded for the unit when it
    restarted it. The unit is enabled already, because an `enable` reloads
    too and would stand in for a reload the script left out.
    """
    device = _device(tmp_path)
    _ = (device.source / UNIT).write_text(f'[Service]\nExecStart=/bin/true\n{INSTALLABLE}')
    _ = (device.live / UNIT).write_text(f'[Service]\nExecStart=/bin/false\n{INSTALLABLE}')
    device.systemd.enable(UNIT)
    device.systemd.activate(UNIT)
    _ = _dropin(device.source, UNIT, DROPIN, '[Service]\nRestart=always\n')

    status, commands = _converge(device)
    restarted_on = device.systemd.started_on(UNIT)

    assert status == 0
    assert f'restart {UNIT}' in commands
    assert restarted_on is not None
    assert restarted_on.unit == f'[Service]\nExecStart=/bin/true\n{INSTALLABLE}'
    assert restarted_on.dropins == {DROPIN: '[Service]\nRestart=always\n'}


def test_a_unit_whose_file_is_in_place_is_enabled_if_it_is_not(tmp_path: Path) -> None:
    """The live copy surviving is not the enablement surviving.

    The two are different files on the device: the unit is copied into the
    unit directory, and enabling it is a link from the target that wants it.
    A device that kept the one and lost the other — or never had the other,
    because the run that installed the file was cut short before it enabled it —
    would run the unit until the next boot and then not at all, so the run
    that finds the file already in place enables it all the same. Quietly: the
    links `systemctl enable` reports are otherwise written to the boot log,
    whose readers take anything there for a device that needs somebody.
    """
    device = _device(tmp_path)
    unit = f'[Service]\nExecStart=/bin/true\n{INSTALLABLE}'
    _ = (device.source / UNIT).write_text(unit)
    _ = (device.live / UNIT).write_text(unit)
    device.systemd.activate(UNIT)

    status, commands = _converge(device)

    assert status == 0
    assert UNIT in device.systemd.enabled
    assert device.complaints.read_text() == ''
    assert f'restart {UNIT}' not in commands, 'a file that did not change is no reason to bounce the unit'


def test_a_unit_that_is_not_running_is_started_and_not_bounced(tmp_path: Path) -> None:
    """Whether the unit runs is checked on every run, not only when its file moved.

    A unit that exited, or that somebody stopped, has a file identical to its
    source, so a converger that acted only on a changed file would leave it
    down on the boot that was meant to bring it back. A start rather than a
    restart, because a start leaves a unit that is already running alone.
    """
    device = _device(tmp_path)
    unit = f'[Service]\nExecStart=/bin/true\n{INSTALLABLE}'
    _ = (device.source / UNIT).write_text(unit)
    _ = (device.live / UNIT).write_text(unit)
    device.systemd.enable(UNIT)

    status, commands = _converge(device)

    assert status == 0
    assert f'start {UNIT}' in commands
    assert f'restart {UNIT}' not in commands
    assert not [command for command in commands if command.endswith(f'enable {UNIT}')], 'it was enabled already'
    assert UNIT in device.systemd.active


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
