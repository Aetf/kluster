"""The gw-config provider: convergence against a device that is not there.

Every test below runs the provider's real code against a fake device. What is
doubled is the wire -- the SSH session, and the HTTP fetch of an artifact -- and
nothing above it: the shell one-liners, the quoting, the digest arithmetic, the
key parsing and the diff logic are the shipped ones. No test opens a socket.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast, final

import asyncssh
import pulumi
import pytest
import pytest_asyncio
import zstandard
from asyncssh.known_hosts import match_known_hosts
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pulumi.runtime import rpc
from pulumi.runtime.stack import wait_for_rpcs

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

ROOTFS_URL = 'https://example.invalid/rootfs/adguard-1.2.3.tar'
ROOTFS = b'a root filesystem, in miniature'
ROOTFS_SHA256 = hashlib.sha256(ROOTFS).hexdigest()
ROOTFS_TARGET = '/data/services/images/adguard.tar'
ROOTFS_TREE = '/data/services/roots/adguard'

#: The same payload as it is actually published: a zstd-compressed archive. The
#: pin is the digest of *these* bytes, which is the whole point of the test that
#: follows the pin through decompression.
ROOTFS_ZST = zstandard.ZstdCompressor().compress(ROOTFS)
ROOTFS_ZST_SHA256 = hashlib.sha256(ROOTFS_ZST).hexdigest()
ROOTFS_ZST_URL = 'https://example.invalid/rootfs/adguard-1.2.3.tar.zst'

#: Every resource the declaration fixture registered: type, name, inputs.
declared: list[tuple[str, str, dict[str, Any]]] = []


@final
@dataclass
class Entry:
    """A file as the device holds it."""

    data: bytes
    mode: str = '0644'
    owner: str = 'root'
    group: str = 'root'


@final
@dataclass
class Device:
    """A device with files on it, and a memory of what it was asked to do.

    `log` is the whole point: several of the design's guarantees are about
    *order* -- a hook that runs after the payload and before the marker -- and an
    unordered set of assertions cannot see them.
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
        return ssh.FileStat(owner=entry.owner, group=entry.group, mode=entry.mode, size=len(entry.data))

    async def write(self, path: str, data: bytes, *, mode: str, owner: str | None) -> None:
        user, _, group = (owner or 'root:root').partition(':')
        self.device.files[path] = Entry(data=data, mode=mode, owner=user, group=group or 'root')
        self.device.log.append(f'write {path}')

    async def remove(self, path: str) -> None:
        _ = self.device.files.pop(path, None)
        self.device.log.append(f'remove {path}')

    async def run(self, command: str) -> ssh.CommandResult:
        self.device.log.append(f'run {command}')
        refused = any(fragment in command for fragment in self.device.refuse)
        status = 1 if refused else self.device.hook_status
        return ssh.CommandResult(exit_status=status, stdout=b'', stderr='refused')


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> Device:
    """A device the provider reaches instead of a real one."""
    fake = Device()
    monkeypatch.setattr(provider, 'open_transport', fake.open)
    return fake


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """What the runner gets when it fetches a URL."""
    served = {ROOTFS_URL: ROOTFS, ROOTFS_ZST_URL: ROOTFS_ZST}

    def fetch(url: str) -> bytes:
        return served[url]

    monkeypatch.setattr(provider, 'fetch', fetch)
    return served


def file_props(**overrides: Any) -> dict[str, Any]:
    return {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'private_key': PRIVATE_KEY,
        'host_key': HOST_KEY,
        'path': CONFIG_PATH,
        'content': CONFIG,
        'mode': '0644',
        'owner': 'root:root',
        'hook': HOOK,
    } | overrides


def artifact_props(**overrides: Any) -> dict[str, Any]:
    return {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'private_key': PRIVATE_KEY,
        'host_key': HOST_KEY,
        'url': ROOTFS_URL,
        'sha256': ROOTFS_SHA256,
        'target': ROOTFS_TARGET,
        'extract': None,
        'mode': '0600',
        'owner': 'root:root',
        'hook': 'systemctl restart adguard',
    } | overrides


def unpacking_props(**overrides: Any) -> dict[str, Any]:
    """An artifact as a container service declares one: an archive and a tree."""
    return artifact_props(url=ROOTFS_ZST_URL, sha256=ROOTFS_ZST_SHA256, extract=ROOTFS_TREE) | overrides


def converged(device: Device, props: Mapping[str, Any]) -> None:
    """Put the device in the state the property bag declares."""
    user, _, group = str(props['owner']).partition(':')
    device.files[str(props['path'])] = Entry(
        data=str(props['content']).encode(),
        mode=str(props['mode']),
        owner=user,
        group=group,
    )


def landed(device: Device, props: Mapping[str, Any], payload: bytes = ROOTFS, digest: str | None = None) -> None:
    """Put the device in the state an artifact bag declares, markers and all.

    `digest` is what the markers claim, which is the digest of the *published*
    artifact and therefore not the digest of the payload lying on the device
    once that artifact was decompressed on the way there.
    """
    user, _, group = str(props['owner']).partition(':')
    target = str(props['target'])
    claimed = digest if digest is not None else hashlib.sha256(payload).hexdigest()
    device.files[target] = Entry(data=payload, mode=str(props['mode']), owner=user, group=group)
    device.files[provider.marker_path(target)] = Entry(data=f'{claimed}\n'.encode())
    tree = props.get('extract')
    if tree:
        device.files[str(tree)] = Entry(data=b'a directory, as far as `stat` is concerned')
        device.files[provider.marker_path(str(tree))] = Entry(data=f'{claimed}\n'.encode())


##
## The file resource
##


def test_a_created_file_lands_before_the_hook_that_makes_it_take_effect(device: Device) -> None:
    """Write, then notify. A hook that ran first would act on the old file."""
    props = file_props()

    result = provider.DeviceFileProvider().create(props)

    assert device.files[CONFIG_PATH] == Entry(data=CONFIG.encode(), mode='0644', owner='root', group='root')
    assert device.log == [f'write {CONFIG_PATH}', f'run {HOOK}']
    assert result.id == f'root@{HOST}:22{CONFIG_PATH}'


def test_a_created_file_carries_the_declared_credentials_to_the_session(device: Device) -> None:
    """The pin and the client key are resource inputs, not runner ambient state."""
    _ = provider.DeviceFileProvider().create(file_props())

    assert device.devices == [
        ssh.Device(host=HOST, username='root', private_key=PRIVATE_KEY, host_key=HOST_KEY, port=22)
    ]


def test_a_file_edited_on_the_device_is_a_change_without_anyone_asking_for_a_refresh(device: Device) -> None:
    """This is the whole reason `diff` opens a session instead of reading state."""
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].data = b'router bgp 65001\n'

    result = provider.DeviceFileProvider().diff('id', props, props)

    assert result.changes is True


def test_a_device_that_already_agrees_is_no_change(device: Device) -> None:
    props = file_props()
    converged(device, props)

    result = provider.DeviceFileProvider().diff('id', props, props)

    assert result.changes is False


def test_a_file_someone_deleted_on_the_device_is_a_change(device: Device) -> None:
    props = file_props()

    result = provider.DeviceFileProvider().diff('id', props, props)

    assert result.changes is True


def test_the_same_mode_written_two_ways_is_not_drift(device: Device) -> None:
    """`stat` says `644` where the declaration says `0644`; they are one mode."""
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].mode = '644'

    result = provider.DeviceFileProvider().diff('id', props, props)

    assert result.changes is False


def test_ownership_the_device_does_not_have_is_drift(device: Device) -> None:
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].group = 'staff'

    result = provider.DeviceFileProvider().diff('id', props, props)

    assert result.changes is True


def test_moving_a_file_replaces_it_and_the_new_path_exists_before_the_old_one_goes(device: Device) -> None:
    olds = file_props()
    news = file_props(path='/data/frr/frr.conf.new')

    result = provider.DeviceFileProvider().diff('id', olds, news)

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

    result = provider.DeviceFileProvider().diff('id', olds, news)

    assert result.replaces == []
    assert result.changes is True
    assert device.sessions == 0

    artifact = provider.DeviceArtifactProvider().diff('id', artifact_props(), artifact_props(host='gateway.invalid'))
    assert artifact.replaces == []
    assert artifact.changes is True


def test_a_rotated_credential_is_a_change_so_state_stops_naming_the_retired_key(device: Device) -> None:
    """Otherwise the eventual `delete` authenticates with a key that is gone."""
    olds = file_props()
    news = file_props(private_key=private_key())

    result = provider.DeviceFileProvider().diff('id', olds, news)

    assert result.changes is True
    assert result.replaces == []
    assert device.sessions == 0


def test_a_declared_change_needs_no_look_at_the_device(device: Device) -> None:
    olds = file_props()
    news = file_props(content='router bgp 65001\n')

    result = provider.DeviceFileProvider().diff('id', olds, news)

    assert result.changes is True
    assert device.sessions == 0


def test_an_input_that_is_still_unknown_is_an_unknown_diff_and_touches_nothing(device: Device) -> None:
    """During a preview a hook may be another resource's unresolved output."""
    olds = file_props()
    news = file_props(hook=rpc.UNKNOWN)

    result = provider.DeviceFileProvider().diff('id', olds, news)

    assert result.changes is None
    assert device.sessions == 0


def test_an_unknown_path_is_never_read_as_a_replacement(device: Device) -> None:
    """A value nobody knows yet cannot have been shown to differ."""
    olds = file_props()
    news = file_props(path=rpc.UNKNOWN)

    result = provider.DeviceFileProvider().diff('id', olds, news)

    assert result.replaces in (None, [])
    assert result.changes is None


def test_a_hook_that_refuses_fails_the_apply(device: Device) -> None:
    device.hook_status = 3

    with pytest.raises(provider.HookFailed) as raised:
        _ = provider.DeviceFileProvider().create(file_props())

    assert 'exited 3' in str(raised.value)


def test_a_file_with_no_hook_is_written_and_nothing_else_happens(device: Device) -> None:
    _ = provider.DeviceFileProvider().create(file_props(hook=None))

    assert device.commands == []
    assert CONFIG_PATH in device.files


def test_deleting_a_file_tells_the_device_it_is_gone(device: Device) -> None:
    """The reader of a file has to learn of its removal as it learned of its arrival."""
    props = file_props()
    converged(device, props)
    device.log.clear()

    provider.DeviceFileProvider().delete('id', props)

    assert CONFIG_PATH not in device.files
    assert device.log == [f'remove {CONFIG_PATH}', f'run {HOOK}']


def test_reading_reports_what_the_device_holds_rather_than_what_state_remembers(device: Device) -> None:
    props = file_props()
    converged(device, props)
    device.files[CONFIG_PATH].data = b'router bgp 65001\n'

    result = provider.DeviceFileProvider().read('id', props)

    assert result.outs is not None
    assert result.outs['content'] == 'router bgp 65001\n'
    assert result.id == 'id'


def test_reading_a_file_someone_deleted_drops_the_identifier(device: Device) -> None:
    """A dropped identifier is how the next up learns to create the file again."""
    result = provider.DeviceFileProvider().read('id', file_props())

    assert result.id is None
    # An empty bag, not `None`: the dynamic-provider host writes its own key
    # into whatever it is handed back, and `None` would fail there rather than
    # here.
    assert result.outs == {}


def test_two_reads_of_an_absent_file_do_not_share_one_bag(device: Device) -> None:
    first = provider.DeviceFileProvider().read('id', file_props())
    assert first.outs is not None
    first.outs['__provider'] = 'whatever the host writes here'

    second = provider.DeviceFileProvider().read('id', file_props())

    assert second.outs == {}


def test_a_relative_path_is_refused_before_anything_is_written() -> None:
    result = provider.DeviceFileProvider().check({}, file_props(path='data/frr/frr.conf'))

    assert [failure.property for failure in result.failures] == ['path']


def test_a_mode_that_is_not_octal_is_refused() -> None:
    result = provider.DeviceFileProvider().check({}, file_props(mode='rw-r--r--'))

    assert [failure.property for failure in result.failures] == ['mode']


def test_a_path_that_is_not_known_yet_is_not_refused() -> None:
    """A preview placeholder is not a validation failure."""
    result = provider.DeviceFileProvider().check({}, file_props(path=rpc.UNKNOWN))

    assert result.failures == []


##
## The artifact resource
##


def test_an_artifact_lands_then_the_hook_runs_then_the_marker_is_written(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """The marker is a claim, so it is made only once the claim is true."""
    assert downloads
    result = provider.DeviceArtifactProvider().create(artifact_props())

    assert device.log == [
        f'write {ROOTFS_TARGET}',
        'run systemctl restart adguard',
        f'write {provider.marker_path(ROOTFS_TARGET)}',
    ]
    assert device.files[ROOTFS_TARGET].data == ROOTFS
    assert device.files[provider.marker_path(ROOTFS_TARGET)].data == f'{ROOTFS_SHA256}\n'.encode()
    assert result.id == f'root@{HOST}:22{ROOTFS_TARGET}'


def test_bytes_that_do_not_match_their_pin_never_reach_the_device(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """Verification precedes the session, so a substituted release cannot land."""
    downloads[ROOTFS_URL] = b'something else entirely'

    with pytest.raises(provider.DigestMismatch) as raised:
        _ = provider.DeviceArtifactProvider().create(artifact_props())

    assert ROOTFS_SHA256 in str(raised.value)
    assert device.sessions == 0
    assert device.files == {}


def test_a_marker_naming_the_pinned_digest_is_no_change(device: Device) -> None:
    """A preview compares two hashes; it does not fetch or stream the payload."""
    props = artifact_props()
    landed(device, props)

    result = provider.DeviceArtifactProvider().diff('id', props, props)

    assert result.changes is False


def test_a_marker_naming_other_bytes_is_a_change(device: Device) -> None:
    props = artifact_props()
    landed(device, props)
    device.files[provider.marker_path(ROOTFS_TARGET)].data = b'0' * 64 + b'\n'

    result = provider.DeviceArtifactProvider().diff('id', props, props)

    assert result.changes is True


def test_a_payload_with_no_marker_beside_it_is_a_change(device: Device) -> None:
    """Bytes of unknown provenance are treated as bytes that are not there."""
    props = artifact_props()
    landed(device, props)
    del device.files[provider.marker_path(ROOTFS_TARGET)]

    result = provider.DeviceArtifactProvider().diff('id', props, props)

    assert result.changes is True


def test_a_new_pin_is_a_change_the_device_is_not_consulted_about(device: Device) -> None:
    olds = artifact_props()
    news = artifact_props(sha256='b' * 64)

    result = provider.DeviceArtifactProvider().diff('id', olds, news)

    assert result.changes is True
    assert device.sessions == 0


def test_reading_an_artifact_reports_the_digest_the_device_claims(device: Device) -> None:
    props = artifact_props()
    landed(device, props, payload=b'a different release')

    result = provider.DeviceArtifactProvider().read('id', props)

    assert result.outs is not None
    assert result.outs['sha256'] == hashlib.sha256(b'a different release').hexdigest()


def test_reading_an_artifact_with_no_marker_drops_the_identifier(device: Device) -> None:
    props = artifact_props()
    landed(device, props)
    del device.files[provider.marker_path(ROOTFS_TARGET)]

    result = provider.DeviceArtifactProvider().read('id', props)

    assert result.id is None


def test_deleting_an_artifact_takes_the_marker_with_it(device: Device) -> None:
    props = artifact_props()
    landed(device, props)
    device.log.clear()

    provider.DeviceArtifactProvider().delete('id', props)

    assert device.files == {}
    assert device.log == [
        f'remove {provider.marker_path(ROOTFS_TARGET)}',
        f'remove {ROOTFS_TARGET}',
        'run systemctl restart adguard',
    ]


def test_a_digest_that_is_not_a_sha256_is_refused() -> None:
    result = provider.DeviceArtifactProvider().check({}, artifact_props(sha256='deadbeef'))

    assert [failure.property for failure in result.failures] == ['sha256']


def test_a_relative_extraction_directory_is_refused_before_anything_is_pushed() -> None:
    result = provider.DeviceArtifactProvider().check({}, unpacking_props(extract='services/roots/adguard'))

    assert [failure.property for failure in result.failures] == ['extract']


##
## Unpacking an artifact into a tree
##


def test_the_pin_is_checked_against_what_was_published_and_the_device_gets_the_archive(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """The published artifact is compressed and the device receives it plain.

    The chain that makes this safe: the digest verifies the bytes as downloaded,
    decompression is a function of bytes already verified, and the marker the
    device keeps names that same digest. So the marker is deliberately *not* the
    checksum of the file beside it — it is a claim about provenance, and a
    reviewer reading `sha256sum` on the device should expect them to differ.
    """
    assert downloads[ROOTFS_ZST_URL] != ROOTFS, 'the published artifact is the compressed one'

    _ = provider.DeviceArtifactProvider().create(unpacking_props())

    assert device.files[ROOTFS_TARGET].data == ROOTFS
    assert device.files[provider.marker_path(ROOTFS_TARGET)].data == f'{ROOTFS_ZST_SHA256}\n'.encode()
    assert device.files[provider.marker_path(ROOTFS_TREE)].data == f'{ROOTFS_ZST_SHA256}\n'.encode()


def test_compressed_bytes_that_do_not_match_their_pin_are_never_decompressed(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """Verification comes first, so nothing hands unverified bytes to a decoder."""
    downloads[ROOTFS_ZST_URL] = zstandard.ZstdCompressor().compress(b'a substituted release')

    with pytest.raises(provider.DigestMismatch):
        _ = provider.DeviceArtifactProvider().create(unpacking_props())

    assert device.sessions == 0


def test_an_uncompressed_payload_is_pushed_as_it_arrived(device: Device, downloads: dict[str, bytes]) -> None:
    """A publisher that stops compressing does not turn a working pin into a
    corrupt one: what the device must receive is a plain archive either way."""
    assert downloads
    _ = provider.DeviceArtifactProvider().create(unpacking_props(url=ROOTFS_URL, sha256=ROOTFS_SHA256))

    assert device.files[ROOTFS_TARGET].data == ROOTFS


def test_the_tree_is_replaced_by_renames_so_a_half_extracted_root_never_runs(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """A container's root is either the previous tree or the new one.

    The archive is unpacked into a sibling staging directory and moved into
    place, and what it displaced is left beside it: at this point the container
    is still running on those files, because the hook that restarts it has not
    run yet. Clearing them is the next push's first act.
    """
    assert downloads
    _ = provider.DeviceArtifactProvider().create(unpacking_props())

    unpack = next(command for command in device.commands if 'tar -xf' in command)
    staging = f'{ROOTFS_TREE}{provider.EXTRACTING_SUFFIX}'
    superseded = f'{ROOTFS_TREE}{provider.SUPERSEDED_SUFFIX}'

    assert unpack.startswith(f'rm -rf {staging} {superseded}')
    assert f'tar -xf {ROOTFS_TARGET} -C {staging}' in unpack
    assert f'mv {ROOTFS_TREE} {superseded}' in unpack
    assert unpack.endswith(f'mv {staging} {ROOTFS_TREE}')
    assert f'rm -rf {ROOTFS_TREE} ' not in unpack, 'the live tree is renamed, never deleted in place'


def test_the_trees_marker_is_written_before_the_hook_and_the_archives_after(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """The hook is what notices a new root filesystem, so it has to see it.

    The gateway's hook restarts a container when a file it reads has changed,
    and the marker beside the tree is that file. The marker beside the archive
    stays last, because it is the claim that the whole sequence succeeded.
    """
    assert downloads
    _ = provider.DeviceArtifactProvider().create(unpacking_props())

    assert device.log == [
        f'write {ROOTFS_TARGET}',
        f'run {provider.unpack_script(ROOTFS_TARGET, ROOTFS_TREE)}',
        f'write {provider.marker_path(ROOTFS_TREE)}',
        'run systemctl restart adguard',
        f'write {provider.marker_path(ROOTFS_TARGET)}',
    ]


def test_an_extraction_that_fails_leaves_no_marker_claiming_it_succeeded(
    device: Device,
    downloads: dict[str, bytes],
) -> None:
    """The hook never runs and neither marker is written, so the next preview
    still has work to do — and the tree the container boots is untouched."""
    assert downloads
    device.refuse = ('tar -xf',)

    with pytest.raises(provider.ExtractFailed):
        _ = provider.DeviceArtifactProvider().create(unpacking_props())

    assert provider.marker_path(ROOTFS_TREE) not in device.files
    assert provider.marker_path(ROOTFS_TARGET) not in device.files
    assert 'systemctl restart adguard' not in device.commands


def test_a_tree_someone_removed_is_a_change_even_though_the_archive_is_intact(device: Device) -> None:
    """What the device runs is the tree; the archive is only how it got there."""
    props = unpacking_props()
    landed(device, props, digest=ROOTFS_ZST_SHA256)
    del device.files[ROOTFS_TREE]

    assert provider.DeviceArtifactProvider().diff('id', props, props).changes is True


def test_a_tree_unpacked_from_another_pin_is_a_change(device: Device) -> None:
    props = unpacking_props()
    landed(device, props, digest=ROOTFS_ZST_SHA256)
    device.files[provider.marker_path(ROOTFS_TREE)].data = b'0' * 64 + b'\n'

    assert provider.DeviceArtifactProvider().diff('id', props, props).changes is True


def test_an_archive_and_tree_that_both_name_the_pin_are_no_change(device: Device) -> None:
    props = unpacking_props()
    landed(device, props, digest=ROOTFS_ZST_SHA256)

    assert provider.DeviceArtifactProvider().diff('id', props, props).changes is False


def test_reading_an_artifact_whose_tree_is_gone_drops_the_identifier(device: Device) -> None:
    """A refresh reports what the device can actually run, which is the tree."""
    props = unpacking_props()
    landed(device, props, digest=ROOTFS_ZST_SHA256)
    del device.files[ROOTFS_TREE]

    assert provider.DeviceArtifactProvider().read('id', props).id is None


def test_deleting_an_extracted_artifact_takes_the_tree_and_its_staging_with_it(device: Device) -> None:
    """Derived state the push created is state the push removes, leftovers of an
    interrupted extraction included."""
    props = unpacking_props()
    landed(device, props, digest=ROOTFS_ZST_SHA256)
    device.log.clear()

    provider.DeviceArtifactProvider().delete('id', props)

    assert device.log == [
        f'remove {provider.marker_path(ROOTFS_TARGET)}',
        f'remove {ROOTFS_TARGET}',
        f'remove {provider.marker_path(ROOTFS_TREE)}',
        f'run {provider.purge_script(ROOTFS_TREE)}',
        'run systemctl restart adguard',
    ]
    assert ROOTFS_TREE in provider.purge_script(ROOTFS_TREE)
    assert f'{ROOTFS_TREE}{provider.SUPERSEDED_SUFFIX}' in provider.purge_script(ROOTFS_TREE)


def test_a_path_a_shell_would_mangle_is_quoted_in_the_extraction_too() -> None:
    """The archive and the directory both reach a shell, so both are quoted."""
    script = provider.unpack_script('/data/a file.tar', '/data/roots/a tree')

    assert "'/data/a file.tar'" in script
    assert "'/data/roots/a tree'" in script


def test_an_archive_that_is_not_compressed_survives_the_decompression_step() -> None:
    assert provider.plain_archive(ROOTFS) == ROOTFS
    assert provider.plain_archive(ROOTFS_ZST) == ROOTFS
    assert ROOTFS_ZST.startswith(provider.ZSTD_MAGIC)


##
## What the engine is handed
##


def test_the_client_key_is_kept_out_of_plain_state_and_a_secret_file_with_it() -> None:
    """The provider echoes its inputs back, so the echo has to be marked."""
    assert provider.secret_outputs() == ['private_key']
    assert provider.secret_outputs(secret_content=True) == ['private_key', 'content']


def test_the_pinned_host_key_stays_readable_because_it_is_a_public_key() -> None:
    """A pin nobody can read in a diff is a pin nobody reviews."""
    assert 'host_key' not in provider.secret_outputs(secret_content=True)


def test_a_connection_hands_every_resource_the_same_five_properties() -> None:
    connection = provider.Connection(host=HOST, private_key=PRIVATE_KEY, host_key=HOST_KEY)

    assert connection.props() == {
        'host': HOST,
        'port': 22,
        'username': 'root',
        'private_key': PRIVATE_KEY,
        'host_key': HOST_KEY,
    }


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    """Declare one of each resource, the way the gateway's services do.

    The same drain as the other declaration suites: a declaration schedules a
    registration task, and only the tasks this module added may be awaited.
    """
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)
    connection = provider.Connection(host=HOST, private_key=PRIVATE_KEY, host_key=HOST_KEY)

    before = asyncio.all_tasks()
    _ = provider.DeviceFile('frr', connection=connection, path=CONFIG_PATH, content=CONFIG, hook=HOOK)
    _ = provider.DeviceArtifact(
        'adguard-rootfs',
        connection=connection,
        url=ROOTFS_ZST_URL,
        sha256=ROOTFS_ZST_SHA256,
        target=ROOTFS_TARGET,
        extract=ROOTFS_TREE,
    )
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def registered(name: str) -> tuple[str, dict[str, Any]]:
    typ, _, inputs = next(row for row in declared if row[1] == name)
    return typ, inputs


def test_a_declared_file_carries_the_connection_and_the_file_into_one_property_bag() -> None:
    """Declaring the resource is also what pickles the provider into state; a
    provider that could not be serialized would fail here rather than at the
    first `pulumi up`."""
    typ, inputs = registered('frr')

    assert typ == 'pulumi-python:dynamic/device:File'
    assert inputs['host'] == HOST
    assert inputs['host_key'] == HOST_KEY
    assert inputs['path'] == CONFIG_PATH
    assert inputs['content'] == CONFIG
    assert inputs['hook'] == HOOK


def test_a_declared_artifact_carries_its_pin_and_never_its_bytes() -> None:
    typ, inputs = registered('adguard-rootfs')

    assert typ == 'pulumi-python:dynamic/device:Artifact'
    assert inputs['url'] == ROOTFS_ZST_URL
    assert inputs['sha256'] == ROOTFS_ZST_SHA256
    assert inputs['target'] == ROOTFS_TARGET
    assert inputs['extract'] == ROOTFS_TREE
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


def test_a_stat_is_read_as_owner_group_mode_size() -> None:
    device, connection = transport(answer(stdout=b'root staff 640 12\n'))

    assert asyncio.run(device.stat('/data/x')) == ssh.FileStat(owner='root', group='staff', mode='640', size=12)
    assert "stat -c '%U %G %a %s' /data/x" in connection.commands[0]


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
    stat = ssh.FileStat(owner='root', group='staff', mode='0644', size=0)

    assert ssh.same_owner(None, stat)
    assert ssh.same_owner('root', stat)
    assert not ssh.same_owner('root:root', stat)
    assert ssh.same_owner('root:staff', stat)
