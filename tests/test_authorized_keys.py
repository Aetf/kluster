"""The keys that open the gateway, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: that a declared key becomes a file of its own, that the converger
appends and never removes, and that the ways of declaring a set that could not
work on the device are refused where they are written.

**The converger is run**: the script the component registers, under `sh`,
against a temporary tree that holds the key files as their resources declare
them, because what it promises is what it leaves in a file — a key nobody here
declared kept, a line that never ended repaired, a line already present left
where it is and one that merely contains a key not mistaken for it, its own
sources left in place, and the modes the daemon accepts.
"""

from __future__ import annotations

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import final

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import access, persistence
from kluster.components.gateway.access import AuthorizedKeys, PublicKey
from kluster.components.gateway.persistence import DevicePersistence
from kluster.providers.device_files.provider import Connection

MECHANISM = 'mechanism'
NAME = 'access'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'
PACKAGES = ('systemd-container',)
CONNECTION = Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER)

#: Two keys, because the point of one file each is that they are independent.
#: Both are invented; what matters is the shape of an `authorized_keys` line.
CI_KEY = PublicKey(name='kluster-physical', key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIci kluster-physical@gw')
OPERATOR_KEY = PublicKey(name='operator', key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIop operator@workstation')
KEYS = (CI_KEY, OPERATOR_KEY)
#: A line nobody here declared: the kind an operator adds on the device by hand.
HAND_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhand operator@recovery'


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def mechanism(monitor: Recorder) -> DevicePersistence:
    """The persistence layer the component builds on, declared once."""
    async with declaring():
        return DevicePersistence(MECHANISM, connection=CONNECTION, packages=PACKAGES)


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def access_layer(mechanism: DevicePersistence) -> AuthorizedKeys:
    """The component declared once, the way `Gateway` declares it."""
    async with declaring():
        declared = AuthorizedKeys(
            NAME,
            connection=CONNECTION,
            mechanism=mechanism,
            keys=(CI_KEY, OPERATOR_KEY),
        )
    return declared


##
## What the device holds
##


def test_every_declared_key_is_a_file_of_its_own(monitor: Recorder) -> None:
    """Which is what makes two keys independent of each other.

    One rendered `authorized_keys` would make adding a key an edit of the file
    the other key is in — and would make the converger the owner of a file that
    also holds keys nobody here declared.
    """
    paths = {monitor.inputs_of(f'{NAME}-key-{key.name}')['path'] for key in KEYS}
    assert len(paths) == len(KEYS), 'one path per key'

    for key in KEYS:
        inputs = monitor.inputs_of(f'{NAME}-key-{key.name}')

        assert inputs['path'] == access.key_path(key.name)
        assert inputs['content'] == f'{key.key}\n'
        assert inputs['mode'] == persistence.FILE_MODE


def test_the_converger_is_a_unit_and_an_executable_rather_than_a_boot_chain_script(monitor: Recorder) -> None:
    """Appending to a file manipulates nothing of systemd's own.

    So the local rule puts it on the unit side, and the executable it runs is
    also every key file's hook: a key declared during a push is usable when the
    push returns rather than at the next boot.
    """
    executable = monitor.inputs_of(f'{NAME}-bin-{access.CONVERGER}')
    unit = monitor.inputs_of(f'{NAME}-unit-{access.CONVERGER_UNIT}')

    assert executable['path'] == persistence.executable_path(access.CONVERGER)
    assert executable['mode'] == persistence.SCRIPT_MODE
    assert 'WantedBy=multi-user.target' in unit['content']
    assert 'Type=oneshot' in unit['content']
    assert 'RemainAfterExit=yes' in unit['content']
    assert f'ExecStart={persistence.executable_path(access.CONVERGER)}' in unit['content']

    for key in (CI_KEY, OPERATOR_KEY):
        assert persistence.executable_path(access.CONVERGER) in monitor.inputs_of(f'{NAME}-key-{key.name}')['hook']

    on_boot = [name for name in monitor.names_declared if name.startswith(f'{NAME}-on-boot-')]
    assert on_boot == [], 'the component put nothing in the boot chain'


@pytest.mark.asyncio
async def test_a_key_waits_for_the_executable_that_installs_it(monitor: Recorder, access_layer: AuthorizedKeys) -> None:
    """A hook that runs a program the device does not have fails its own write.

    So a key file is declared after the executable that is its hook, and after
    the directory it lands in.
    """
    depends = monitor.depends_on(f'{NAME}-key-{CI_KEY.name}')

    assert str(await access_layer.converger.urn.future()) in depends
    assert str(await access_layer.directory.urn.future()) in depends


##
## What the converger does
##


@final
@dataclass(frozen=True)
class _Device:
    """A temporary tree laid out as the device's, with the converger the component ships beside it."""

    root: Path
    script: Path

    def at(self, path: str) -> Path:
        """`path` on the device, as it sits in the temporary tree."""
        return self.root / path.lstrip('/')

    @property
    def authorized(self) -> Path:
        return self.at(access.AUTHORIZED_KEYS)

    def lines(self) -> list[str]:
        """The file's lines, none where the run never wrote it."""
        return self.authorized.read_text().splitlines() if self.authorized.exists() else []


def _registered_keys(monitor: Recorder) -> dict[str, str]:
    """Each declared key file as its resource is registered: path to content."""
    registered = (monitor.inputs_of(f'{NAME}-key-{key.name}') for key in KEYS)
    return {str(inputs['path']): str(inputs['content']) for inputs in registered}


def _device(tmp_path: Path, monitor: Recorder) -> _Device:
    """The declared keys and the converger, both as the component registers them, under a temporary tree.

    Each key file is written at the path and with the content its resource
    carries, so the run finds what a push would leave on the device. The
    converger is the content the component registers for its executable, with
    the two device paths it acts on moved under the tree: the directory the
    key files were registered in, and the file the daemon reads. Each is
    asserted present before it is moved, so a converger that names another
    place for either fails here rather than running against the device's own
    path.
    """
    device = _Device(root=tmp_path / 'device', script=tmp_path / access.CONVERGER)
    registered = _registered_keys(monitor)
    for path, content in registered.items():
        delivered = device.at(path)
        delivered.parent.mkdir(parents=True, exist_ok=True)
        _ = delivered.write_text(content)

    (key_dir,) = {str(PurePosixPath(path).parent) for path in registered}
    script = str(monitor.inputs_of(f'{NAME}-bin-{access.CONVERGER}')['content'])
    for on_device in (key_dir, access.AUTHORIZED_KEYS):
        assert on_device in script, f'the converger does not act on {on_device}'
        script = script.replace(on_device, str(device.at(on_device)))
    _ = device.script.write_text(script)
    return device


def _hand_written(device: _Device, content: str) -> None:
    """An `authorized_keys` the device already holds, written by somebody other than this program."""
    device.authorized.parent.mkdir(parents=True)
    _ = device.authorized.write_text(content)


def _converge(device: _Device) -> None:
    """Run the converger once, as the unit and every key file's hook run it."""
    completed = subprocess.run(
        ['/bin/sh', str(device.script)],
        env={'PATH': '/usr/bin:/bin'},
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_every_declared_key_reaches_the_file_the_daemon_reads(monitor: Recorder, tmp_path: Path) -> None:
    """Each key is delivered where the converger looks for keys.

    A key file anywhere else, or under a name its loop does not match, is one
    the converger never reads: it looks installed in a preview and opens
    nothing.
    """
    device = _device(tmp_path, monitor)
    _converge(device)

    assert sorted(device.lines()) == sorted(key.key for key in KEYS)


def test_a_key_added_on_the_device_by_hand_is_never_removed(monitor: Recorder, tmp_path: Path) -> None:
    """The file also holds the only other way in, and this program did not put it there.

    Mirroring the declaration onto it would delete an operator's own key —
    which on this machine is how the last door closes. So the converger appends
    what is missing and takes nothing away, on the run that installs the keys
    and on every run after it.
    """
    device = _device(tmp_path, monitor)
    _hand_written(device, f'{HAND_KEY}\n')

    _converge(device)
    _converge(device)

    lines = device.lines()
    assert lines[:1] == [HAND_KEY]
    assert sorted(lines[1:]) == sorted(key.key for key in KEYS)
    # Nor are its own sources taken away: the key files on the custom root are
    # what puts the installation's key back after a reset takes the file.
    for path, content in _registered_keys(monitor).items():
        source = device.at(path)
        assert source.is_file(), f'{path} was removed'
        assert source.read_text() == content, path


def test_a_hand_added_key_whose_line_never_ended_is_not_glued_to_the_next_one(
    monitor: Recorder, tmp_path: Path
) -> None:
    """Two keys run together authorize neither, and the ruined one is somebody's.

    `authorized_keys` is a line-oriented file that nothing guarantees ends in a
    newline — an operator appends a key with an editor that does not add one,
    or `printf` without a trailing `\\n`. Appending onto that makes one string
    out of two keys, and the first of them is the hand-added one this component
    promises never to touch.
    """
    device = _device(tmp_path, monitor)
    _hand_written(device, HAND_KEY)

    _converge(device)

    lines = device.lines()
    assert lines[:1] == [HAND_KEY]
    assert sorted(lines[1:]) == sorted(key.key for key in KEYS)
    assert device.authorized.read_text().endswith('\n')


def test_a_key_already_in_the_file_is_left_where_it_is(monitor: Recorder, tmp_path: Path) -> None:
    """The converger runs at every boot and after every key push.

    It compares whole lines, so a key already present — declared here or added
    by hand — is neither duplicated nor moved to the end.
    """
    device = _device(tmp_path, monitor)
    _hand_written(device, f'{OPERATOR_KEY.key}\n{HAND_KEY}\n')

    _converge(device)
    _converge(device)

    assert device.lines() == [OPERATOR_KEY.key, HAND_KEY, CI_KEY.key]


def test_a_line_that_only_contains_a_key_is_not_that_key(monitor: Recorder, tmp_path: Path) -> None:
    """A commented-out copy, or the key behind an options prefix, authorizes nothing as written.

    So presence is a whole line, and a line that merely holds a declared key
    leaves that key to be appended.
    """
    device = _device(tmp_path, monitor)
    _hand_written(device, f'# {CI_KEY.key}\n')

    _converge(device)

    lines = device.lines()
    assert lines[:1] == [f'# {CI_KEY.key}']
    assert sorted(lines[1:]) == sorted(key.key for key in KEYS)


def test_the_file_the_daemon_reads_is_left_only_to_its_owner(monitor: Recorder, tmp_path: Path) -> None:
    """An `authorized_keys` anything else may write is one anyone may add a key to.

    The daemon refuses a group- or world-writable file outright, so the
    converger sets the modes the daemon accepts on the directory and the file
    rather than assuming whatever left them there left them that way.
    """
    device = _device(tmp_path, monitor)
    _hand_written(device, f'{HAND_KEY}\n')
    device.authorized.parent.chmod(0o775)
    device.authorized.chmod(0o664)

    _converge(device)

    assert stat.S_IMODE(device.authorized.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(device.authorized.stat().st_mode) == 0o600


##
## What cannot be declared
##


@pytest.mark.asyncio
async def test_a_set_with_no_key_in_it_is_refused(mechanism: DevicePersistence) -> None:
    """The converger removes nothing, so an empty set is not a revocation.

    What it would be instead is a device this program can no longer open a
    session on once a major-version jump or a factory reset takes `/root`
    away — declared by a component that cannot fail, because there is nothing
    for it to do.
    """
    with pytest.raises(ValueError, match='at least one key'):
        _ = AuthorizedKeys('empty', connection=CONNECTION, mechanism=mechanism, keys=())


@pytest.mark.asyncio
async def test_a_key_that_is_not_one_line_is_refused(mechanism: DevicePersistence) -> None:
    """The converger reports a multi-line file present as soon as either line is.

    So the second line would never be appended, and a key nobody could use
    would look installed.
    """
    with pytest.raises(ValueError, match='single authorized_keys line'):
        _ = AuthorizedKeys(
            'pair',
            connection=CONNECTION,
            mechanism=mechanism,
            keys=(PublicKey(name='two', key=f'{CI_KEY.key}\n{OPERATOR_KEY.key}'),),
        )


@pytest.mark.asyncio
async def test_two_keys_under_one_name_are_refused(mechanism: DevicePersistence) -> None:
    """The name is the file, so the second would replace the first on the device."""
    with pytest.raises(ValueError, match='declared twice'):
        _ = AuthorizedKeys(
            'twice',
            connection=CONNECTION,
            mechanism=mechanism,
            keys=(CI_KEY, PublicKey(name=CI_KEY.name, key=OPERATOR_KEY.key)),
        )
