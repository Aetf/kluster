"""The gw-config provider: convergence against a device that is not there.

Every test below runs the provider's real code against a fake device. What is
doubled is the wire -- the SSH session -- and nothing above it: the shell
one-liners, the quoting, the key parsing and the diff logic are the shipped
ones. The device pulls its own images, so a suite that has replaced the session
has replaced everything that leaves the runner, and no test opens a socket.

**A provider under test is configured first**, because a provider in production
is: the plugin deserializes it out of a resource's `__provider` property and
calls `configure` before handing it any operation (rfc-002 §7.5 E2). `configured`
below does that with a `ConfigureRequest` built the way the plugin builds one --
the same class, the same project namespace -- so what the tests exercise is the
real ordering rather than an attribute set by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, final

import asyncssh
import pulumi.dynamic as dynamic
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from asyncssh.known_hosts import match_known_hosts
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The engine's own provider serialization, which is what a `__provider` property
# holds. It lives beside the base class rather than in the package's exports.
from pulumi.dynamic.dynamic import serialize_provider  # pyright: ignore[reportUnknownVariableType]
from pulumi.runtime import rpc

from kluster.providers.configured import FINGERPRINT_LENGTH, PROVIDER_VERSION, SESSION
from kluster.providers.device_files import provider, ssh


def private_key() -> str:
    """A fresh client credential, in the OpenSSH form the stack configuration holds."""
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def public_key(private: str) -> str:
    """The matching public half, in the `ssh-ed25519 <blob>` form the pin takes."""
    key = serialization.load_ssh_private_key(private.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


# Real key material, generated once. The provider tests never parse it, but the
# transport tests do, and one pair for both keeps the two sets comparable. It is
# generated rather than checked in: a repository is no place for a private key,
# even a throwaway one.
PRIVATE_KEY = private_key()
HOST_KEY = public_key(private_key())

HOST = '10.144.1.1'
CONFIG_PATH = '/data/frr/frr.conf'
CONFIG = 'router bgp 65000\n'
HOOK = "vtysh -c 'configure terminal'"

DIRECTORY_PATH = '/data/custom/machines'
DIRECTORY_HOOK = 'systemctl daemon-reload'
#: What the fake's `stat` finds where a directory is. The fake keeps files, and a
#: directory is an entry whose contents nothing reads.
DIRECTORY_ENTRY = b'a directory, as far as `stat` is concerned'

ROOTFS_REPOSITORY = 'registry.invalid/installation/adguard'
#: Deliberately unlike everything else here, so that a test asserting the pull
#: does not name the tag can tell the difference.
ROOTFS_TAG = 'v7.2.1'
#: The manifest digest that is the pin, and the only thing a pull is performed
#: by. Nothing on the runner ever hashes anything to compare against it: the
#: verification belongs to the device's own `skopeo`.
ROOTFS_DIGEST = f'sha256:{"e" * 64}'
ROOTFS_REFERENCE = f'{ROOTFS_REPOSITORY}@{ROOTFS_DIGEST}'
ROOTFS_TREE = '/data/services/roots/adguard'
#: What the device's `stat` sees where a tree is. The fake keeps files, and a
#: directory is a file whose contents nothing reads.
TREE_ENTRY = b'a directory, as far as `stat` is concerned'
#: The command fragments a test refuses to make one step of a push fail.
SKOPEO = 'skopeo copy'
UMOCI = 'umoci raw unpack'
HOOK_COMMAND = 'systemctl restart adguard'


@final
@dataclass
class Entry:
    """A file as the device holds it.

    `kind` is what `stat` would call it, because a path holding the wrong kind
    of thing is one of the answers the providers act on.
    """

    data: bytes
    mode: str = '0644'
    owner: str = 'root'
    group: str = 'root'
    kind: str = 'regular file'


@final
@dataclass
class Device:
    """A device with files on it, and a memory of what it was asked to do.

    `log` is the whole point: several of the design's guarantees are about
    *order* -- a hook that runs after the tree is in place and after the marker
    that announces it -- and an unordered set of assertions cannot see them.
    """

    files: dict[str, Entry] = field(default_factory=dict[str, Entry])
    log: list[str] = field(default_factory=list[str])
    sessions: int = 0
    devices: list[ssh.Device] = field(default_factory=list[ssh.Device])
    hook_status: int = 0
    #: Commands whose text contains one of these exit non-zero. A sequence's
    #: guarantees are about which step failed, so a test has to be able to
    #: refuse one of them and let the rest through.
    refuse: tuple[str, ...] = ()
    #: The same, where the status itself is what the provider reads: the
    #: fragment that identifies the command, and what it exits with.
    statuses: dict[str, int] = field(default_factory=dict[str, int])
    #: Commands whose text contains one of these never answer at all: the
    #: session drops, or the command overruns the timeout it was given. That
    #: failure reaches the provider as an exception rather than as an exit
    #: status, which is a different path through the same code.
    interrupt: tuple[str, ...] = ()

    @property
    def commands(self) -> list[str]:
        return [line.removeprefix('run ') for line in self.log if line.startswith('run ')]

    def open(self, device: ssh.Device) -> Any:
        self.devices.append(device)
        self.sessions += 1

        @asynccontextmanager
        async def session() -> AsyncGenerator[ssh.Transport]:
            yield Transport(self)

        return session()


@final
class Transport:
    """`ssh.Transport` against a dictionary."""

    def __init__(self, device: Device) -> None:
        self.device: Device = device

    async def read(self, path: str) -> bytes | None:
        entry = self.device.files.get(path)
        self.device.log.append(f'read {path}')
        return None if entry is None else entry.data

    async def stat(self, path: str) -> ssh.FileStat | None:
        entry = self.device.files.get(path)
        self.device.log.append(f'stat {path}')
        if entry is None:
            return None
        return ssh.FileStat(
            owner=entry.owner, group=entry.group, mode=entry.mode, size=len(entry.data), kind=entry.kind
        )

    async def write(self, path: str, data: bytes, *, mode: str, owner: str | None) -> None:
        user, _, group = (owner or 'root:root').partition(':')
        self.device.files[path] = Entry(data=data, mode=mode, owner=user, group=group or 'root')
        self.device.log.append(f'write {path}')

    async def remove(self, path: str) -> None:
        _ = self.device.files.pop(path, None)
        self.device.log.append(f'remove {path}')

    async def run(self, command: str) -> ssh.CommandResult:
        self.device.log.append(f'run {command}')
        if any(fragment in command for fragment in self.device.interrupt):
            # The builtin, which is what `asyncssh.TimeoutError` is a subclass
            # of: a command given a timeout that runs past it raises rather
            # than returning a status.
            raise TimeoutError(f'`{command}` never answered')
        answered = [status for fragment, status in self.device.statuses.items() if fragment in command]
        refused = any(fragment in command for fragment in self.device.refuse)
        status = answered[0] if answered else (1 if refused else self.device.hook_status)
        return ssh.CommandResult(exit_status=status, stdout=b'', stderr='refused')


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> Device:
    """A device the provider reaches instead of a real one."""
    fake = Device()
    monkeypatch.setattr(provider, 'open_transport', fake.open)
    return fake


#: The project the configuration keys below are namespaced by, and the one this
#: module's declarations are mocked under. An unqualified key is resolved
#: against the running project, which is how the plugin finds it (rfc-002 §7.5
#: E2); which project that is has no bearing on anything asserted here.
PROJECT = 'kluster'


def configured[T: provider.DeviceProvider](instance: T, credential: str = PRIVATE_KEY) -> T:
    """A provider as an operation receives one: revived, then handed the config.

    The credential arrives already decrypted, which is what the plugin does with
    a secret configuration value before calling `configure`.
    """
    config = dynamic.Config({f'{PROJECT}:{provider.PRIVATE_KEY_CONFIG}': credential}, PROJECT)
    instance.configure(dynamic.ConfigureRequest(config=config))
    return instance


def file_provider(credential: str = PRIVATE_KEY) -> provider.DeviceFileProvider:
    return configured(provider.DeviceFileProvider(), credential)


def artifact_provider(credential: str = PRIVATE_KEY) -> provider.DeviceArtifactProvider:
    return configured(provider.DeviceArtifactProvider(), credential)


def directory_provider(credential: str = PRIVATE_KEY) -> provider.DeviceDirectoryProvider:
    return configured(provider.DeviceDirectoryProvider(), credential)


def checked(instance: provider.DeviceProvider, props: dict[str, Any]) -> dict[str, Any]:
    """The inputs as the engine stores and compares them: what `check` returned."""
    return instance.check({}, props).inputs


def file_props(**overrides: Any) -> dict[str, Any]:
    return {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'host_key': HOST_KEY,
        'path': CONFIG_PATH,
        'content': CONFIG,
        'mode': '0644',
        'owner': 'root:root',
        'hook': HOOK,
    } | overrides


def artifact_props(**overrides: Any) -> dict[str, Any]:
    """An artifact as a container service declares one: a pin and a tree."""
    return {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'host_key': HOST_KEY,
        'repository': ROOTFS_REPOSITORY,
        'tag': ROOTFS_TAG,
        'digest': ROOTFS_DIGEST,
        'root': ROOTFS_TREE,
        'hook': HOOK_COMMAND,
    } | overrides


def directory_props(**overrides: Any) -> dict[str, Any]:
    """A directory as a layer that fills it declares one: a path and a shape."""
    return {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'host_key': HOST_KEY,
        'path': DIRECTORY_PATH,
        'mode': '0755',
        'owner': 'root:root',
        'hook': DIRECTORY_HOOK,
    } | overrides


def converged(device: Device, props: Mapping[str, Any]) -> None:
    """Put the device in the state the property bag declares."""
    user, _, group = str(props['owner']).partition(':')
    device.files[str(props['path'])] = Entry(
        data=str(props['content']).encode(),
        mode=str(props['mode']),
        owner=user,
        group=group,
    )


def made(device: Device, props: Mapping[str, Any]) -> None:
    """Put the device in the state a directory bag declares."""
    user, _, group = str(props['owner']).partition(':')
    device.files[str(props['path'])] = Entry(
        data=DIRECTORY_ENTRY, mode=str(props['mode']), owner=user, group=group, kind='directory'
    )


def landed(device: Device, props: Mapping[str, Any], digest: str | None = None) -> None:
    """Put the device in the state an artifact bag declares, marker and all.

    `digest` is what the marker claims, which is the *manifest* digest of the
    image the tree was unpacked from and never a checksum of the tree itself.
    """
    root = str(props['root'])
    claimed = digest if digest is not None else str(props['digest'])
    device.files[root] = Entry(data=TREE_ENTRY, kind='directory')
    device.files[provider.marker_path(root)] = Entry(data=f'{claimed}\n'.encode())


##
## The file resource
##


def test_a_created_file_lands_before_the_hook_that_makes_it_take_effect(device: Device) -> None:
    """Write, then notify. A hook that ran first would act on the old file."""
    props = file_props()

    _ = file_provider().create(props)

    assert device.files[CONFIG_PATH] == Entry(data=CONFIG.encode(), mode='0644', owner='root', group='root')
    assert device.log == [f'write {CONFIG_PATH}', f'run {HOOK}']


def test_a_resource_is_identified_by_its_path_on_the_device(device: Device) -> None:
    """A provider instance stands for one device, so the path is the whole name.

    Nothing about the session is in it: an identifier carrying the address would
    have to be re-derived the day the dial moves, and the ceremony that moves it
    (physical/gateway.md §2.5) is meant to be an update to these same resources.
    """
    result = file_provider().create(file_props())

    assert result.id == CONFIG_PATH


def test_a_created_file_opens_the_session_with_the_configured_credential(device: Device) -> None:
    """The address and the pin are declared; the key that answers them is not.

    It comes from `configure`, so no property bag carries it and no caller could
    have passed a different one.
    """
    _ = file_provider().create(file_props())

    assert device.devices == [
        ssh.Device(host=HOST, username='root', private_key=PRIVATE_KEY, host_key=HOST_KEY, port=22)
    ]
    assert 'private_key' not in file_props()


def test_a_file_edited_on_the_device_is_a_change_without_anyone_asking_for_a_refresh(device: Device) -> None:
    """This is the whole reason `diff` opens a session instead of reading state."""
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].data = b'router bgp 65001\n'

    result = file_provider().diff('id', props, props)

    assert result.changes is True


def test_a_device_that_already_agrees_is_no_change(device: Device) -> None:
    props = file_props()
    converged(device, props)

    result = file_provider().diff('id', props, props)

    assert result.changes is False


def test_a_file_someone_deleted_on_the_device_is_a_change(device: Device) -> None:
    props = file_props()

    result = file_provider().diff('id', props, props)

    assert result.changes is True


def test_the_same_mode_written_two_ways_is_not_drift(device: Device) -> None:
    """`stat` says `644` where the declaration says `0644`; they are one mode."""
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].mode = '644'

    result = file_provider().diff('id', props, props)

    assert result.changes is False


def test_ownership_the_device_does_not_have_is_drift(device: Device) -> None:
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].group = 'staff'

    result = file_provider().diff('id', props, props)

    assert result.changes is True


def test_moving_a_file_replaces_it_and_the_new_path_exists_before_the_old_one_goes(device: Device) -> None:
    olds = file_props()
    news = file_props(path='/data/frr/frr.conf.new')

    result = file_provider().diff('id', olds, news)

    assert result.replaces == ['path']
    assert result.delete_before_replace is False
    assert device.sessions == 0


def test_a_moved_dial_address_rewrites_the_file_and_deletes_nothing(device: Device) -> None:
    """The address is where the device answers, not which device it is.

    A first bring-up dials the gateway over the LAN and every later run dials it
    over the overlay (physical/gateway.md §2.5), and both reach the same box. A
    replacement would therefore write the file at the new address and then delete
    it at the old one — the same file, on the same device — leaving the device
    without it at the end of a run that reported success. It is an ordinary declared
    change instead: the same bytes are written again, through the new address.
    """
    olds = file_props()
    news = file_props(host='gateway.invalid')

    result = file_provider().diff('id', olds, news)

    assert result.replaces == []
    assert result.changes is True
    assert device.sessions == 0

    artifact = artifact_provider().diff('id', artifact_props(), artifact_props(host='gateway.invalid'))
    assert artifact.replaces == []
    assert artifact.changes is True


def test_a_rotated_credential_is_a_change_nobody_declared(device: Device) -> None:
    """The point of the session stamp: a rotation is a diff with no program in it.

    No caller mentions the credential, so the only thing that can carry a
    rotation into a preview is a property the provider adds to the checked
    inputs itself.
    """
    olds = checked(file_provider(), file_props())
    news = checked(file_provider(private_key()), file_props())

    result = file_provider().diff('id', olds, news)

    assert result.changes is True
    assert result.replaces == []
    assert device.sessions == 0


def test_the_session_stamp_names_the_endpoint_and_fingerprints_the_key() -> None:
    """`root@10.144.1.1:22#<12 hex>` — the door, and which key opens it.

    The digest is what a preview shows on a rotation, so it is stored in the
    clear: a truncated digest of a key is not the key, and a redacted one would
    say only that something opaque changed.
    """
    session = checked(file_provider(), file_props())[SESSION]
    endpoint, _, fingerprint = session.partition('#')

    assert endpoint == f'root@{HOST}:22'
    assert len(fingerprint) == FINGERPRINT_LENGTH
    assert set(fingerprint) <= set('0123456789abcdef')
    assert PRIVATE_KEY not in session
    assert fingerprint == hashlib.sha256(PRIVATE_KEY.encode()).hexdigest()[:FINGERPRINT_LENGTH]


def test_two_keys_are_two_fingerprints_and_one_key_is_always_the_same_one() -> None:
    rotated = private_key()

    first = checked(file_provider(), file_props())[SESSION]
    again = checked(file_provider(), file_props())[SESSION]
    after = checked(file_provider(rotated), file_props())[SESSION]

    assert first == again
    assert first != after


def test_a_change_to_this_module_is_a_change_a_reader_can_see(
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider is pickled by reference, so editing an operation moves nothing.

    The version constant is what makes such an edit an update instead of a
    silent no-op that leaves every resource's outputs as the old code left them.
    """
    shipped = provider.VERSION
    olds = checked(file_provider(), file_props())
    monkeypatch.setattr(provider, 'VERSION', f'{shipped}-next')
    news = checked(file_provider(), file_props())

    assert olds[PROVIDER_VERSION] == shipped
    assert news[PROVIDER_VERSION] == f'{shipped}-next'
    assert file_provider().diff('id', olds, news).changes is True
    assert device.sessions == 0


def test_a_declared_change_needs_no_look_at_the_device(device: Device) -> None:
    olds = file_props()
    news = file_props(content='router bgp 65001\n')

    result = file_provider().diff('id', olds, news)

    assert result.changes is True
    assert device.sessions == 0


def test_an_input_that_is_still_unknown_is_an_unknown_diff_and_touches_nothing(device: Device) -> None:
    """During a preview a hook may be another resource's unresolved output."""
    olds = file_props()
    news = file_props(hook=rpc.UNKNOWN)

    result = file_provider().diff('id', olds, news)

    assert result.changes is None
    assert device.sessions == 0


def test_an_unknown_path_is_never_read_as_a_replacement(device: Device) -> None:
    """A value nobody knows yet cannot have been shown to differ."""
    olds = file_props()
    news = file_props(path=rpc.UNKNOWN)

    result = file_provider().diff('id', olds, news)

    assert result.replaces in (None, [])
    assert result.changes is None


def test_a_stored_output_bag_wider_than_the_inputs_is_not_a_change(device: Device) -> None:
    """`diff` compares the keys it declares, and only those.

    Its `olds` is the stored output bag and its `news` the checked input bag, so
    a key some earlier operation returned is in `olds` and can never be in
    `news`. A provider comparing the bags wholesale would call that a change and
    report one on every single run.
    """
    props = checked(file_provider(), file_props())
    converged(device, props)
    olds = props | {'a_property_an_older_version_returned': 'and this one no longer does'}

    result = file_provider().diff('id', olds, props)

    assert result.changes is False


def test_a_rotation_restamps_the_resource_and_leaves_the_device_alone(device: Device) -> None:
    """The whole point of the requirement: a new key is not new content.

    Rewriting every file on the gateway because a credential was rotated is what
    this branch exists to prevent. The diff stays visible — the stamp is in the
    outputs the update returns — and the device is not written to, deleted from,
    or created on.
    """
    olds = checked(file_provider(), file_props())
    converged(device, olds)
    device.log.clear()
    rotated = file_provider(private_key())
    news = checked(rotated, file_props())

    result = rotated.update('id', olds, news)

    assert [line for line in device.log if line.startswith(('write', 'remove'))] == []
    assert device.files[CONFIG_PATH].data == CONFIG.encode()
    assert result.outs is not None
    assert result.outs[SESSION] == news[SESSION]
    assert result.outs[SESSION] != olds[SESSION]


def test_an_update_still_writes_when_the_device_is_the_reason_for_it(device: Device) -> None:
    """A rotation and a hand-edited file can arrive in the same run.

    `diff` reports one change either way, so the update asks the device again
    rather than assuming the stamp was the whole story — otherwise a run would
    report the file converged while the device kept the edit.
    """
    olds = checked(file_provider(), file_props())
    converged(device, olds)
    device.files[CONFIG_PATH].data = b'router bgp 65001\n'
    rotated = file_provider(private_key())
    news = checked(rotated, file_props())

    _ = rotated.update('id', olds, news)

    assert device.files[CONFIG_PATH].data == CONFIG.encode()


def test_an_update_that_changes_content_writes_without_consulting_the_device(device: Device) -> None:
    """A declared change is a change; the device has no say in it."""
    olds = checked(file_provider(), file_props())
    converged(device, olds)
    device.log.clear()
    news = checked(file_provider(), file_props(content='router bgp 65001\n'))

    _ = file_provider().update('id', olds, news)

    assert device.log == [f'write {CONFIG_PATH}', f'run {HOOK}']


def test_an_update_returns_the_bag_state_keeps(device: Device) -> None:
    """The outs replace the stored output bag, so the record stays current."""
    olds = checked(file_provider(), file_props())
    converged(device, olds)
    news = checked(file_provider(), file_props(content='router bgp 65001\n'))

    result = file_provider().update('id', olds, news)

    assert result.outs == news


def test_a_hook_that_refuses_fails_the_apply(device: Device) -> None:
    device.hook_status = 3

    with pytest.raises(provider.HookFailed) as raised:
        _ = file_provider().create(file_props())

    assert 'exited 3' in str(raised.value)


def test_a_file_with_no_hook_is_written_and_nothing_else_happens(device: Device) -> None:
    _ = file_provider().create(file_props(hook=None))

    assert device.commands == []
    assert CONFIG_PATH in device.files


def test_deleting_a_file_tells_the_device_it_is_gone(device: Device) -> None:
    """The reader of a file has to learn of its removal as it learned of its arrival."""
    props = file_props()
    converged(device, props)
    device.log.clear()

    file_provider().delete('id', props)

    assert CONFIG_PATH not in device.files
    assert device.log == [f'remove {CONFIG_PATH}', f'run {HOOK}']


def test_reading_reports_what_the_device_holds_rather_than_what_state_remembers(device: Device) -> None:
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].data = b'router bgp 65001\n'

    result = file_provider().read('id', props)

    assert result.outs is not None
    assert result.outs['content'] == 'router bgp 65001\n'
    assert result.id == 'id'


def test_reading_a_file_someone_deleted_drops_the_identifier(device: Device) -> None:
    """A dropped identifier is how the next up learns to create the file again."""
    result = file_provider().read('id', file_props())

    assert result.id is None
    # An empty bag, not `None`: the dynamic-provider host writes its own key
    # into whatever it is handed back, and `None` would fail there rather than
    # here.
    assert result.outs == {}


def test_two_reads_of_an_absent_file_do_not_share_one_bag(device: Device) -> None:
    first = file_provider().read('id', file_props())
    assert first.outs is not None
    first.outs['__provider'] = 'whatever the host writes here'

    second = file_provider().read('id', file_props())

    assert second.outs == {}


def test_a_relative_path_is_refused_before_anything_is_written() -> None:
    result = file_provider().check({}, file_props(path='data/frr/frr.conf'))

    assert [failure.property for failure in result.failures] == ['path']


def test_a_mode_that_is_not_octal_is_refused() -> None:
    result = file_provider().check({}, file_props(mode='rw-r--r--'))

    assert [failure.property for failure in result.failures] == ['mode']


def test_a_path_that_is_not_known_yet_is_not_refused() -> None:
    """A preview placeholder is not a validation failure."""
    result = file_provider().check({}, file_props(path=rpc.UNKNOWN))

    assert result.failures == []


##
## The directory resource
##


def test_a_created_directory_is_made_before_the_hook_that_is_told_about_it(device: Device) -> None:
    """Make, then notify — the order a file's write and hook are in."""
    made_command = provider.make_script(DIRECTORY_PATH, '0755', 'root:root')

    result = directory_provider().create(directory_props())

    assert device.log == [f'run {made_command}', f'run {DIRECTORY_HOOK}']
    assert result.id == DIRECTORY_PATH


def test_making_a_directory_sets_its_mode_and_ownership_in_one_idempotent_command() -> None:
    """`mkdir -p` accepts what is already there, so create and update are one script.

    The mode and the owner are set rather than compared, which is what converges
    a directory somebody chmodded on the device without replacing it.
    """
    script = provider.make_script(DIRECTORY_PATH, '0750', 'root:staff')

    assert script == f'mkdir -p {DIRECTORY_PATH} && chmod 0750 {DIRECTORY_PATH} && chown root:staff {DIRECTORY_PATH}'


def test_a_directory_with_no_declared_owner_keeps_whatever_the_device_gave_it() -> None:
    assert 'chown' not in provider.make_script(DIRECTORY_PATH, '0755', None)


def test_a_directory_path_a_shell_would_mangle_is_quoted() -> None:
    assert "mkdir -p '/data/a dir; rm -rf /'" in provider.make_script('/data/a dir; rm -rf /', '0755', None)
    assert "rmdir '/data/a dir; rm -rf /'" in provider.remove_script('/data/a dir; rm -rf /')


def test_a_directory_someone_removed_on_the_device_is_a_change(device: Device) -> None:
    """A directory taken away by hand, or by a firmware update, is work to do.

    The next preview reports it without anybody asking for a refresh, which is
    what makes the resource a statement about the device rather than a record of
    what was once pushed to it.
    """
    props = directory_props()

    result = directory_provider().diff('id', props, props)

    assert result.changes is True


def test_a_directory_the_device_already_has_is_no_change(device: Device) -> None:
    props = directory_props()
    made(device, props)

    result = directory_provider().diff('id', props, props)

    assert result.changes is False
    assert device.commands == []


def test_a_mode_the_device_does_not_have_is_drift(device: Device) -> None:
    props = directory_props()
    made(device, props)
    device.files[DIRECTORY_PATH].mode = '0700'

    assert directory_provider().diff('id', props, props).changes is True


def test_ownership_a_directory_does_not_have_is_drift(device: Device) -> None:
    props = directory_props()
    made(device, props)
    device.files[DIRECTORY_PATH].group = 'staff'

    assert directory_provider().diff('id', props, props).changes is True


def test_moving_a_directory_replaces_it_and_the_new_path_exists_before_the_old_one_goes(device: Device) -> None:
    """The path is the identity; the address it is reached at is not."""
    result = directory_provider().diff('id', directory_props(), directory_props(path='/data/custom/other'))

    assert result.replaces == ['path']
    assert result.delete_before_replace is False

    moved = directory_provider().diff('id', directory_props(), directory_props(host='gateway.invalid'))
    assert moved.replaces == []
    assert moved.changes is True
    assert device.sessions == 0


def test_a_declared_mode_change_is_converged_in_place(device: Device) -> None:
    """A mode is a value the device converges to, not a reason to make it again."""
    olds = checked(directory_provider(), directory_props())
    made(device, olds)
    news = checked(directory_provider(), directory_props(mode='0700'))
    device.log.clear()

    result = directory_provider().update('id', olds, news)

    assert device.commands == [provider.make_script(DIRECTORY_PATH, '0700', 'root:root'), DIRECTORY_HOOK]
    assert result.outs == news


def test_a_rotation_restamps_a_directory_and_leaves_the_device_alone(device: Device) -> None:
    """A new credential is not a new directory, and nothing is run because of one."""
    olds = checked(directory_provider(), directory_props())
    made(device, olds)
    device.log.clear()
    rotated = directory_provider(private_key())
    news = checked(rotated, directory_props())

    result = rotated.update('id', olds, news)

    assert device.commands == []
    assert result.outs is not None
    assert result.outs[SESSION] == news[SESSION]


def test_a_directory_update_still_runs_when_the_device_is_the_reason_for_it(device: Device) -> None:
    """A rotation and a directory somebody removed can arrive in the same run."""
    olds = checked(directory_provider(), directory_props())
    rotated = directory_provider(private_key())

    _ = rotated.update('id', olds, checked(rotated, directory_props()))

    assert device.commands == [provider.make_script(DIRECTORY_PATH, '0755', 'root:root'), DIRECTORY_HOOK]


def test_deleting_an_empty_directory_takes_it_away_and_says_so(device: Device) -> None:
    made(device, directory_props())
    device.log.clear()

    directory_provider().delete('id', directory_props())

    assert device.commands == [provider.remove_script(DIRECTORY_PATH), DIRECTORY_HOOK]


def test_deleting_a_directory_somebody_filled_is_refused(device: Device) -> None:
    """What is inside was never declared here, so it is not this resource's to delete.

    The removal is the one operation that could destroy state the device or a
    person put there, so a directory with anything in it fails the delete and
    names itself, rather than being emptied.
    """
    made(device, directory_props())
    device.statuses = {'rmdir': ssh.NOT_EMPTY}

    with pytest.raises(provider.DirectoryNotEmpty) as raised:
        directory_provider().delete('id', directory_props())

    assert DIRECTORY_PATH in str(raised.value)
    assert DIRECTORY_HOOK not in device.commands


def test_a_removal_the_device_refused_for_its_own_reason_is_not_read_as_content(device: Device) -> None:
    """An unwritable directory and a full one are two answers, and `rmdir` gives one.

    So the script says which it was with a status of its own, and anything else
    is reported as the device's failure with the device's own message.
    """
    made(device, directory_props())
    device.statuses = {'rmdir': 1}

    with pytest.raises(provider.RemoveDirectoryFailed) as raised:
        directory_provider().delete('id', directory_props())

    assert 'refused' in str(raised.value)


def test_a_removal_of_a_directory_that_is_already_gone_is_a_success() -> None:
    """Nothing to remove is the outcome a delete wanted, so the script exits 0."""
    script = provider.remove_script(DIRECTORY_PATH)

    assert script.startswith(f'if [ ! -e {DIRECTORY_PATH} ]; then exit 0; fi')
    assert f'exit {ssh.NOT_EMPTY}' in script
    assert script.endswith(f'rmdir {DIRECTORY_PATH}')
    assert 'rm -r' not in script


def test_only_a_directory_is_ever_called_not_empty() -> None:
    """`ls -A` on a regular file prints that file's own name.

    Without the kind test in front of it, a file left where a directory is
    declared would be refused as "not empty" -- a claim about contents a file
    does not have. It reaches `rmdir` instead, which says what is actually wrong
    with it.
    """
    script = provider.remove_script(DIRECTORY_PATH)

    assert f'if [ -d {DIRECTORY_PATH} ] && [ -n "$(ls -A {DIRECTORY_PATH})" ]' in script


def test_a_directory_that_could_not_be_made_fails_the_apply(device: Device) -> None:
    device.statuses = {'mkdir': 5}

    with pytest.raises(provider.MakeDirectoryFailed) as raised:
        _ = directory_provider().create(directory_props())

    assert 'exited 5' in str(raised.value)
    assert DIRECTORY_HOOK not in device.commands


def test_reading_a_directory_reports_the_shape_the_device_has(device: Device) -> None:
    props = directory_props()
    made(device, props)
    device.files[DIRECTORY_PATH].mode = '0700'

    result = directory_provider().read('id', props)

    assert result.outs is not None
    assert result.outs['mode'] == '0700'
    assert result.outs['owner'] == 'root:root'


def test_reading_a_directory_someone_removed_drops_the_identifier(device: Device) -> None:
    """Absence is a deleted resource, which is how the next up makes it again."""
    result = directory_provider().read('id', directory_props())

    assert result.id is None
    assert result.outs == {}


def test_a_file_left_where_the_directory_should_be_is_a_change(device: Device) -> None:
    """Existence is not the question; what is there is.

    The mode and the owner can agree by coincidence -- 0755 owned by root is
    what a script looks like too -- so a comparison that stopped at those would
    report a converged directory for a path holding a file, on every preview,
    for as long as nobody applied.
    """
    props = directory_props()
    made(device, props)
    device.files[DIRECTORY_PATH].kind = 'regular file'

    assert directory_provider().diff('id', props, props).changes is True


def test_reading_a_path_something_else_occupies_drops_the_identifier(device: Device) -> None:
    """A refresh records what the device can offer, and it is not this directory."""
    props = directory_props()
    made(device, props)
    device.files[DIRECTORY_PATH].kind = 'symbolic link'

    assert directory_provider().read('id', props).id is None


def test_a_relative_directory_is_refused_before_anything_is_made() -> None:
    result = directory_provider().check({}, directory_props(path='custom/machines'))

    assert [failure.property for failure in result.failures] == ['path']


def test_a_directory_mode_that_is_not_octal_is_refused() -> None:
    result = directory_provider().check({}, directory_props(mode='rwxr-xr-x'))

    assert [failure.property for failure in result.failures] == ['mode']


def test_an_unknown_input_on_a_directory_is_an_unknown_diff_and_touches_nothing(device: Device) -> None:
    result = directory_provider().diff('id', directory_props(), directory_props(hook=rpc.UNKNOWN))

    assert result.changes is None
    assert device.sessions == 0


def test_every_resource_of_this_module_carries_the_one_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """One constant, because the three kinds share the code a bump would announce.

    The session, the stamping, the hook and the transport are one implementation
    serving all three, so a change to any of them changes every resource's
    behavior -- and a second constant would have to be remembered twice, leaving
    one kind silent about a change the others reported.
    """
    monkeypatch.setattr(provider, 'VERSION', f'{provider.VERSION}-next')
    stamped = {
        checked(directory_provider(), directory_props())[PROVIDER_VERSION],
        checked(file_provider(), file_props())[PROVIDER_VERSION],
        checked(artifact_provider(), artifact_props())[PROVIDER_VERSION],
    }

    assert stamped == {provider.VERSION}


##
## The artifact resource
##


def test_the_device_pulls_the_pin_itself_and_the_marker_precedes_the_hook(device: Device) -> None:
    """The whole apply, in order, and every step of it a command the device ran.

    Nothing here streams a payload: the runner hands over a reference and the
    device does the fetching. The marker is written before the hook because the
    hook is what notices -- the gateway's hook restarts a container when a file
    it reads has changed, and the marker beside the tree is that file.
    """
    result = artifact_provider().create(artifact_props())

    assert device.log == [
        f'run {provider.pull_script(ROOTFS_REFERENCE, ROOTFS_TREE)}',
        f'run {provider.unpack_script(ROOTFS_TREE)}',
        f'write {provider.marker_path(ROOTFS_TREE)}',
        f'run {HOOK_COMMAND}',
    ]
    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'{ROOTFS_DIGEST}\n'.encode()
    assert result.id == ROOTFS_TREE


def test_one_session_carries_the_whole_push(device: Device) -> None:
    """The pull is a command on the far end, so it rides the session already open."""
    _ = artifact_provider().create(artifact_props())

    assert device.sessions == 1


def test_the_pull_names_the_digest_and_never_the_tag(device: Device) -> None:
    """A tag is a name somebody else can move, so it is declared and not resolved."""
    _ = artifact_provider().create(artifact_props())
    pull = device.commands[0]

    assert f'docker://{ROOTFS_REPOSITORY}@{ROOTFS_DIGEST}' in pull
    assert ROOTFS_TAG not in pull


def test_the_marker_names_the_pin_and_not_the_bytes_lying_beside_it(device: Device) -> None:
    """What the device holds is a tree; what it records is where the tree came from.

    The pin verifies the manifest and the manifest verifies the layers, all of
    it inside the device's own `skopeo`. So the marker is deliberately not a
    checksum of anything on the device -- it is a claim about provenance, and
    nothing on either end computes a digest to write it.
    """
    _ = artifact_provider().create(artifact_props(digest=ROOTFS_DIGEST))

    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'{ROOTFS_DIGEST}\n'.encode()
    assert not any('sha256sum' in command or 'cksum' in command for command in device.commands)


def test_a_pull_that_fails_leaves_the_live_tree_untouched(device: Device) -> None:
    """The registry is unreachable, or serves something else, or refuses: the
    container goes on running exactly what it was running."""
    landed(device, artifact_props(), digest=f'sha256:{"a" * 64}')
    device.refuse = (SKOPEO,)

    with pytest.raises(provider.PullFailed) as raised:
        _ = artifact_provider().create(artifact_props())

    assert ROOTFS_REFERENCE in str(raised.value)
    assert 'refused' in str(raised.value), 'the device said why, and the error carries it'
    assert device.files[ROOTFS_TREE].data == TREE_ENTRY
    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'sha256:{"a" * 64}\n'.encode()
    assert not any(UMOCI in command or HOOK_COMMAND in command for command in device.commands)


def test_an_unpack_that_fails_leaves_the_live_tree_untouched(device: Device) -> None:
    """The swap is the last thing the unpack does, so a failure before it is invisible
    to the running container, and the marker still names the pin it was unpacked from."""
    landed(device, artifact_props(), digest=f'sha256:{"a" * 64}')
    device.refuse = (UMOCI,)

    with pytest.raises(provider.UnpackFailed) as raised:
        _ = artifact_provider().create(artifact_props())

    assert ROOTFS_TREE in str(raised.value)
    assert 'refused' in str(raised.value)
    assert device.files[ROOTFS_TREE].data == TREE_ENTRY
    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'sha256:{"a" * 64}\n'.encode()
    assert HOOK_COMMAND not in device.commands


def test_a_hook_that_refuses_withdraws_the_marker_it_was_about_to_be_told_about(device: Device) -> None:
    """The marker has to precede the hook and must not survive one that failed.

    It is the claim that this tree came from this pin *and* that the device has
    acted on it. Leaving it behind would leave the next preview with nothing to
    notice, and a container running an old image nobody is told about.
    """
    device.refuse = (HOOK_COMMAND,)

    with pytest.raises(provider.HookFailed):
        _ = artifact_provider().create(artifact_props())

    assert provider.marker_path(ROOTFS_TREE) not in device.files
    assert device.log[-2:] == [
        f'run {HOOK_COMMAND}',
        f'remove {provider.marker_path(ROOTFS_TREE)}',
    ]


def test_a_hook_that_never_answers_withdraws_the_marker_as_well(device: Device) -> None:
    """A non-zero exit is not the only way a hook fails to converge.

    A hook that overruns the session's timeout, or a session that goes away
    under it, raises instead of reporting a status. A marker that survived that
    would make the next `diff` report a device that agrees, while the container
    goes on running a tree the marker no longer describes.
    """
    device.interrupt = (HOOK_COMMAND,)

    with pytest.raises(TimeoutError):
        _ = artifact_provider().create(artifact_props())

    assert provider.marker_path(ROOTFS_TREE) not in device.files


def test_a_marker_naming_the_pinned_digest_is_no_change(device: Device) -> None:
    """A preview compares two hashes; it does not pull or unpack anything."""
    props = artifact_props()
    landed(device, props)

    result = artifact_provider().diff('id', props, props)

    assert result.changes is False
    assert device.commands == []


def test_a_marker_naming_other_bytes_is_a_change(device: Device) -> None:
    props = artifact_props()
    landed(device, props)
    device.files[provider.marker_path(ROOTFS_TREE)].data = b'0' * 64 + b'\n'

    assert artifact_provider().diff('id', props, props).changes is True


def test_a_tree_with_no_marker_beside_it_is_a_change(device: Device) -> None:
    """A tree of unknown provenance is treated as a tree that is not there."""
    props = artifact_props()
    landed(device, props)
    del device.files[provider.marker_path(ROOTFS_TREE)]

    assert artifact_provider().diff('id', props, props).changes is True


def test_a_tree_someone_removed_is_a_change(device: Device) -> None:
    """A firmware update takes the tree and leaves the marker; both are asked about."""
    props = artifact_props()
    landed(device, props)
    del device.files[ROOTFS_TREE]

    assert artifact_provider().diff('id', props, props).changes is True


def test_a_new_pin_is_a_change_the_device_is_not_consulted_about(device: Device) -> None:
    olds = artifact_props()
    news = artifact_props(digest=f'sha256:{"b" * 64}')

    result = artifact_provider().diff('id', olds, news)

    assert result.changes is True
    assert device.sessions == 0


def test_a_rotation_restamps_an_artifact_without_pulling_it_again(device: Device) -> None:
    """Nothing is fetched because a credential changed.

    The marker on the device already names the pin, and neither end has a
    reason to go and get the image to find that out.
    """
    olds = checked(artifact_provider(), artifact_props())
    landed(device, olds)
    device.log.clear()
    rotated = artifact_provider(private_key())
    news = checked(rotated, artifact_props())

    result = rotated.update('id', olds, news)

    assert [line for line in device.log if line.startswith(('write', 'remove'))] == []
    assert not any(SKOPEO in command for command in device.commands)
    assert result.outs is not None
    assert result.outs[SESSION] == news[SESSION]


def test_an_artifact_update_still_pushes_when_the_marker_disagrees(device: Device) -> None:
    """A stamp does not excuse a device that no longer holds what it claimed."""
    olds = checked(artifact_provider(), artifact_props())
    landed(device, olds)
    del device.files[provider.marker_path(ROOTFS_TREE)]
    rotated = artifact_provider(private_key())

    _ = rotated.update('id', olds, checked(rotated, artifact_props()))

    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'{ROOTFS_DIGEST}\n'.encode()


def test_reading_an_artifact_reports_the_digest_the_device_claims(device: Device) -> None:
    props = artifact_props()
    landed(device, props, digest=f'sha256:{"d" * 64}')

    result = artifact_provider().read('id', props)

    assert result.outs is not None
    assert result.outs['digest'] == f'sha256:{"d" * 64}'


def test_reading_an_artifact_with_no_marker_drops_the_identifier(device: Device) -> None:
    props = artifact_props()
    landed(device, props)
    del device.files[provider.marker_path(ROOTFS_TREE)]

    assert artifact_provider().read('id', props).id is None


def test_reading_an_artifact_whose_tree_is_gone_drops_the_identifier(device: Device) -> None:
    """A refresh reports what the device can actually run, which is the tree."""
    props = artifact_props()
    landed(device, props)
    del device.files[ROOTFS_TREE]

    assert artifact_provider().read('id', props).id is None


def test_deleting_an_artifact_takes_the_tree_and_its_staging_with_it(device: Device) -> None:
    """Derived state the push created is state the push removes, leftovers of an
    interrupted pull or unpack included."""
    props = artifact_props()
    landed(device, props)
    device.log.clear()

    artifact_provider().delete('id', props)

    assert device.log == [
        f'remove {provider.marker_path(ROOTFS_TREE)}',
        f'run {provider.purge_script(ROOTFS_TREE)}',
        f'run {HOOK_COMMAND}',
    ]
    purge = provider.purge_script(ROOTFS_TREE)
    assert ROOTFS_TREE in purge
    assert f'{ROOTFS_TREE}{provider.SUPERSEDED_SUFFIX}' in purge
    assert f'{ROOTFS_TREE}{provider.UNPACKING_SUFFIX}' in purge
    assert f'{ROOTFS_TREE}{provider.LAYOUT_SUFFIX}' in purge


@pytest.mark.parametrize(
    'value',
    ['deadbeef', 'e' * 64, f'sha256:{"E" * 64}', f'sha512:{"e" * 64}'],
    ids=['too short', 'unqualified', 'upper case', 'another algorithm'],
)
def test_a_digest_that_is_not_a_registry_digest_is_refused(value: str) -> None:
    """The marker on the device is compared byte for byte, so the spelling is the pin."""
    result = artifact_provider().check({}, artifact_props(digest=value))

    assert [failure.property for failure in result.failures] == ['digest']


def test_a_relative_tree_is_refused_before_anything_is_pushed() -> None:
    result = artifact_provider().check({}, artifact_props(root='services/roots/adguard'))

    assert [failure.property for failure in result.failures] == ['root']


@pytest.mark.parametrize(
    'value',
    ['alpine', 'library/alpine', 'installation/adguard'],
    ids=['bare name', 'namespaced name', 'two components, no host'],
)
def test_a_repository_that_does_not_name_its_registry_is_refused(value: str) -> None:
    """The device resolves the reference, so an unqualified one would be
    resolved against whatever default that device is configured with -- which is
    exactly the decision a pin exists to take away from it."""
    result = artifact_provider().check({}, artifact_props(repository=value))

    assert [failure.property for failure in result.failures] == ['repository']


def test_a_repository_on_a_registry_with_a_port_is_accepted() -> None:
    result = artifact_provider().check({}, artifact_props(repository='registry.invalid:5000/installation/adguard'))

    assert result.failures == []


##
## The commands the device runs
##


def test_the_pull_clears_the_last_push_before_it_asks_for_room() -> None:
    """Three siblings can be lying there: the tree the last swap displaced, a
    staging tree an interrupted unpack left, and a layout an interrupted pull
    left. Clearing them first is what keeps a bumped pin from needing room for
    every copy at once."""
    script = provider.pull_script(ROOTFS_REFERENCE, ROOTFS_TREE)
    layout = f'{ROOTFS_TREE}{provider.LAYOUT_SUFFIX}'
    staging = f'{ROOTFS_TREE}{provider.UNPACKING_SUFFIX}'
    superseded = f'{ROOTFS_TREE}{provider.SUPERSEDED_SUFFIX}'

    assert script.startswith(f'rm -rf {layout} {staging} {superseded}')
    assert f'mkdir -p {layout}' in script, 'which is also how the directory above the tree gets made'
    assert f'skopeo copy --quiet docker://{ROOTFS_REFERENCE} oci:{layout}:{provider.LAYOUT_TAG}' in script


def test_the_unpack_is_two_renames_so_a_half_unpacked_root_never_boots() -> None:
    """A container's root is either the previous tree or the new one.

    What the swap displaced is left beside it: at this point the container is
    still running on those files, because the hook that restarts it has not run
    yet. Clearing them is the next push's first act.
    """
    script = provider.unpack_script(ROOTFS_TREE)
    layout = f'{ROOTFS_TREE}{provider.LAYOUT_SUFFIX}'
    staging = f'{ROOTFS_TREE}{provider.UNPACKING_SUFFIX}'
    superseded = f'{ROOTFS_TREE}{provider.SUPERSEDED_SUFFIX}'

    assert script.startswith(f'umoci raw unpack --image {layout}:{provider.LAYOUT_TAG} {staging}')
    assert f'mv {ROOTFS_TREE} {superseded}' in script
    assert script.endswith(f'mv {staging} {ROOTFS_TREE}')
    assert f'rm -rf {ROOTFS_TREE} ' not in script, 'the live tree is renamed, never deleted in place'


def test_the_staging_layout_goes_as_soon_as_the_unpack_has_read_it() -> None:
    """The image is on the device in two forms until this runs, and in one after it."""
    script = provider.unpack_script(ROOTFS_TREE)
    layout = f'{ROOTFS_TREE}{provider.LAYOUT_SUFFIX}'
    staging = f'{ROOTFS_TREE}{provider.UNPACKING_SUFFIX}'

    assert script.index(f'rm -rf {layout}') > script.index('umoci raw unpack')
    assert script.index(f'rm -rf {layout}') < script.index(f'mv {staging} {ROOTFS_TREE}')


def test_a_path_a_shell_would_mangle_is_quoted_in_the_pull_and_the_unpack() -> None:
    """Both commands take the tree's path, so both quote it."""
    pull = provider.pull_script(ROOTFS_REFERENCE, '/data/roots/a tree')
    unpack = provider.unpack_script('/data/roots/a tree')

    assert "'/data/roots/a tree.kluster-oci'" in pull
    assert "'oci:/data/roots/a tree.kluster-oci:pinned'" in pull
    assert "'/data/roots/a tree.kluster-oci:pinned'" in unpack
    assert "'/data/roots/a tree'" in unpack


##
## What the engine is handed
##


def test_only_a_files_content_is_ever_kept_out_of_plain_state() -> None:
    """The provider echoes its inputs back, so a secret one has to be marked.

    There is one, and it is a file's content. The credential is not a property
    of any resource, and nothing about the connection is marked either: the pin
    is a public key, and a pin a reviewer can read is worth more than a
    redacted one, so it reaches the engine in the clear from a constant the
    program holds (`conventions.gateway.HOST_KEY`).
    """
    assert provider.secret_outputs() == []
    assert provider.secret_outputs(secret_content=True) == ['content']
    assert 'host_key' not in provider.secret_outputs(secret_content=True)


def test_a_connection_hands_every_resource_the_same_four_properties() -> None:
    """Where the device answers, as whom, and which key it must present."""
    connection = provider.Connection(host=HOST, host_key=HOST_KEY)

    assert connection.props() == {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'host_key': HOST_KEY,
    }


def test_what_lands_in_state_is_a_provider_with_nothing_in_it() -> None:
    """Every resource stores a pickle of its provider; this one is a name.

    Serialized through the engine's own function, so what is asserted is what a
    `__provider` property would actually hold. A class imported from a module is
    pickled by reference and `__getstate__` returns an empty bag, so state
    carries something inert: identical for every resource, identical across a
    rotation, and holding nothing that a rotation would have to reach into.
    """
    one = serialize_provider(file_provider())
    rotated = serialize_provider(file_provider(private_key()))

    assert file_provider().__getstate__() == {}
    assert one == rotated
    assert PRIVATE_KEY not in one
    assert len(one) < 256


def test_a_provider_that_was_never_configured_has_no_credential_to_dial_with() -> None:
    """The attribute exists only after `configure`, and that is the design.

    A default would not make an unconfigured provider safe; it would make one
    that dials with the wrong key. The plugin configures before the first
    operation, so nothing in production sees this state.
    """
    with pytest.raises(AttributeError):
        _ = provider.DeviceFileProvider().private_key


def test_a_missing_credential_refuses_by_name() -> None:
    """A half-filled configuration stops the run rather than the session."""
    with pytest.raises(ValueError, match=provider.PRIVATE_KEY_CONFIG):
        provider.DeviceFileProvider().configure(
            dynamic.ConfigureRequest(config=dynamic.Config({}, PROJECT)),
        )


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> Recorder:
    """One of each resource, declared the way the gateway's services declare them."""
    connection = provider.Connection(host=HOST, host_key=HOST_KEY)

    monitor = await run_with(Recorder(), stack='physical')
    async with declaring():
        _ = provider.DeviceFile('frr', connection=connection, path=CONFIG_PATH, content=CONFIG, hook=HOOK)
        _ = provider.DeviceDirectory('machines', connection=connection, path=DIRECTORY_PATH)
        _ = provider.DeviceArtifact(
            'adguard-rootfs',
            connection=connection,
            repository=ROOTFS_REPOSITORY,
            tag=ROOTFS_TAG,
            digest=ROOTFS_DIGEST,
            root=ROOTFS_TREE,
        )
    return monitor


def test_a_declared_file_carries_the_connection_and_the_file_into_one_property_bag(stack: Recorder) -> None:
    """Declaring the resource is also what pickles the provider into state; a
    provider that could not be serialized would fail here rather than at the
    first `pulumi up`."""
    declaration = stack.one('frr')
    typ, inputs = declaration.typ, declaration.inputs

    assert typ == 'pulumi-python:dynamic/device:File'
    assert inputs['host'] == HOST
    assert inputs['host_key'] == HOST_KEY
    assert inputs['path'] == CONFIG_PATH
    assert inputs['content'] == CONFIG
    assert inputs['hook'] == HOOK
    # What the caller declares, and no more: the credential is the provider's,
    # and the two stamps are added by `check` in the plugin's process.
    assert 'private_key' not in inputs
    assert SESSION not in inputs
    assert PROVIDER_VERSION not in inputs


def test_a_declared_directory_carries_a_shape_and_nothing_about_its_contents(stack: Recorder) -> None:
    """A resource kind of its own, so a preview shows a directory as a directory.

    The mode a caller leaves out is the one a directory has to have to be entered
    at all, and the contents are the caller's business rather than an input.
    """
    declaration = stack.one('machines')
    typ, inputs = declaration.typ, declaration.inputs

    assert typ == 'pulumi-python:dynamic/device:Directory'
    assert inputs['path'] == DIRECTORY_PATH
    assert inputs['mode'] == '0755'
    assert 'content' not in inputs


def test_a_declared_artifact_carries_its_pin_and_never_its_bytes(stack: Recorder) -> None:
    declaration = stack.one('adguard-rootfs')
    typ, inputs = declaration.typ, declaration.inputs

    assert typ == 'pulumi-python:dynamic/device:Artifact'
    assert inputs['repository'] == ROOTFS_REPOSITORY
    assert inputs['tag'] == ROOTFS_TAG
    assert inputs['digest'] == ROOTFS_DIGEST
    assert inputs['root'] == ROOTFS_TREE
    assert 'content' not in inputs


##
## The transport itself
##


@final
class Connection:
    """An `ssh.Runner` that answers from a script and remembers what it was asked."""

    def __init__(self, *answers: asyncssh.SSHCompletedProcess) -> None:
        self.answers: list[asyncssh.SSHCompletedProcess] = list(answers)
        self.commands: list[str] = []
        self.inputs: list[bytes] = []

    async def run(
        self,
        command: str,
        *,
        input: bytes,  # noqa: A002 -- asyncssh's parameter name
        encoding: None,
        check: bool,
        timeout: float,
    ) -> asyncssh.SSHCompletedProcess:
        assert encoding is None, 'the channel has to stay in bytes: a payload is not text'
        assert check is False, 'the transport reads exit statuses itself'
        assert timeout > 0
        self.commands.append(command)
        self.inputs.append(input)
        return self.answers.pop(0)


def answer(*, exit_status: int | None = 0, stdout: bytes = b'', stderr: bytes = b'') -> asyncssh.SSHCompletedProcess:
    return asyncssh.SSHCompletedProcess(exit_status=exit_status, stdout=stdout, stderr=stderr)


def transport(*answers: asyncssh.SSHCompletedProcess) -> tuple[ssh.SshTransport, Connection]:
    connection = Connection(*answers)
    return ssh.SshTransport(connection), connection


def test_the_pin_is_a_parsed_key_because_a_string_would_name_a_file_to_read() -> None:
    """asyncssh reads a string in this position as the path of a file to open,
    so a pin handed over as `ssh-ed25519 AAAA…` text would pin nothing at all.
    What the matcher concludes is asserted, not what it was handed."""
    trusted, _, _, _, _, _, _ = match_known_hosts(ssh.pinned_host_keys(HOST_KEY), HOST, HOST, 22)

    assert [key.export_public_key('openssh').decode().strip() for key in trusted] == [HOST_KEY]


def test_the_pin_matches_the_device_at_whatever_address_the_session_dials() -> None:
    """One key, no host name in front of it — so the address may change.

    The pin is a bare `ssh-ed25519 <blob>` line rather than a `known_hosts`
    entry, and a matcher given a parsed key applies it to whatever host it is
    asked about. That is what lets the same configured value serve a session
    dialled at the device's overlay address and one dialled at a LAN name during
    first bring-up (`stacks/physical.py`, `gatewayBootstrapHost`): the device
    presents the same key either way, and nothing in the pin disagrees.
    """
    lan = 'gateway.invalid'

    over_overlay, _, _, _, _, _, _ = match_known_hosts(ssh.pinned_host_keys(HOST_KEY), HOST, HOST, 22)
    over_lan, _, _, _, _, _, _ = match_known_hosts(ssh.pinned_host_keys(HOST_KEY), lan, lan, 22)

    exported = [
        [key.export_public_key('openssh').decode().strip() for key in keys] for keys in (over_overlay, over_lan)
    ]
    assert exported == [[HOST_KEY], [HOST_KEY]]


def test_nothing_may_vouch_for_a_substitute_key() -> None:
    """Empty authority and revocation lists are the point, not an omission."""
    _, authorities, revoked, *certificates = match_known_hosts(ssh.pinned_host_keys(HOST_KEY), HOST, HOST, 22)

    assert list(authorities) == []
    assert list(revoked) == []
    assert [list(entry) for entry in certificates] == [[], [], [], []]


def test_an_absent_pin_refuses_to_open_a_session() -> None:
    with pytest.raises(ssh.PinRejected):
        _ = ssh.pinned_host_keys('   ')


def test_a_pin_that_is_not_a_public_key_refuses_to_open_a_session() -> None:
    with pytest.raises(ssh.PinRejected):
        _ = ssh.pinned_host_keys('ssh-ed25519 not-really-a-key')


def test_a_client_credential_that_is_not_a_key_is_refused_before_the_handshake() -> None:
    with pytest.raises(ssh.PinRejected):
        _ = ssh.client_credential('-----BEGIN OPENSSH PRIVATE KEY-----\nnope\n')


def test_a_write_is_staged_and_moved_into_place() -> None:
    """An interrupted write leaves the previous file whole."""
    device, connection = transport(answer())

    asyncio.run(device.write('/data/frr/frr.conf', b'hello', mode='0640', owner='root:root'))

    assert connection.commands == [
        'mkdir -p /data/frr'
        ' && cat > /data/frr/frr.conf.kluster-staged'
        ' && chmod 0640 /data/frr/frr.conf.kluster-staged'
        ' && chown root:root /data/frr/frr.conf.kluster-staged'
        ' && mv -f /data/frr/frr.conf.kluster-staged /data/frr/frr.conf'
    ]
    assert connection.inputs == [b'hello']


def test_a_write_with_no_declared_owner_leaves_ownership_alone() -> None:
    device, connection = transport(answer())

    asyncio.run(device.write('/data/x', b'hello', mode='0644', owner=None))

    assert 'chown' not in connection.commands[0]


def test_a_path_a_shell_would_mangle_is_quoted() -> None:
    device, connection = transport(answer())

    asyncio.run(device.remove('/data/a file; rm -rf /'))

    assert connection.commands == ["rm -f '/data/a file; rm -rf /'"]


def test_an_absent_file_reads_as_absent_rather_than_as_a_fault() -> None:
    """`cat` exits 1 both for a missing file and an unreadable one; only one of
    those is a resource to create, so the shell distinguishes them itself."""
    device, _ = transport(answer(exit_status=ssh.ABSENT))

    assert asyncio.run(device.read('/data/frr/frr.conf')) is None


def test_a_read_that_actually_failed_is_a_fault() -> None:
    device, _ = transport(answer(exit_status=1, stderr=b'Permission denied'))

    with pytest.raises(ssh.CommandFailed) as raised:
        _ = asyncio.run(device.read('/data/frr/frr.conf'))

    assert 'Permission denied' in str(raised.value)


def test_a_stat_is_read_as_owner_group_mode_size_and_kind() -> None:
    device, connection = transport(answer(stdout=b'root staff 640 12 regular empty file\n'))

    assert asyncio.run(device.stat('/data/x')) == ssh.FileStat(
        owner='root', group='staff', mode='640', size=12, kind='regular empty file'
    )
    assert "stat -c '%U %G %a %s %F' /data/x" in connection.commands[0]


def test_the_kind_takes_the_rest_of_the_line_because_it_is_the_field_with_spaces() -> None:
    """`%F` says `regular empty file`, and a split into fixed fields would lose it."""
    device, _ = transport(answer(stdout=b'root root 755 4096 directory\n'))
    stat = asyncio.run(device.stat('/data/custom/dpkg'))

    assert stat is not None
    assert stat.kind == ssh.DIRECTORY
    assert stat.is_directory


def test_an_unreadable_stat_is_a_fault_rather_than_a_guess() -> None:
    device, _ = transport(answer(stdout=b'root\n'))

    with pytest.raises(ssh.CommandFailed):
        _ = asyncio.run(device.stat('/data/x'))


def test_a_command_killed_by_a_signal_reports_a_failure() -> None:
    """A signalled process has no exit status; reading that as success is the
    one wrong answer available."""
    device, _ = transport(answer(exit_status=None))

    assert asyncio.run(device.run('true')).ok is False


def test_modes_written_two_ways_compare_equal() -> None:
    assert ssh.same_mode('0644', '644')
    assert not ssh.same_mode('0644', '0640')
    assert not ssh.same_mode('rw-', 'rwx')


def test_ownership_is_compared_only_as_far_as_it_was_declared() -> None:
    stat = ssh.FileStat(owner='root', group='staff', mode='0644', size=0, kind='regular file')

    assert ssh.same_owner(None, stat)
    assert ssh.same_owner('root', stat)
    assert not ssh.same_owner('root:root', stat)
    assert ssh.same_owner('root:staff', stat)
