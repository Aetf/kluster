"""The gw-config dynamic provider: desired-state files on the gateway device.

The device has no API for what matters on it -- routing, the container estate,
the scripts that re-establish both after a firmware update -- but it has a proven
convention: files under `/data`, written idempotently, with a command run
afterwards to make whatever reads them notice. This module turns that convention
into two Pulumi resources (architecture.md §5.2):

-   `GwFile` is a file: path, content, ownership, mode, and an optional hook. Its
    content lives in state, because a configuration file is small and its diff is
    the reason to have a preview at all.
-   `GwArtifact` is a payload too big for state: a URL, the digest that pins it,
    and where it lands. The bytes are fetched on the runner, checked against the
    pin, and streamed to the device; what state carries is the pin, and what the
    device carries is a marker file beside the payload naming the digest it
    holds. A preview compares two hashes, never megabytes. An artifact may also
    name a directory to `extract` into, for the payloads that are root
    filesystem archives rather than single files.

**An extracted artifact owns two things on the device**: the archive at
`target`, and the tree unpacked from it. The tree is derived state -- the push
puts it there, the push replaces it, and the push takes it away -- so it is
never edited in place: the archive is unpacked into a sibling staging directory
and moved into position with two renames, and no moment passes in which a
half-extracted tree is the one a container boots. The tree it displaced is left
behind until the *next* push clears it, because at the moment of the swap the
container is still running on it and has not yet been restarted.

**The digest pins the artifact as published, not the bytes on the wire.** The
estate's root filesystems are published compressed; the runner verifies the
downloaded bytes against the pin, decompresses them, and pushes the plain
archive, because the device cannot be assumed to have a decompressor while `tar`
is the floor every such system clears. So the chain is: the pin verifies
what was downloaded, decompression is a function of bytes already verified, the
session that carries them is pinned by host key, and the markers the device
keeps record *the pin* -- which is therefore the digest of the published archive
and not of the file lying beside the marker. A marker is the device's claim
about provenance, not a checksum of its neighbour.

**Both resources diff against the device, not only against state.** A file
someone edited on the box shows up as a change in `pulumi preview` without a
refresh, which is what makes this convergence rather than record-keeping. The
cost is that a preview opens a session per resource and fails if the device is
unreachable -- deliberate, since a preview that silently reports "no changes"
about a device it never reached is worse than one that says it could not look.

**A change to any declared input is a change**, whether or not the device already
agrees, and that includes the two credentials. An update rewrites bytes the
device may already have, which is free and idempotent; the alternative -- calling
a credential rotation "no change" because the file is already right -- leaves the
superseded key in state, and a `delete` months later would then authenticate with
a key that no longer opens the door. The address dialled is declared for the same
reason and converges the same way: it is the same box behind a new address --
during a first bring-up the device is reached over the LAN rather than over the
overlay (physical/gateway.md §2.5) -- so the file is rewritten at the new address
rather than moved to it, and nothing is deleted at the old one.

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
the gateway estate's hook is a script that restarts a container only when a file
it reads has changed, and a marker written after the hook would be a change the
hook could never see. The marker beside the archive stays last, because it is
the claim that the whole sequence succeeded.

Secret-bearing inputs are declared secret: the client private key always, and a
file's content on request (`secret=True`), for the files that carry device
credentials. Content is not secret by default, because a secret input renders as
a hash in a preview and most of this estate is configuration one wants to read.
The pinned host key is deliberately *not* among them -- it is a public key, and a
reviewable pin is worth more than a redacted one.

The provider classes hold no state of their own. A dynamic provider is pickled
into the stack's state and revived in a separate process where each call arrives
on a thread with no event loop, so every operation runs its own loop, opens its
own session and closes both. The two seams a test replaces -- how a session is
opened, how a URL is fetched -- are module-level functions looked up when they
are called rather than attributes captured at construction, which keeps the
pickled provider a constant.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shlex
from collections.abc import Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic
import requests
import zstandard
from pulumi.runtime import rpc

from kluster.gateway import ssh

__all__ = (
    'ADDRESS',
    'ARTIFACT_DECLARED',
    'CREDENTIALS',
    'EXTRACTING_SUFFIX',
    'FILE_DECLARED',
    'SUPERSEDED_SUFFIX',
    'ZSTD_MAGIC',
    'Connection',
    'DigestMismatch',
    'ExtractFailed',
    'GwArtifact',
    'GwArtifactProvider',
    'GwFile',
    'GwFileProvider',
    'HookFailed',
    'fetch',
    'gone',
    'marker_path',
    'open_transport',
    'plain_archive',
    'purge_script',
    'secret_outputs',
    'unpack_script',
)

#: Where the device answers and as whom the session authenticates. These are
#: declared inputs rather than a resource identity: a change to one dials the new
#: address and writes the same file there, and does *not* delete anything at the
#: old one. The estate has exactly one device, pinned by its host key rather than
#: by its address, so an address that moved -- the LAN dial of a first bring-up,
#: physical/gateway.md §2.5 -- is the same box, and a replacement's delete would
#: remove the file its own create had just written. A device genuinely swapped
#: out keeps whatever was on it, which is what happens to a box that has been
#: taken off the network anyway.
ADDRESS = ('host', 'port', 'username')

#: The credentials the session is opened with. They say nothing about the file's
#: contents, but they are declared inputs, so a rotation is a change -- see the
#: module docstring on why "no change" would be a trap.
CREDENTIALS = ('private_key', 'host_key')

#: What a `GwFile` declares beyond its own path.
FILE_DECLARED = ('content', 'mode', 'owner', 'hook', *ADDRESS, *CREDENTIALS)

#: The same for a `GwArtifact`. The digest is the payload's identity and the URL
#: is only where the bytes were found, but both are declared, so moving a release
#: to another mirror is a diff a reviewer sees.
ARTIFACT_DECLARED = ('url', 'sha256', 'extract', 'mode', 'owner', 'hook', *ADDRESS, *CREDENTIALS)

#: How long the runner may spend fetching an artifact before giving up.
FETCH_TIMEOUT = 300

#: The mode a digest marker is written with. The marker is the provider's own
#: bookkeeping rather than part of the estate, so it does not inherit the
#: payload's ownership.
MARKER_MODE = '0644'

#: The first four bytes of a zstd frame. Recognizing the compression rather than
#: declaring it keeps one knob off the resource: what the device must receive is
#: a plain archive either way, and a publisher that stopped compressing would
#: otherwise turn a working pin into a corrupt one.
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'

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
class DigestMismatch(Exception):
    """The fetched bytes are not the bytes the pin names, so nothing is pushed."""

    def __init__(self, url: str, expected: str, actual: str) -> None:
        super().__init__(f'{url} hashes to {actual}, not the pinned {expected}; refusing to push it')
        self.url: str = url
        self.expected: str = expected
        self.actual: str = actual


@final
@dataclass(frozen=True)
class Connection:
    """Where and as whom to write, and the key the device must present.

    `host_key` is the pin. It is an input like any other so that it can come from
    stack configuration rather than from a file the runner assembled, and it is
    required: there is no shape of this object that means "trust whatever
    answers".
    """

    host: pulumi.Input[str]
    private_key: pulumi.Input[str]
    host_key: pulumi.Input[str]
    username: pulumi.Input[str] = 'root'
    port: pulumi.Input[int] = 22

    def props(self) -> dict[str, Any]:
        return {
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'private_key': self.private_key,
            'host_key': self.host_key,
        }


def open_transport(device: ssh.Device) -> AbstractAsyncContextManager[ssh.Transport]:
    """Open the session an operation runs through.

    The one seam between the provider and a real device: a test replaces this
    module attribute with a fake, and everything above it is the code that runs
    in production.
    """
    return ssh.connect(device)


def fetch(url: str) -> bytes:
    """Fetch an artifact on the runner. The other seam a test replaces.

    The payload is held in memory for the moment between fetching and streaming
    it, which suits the root filesystem images this exists for and would not suit
    a disk image.
    """
    response = requests.get(url, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response.content


def plain_archive(data: bytes) -> bytes:
    """The archive as the device will read it, decompressed here if it arrived so.

    Decompression runs on the runner rather than on the device on purpose. The
    gateway's user space is the cut-down one a router ships, where a zstd binary
    is not something a deployment may assume; extracting an uncompressed archive
    with `tar` is the floor every such system clears. The cost is that the bytes
    on the device are not the bytes the pin names -- see the module docstring on
    what the digest verifies and where.
    """
    if not data.startswith(ZSTD_MAGIC):
        return data
    # `stream_reader` rather than `decompress`: the latter refuses a frame whose
    # header omits the uncompressed size, which is a legal thing for a
    # compressor to write and not something a consumer gets to require.
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
        return reader.read()


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
    filesystem, and the tree is the image, not the estate's bookkeeping.
    """
    return f'{target}.digest'


def secret_outputs(*, secret_content: bool = False) -> list[str]:
    """The properties the engine must keep out of plain state and previews."""
    return ['private_key', *(['content'] if secret_content else [])]


def device(props: Mapping[str, Any]) -> ssh.Device:
    """The device a property bag names."""
    return ssh.Device(
        host=str(props['host']),
        port=int(props['port']),
        username=str(props['username']),
        private_key=str(props['private_key']),
        host_key=str(props['host_key']),
    )


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion from a provider's synchronous method.

    Pulumi calls a Python dynamic provider on a gRPC servicer thread that has no
    event loop of its own, so each operation gets a loop, uses it, and closes it.
    Sharing one would be wrong as well as unnecessary: those threads run
    concurrently.
    """
    return asyncio.run(coroutine)


def is_unknown(value: Any) -> bool:
    """Whether a property is still a preview placeholder."""
    return isinstance(value, str) and value == rpc.UNKNOWN


def has_unknowns(props: Mapping[str, Any]) -> bool:
    """Whether a property bag still holds preview placeholders.

    During a preview an input may be another resource's unresolved output. There
    is nothing to compare it against and no point opening a session to try, so
    the diff answers "unknown" and the engine plans on that basis.
    """
    return any(is_unknown(value) for value in props.values())


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
    the new path and go from the old one. Everything else about a `GwFile` --
    where the device answers included -- is a value the next apply writes.

    A property that is still unknown cannot have been shown to differ, so it is
    never a reason to plan a replacement; the unknown diff that follows says so
    honestly instead.
    """
    moved = not is_unknown(news.get(location)) and olds.get(location) != news.get(location)
    return [location] if moved else []


def declared_change(olds: Mapping[str, Any], news: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(olds.get(key) != news.get(key) for key in keys)


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


@final
class GwFileProvider(dynamic.ResourceProvider):
    """One desired-state file on the device: write it, then make it take effect."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return dynamic.CheckResult(news, [*_absolute(news, 'path'), *_octal(news, 'mode')])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        run_sync(self._apply(props))
        return dynamic.CreateResult(id_=_identifier(props, str(props['path'])), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'path')
        if replaces:
            # Created before deleted: the paths differ, so the new file exists
            # before the old one goes, and no moment passes with neither there.
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, FILE_DECLARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        run_sync(self._apply(news))
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    async def _apply(self, props: Mapping[str, Any]) -> None:
        async with open_transport(device(props)) as transport:
            await transport.write(
                str(props['path']),
                str(props['content']).encode(),
                mode=str(props['mode']),
                owner=_owner(props),
            )
            await run_hook(transport, props)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        path = str(props['path'])
        async with open_transport(device(props)) as transport:
            stat = await transport.stat(path)
            if stat is None:
                return True
            if not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat):
                return True
            return await transport.read(path) != str(props['content']).encode()

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        path = str(props['path'])
        async with open_transport(device(props)) as transport:
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
        async with open_transport(device(props)) as transport:
            await transport.remove(str(props['path']))
            # The hook runs on the way out too: whatever reads this file has to
            # be told it is gone, exactly as it was told it had arrived.
            await run_hook(transport, props)


@final
class GwArtifactProvider(dynamic.ResourceProvider):
    """A digest-pinned payload on the device, tracked by a marker file."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        failures = [*_absolute(news, 'target'), *_absolute(news, 'extract'), *_octal(news, 'mode')]
        sha256 = news.get('sha256')
        if sha256 is not None and not is_unknown(sha256) and not _is_sha256(str(sha256)):
            failures.append(dynamic.CheckFailure('sha256', f'must be a hex SHA-256 digest, got {sha256!r}'))
        return dynamic.CheckResult(news, failures)

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        self._push(props)
        return dynamic.CreateResult(id_=_identifier(props, str(props['target'])), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'target')
        if replaces:
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, ARTIFACT_DECLARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        self._push(news)
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    def _push(self, props: Mapping[str, Any]) -> None:
        """Fetch, verify, then land. Nothing reaches the device unverified."""
        url = str(props['url'])
        expected = str(props['sha256']).lower()
        data = fetch(url)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise DigestMismatch(url, expected, actual)
        # Decompressed only after the pin has been checked, so what is unpacked
        # is a function of bytes this runner has already vouched for.
        payload = plain_archive(data) if _extract(props) else data
        run_sync(self._land(props, payload, actual))

    async def _land(self, props: Mapping[str, Any], data: bytes, digest: str) -> None:
        target = str(props['target'])
        tree = _extract(props)
        async with open_transport(device(props)) as transport:
            await transport.write(target, data, mode=str(props['mode']), owner=_owner(props))
            if tree:
                await self._unpack(transport, target, tree)
                # Before the hook, because the hook is what notices: the estate's
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
        pinned = str(props['sha256']).lower()
        async with open_transport(device(props)) as transport:
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
        async with open_transport(device(props)) as transport:
            stat = await transport.stat(target)
            marker = None if stat is None else await transport.read(marker_path(target))
            if stat is None or marker is None:
                # A payload with no marker is a payload of unknown provenance,
                # which is the same situation as no payload at all.
                return gone()
            claimed = marker.decode(errors='replace').strip()
            if tree and not await _tree_holds(transport, tree, claimed):
                # The archive alone is not the resource: what the estate runs is
                # the tree, and a tree someone removed is a resource to create.
                return gone()
            outs = {
                **props,
                'sha256': claimed,
                'mode': stat.mode,
                'owner': _observed_owner(props.get('owner'), stat),
            }
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        target = str(props['target'])
        tree = _extract(props)
        async with open_transport(device(props)) as transport:
            await transport.remove(marker_path(target))
            await transport.remove(target)
            if tree:
                await transport.remove(marker_path(tree))
                _ = await transport.run(purge_script(tree))
            await run_hook(transport, props)


@final
class GwFile(dynamic.Resource, module='gateway', name='File'):
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
            GwFileProvider(),
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
class GwArtifact(dynamic.Resource, module='gateway', name='Artifact'):
    """A digest-pinned payload -- a container root filesystem, a release tarball."""

    url: pulumi.Output[str]
    sha256: pulumi.Output[str]
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
        url: pulumi.Input[str],
        sha256: pulumi.Input[str],
        target: pulumi.Input[str],
        extract: pulumi.Input[str] | None = None,
        mode: pulumi.Input[str] = '0644',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `target` holds the bytes `sha256` names.

        The bytes never enter state: `url` is fetched on the runner at apply
        time, checked against `sha256`, and streamed to the device, which keeps a
        marker beside the payload naming what it holds.

        `extract` names a directory the payload is unpacked into, for an archive
        rather than a single file, and the resource owns that directory: it
        replaces it whole on a new pin and removes it on a delete. Only `target`
        is a replacement, so a caller that moves the tree is expected to move the
        archive with it -- both are named after the same thing, and a tree left
        at the old path would be an orphan nothing declares.
        """
        super().__init__(
            GwArtifactProvider(),
            name,
            {
                **connection.props(),
                'url': url,
                'sha256': sha256,
                'target': target,
                'extract': extract,
                'mode': mode,
                'owner': owner,
                'hook': hook,
            },
            pulumi.ResourceOptions.merge(
                pulumi.ResourceOptions(additional_secret_outputs=secret_outputs()),
                opts,
            ),
        )


def _identifier(props: Mapping[str, Any], location: str) -> str:
    return f'{props["username"]}@{props["host"]}:{props["port"]}{location}'


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


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in '0123456789abcdefABCDEF' for character in value)
