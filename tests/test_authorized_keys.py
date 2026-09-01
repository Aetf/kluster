"""The keys that open the gateway, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is what a diff cannot show a
reviewer: that a declared key becomes a file of its own, that the converger
appends and never removes, and that the ways of declaring a set that could not
work on the device are refused where they are written.
"""

from __future__ import annotations

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


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def mechanism(monitor: Recorder) -> DevicePersistence:
    """The persistence layer the component builds on, declared once."""
    async with declaring():
        return DevicePersistence(MECHANISM, connection=CONNECTION, packages=PACKAGES)


@pytest_asyncio.fixture(scope='module', autouse=True)
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
    for key in (CI_KEY, OPERATOR_KEY):
        inputs = monitor.inputs_of(f'{NAME}-key-{key.name}')

        assert inputs['path'] == f'{conventions.gateway.CUSTOM_ROOT}/{access.KEY_DIRECTORY}/{key.name}.pub'
        assert inputs['path'] == access.key_path(key.name)
        assert inputs['content'] == f'{key.key}\n'
        assert inputs['mode'] == persistence.FILE_MODE


def test_the_converger_is_a_unit_and_an_executable_rather_than_a_boot_chain_script(monitor: Recorder) -> None:
    """Appending to a file manipulates nothing of systemd's own.

    So the local rule puts it on the unit side, and the executable it runs is
    also every key file's hook: a key declared during a push is usable when the
    push returns rather than at the next boot.
    """
    executable = monitor.inputs_of(f'{MECHANISM}-bin-{access.CONVERGER}')
    unit = monitor.inputs_of(f'{MECHANISM}-unit-{access.CONVERGER_UNIT}')

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


def test_a_key_added_on_the_device_by_hand_is_never_removed() -> None:
    """The file also holds the only other way in, and this program did not put it there.

    Mirroring the declaration onto it would delete an operator's own key —
    which on this machine is how the last door closes. So the converger appends
    what is missing and takes nothing away.
    """
    script = access.converger_script()

    assert 'cat "$pub" >>"$authorized"' in script
    assert 'rm ' not in script
    assert '>"$authorized"' not in script.replace('>>"$authorized"', '')


def test_a_hand_added_key_whose_line_never_ended_is_not_glued_to_the_next_one() -> None:
    """Two keys run together authorize neither, and the ruined one is somebody's.

    `authorized_keys` is a line-oriented file that nothing guarantees ends in a
    newline — an operator appends a key with an editor that does not add one,
    or `printf` without a trailing `\\n`. Appending onto that makes one string
    out of two keys, and the first of them is the hand-added one this component
    promises never to touch.
    """
    script = access.converger_script()

    assert '[ -s "$authorized" ] && [ -n "$(tail -c1 "$authorized")" ]' in script
    assert script.index('tail -c1') < script.index('cat "$pub" >>"$authorized"'), (
        'the newline is ensured before anything is appended'
    )


def test_a_key_already_in_the_file_is_left_where_it_is() -> None:
    """The converger runs at every boot and after every key push.

    It compares whole lines, so a key already present — declared here or added
    by hand — is neither duplicated nor moved to the end.
    """
    script = access.converger_script()

    assert 'grep -qxF "$(cat "$pub")" "$authorized"' in script
    assert f'authorized={access.AUTHORIZED_KEYS}' in script


def test_the_file_the_daemon_reads_is_left_only_to_its_owner() -> None:
    """An `authorized_keys` anything else may write is one anyone may add a key to.

    The daemon refuses a group- or world-writable file outright, so the
    converger creates the directory and the file with the modes the daemon
    accepts rather than assuming an update left them that way.
    """
    script = access.converger_script()

    assert 'chmod 700 "$(dirname "$authorized")"' in script
    assert 'chmod 600 "$authorized"' in script


##
## What cannot be declared
##


@pytest.mark.asyncio
async def test_a_set_with_no_key_in_it_is_refused(mechanism: DevicePersistence) -> None:
    """The converger removes nothing, so an empty set is not a revocation.

    What it would be instead is a device this program can no longer open a
    session on once a firmware update takes `/root` away — declared by a
    component that cannot fail, because there is nothing for it to do.
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
