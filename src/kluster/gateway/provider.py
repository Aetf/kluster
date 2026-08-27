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
    holds. A preview compares two hashes, never megabytes.

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
a key that no longer opens the door.

**Order within an apply is chosen so that a failure is visible.** The payload
lands, then the hook runs, then the marker is written: a hook that fails leaves
the device without the marker, so the next preview still sees work to do. A
failed create raises, which means the resource is not recorded -- the file may
already exist on the device, and the retry rewrites the same bytes, both
operations being idempotent by construction.

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
from collections.abc import Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic
import requests
from pulumi.runtime import rpc

from kluster.gateway import ssh

__all__ = (
    'ARTIFACT_DECLARED',
    'CREDENTIALS',
    'FILE_DECLARED',
    'IDENTITY',
    'Connection',
    'DigestMismatch',
    'GwArtifact',
    'GwArtifactProvider',
    'GwFile',
    'GwFileProvider',
    'HookFailed',
    'fetch',
    'gone',
    'marker_path',
    'open_transport',
    'secret_outputs',
)

#: Properties that name *which* device, rather than what is on it. A change to
#: any of them moves the file to a different box, which is a replacement: the new
#: device gets the file and the old one loses it.
IDENTITY = ('host', 'port', 'username')

#: The credentials the session is opened with. They say nothing about the file's
#: contents, but they are declared inputs, so a rotation is a change -- see the
#: module docstring on why "no change" would be a trap.
CREDENTIALS = ('private_key', 'host_key')

#: What a `GwFile` declares about the device beyond which device it is.
FILE_DECLARED = ('content', 'mode', 'owner', 'hook', *CREDENTIALS)

#: The same for a `GwArtifact`. The digest is the payload's identity and the URL
#: is only where the bytes were found, but both are declared, so moving a release
#: to another mirror is a diff a reviewer sees.
ARTIFACT_DECLARED = ('url', 'sha256', 'mode', 'owner', 'hook', *CREDENTIALS)

#: How long the runner may spend fetching an artifact before giving up.
FETCH_TIMEOUT = 300

#: The mode a digest marker is written with. The marker is the provider's own
#: bookkeeping rather than part of the estate, so it does not inherit the
#: payload's ownership.
MARKER_MODE = '0644'


@final
class HookFailed(Exception):
    """A post-apply hook exited non-zero, so the apply did not converge."""

    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'post-apply hook `{command}` exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.command: str = command
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


def marker_path(target: str) -> str:
    """Where the digest of the payload at `target` is recorded on the device."""
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

    A property that is still unknown cannot have been shown to differ, so it is
    never a reason to plan a replacement; the unknown diff that follows says so
    honestly instead.
    """
    return [key for key in (location, *IDENTITY) if not is_unknown(news.get(key)) and olds.get(key) != news.get(key)]


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
        failures = [*_absolute(news, 'target'), *_octal(news, 'mode')]
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
        run_sync(self._land(props, data, actual))

    async def _land(self, props: Mapping[str, Any], data: bytes, digest: str) -> None:
        target = str(props['target'])
        async with open_transport(device(props)) as transport:
            await transport.write(target, data, mode=str(props['mode']), owner=_owner(props))
            await run_hook(transport, props)
            # Written last, and only once everything else has succeeded: the
            # marker is the claim that this device holds these bytes and has
            # acted on them.
            await transport.write(marker_path(target), f'{digest}\n'.encode(), mode=MARKER_MODE, owner=None)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        target = str(props['target'])
        async with open_transport(device(props)) as transport:
            stat = await transport.stat(target)
            if stat is None:
                return True
            if not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat):
                return True
            marker = await transport.read(marker_path(target))
            return marker is None or marker.decode(errors='replace').strip() != str(props['sha256']).lower()

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        target = str(props['target'])
        async with open_transport(device(props)) as transport:
            stat = await transport.stat(target)
            marker = None if stat is None else await transport.read(marker_path(target))
            if stat is None or marker is None:
                # A payload with no marker is a payload of unknown provenance,
                # which is the same situation as no payload at all.
                return gone()
            outs = {
                **props,
                'sha256': marker.decode(errors='replace').strip(),
                'mode': stat.mode,
                'owner': _observed_owner(props.get('owner'), stat),
            }
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        target = str(props['target'])
        async with open_transport(device(props)) as transport:
            await transport.remove(marker_path(target))
            await transport.remove(target)
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
        mode: pulumi.Input[str] = '0644',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `target` holds the bytes `sha256` names.

        The bytes never enter state: `url` is fetched on the runner at apply
        time, checked against `sha256`, and streamed to the device, which keeps a
        marker beside the payload naming what it holds.
        """
        super().__init__(
            GwArtifactProvider(),
            name,
            {
                **connection.props(),
                'url': url,
                'sha256': sha256,
                'target': target,
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


def _owner(props: Mapping[str, Any]) -> str | None:
    owner = props.get('owner')
    return str(owner) if owner else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in '0123456789abcdefABCDEF' for character in value)
