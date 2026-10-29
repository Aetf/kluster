"""The Talos Image Factory's artifacts, on the machine running the program.

**The worker's artifact takes a detour through the machine running the
program.** The factory serves `nocloud` as `.raw.xz`; the libvirt provider does
not decompress an xz source (dmacvicar/terraform-provider-libvirt#390) and a
raw image cannot back a copy-on-write chain, so there is no way to hand libvirt
the factory URL directly. `FactoryImage` fetches and decompresses it where the
program runs, and the libvirt volume is created *from that file*, which the
provider uploads into the pool over its own connection.

A dynamic provider rather than `local.Command`
(`docs/rfc/rfc-002-src-layout-and-the-gateway.md` §7.3): the download is
checked for truncation against the length the server declared, it needs neither
`curl` nor `xz` on the machine running the program, and the seam a test
replaces is a Python function rather than a shell command.

**The file can be longer than the artifact.** A volume created from a file
takes the file's length as its capacity, and a Talos disk booted in place
needs room past the image's last partition for the partitions Talos creates
there at first boot. So the decompressed artifact is extended by `room` bytes
of zeros, sparsely: the extension costs the machine running the program no
space, and how much room a disk needs is the consumer's to say.

**A delete removes only the file its own create left.** The object a
`FactoryImage` stands for is the file, not anything at the factory: the
factory names an artifact by the schematic's content and offers no delete.
The file's path is the caller's, and one declaration names one path on every
run and every machine, so a replacement that declares the same artifact, a
targeted `--replace` say, declares its original's path, finds the file there
and keeps it, and the engine then deletes the original. Each create therefore
records the identity of its own last write to the file, `written`, and the
delete unlinks the file only while it still carries that identity. A file with
another identity, a replacement's write say, is left in place, with a
warning. The identity tells a resource's write apart from its own
replacement's and from no other resource's, which is why a caller gives every
resource a path of its own (`materialize`).

What is *done* with an artifact — which schematic it was built from, which
volume or catalog it feeds — belongs to the components in
`kluster.components.talos`, not here.
"""

from __future__ import annotations

import lzma
import os
import tempfile
from pathlib import Path
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic
import requests
from pulumi.runtime import rpc

__all__ = (
    'ARTIFACT_MODE',
    'CHUNK_BYTES',
    'ERROR_BODY_BYTES',
    'FETCH_TIMEOUT',
    'WRITTEN',
    'FactoryImage',
    'FactoryImageProvider',
    'FactoryRefused',
    'TruncatedArtifact',
    'fetch',
    'materialize',
)

#: How long a single read from the factory may stall before the fetch is
#: abandoned. Not a budget for the whole transfer — the artifact is large and a
#: slow link is not a failure; a link that has stopped moving is.
FETCH_TIMEOUT = 60

#: Bytes pulled from the network per decompression step. The artifact never
#: exists in memory whole, in either form.
CHUNK_BYTES = 1 << 20

#: The mode the finished artifact is left at. `tempfile` creates the download
#: private to its owner, and this is a public image whose only secret is the
#: bandwidth it took to get.
ARTIFACT_MODE = 0o644


#: The output holding the identity of the resource's own write to the file.
#: State records every identity under this name, and nothing rewrites it,
#: so a rename needs a migration: a resource recorded under the old name
#: reads as one with nothing recorded, and its delete unlinks the file.
WRITTEN = 'written'


@final
class TruncatedArtifact(Exception):
    """The stream ended before the compressed artifact did."""

    def __init__(self, url: str) -> None:
        super().__init__(f'{url} ended mid-stream: the artifact is incomplete and was not kept')
        self.url: str = url


@final
class FactoryRefused(Exception):
    """The factory answered with an error status, and said why in the body.

    The factory builds an artifact when it is first asked for, and a schematic
    it cannot build — an extension name it does not list for the release, say
    — is refused there, with the reason as the response body. A status code
    alone names none of that, so the body travels with the error.
    """

    def __init__(self, url: str, status: int, reason: str) -> None:
        super().__init__(f'{url} answered HTTP {status}: {reason or "(no reason given)"}')
        self.url: str = url
        self.status: int = status
        self.reason: str = reason


#: How much of an error response's body is kept as its reason. An error body
#: is a sentence; this only bounds what a misbehaving server can make the
#: program hold.
ERROR_BODY_BYTES = 1 << 14


def fetch(url: str) -> requests.Response:
    """Open the artifact's byte stream. The one seam a test replaces.

    An error status raises `FactoryRefused`, carrying the start of the body,
    which is where the factory says what it refused.
    """
    response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT)
    if not response.ok:
        with response:
            body = next(response.iter_content(ERROR_BODY_BYTES), b'')
        raise FactoryRefused(url, response.status_code, body.decode(errors='replace').strip())
    return response


def materialize(url: str, path: Path, *, room: int) -> None:
    """Leave the artifact at `url` sitting decompressed at `path`, followed by `room` bytes of zeros.

    A file already at `path` is the artifact and is reused: the download lands
    under a temporary name in the same directory and is renamed only once the
    xz stream has ended cleanly, so a file under the final name is whole by
    construction. That is what keeps a re-created resource from spending a
    gigabyte of bandwidth again.

    The reuse is for a resource's own replacement, which declares its
    original's path. No identity tells a delete who else declares a file. So a
    file two live resources declare is the last writer's, and that writer's
    delete removes it from under the other. A caller gives every resource a
    path no other resource declares. On a machine that several stacks' runs
    share, that means a path the stack's own name is part of.

    Fetching and decompressing are the same pass. Neither form of the artifact
    is ever held in memory, and a failure removes the partial file rather than
    leaving something that looks finished. The room is added before the
    rename too, so a file under the final name has it, and a consumer that
    wants another room names another path.

    Which of the two it does -- the pass, or reusing the file -- it says
    first, through `pulumi.log`, which `pulumi` draws on the stack's row as the
    run goes; the standard `logging` module would reach nobody in the process a
    dynamic provider runs in.
    """
    if path.exists():
        pulumi.log.info(f'{path} holds the artifact already: reused, nothing fetched')
        return
    pulumi.log.info(f'fetching {url} and decompressing it into {path} as it arrives')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f'{path.name}.', suffix='.part')
    partial = Path(name)
    try:
        decompressor = lzma.LZMADecompressor()
        # The sink is entered first so that a fetch which raises still closes
        # the descriptor `mkstemp` handed over.
        with os.fdopen(descriptor, 'wb') as sink, fetch(url) as response:
            for chunk in response.iter_content(CHUNK_BYTES):
                _ = sink.write(decompressor.decompress(chunk))
            if not decompressor.eof:
                raise TruncatedArtifact(url)
            # Past the end of what was written: on POSIX, growing a file this
            # way reads back as zeros and allocates nothing.
            _ = sink.truncate(sink.tell() + room)
        partial.chmod(ARTIFACT_MODE)
        # Atomic, and within one directory so it stays atomic: either the
        # whole artifact is under its final name or nothing is.
        _ = partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _written(path: Path) -> str:
    """Set the artifact's mode, and answer the identity of that write: the file number and status-change time.

    Run on a file the create reused as much as on one it fetched, since the
    `chmod` is the write: a `chmod` to the mode a file already has still marks
    its status-change time (POSIX `chmod()`, "shall mark for update the last
    file status change timestamp"), so a reused file gets an identity of its
    own while the gigabyte it holds is kept. The mark is a time from the file
    system's clock, so it tells apart only writes that clock tells apart. A
    replacement is created in a later run than its original. Two writes
    within one tick of a file system that stamps coarsely, ZFS among them,
    share a time. Neither half is derived from the
    content, which a replacement writing the same artifact shares, and the
    pair survives a later write at the same path, which a file number alone,
    allocated again once freed, does not.

    A string rather than numbers: a number crosses the engine as a float,
    and a status-change time in nanoseconds is past the integers a float holds
    exactly.
    """
    path.chmod(ARTIFACT_MODE)
    return _identity(path.stat())


def _identity(status: os.stat_result) -> str:
    return f'{status.st_ino} {status.st_ctime_ns}'


#: Every input, each of which makes the file another file.
_IDENTITY = ('url', 'path', 'room')


def _is_unknown(value: Any) -> bool:
    """Whether a property is still a preview placeholder."""
    return isinstance(value, str) and value == rpc.UNKNOWN


@final
class FactoryImageProvider(dynamic.ResourceProvider):
    """One factory artifact, decompressed on whatever machine runs the program.

    There is no update: the resource *is* a particular artifact at a particular
    path with a particular room after it, so a different schematic, a different
    Talos version or a different room is a different file and a replacement.

    **`read` is deliberately the inherited one, which reports no drift.** The
    file is a build artifact rather than managed state — a CI runner is fresh
    every time and has none of them — and a refresh that called a missing file
    a deleted resource would take the worker's disk down with it. It also
    hands back `written` as the create recorded it rather than reading it off
    the file: a refresh reads an original awaiting its delete as well, and one
    that recorded what it found there would give the original the identity of
    its replacement's write.
    """

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        path = Path(str(props['path']))
        # A number crosses the engine as a float, whatever the program declared.
        materialize(str(props['url']), path, room=int(props['room']))
        return dynamic.CreateResult(id_=str(path), outs={**props, WRITTEN: _written(path)})

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if any(_is_unknown(news.get(key)) for key in _IDENTITY):
            # The schematic has not been created yet, so what the factory will
            # serve is not knowable here; the engine plans on "unknown" rather
            # than on a guess.
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in _IDENTITY if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            # The paths differ whenever the artifact does, so the new file can
            # exist before the old one goes.
            delete_before_replace=False,
        )

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        """Unlink the file while it is still the one this resource's create left.

        Another identity at the path is another write, by a replacement or on
        another machine than the create ran on, or a status change since then,
        and the file is left: the delete says so and returns rather than failing,
        since an original whose delete fails stays in state and that delete
        fails again on every later run. A resource with nothing recorded
        unlinks the file, as every delete did before the create recorded an
        identity. The provider has no update to record one later, so such a
        resource keeps none until it is replaced. If that replacement reused
        the file, the original's delete removes it.

        The check and the unlink are two calls, so a write landing between
        them would be removed. Nothing writes the path at that point: the
        engine deletes an original only after its replacement's create has
        returned, and no other resource declares the path, in this stack or
        another (`materialize`).
        """
        path = Path(str(props['path']))
        try:
            status = path.stat()
        except FileNotFoundError:
            return
        recorded = props.get(WRITTEN)
        if recorded is not None and recorded != _identity(status):
            pulumi.log.warn(f'{path} is not the file this resource recorded, so it is left in place')
            return
        path.unlink(missing_ok=True)


@final
class FactoryImage(dynamic.Resource, module='talos_factory', name='FactoryImage'):
    """A factory artifact, fetched and decompressed at `path`, with `room` bytes of zeros after it."""

    url: pulumi.Output[str]
    path: pulumi.Output[str]
    room: pulumi.Output[int]

    def __init__(
        self,
        name: str,
        *,
        url: pulumi.Input[str],
        path: pulumi.Input[str],
        room: pulumi.Input[int],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `path` holds the decompressed contents of `url`, and `room` zero bytes after them.

        `path` is the caller's, and it is the same for one declaration on every
        run and on every machine rather than generated — which is what lets a
        consumer take the path as an input without proposing a change each
        time the program moves. No other resource declares it
        (`materialize`). The room is part of what the file contains: a file
        already at the path is reused as it is, so a path that did not change
        with the room would serve a file with the old one.
        """
        super().__init__(FactoryImageProvider(), name, {'url': url, 'path': path, 'room': room}, opts)
