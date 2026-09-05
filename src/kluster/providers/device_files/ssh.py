"""The transport the gateway provider writes through: SSH to one pinned device.

The device is a router reached over a management overlay, so its host key is
**pinned**: the trusted-key list is built from the key the caller declared and
from nothing else. `asyncssh` reads `~/.ssh/known_hosts` when `known_hosts` is
left at its default and validates nothing at all when it is `None`; neither is
ever constructed here, because a first contact that accepts whatever answers
would hand an interposer root on the router. The same reasoning switches off the
other two ambient inputs a client normally has -- no `ssh-agent`
(`agent_path=None`) and no OpenSSH client configuration (`config=None`) -- so the
session a continuous-integration runner opens is the session a workstation opens.

The pin is passed as a *parsed key object*. Handing `known_hosts` a bare
`ssh-ed25519 AAAA…` string does not pin anything: asyncssh treats a string in
that position as the name of a file to read, and the connection fails with a
`FileNotFoundError` naming the key.

The surface is deliberately five verbs -- read a file, stat a file, write a file,
remove a file, run a command. Everything the provider does is one of those, so a
test double is a dictionary of files and a list of commands rather than a shell
emulator. The verbs are `sh` one-liners over an exec channel rather than SFTP:
UniFi OS is not guaranteed to offer the SFTP subsystem, while a shell is the one
thing an SSH server on such a device always has.

Writes are staged: the bytes land in a sibling temporary file, take their mode
and ownership there, and are moved into place with `mv`, which is atomic within a
filesystem. A write interrupted halfway therefore leaves the previous file intact
and a stale temporary beside it, which the next write overwrites.

**The two verbs that change the device refuse to act on what they were not
given.** A write refuses a symbolic link at the path rather than replacing it
or landing the bytes inside whatever it points at (`symlink_test`), and
refuses anything else that is not a regular file rather than moving the staged
file into it (`regular_file_test`); a remove refuses a link rather than taking
away an indirection the caller did not put there. `stat` answers about the path
itself rather than through it, which is how a caller learns what is there
before it asks for either.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import IntEnum, unique
from typing import Protocol, cast, final

import asyncssh
from asyncssh.known_hosts import KnownHostsArg

from kluster.providers.device_files import paths

__all__ = (
    'DEFAULT_TIMEOUT',
    'DIRECTORY',
    'REGULAR_EMPTY_FILE',
    'REGULAR_FILE',
    'SYMBOLIC_LINK',
    'CommandFailed',
    'CommandResult',
    'Device',
    'DeviceError',
    'FileStat',
    'HostKeyRefused',
    'PinRejected',
    'ReservedStatus',
    'Runner',
    'SshTransport',
    'SymbolicLinkAtPath',
    'Transport',
    'WrongKindAtPath',
    'client_credential',
    'connect',
    'pinned_host_keys',
    'same_mode',
    'same_owner',
    'regular_file_test',
    'symlink_test',
)

#: How long a single command or the handshake may take. Long enough for the
#: device's own `skopeo copy` to download a root filesystem image and for
#: `umoci` to write it out, each of which is one command; short enough that an
#: unreachable device fails its resource instead of hanging the deployment.
DEFAULT_TIMEOUT = 600.0

#: The words `stat`'s `%F` gives for the kinds a caller here asks about. A
#: resource compares them because existence alone does not say what is at a
#: path. A file with nothing in it is `regular empty file` rather than
#: `regular file`, so both words mean the one kind.
DIRECTORY = 'directory'
SYMBOLIC_LINK = 'symbolic link'
REGULAR_FILE = 'regular file'
REGULAR_EMPTY_FILE = 'regular empty file'

#: The suffix a staged write uses before it is moved into place.
STAGING_SUFFIX = '.kluster-staged'


@unique
class ReservedStatus(IntEnum):
    """The exit statuses this module speaks, for answers a shell has none for.

    **Membership**: a script here exits one of these when the shell has no
    status that means what happened, and the caller has to tell that answer
    apart from a command's own failure. Anything a command already reports for
    itself stays the command's, message and all.

    **Range**: `available`, and every member is drawn from it. Statuses below it
    are what commands exit for their own failures, and the band from 126 up is
    the shell's own -- `command not found`, a file that cannot be executed, and
    the 128 + signal a killed process reports. A member outside the range would
    collide with an answer the session did not mean, which is why adding a
    fourth is a change to this class rather than a new name beside it.
    """

    #: There is no such path. `cat` and `stat` both exit 1 for an absent file
    #: *and* for one that cannot be read, and those two are not the same
    #: answer: the first is a resource to create, the second is a fault to
    #: report.
    ABSENT = 42
    #: This directory has something in it, which `rmdir` reports with the same
    #: status as a directory it may not touch, in whatever language the session
    #: speaks.
    NOT_EMPTY = 43
    #: A symbolic link is at this path, which no command reports with a status
    #: of its own: the ones that would converge it follow it, and `rmdir`
    #: refuses it with the status it gives everything else.
    SYMBOLIC_LINK = 44
    #: Something of another kind is at this path -- a directory where a file is
    #: declared, most of all. `mv -f` moves the staged file *into* a directory
    #: and reports success, so this is the one wrong kind no command complains
    #: about.
    WRONG_KIND = 45

    def __init__(self, status: int) -> None:
        if status not in self.available():
            raise ValueError(f'{status} is outside the range this class reserves ({self.available()})')

    @classmethod
    def available(cls) -> range:
        """The statuses a member may be drawn from, and the whole of it."""
        return range(42, 64)


class DeviceError(Exception):
    """Anything that went wrong between here and the device."""


@final
class PinRejected(DeviceError):
    """A key the session would need is unusable, so no session is opened."""


@final
class HostKeyRefused(DeviceError):
    """The device presented a host key that is not the pinned one.

    This is the interposition case, and it is fatal by construction: there is no
    prompt, no cache, and no first-contact exception to fall back on.
    """


@final
class SymbolicLinkAtPath(DeviceError):
    """A symbolic link stands where a path was declared, so nothing acted on it.

    Why a link is refused rather than followed or replaced is the rule the
    resources above share, stated in `device_files.provider`.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f'refusing to act on {path}: it is a symbolic link, and the declaration names that path '
            f'rather than whatever the link points at'
        )
        self.path: str = path


@final
class WrongKindAtPath(DeviceError):
    """Something of another kind stands where a file was declared.

    The same rule as `SymbolicLinkAtPath` and the same reason: a declaration
    names what it declares, and a path holding something else is a decision for
    whoever put it there.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f'refusing to write {path}: it holds something that is not a regular file, '
            f'and the declaration names a file at that path'
        )
        self.path: str = path


@final
class CommandFailed(DeviceError):
    """A command exited non-zero."""

    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'`{command}` exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.command: str = command
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
@dataclass(frozen=True)
class Device:
    """Everything needed to open the one session, and nothing else.

    `host_key` is the device's public key in `authorized_keys` form
    (`ssh-ed25519 AAAA…`, no host name in front of it) and `private_key` is the
    client credential in PEM or OpenSSH form. Both are plain strings because they
    arrive from resource inputs, and neither has a default: an unset credential
    must fail to build a device rather than quietly fall back on whatever the
    runner's home directory holds.
    """

    host: str
    username: str
    private_key: str
    host_key: str
    port: int = 22


@final
@dataclass(frozen=True)
class FileStat:
    """What the device says about a path that exists.

    `kind` is `stat`'s own description of it -- `directory`, `regular file`,
    `symbolic link` -- because existence alone does not say what is there, and a
    resource that declares a directory has to be able to tell one from a file
    somebody left at the same path. `stat` is asked about the path itself
    rather than through it, so a link is reported as a link.
    """

    owner: str
    group: str
    mode: str
    size: int
    kind: str

    @property
    def is_directory(self) -> bool:
        return self.kind == DIRECTORY

    @property
    def is_symbolic_link(self) -> bool:
        return self.kind == SYMBOLIC_LINK

    @property
    def is_regular_file(self) -> bool:
        return self.kind in (REGULAR_FILE, REGULAR_EMPTY_FILE)


@final
@dataclass(frozen=True)
class CommandResult:
    """The outcome of one command, whatever its exit status."""

    exit_status: int
    stdout: bytes
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def text(self) -> str:
        return self.stdout.decode(errors='replace')


class Transport(Protocol):
    """The device as the provider sees it.

    A transport is opened per operation and closed with it, so an implementation
    may hold a connection but need not: nothing here is stateful across calls.
    """

    async def read(self, path: str) -> bytes | None:
        """The file's bytes, or `None` if there is no such file."""
        ...

    async def stat(self, path: str) -> FileStat | None:
        """What the path is and whose it is, or `None` if there is no such path."""
        ...

    async def write(self, path: str, data: bytes, *, mode: str, owner: str | None) -> None:
        """Put `data` at `path`, creating parent directories, atomically.

        A symbolic link at `path` raises `SymbolicLinkAtPath` and anything else
        that is not a regular file raises `WrongKindAtPath`; nothing is written
        either way.
        """
        ...

    async def remove(self, path: str) -> None:
        """Delete `path`. Removing what is not there is not an error.

        A symbolic link at `path` raises `SymbolicLinkAtPath` and stays where
        it is.
        """
        ...

    async def run(self, command: str) -> CommandResult:
        """Run a command through the device's shell and report how it went."""
        ...


def pinned_host_keys(host_key: str) -> KnownHostsArg:
    """The trusted-key lists that accept exactly the declared key.

    asyncssh reads this tuple as (trusted host keys, trusted certificate
    authorities, revoked keys) followed by four X.509 sequences. Everything but
    the first list is empty, which is the whole point: no authority may vouch
    for a substitute key, and no certificate stands in for one.
    """
    text = host_key.strip()
    if not text:
        raise PinRejected('no host key was pinned, and an unpinned session is not opened')
    try:
        key = asyncssh.import_public_key(text)
    except asyncssh.KeyImportError as exc:
        raise PinRejected(f'the pinned host key is not a public key: {exc}') from exc
    return ([key], [], [], [], [], [], [])


def client_credential(private_key: str) -> asyncssh.SSHKey:
    """The client key the session authenticates with."""
    try:
        return asyncssh.import_private_key(private_key)
    except asyncssh.KeyImportError as exc:
        raise PinRejected(f'the client private key could not be read: {exc}') from exc


class Runner(Protocol):
    """The one asyncssh method the transport uses, named so a test can stand in."""

    async def run(
        self,
        command: str,
        *,
        input: bytes,  # noqa: A002 -- asyncssh's parameter name
        encoding: None,
        check: bool,
        timeout: float,
    ) -> asyncssh.SSHCompletedProcess: ...


@final
class SshTransport:
    """`Transport` over one live connection."""

    def __init__(self, connection: Runner, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._connection: Runner = connection
        self._timeout: float = timeout

    async def read(self, path: str) -> bytes | None:
        quoted = shlex.quote(path)
        result = await self._sh(f'if [ ! -f {quoted} ]; then exit {ReservedStatus.ABSENT}; fi; cat {quoted}')
        if result.exit_status == ReservedStatus.ABSENT:
            return None
        _check(f'read {path}', result)
        return result.stdout

    async def stat(self, path: str) -> FileStat | None:
        quoted = shlex.quote(path)
        script = f"if [ ! -e {quoted} ]; then exit {ReservedStatus.ABSENT}; fi; stat -c '%U %G %a %s %F' {quoted}"
        result = await self._sh(script)
        if result.exit_status == ReservedStatus.ABSENT:
            return None
        _check(f'stat {path}', result)
        # The kind is last and is the one field with spaces in it (`regular
        # empty file`), so it takes whatever remains of the line.
        fields = result.text.split(None, 4)
        if len(fields) != 5:
            raise CommandFailed(f'stat {path}', result.exit_status, f'unreadable stat output: {result.text!r}')
        owner, group, mode, size, kind = fields
        return FileStat(owner=owner, group=group, mode=mode, size=int(size), kind=kind.strip())

    async def write(self, path: str, data: bytes, *, mode: str, owner: str | None) -> None:
        """The staged write, refusing whatever the declaration did not name.

        Both refusals are the script's first acts, in the same shell run as the
        `mv` they guard, because `mv -f` is silent about everything it should
        not do: it moves the staged file *into* a directory or a link that
        points at one, and *over* a link that points at a file. The parent it
        creates is the one `paths` gives, so the directory made is the one the
        declared path sits in and no other spelling of it.
        """
        staged = shlex.quote(f'{path}{STAGING_SUFFIX}')
        script = [
            symlink_test(path),
            regular_file_test(path),
            f'mkdir -p {shlex.quote(paths.parent(path))}',
            f'cat > {staged}',
            f'chmod {shlex.quote(mode)} {staged}',
        ]
        if owner:
            script.append(f'chown {shlex.quote(owner)} {staged}')
        script.append(f'mv -f {staged} {shlex.quote(path)}')
        result = await self._sh(' && '.join(script), stdin=data)
        if result.exit_status == ReservedStatus.SYMBOLIC_LINK:
            raise SymbolicLinkAtPath(path)
        if result.exit_status == ReservedStatus.WRONG_KIND:
            raise WrongKindAtPath(path)
        _check(f'write {path}', result)

    async def remove(self, path: str) -> None:
        """The delete, refusing a link at the path.

        `rm -f` takes a link away without following it, so the guard is what
        keeps an indirection nobody here declared from being deleted by a
        resource that only ever named the path.
        """
        result = await self._sh(f'{symlink_test(path)} && rm -f {shlex.quote(path)}')
        if result.exit_status == ReservedStatus.SYMBOLIC_LINK:
            raise SymbolicLinkAtPath(path)
        _check(f'remove {path}', result)

    async def run(self, command: str) -> CommandResult:
        return await self._sh(command)

    async def _sh(self, command: str, *, stdin: bytes | None = None) -> CommandResult:
        # `encoding=None` keeps stdout bytes: an artifact marker is text but a
        # payload is not, and one channel serves both.
        completed = await self._connection.run(
            command,
            input=stdin if stdin is not None else b'',
            encoding=None,
            check=False,
            timeout=self._timeout,
        )
        stdout = cast('bytes | None', completed.stdout) or b''
        stderr = cast('bytes | None', completed.stderr) or b''
        status = completed.exit_status
        return CommandResult(
            # A process killed by a signal reports no status at all; treating
            # that as a failure is the only safe reading.
            exit_status=status if isinstance(status, int) else 1,
            stdout=stdout,
            stderr=stderr.decode(errors='replace'),
        )


@asynccontextmanager
async def connect(device: Device, *, timeout: float = DEFAULT_TIMEOUT) -> AsyncGenerator[Transport]:
    """Open one session to one device, refusing anything but the pinned key."""
    known_hosts = pinned_host_keys(device.host_key)
    client_key = client_credential(device.private_key)
    try:
        connection = await asyncio.wait_for(
            asyncssh.connect(
                device.host,
                port=device.port,
                username=device.username,
                client_keys=[client_key],
                known_hosts=known_hosts,
                agent_path=None,
                config=None,
            ),
            timeout,
        )
    except asyncssh.HostKeyNotVerifiable as exc:
        raise HostKeyRefused(
            f'{device.host} presented a host key that is not the pinned one; refusing the session'
        ) from exc
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise DeviceError(f'could not reach {device.username}@{device.host}:{device.port}: {exc}') from exc
    async with connection:
        yield SshTransport(connection, timeout=timeout)


def symlink_test(path: str) -> str:
    """The test that refuses a symbolic link at a declared path.

    Every script here begins with it, because every command after it follows a
    link, displaces one or deletes one (`SymbolicLinkAtPath`). It asks `-L`
    rather than about the kind wanted, because a link is what every command is
    silent about: `mkdir -p` refuses a regular file at the path and says so in
    the device's own words, while `mv -f`, `chmod` and `rmdir` each do
    something to a link that nobody asked for.

    An `if` whose condition is false exits zero, so the fragment chains with
    `&&` in front of the commands it guards.
    """
    quoted = shlex.quote(path)
    return f'if [ -L {quoted} ]; then exit {ReservedStatus.SYMBOLIC_LINK}; fi'


def regular_file_test(path: str) -> str:
    """The test that refuses anything but a regular file at a declared path.

    Only a write needs it, and it needs it because `mv -f` is the one command
    here that is silent about a wrong kind: given a directory at the path it
    moves the staged file *inside* and exits zero, so the apply reports success
    and the next preview asks for the same write again, forever
    (`WrongKindAtPath`). Where a directory is declared the device says it
    itself -- `mkdir -p` refuses a file, `rmdir` refuses a non-directory.

    It follows `symlink_test` rather than standing on its own: `[ -e ]` and
    `[ -f ]` both resolve a link, so at a link to a file this test would find a
    regular file and pass.
    """
    quoted = shlex.quote(path)
    return f'if [ -e {quoted} ] && [ ! -f {quoted} ]; then exit {ReservedStatus.WRONG_KIND}; fi'


def same_mode(left: str, right: str) -> bool:
    """Whether two octal modes mean the same thing (`0644` is `644`)."""
    try:
        return int(left, 8) == int(right, 8)
    except ValueError:
        return left == right


def same_owner(declared: str | None, stat: FileStat) -> bool:
    """Whether the device's ownership matches what was declared.

    A declaration may name a user (`root`) or a user and a group (`root:root`);
    naming neither means the file's ownership is not this resource's business.
    """
    if not declared:
        return True
    user, _, group = declared.partition(':')
    return stat.owner == user and (not group or stat.group == group)


def _check(what: str, result: CommandResult) -> None:
    if not result.ok:
        raise CommandFailed(what, result.exit_status, result.stderr)
