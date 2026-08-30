"""The gw-config dynamic provider: desired-state files on the gateway device.

The device has no API for what matters on it -- routing, the container services,
the scripts that re-establish both after a firmware update -- but it has a proven
convention: files under `/data`, written idempotently, with a command run
afterwards to make whatever reads them notice. This module turns that convention
into two Pulumi resources (architecture.md §5.2):

-   `DeviceFile` is a file: path, content, ownership, mode, and an optional
    hook. Its content lives in state, because a configuration file is small
    and its diff is the reason to have a preview at all.
-   `DeviceArtifact` is a payload too big for state: a container image, the
    manifest digest that pins it, and where it lands. The image is pulled on
    the runner, verified against the pin, flattened into one archive and
    streamed to the device; what state carries is the pin, and what the device
    carries is a marker file beside the payload naming the digest it holds. A
    preview compares two hashes, never megabytes. An artifact may also name a
    directory to `extract` into, for the payloads that are root filesystem
    archives rather than single files.

**An extracted artifact owns two things on the device**: the archive at
`target`, and the tree unpacked from it. The tree is derived state -- the push
puts it there, the push replaces it, and the push takes it away -- so it is
never edited in place: the archive is unpacked into a sibling staging directory
and moved into position with two renames, and no moment passes in which a
half-extracted tree is the one a container boots. The tree it displaced is left
behind until the *next* push clears it, because at the moment of the swap the
container is still running on it and has not yet been restarted.

**The digest pins the artifact as published, not the bytes on the wire.** The
gateway's container root filesystems are published as registry images; the
runner verifies the manifest against the pin, verifies every layer against that
manifest, flattens them into one plain archive and pushes that, because the
device runs `systemd-nspawn` over a directory rather than a container engine and
`tar` is the floor every such system clears (`registry`). So the chain is: the
pin verifies the manifest, the manifest verifies the layers, flattening is a
function of bytes already verified, the session that carries them is pinned by
host key, and the markers the device keeps record *the pin* -- which is
therefore the manifest's digest and not a checksum of the file lying beside the
marker. A marker is the device's claim about provenance, not a checksum of its
neighbour.

**Both resources diff against the device, not only against state.** A file
someone edited on the box shows up as a change in `pulumi preview` without a
refresh, which is what makes this convergence rather than record-keeping. The
cost is that a preview opens a session per resource and fails if the device is
unreachable -- deliberate, since a preview that silently reports "no changes"
about a device it never reached is worse than one that says it could not look.

**A change to any declared input is a change**, whether or not the device already
agrees: an update rewrites bytes the device may already have, which is free and
idempotent. The address dialled is declared and converges the same way -- it is
the same box behind a new address, during a first bring-up reached over the LAN
rather than over the overlay (physical/gateway.md §2.5) -- so the file is
rewritten at the new address rather than moved to it, and nothing is deleted at
the old one.

**The session is the provider's own business, and `check` is what makes it
visible.** The credential that opens the device is read in `configure`, out of
stack configuration, inside the plugin's process: no caller declares it, no
component passes it, and nothing pickles it. So that a reader still sees what it
does, `check` adds two properties to every resource's checked inputs --
`session`, the endpoint and a short digest of the credential, and
`provider_version`, this module's own version. The engine compares checked
inputs, so a rotation and a change to this module's behavior each render as a
property diff no caller declared (rfc-002 §7.4). Neither is a change to the
device: an update whose declared inputs all match re-stamps the resource and
writes nothing. That whole shape is `kluster.providers.configured`, which every
custom provider here shares; what is this module's own is which key holds the
credential and what an endpoint is.

**Only the path is a replacement.** The bytes cannot be at two paths at once, so
a moved file is created before the old one is deleted. Nothing else about a
resource here identifies it, the address least of all: a replacement triggered by
an address would delete, at what is the same device, the file its own create had
just written there.

**Order within an apply is chosen so that a failure is visible.** The payload
lands, then the hook runs, then the marker is written: a hook that fails leaves
the device without the marker, so the next preview still sees work to do. A
failed create raises, which means the resource is not recorded -- the file may
already exist on the device, and the retry rewrites the same bytes, both
operations being idempotent by construction.

An extracted artifact adds two steps inside that order: the tree is replaced
after the archive lands and *before* the hook, and the marker beside the tree is
written in that same window. That is what lets a hook notice the tree changed --
the gateway's hook is a script that restarts a container only when a file
it reads has changed, and a marker written after the hook would be a change the
hook could never see. The marker beside the archive stays last, because it is
the claim that the whole sequence succeeded.

A file's content is declared secret on request (`secret=True`), for the files
that carry device credentials, and not by default: a secret input renders as a
hash in a preview, and most of what a device holds is configuration one wants to
read. The pinned host key is deliberately not secret either -- it is a public
key, and a reviewable pin is worth more than a redacted one.

The provider classes carry nothing into state. A dynamic provider is pickled into
every resource it manages, so what these classes serialize to is what state
holds: their own module and class name and an empty bag -- inert, identical for
every resource, and unchanged by a credential rotation. They are revived in a
separate process where each call arrives on a thread with no event loop, so every
operation runs its own loop, opens its own session and closes both. The two seams
a test replaces -- how a session is opened, how an image is pulled -- are
module-level functions looked up when they are called rather than attributes
captured at construction, which keeps the pickled provider a constant.
"""

from __future__ import annotations

import abc
import asyncio
import shlex
from collections.abc import Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.configured import (
    STAMPS,
    ConfiguredProvider,
    declared_change,
    has_unknowns,
    is_unknown,
)
from kluster.providers.device_files import registry, ssh
from kluster.providers.device_files.registry import DigestMismatch, Image

__all__ = (
    'ADDRESS',
    'ARTIFACT_COMPARED',
    'ARTIFACT_DECLARED',
    'EXTRACTING_SUFFIX',
    'FILE_COMPARED',
    'FILE_DECLARED',
    'PIN',
    'PRIVATE_KEY_CONFIG',
    'SUPERSEDED_SUFFIX',
    'VERSION',
    'Connection',
    'DigestMismatch',
    'ExtractFailed',
    'DeviceArtifact',
    'DeviceArtifactProvider',
    'DeviceFile',
    'DeviceFileProvider',
    'DeviceProvider',
    'HookFailed',
    'Image',
    'endpoint',
    'fetch',
    'gone',
    'marker_path',
    'open_transport',
    'purge_script',
    'secret_outputs',
    'unpack_script',
)

#: Where the device answers and as whom the session authenticates. These are
#: declared inputs rather than a resource identity: a change to one dials the new
#: address and writes the same file there, and does *not* delete anything at the
#: old one. A session names exactly one device, pinned by its host key rather than
#: by its address, so an address that moved -- the LAN dial of a first bring-up,
#: physical/gateway.md §2.5 -- is the same box, and a replacement's delete would
#: remove the file its own create had just written. A device genuinely swapped
#: out keeps whatever was on it, which is what happens to a box that has been
#: taken off the network anyway.
ADDRESS = ('host', 'port', 'username')

#: The key the device must present. Declared like the address and for the same
#: reason: it is the caller's decision, and a provider has no way to reach the
#: caller's decisions for itself. A public key by nature, and nothing marks it
#: secret on either side of the boundary, so a preview shows it -- which is
#: where a reviewer checks the pin a session will be held to, and a pin nobody
#: can read is a pin nobody reviews. The credential that answers it goes the
#: other way -- `PRIVATE_KEY_CONFIG`, read in `configure` and declared by
#: nobody.
PIN = ('host_key',)

#: What a `DeviceFile` declares beyond its own path.
FILE_DECLARED = ('content', 'mode', 'owner', 'hook', *ADDRESS, *PIN)

#: The same for a `DeviceArtifact`. The digest is the payload's identity, and the
#: repository and tag are only where those bytes were found -- but all three are
#: declared, so moving publication to another registry, or a tag that moved onto
#: a digest the device already holds, is a diff a reviewer sees rather than a
#: silent equivalence.
ARTIFACT_DECLARED = ('repository', 'tag', 'digest', 'extract', 'mode', 'owner', 'hook', *ADDRESS, *PIN)

#: The stack-configuration key the session's credential is read from
#: (`configured`), which makes this module the only code in the repository that
#: ever holds the device's private key.
PRIVATE_KEY_CONFIG = 'gatewayPrivateKey'

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '2'

#: What `diff` compares, and the whole of it. Its `olds` is the stored *output*
#: bag while its `news` is the checked *input* bag (rfc-002 §7.5 E7), so a key
#: that only an operation's outs ever carried lives in `olds` alone -- and a
#: provider comparing the two bags wholesale reports a change on every run.
FILE_COMPARED = (*FILE_DECLARED, *STAMPS)
ARTIFACT_COMPARED = (*ARTIFACT_DECLARED, *STAMPS)

#: The mode a digest marker is written with. The marker is the provider's own
#: bookkeeping rather than part of the desired state, so it does not inherit the
#: payload's ownership.
MARKER_MODE = '0644'

#: Where a tree is assembled before it becomes the live one, and where the tree
#: it displaced waits to be cleared. Both sit beside the tree rather than in a
#: temporary directory, so the renames that swap them stay within one filesystem
#: and are therefore atomic.
EXTRACTING_SUFFIX = '.kluster-extracting'
SUPERSEDED_SUFFIX = '.kluster-superseded'


@final
class HookFailed(Exception):
    """A post-apply hook exited non-zero, so the apply did not converge."""

    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'post-apply hook `{command}` exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.command: str = command
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class ExtractFailed(Exception):
    """Unpacking the payload failed, so the tree the device boots is untouched."""

    def __init__(self, directory: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'unpacking into {directory} exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.directory: str = directory
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
@dataclass(frozen=True)
class Connection:
    """Where and as whom to write, and the key the device must present.

    Everything here is a caller's decision, which is why it travels as resource
    inputs, and none of it is a credential. `host_key` is the pin, and it is
    required: there is no shape of this object that means "trust whatever
    answers". It is a public key and it travels in the clear, so a preview
    shows it (see `PIN`). The credential the session authenticates with is not
    here at all -- it is `PRIVATE_KEY_CONFIG`, read in the provider's own
    process.
    """

    host: pulumi.Input[str]
    host_key: pulumi.Input[str]
    username: pulumi.Input[str] = 'root'
    port: pulumi.Input[int] = 22

    def props(self) -> dict[str, Any]:
        return {
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'host_key': self.host_key,
        }


def open_transport(device: ssh.Device) -> AbstractAsyncContextManager[ssh.Transport]:
    """Open the session an operation runs through.

    The one seam between the provider and a real device: a test replaces this
    module attribute with a fake, and everything above it is the code that runs
    in production.
    """
    return ssh.connect(device)


def fetch(image: Image) -> bytes:
    """Pull an image on the runner and flatten it. The other seam a test replaces.

    The verification is `registry`'s and the flattening is too; what this line
    is, is the seam. The payload is held in memory for the moment between
    pulling and streaming it, which suits the root filesystems this exists for
    and would not suit a disk image.
    """
    return registry.rootfs(image)


def unpack_script(archive: str, directory: str) -> str:
    """The commands that make `directory` the contents of `archive`, atomically.

    Unpacked into a sibling staging directory and moved into place with two
    renames, so the tree a container boots is either the previous one or the new
    one and never a partial one. What the swap displaced is left as a sibling
    too: the container is still running on those files at this point in the
    sequence -- the hook that restarts it has not run yet -- so it is cleared at
    the start of the *next* push instead, along with any staging directory a
    failed run left behind.
    """
    quoted_archive, quoted_directory = shlex.quote(archive), shlex.quote(directory)
    staging = shlex.quote(f'{directory}{EXTRACTING_SUFFIX}')
    superseded = shlex.quote(f'{directory}{SUPERSEDED_SUFFIX}')
    return ' && '.join(
        (
            f'rm -rf {staging} {superseded}',
            f'mkdir -p {staging}',
            f'tar -xf {quoted_archive} -C {staging}',
            f'if [ -e {quoted_directory} ]; then mv {quoted_directory} {superseded}; fi',
            f'mv {staging} {quoted_directory}',
        )
    )


def purge_script(directory: str) -> str:
    """The command that removes a tree and every sibling the swap uses."""
    paths = (directory, f'{directory}{EXTRACTING_SUFFIX}', f'{directory}{SUPERSEDED_SUFFIX}')
    return 'rm -rf ' + ' '.join(shlex.quote(path) for path in paths)


def marker_path(target: str) -> str:
    """Where the digest of the payload at `target` is recorded on the device.

    A tree gets one of these too, beside it rather than inside it: a file the
    push wrote into the tree would be a file inside the container's root
    filesystem, and the tree is the image, not the push's bookkeeping.
    """
    return f'{target}.digest'


def secret_outputs(*, secret_content: bool = False) -> list[str]:
    """The properties the engine must keep out of plain state and previews.

    Only a file's content is ever one. The address and the pin are public, and
    the credential is not a property of any resource.
    """
    return ['content'] if secret_content else []


def endpoint(props: Mapping[str, Any]) -> str:
    """Where a property bag says the session goes, as one legible string."""
    return f'{props["username"]}@{props["host"]}:{props["port"]}'


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion from a provider's synchronous method.

    Pulumi calls a Python dynamic provider on a gRPC servicer thread that has no
    event loop of its own, so each operation gets a loop, uses it, and closes it.
    Sharing one would be wrong as well as unnecessary: those threads run
    concurrently.
    """
    return asyncio.run(coroutine)


async def run_hook(transport: ssh.Transport, props: Mapping[str, Any]) -> None:
    """Run the post-apply hook, if the resource declares one."""
    hook = props.get('hook')
    if not hook:
        return
    command = str(hook)
    result = await transport.run(command)
    if not result.ok:
        raise HookFailed(command, result.exit_status, result.stderr)


def replacements(olds: Mapping[str, Any], news: Mapping[str, Any], location: str) -> list[str]:
    """The properties whose change means a different resource, not a new value.

    Where the file sits on the device, and nothing else: that is the only change
    the device cannot converge in place, because the bytes have to appear under
    the new path and go from the old one. Everything else about a `DeviceFile` --
    where the device answers included -- is a value the next apply writes.

    A property that is still unknown cannot have been shown to differ, so it is
    never a reason to plan a replacement; the unknown diff that follows says so
    honestly instead.
    """
    moved = not is_unknown(news.get(location)) and olds.get(location) != news.get(location)
    return [location] if moved else []


def _observed_owner(declared: Any, stat: ssh.FileStat) -> str | None:
    """The device's ownership, in the shape the resource declared it."""
    if not declared:
        return None
    return f'{stat.owner}:{stat.group}' if ':' in str(declared) else stat.owner


def _absolute(props: Mapping[str, Any], key: str) -> list[dynamic.CheckFailure]:
    value = props.get(key)
    if value is None or is_unknown(value):
        return []
    if not str(value).startswith('/'):
        return [dynamic.CheckFailure(key, f'must be an absolute path on the device, got {value!r}')]
    return []


def _octal(props: Mapping[str, Any], key: str) -> list[dynamic.CheckFailure]:
    value = props.get(key)
    if value is None or is_unknown(value):
        return []
    try:
        _ = int(str(value), 8)
    except ValueError:
        return [dynamic.CheckFailure(key, f'must be an octal mode such as "0644", got {value!r}')]
    return []


def gone() -> dynamic.ReadResult:
    """What a `read` returns for a resource the device no longer has.

    Dropping the identifier is how the engine learns the resource is gone. The
    outputs are an empty bag rather than `None`, and a fresh one each time,
    because the dynamic-provider host writes its own bookkeeping key into
    whatever bag it is handed.
    """
    return dynamic.ReadResult(id_=None, outs={})


class DeviceProvider(ConfiguredProvider, abc.ABC):
    """What both device providers share: the session, and what it makes visible.

    The stateless-provider machinery is `kluster.providers.configured`: the key
    is read in `configure`, nothing is pickled, and `check` stamps the session
    and the version. What this class adds is what those hooks mean for a device
    -- the key is `PRIVATE_KEY_CONFIG`, the endpoint is where a session dials,
    and an update that only re-stamps must still ask the device whether it
    agrees.
    """

    private_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.private_key = str(config.require(PRIVATE_KEY_CONFIG))

    def _credential(self) -> str:
        return self.private_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return endpoint(props)

    def _version(self) -> str:
        return VERSION

    def _device(self, props: Mapping[str, Any]) -> ssh.Device:
        """The device a property bag names, opened with the configured credential."""
        return ssh.Device(
            host=str(props['host']),
            port=int(props['port']),
            username=str(props['username']),
            private_key=self.private_key,
            host_key=str(props['host_key']),
        )

    async def _restamp_only(self, olds: Mapping[str, Any], news: Mapping[str, Any], declared: tuple[str, ...]) -> bool:
        """Whether this update has nothing for the device: only a stamp moved.

        The stamps change without the device changing, so a rotation or a version
        bump must not rewrite every file on the gateway -- the update re-stamps
        the resource and leaves the device alone. The device still gets the last
        word, because drift is the other reason an update is planned with every
        declared input equal, and `diff` cannot say through its result which of
        the two answers it gave. So it is asked again, and this branch reads:
        writing is what it never does.
        """
        if declared_change(olds, news, declared):
            return False
        return not await self._drifted(news)

    @abc.abstractmethod
    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        """Whether the device disagrees with what `props` declares."""


@final
class DeviceFileProvider(DeviceProvider):
    """One desired-state file on the device: write it, then make it take effect."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [*_absolute(news, 'path'), *_octal(news, 'mode')])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        run_sync(self._apply(props))
        # The checked inputs go back out as the outputs, stamps included, so the
        # stored bag records the session that wrote this file.
        return dynamic.CreateResult(id_=str(props['path']), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'path')
        if replaces:
            # Created before deleted: the paths differ, so the new file exists
            # before the old one goes, and no moment passes with neither there.
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, FILE_COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if not run_sync(self._restamp_only(olds, news, FILE_DECLARED)):
            run_sync(self._apply(news))
        # The outs replace the stored output bag (rfc-002 §7.5 E9), so what state
        # says about the session that last wrote this file stays true.
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    async def _apply(self, props: Mapping[str, Any]) -> None:
        async with open_transport(self._device(props)) as transport:
            await transport.write(
                str(props['path']),
                str(props['content']).encode(),
                mode=str(props['mode']),
                owner=_owner(props),
            )
            await run_hook(transport, props)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(path)
            if stat is None:
                return True
            if not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat):
                return True
            return await transport.read(path) != str(props['content']).encode()

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(path)
            content = None if stat is None else await transport.read(path)
            if stat is None or content is None:
                # Gone from the device is gone: the next up creates it again,
                # which is how a file someone deleted by hand comes back.
                return gone()
            outs = {
                **props,
                'content': content.decode(errors='replace'),
                'mode': stat.mode,
                'owner': _observed_owner(props.get('owner'), stat),
            }
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        async with open_transport(self._device(props)) as transport:
            await transport.remove(str(props['path']))
            # The hook runs on the way out too: whatever reads this file has to
            # be told it is gone, exactly as it was told it had arrived.
            await run_hook(transport, props)


@final
class DeviceArtifactProvider(DeviceProvider):
    """A digest-pinned payload on the device, tracked by a marker file."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        failures = [*_absolute(news, 'target'), *_absolute(news, 'extract'), *_octal(news, 'mode')]
        digest = news.get('digest')
        if digest is not None and not is_unknown(digest) and not _is_digest(str(digest)):
            failures.append(dynamic.CheckFailure('digest', f'must be a `sha256:<hex>` manifest digest, got {digest!r}'))
        return self._stamp(news, failures)

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        self._push(props)
        return dynamic.CreateResult(id_=str(props['target']), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'target')
        if replaces:
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, ARTIFACT_COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        # A re-stamp fetches nothing either: the payload the device holds is the
        # payload the pin names, and the runner has no reason to download it
        # again to find that out.
        if not run_sync(self._restamp_only(olds, news, ARTIFACT_DECLARED)):
            self._push(news)
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    def _push(self, props: Mapping[str, Any]) -> None:
        """Pull, verify, then land. Nothing reaches the device unverified.

        The verification is inside the pull rather than repeated out here: the
        pin names a manifest, the manifest names the layers, and the archive
        that comes back is assembled from bytes each of those digests already
        vouched for. What lands is therefore not something this runner can hash
        against the pin -- which is the same relationship the marker on the
        device has to the payload beside it.
        """
        digest = str(props['digest'])
        data = fetch(Image(repository=str(props['repository']), digest=digest))
        run_sync(self._land(props, data, digest))

    async def _land(self, props: Mapping[str, Any], data: bytes, digest: str) -> None:
        target = str(props['target'])
        tree = _extract(props)
        async with open_transport(self._device(props)) as transport:
            await transport.write(target, data, mode=str(props['mode']), owner=_owner(props))
            if tree:
                await self._unpack(transport, target, tree)
                # Before the hook, because the hook is what notices: the
                # hook restarts a container when a file it reads has changed, and
                # this marker is that file.
                await _mark(transport, tree, digest)
            await run_hook(transport, props)
            # Written last, and only once everything else has succeeded: the
            # marker is the claim that this device holds these bytes and has
            # acted on them.
            await _mark(transport, target, digest)

    async def _unpack(self, transport: ssh.Transport, archive: str, directory: str) -> None:
        result = await transport.run(unpack_script(archive, directory))
        if not result.ok:
            raise ExtractFailed(directory, result.exit_status, result.stderr)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        target = str(props['target'])
        tree = _extract(props)
        pinned = str(props['digest'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(target)
            if stat is None:
                return True
            if not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat):
                return True
            if tree and not await _tree_holds(transport, tree, pinned):
                return True
            marker = await transport.read(marker_path(target))
            return marker is None or marker.decode(errors='replace').strip() != pinned

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        target = str(props['target'])
        tree = _extract(props)
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(target)
            marker = None if stat is None else await transport.read(marker_path(target))
            if stat is None or marker is None:
                # A payload with no marker is a payload of unknown provenance,
                # which is the same situation as no payload at all.
                return gone()
            claimed = marker.decode(errors='replace').strip()
            if tree and not await _tree_holds(transport, tree, claimed):
                # The archive alone is not the resource: what the device runs is
                # the tree, and a tree someone removed is a resource to create.
                return gone()
            outs = {
                **props,
                'digest': claimed,
                'mode': stat.mode,
                'owner': _observed_owner(props.get('owner'), stat),
            }
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        target = str(props['target'])
        tree = _extract(props)
        async with open_transport(self._device(props)) as transport:
            await transport.remove(marker_path(target))
            await transport.remove(target)
            if tree:
                await transport.remove(marker_path(tree))
                _ = await transport.run(purge_script(tree))
            await run_hook(transport, props)


@final
class DeviceFile(dynamic.Resource, module='device', name='File'):
    """A file the device must have, and what to run once it has it."""

    path: pulumi.Output[str]
    content: pulumi.Output[str]
    mode: pulumi.Output[str]
    owner: pulumi.Output[str | None]
    hook: pulumi.Output[str | None]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        path: pulumi.Input[str],
        content: pulumi.Input[str],
        mode: pulumi.Input[str] = '0644',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        secret: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `path` on the device.

        `owner` is `user` or `user:group`, and omitting it leaves ownership to
        whatever the device does by default. `hook` runs after every write and
        after the delete; a non-zero exit fails the operation. `secret=True`
        marks the content a credential, for the files that carry one.
        """
        super().__init__(
            DeviceFileProvider(),
            name,
            {
                **connection.props(),
                'path': path,
                'content': content,
                'mode': mode,
                'owner': owner,
                'hook': hook,
            },
            pulumi.ResourceOptions.merge(
                pulumi.ResourceOptions(additional_secret_outputs=secret_outputs(secret_content=secret)),
                opts,
            ),
        )


@final
class DeviceArtifact(dynamic.Resource, module='device', name='Artifact'):
    """A digest-pinned container image, landed on the device as a flat archive."""

    repository: pulumi.Output[str]
    tag: pulumi.Output[str]
    digest: pulumi.Output[str]
    target: pulumi.Output[str]
    extract: pulumi.Output[str | None]
    mode: pulumi.Output[str]
    owner: pulumi.Output[str | None]
    hook: pulumi.Output[str | None]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        repository: pulumi.Input[str],
        tag: pulumi.Input[str],
        digest: pulumi.Input[str],
        target: pulumi.Input[str],
        extract: pulumi.Input[str] | None = None,
        mode: pulumi.Input[str] = '0644',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `target` holds the root filesystem `digest` names.

        The bytes never enter state: the image is pulled from `repository` on
        the runner at apply time, verified against `digest` and flattened, then
        streamed to the device, which keeps a marker beside the payload naming
        what it holds. `tag` is where the digest was found and is declared
        rather than used -- the pull is by digest, because a tag is a name
        somebody else can move.

        `extract` names a directory the payload is unpacked into, for an archive
        rather than a single file, and the resource owns that directory: it
        replaces it whole on a new pin and removes it on a delete. Only `target`
        is a replacement, so a caller that moves the tree is expected to move the
        archive with it -- both are named after the same thing, and a tree left
        at the old path would be an orphan nothing declares.
        """
        super().__init__(
            DeviceArtifactProvider(),
            name,
            {
                **connection.props(),
                'repository': repository,
                'tag': tag,
                'digest': digest,
                'target': target,
                'extract': extract,
                'mode': mode,
                'owner': owner,
                'hook': hook,
            },
            # No property of an artifact is a secret: a reference, a digest and
            # a path are all things a reviewer should read in a preview.
            opts,
        )


def _extract(props: Mapping[str, Any]) -> str | None:
    """The directory this artifact is unpacked into, if it is unpacked at all."""
    directory = props.get('extract')
    return str(directory) if directory else None


async def _mark(transport: ssh.Transport, location: str, digest: str) -> None:
    """Record which published artifact the payload or tree at `location` came from."""
    await transport.write(marker_path(location), f'{digest}\n'.encode(), mode=MARKER_MODE, owner=None)


async def _tree_holds(transport: ssh.Transport, directory: str, digest: str) -> bool:
    """Whether the device has a tree at `directory` unpacked from `digest`.

    Both halves are asked about, because either can be false on its own: a
    firmware update takes the tree and leaves nothing, and a marker naming
    another digest is a tree from a pin nobody declares any more.
    """
    if await transport.stat(directory) is None:
        return False
    marker = await transport.read(marker_path(directory))
    return marker is not None and marker.decode(errors='replace').strip() == digest


def _owner(props: Mapping[str, Any]) -> str | None:
    owner = props.get('owner')
    return str(owner) if owner else None


def _is_digest(value: str) -> bool:
    """Whether this is a registry digest, in the one spelling a registry uses.

    Lower case and algorithm-qualified, both because that is the form a
    reference carries and because the marker on the device is compared byte for
    byte -- a differently-spelled digest is a pin that never matches.
    """
    algorithm, separator, hex_digest = value.partition(':')
    return (
        algorithm == 'sha256'
        and bool(separator)
        and len(hex_digest) == 64
        and all(character in '0123456789abcdef' for character in hex_digest)
    )
