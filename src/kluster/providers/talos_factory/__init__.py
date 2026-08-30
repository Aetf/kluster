"""The Talos Image Factory's artefacts, on the machine running the program.

**The worker's artefact takes a detour through the machine running the
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

What is *done* with an artefact — which schematic it was built from, which
volume or catalogue it feeds — belongs to the components in
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
    'ARTEFACT_MODE',
    'CHUNK_BYTES',
    'FETCH_TIMEOUT',
    'FactoryImage',
    'FactoryImageProvider',
    'TruncatedArtefact',
    'fetch',
    'materialise',
)

#: How long a single read from the factory may stall before the fetch is
#: abandoned. Not a budget for the whole transfer — the artefact is large and a
#: slow link is not a failure; a link that has stopped moving is.
FETCH_TIMEOUT = 60

#: Bytes pulled from the network per decompression step. The artefact never
#: exists in memory whole, in either form.
CHUNK_BYTES = 1 << 20

#: The mode the finished artefact is left at. `tempfile` creates the download
#: private to its owner, and this is a public image whose only secret is the
#: bandwidth it took to get.
ARTEFACT_MODE = 0o644


@final
class TruncatedArtefact(Exception):
    """The stream ended before the compressed artefact did."""

    def __init__(self, url: str) -> None:
        super().__init__(f'{url} ended mid-stream: the artefact is incomplete and was not kept')
        self.url: str = url


def fetch(url: str) -> requests.Response:
    """Open the artefact's byte stream. The one seam a test replaces."""
    response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response


def materialise(url: str, path: Path) -> None:
    """Leave the artefact at `url` sitting decompressed at `path`.

    A file already at `path` is the artefact and is reused: the download lands
    under a temporary name in the same directory and is renamed only once the
    xz stream has ended cleanly, so a file under the final name is whole by
    construction. That is what keeps a re-created resource — or a second stack
    on the same machine — from spending a gigabyte of bandwidth again.

    Fetching and decompressing are the same pass. Neither form of the artefact
    is ever held in memory, and a failure removes the partial file rather than
    leaving something that looks finished.
    """
    if path.exists():
        return
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
            raise TruncatedArtefact(url)
        partial.chmod(ARTEFACT_MODE)
        # Atomic, and within one directory so it stays atomic: either the
        # whole artefact is under its final name or nothing is.
        _ = partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _is_unknown(value: Any) -> bool:
    """Whether a property is still a preview placeholder."""
    return isinstance(value, str) and value == rpc.UNKNOWN


@final
class FactoryImageProvider(dynamic.ResourceProvider):
    """One factory artefact, decompressed on whatever machine runs the program.

    There is no update: the resource *is* a particular artefact at a particular
    path, so a different schematic or a different Talos version is a different
    file and a replacement.

    **`read` is deliberately the inherited one, which reports no drift.** The
    file is a build artefact rather than managed state — a CI runner is fresh
    every time and has none of them — and a refresh that called a missing file
    a deleted resource would take the worker's disk down with it.
    """

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        path = Path(str(props['path']))
        materialise(str(props['url']), path)
        return dynamic.CreateResult(id_=str(path), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if any(_is_unknown(news.get(key)) for key in ('url', 'path')):
            # The schematic has not been created yet, so what the factory will
            # serve is not knowable here; the engine plans on "unknown" rather
            # than on a guess.
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in ('url', 'path') if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            # The paths differ whenever the artefact does, so the new file can
            # exist before the old one goes.
            delete_before_replace=False,
        )

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        Path(str(props['path'])).unlink(missing_ok=True)


@final
class FactoryImage(dynamic.Resource, module='physical', name='FactoryImage'):
    """A factory artefact, fetched and decompressed at `path`.

    The `module` half of the type token names the stack that declares this
    resource rather than the package it lives in: the token is part of every
    URN, so it is a name of the resource and not of the source tree.
    """

    url: pulumi.Output[str]
    path: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        url: pulumi.Input[str],
        path: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `path` holds the decompressed contents of `url`.

        `path` is derived from the schematic and the Talos version rather than
        generated, so the same declaration names the same file on every run and
        on every machine — which is what lets a consumer take the path as an
        input without proposing a change each time the program moves.
        """
        super().__init__(FactoryImageProvider(), name, {'url': url, 'path': path}, opts)
