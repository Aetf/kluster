"""A local Helm repository server: a case's own chart, named `probe` at version 0.1.0, served from `127.0.0.1`.

A case that runs the pinned Helm against a chart takes the chart from here
rather than from the network: `served` packages the chart's files as Helm
does, writes the repository's index beside the archive in a directory of
the case's, and serves both with the standard library's server on a port
the system picks, for as long as its block lasts.
"""

from __future__ import annotations

import functools
import hashlib
import io
import tarfile
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http import server as http_server
from pathlib import Path

#: A Helm repository's index naming the one packaged chart, by a URL relative
#: to the repository's own.
INDEX = """\
apiVersion: v1
entries:
  probe:
    - apiVersion: v2
      name: probe
      version: 0.1.0
      digest: {digest}
      urls: [probe-0.1.0.tgz]
      created: "2026-01-01T00:00:00Z"
generated: "2026-01-01T00:00:00Z"
"""


def packaged(chart: Mapping[str, str]) -> bytes:
    """`chart`, files by their path under its directory, as Helm packages it: a gzipped tar under a directory named for it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path, content in chart.items():
            data = content.encode()
            member = tarfile.TarInfo(f'probe/{path}')
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@contextmanager
def served(directory: Path, chart: Mapping[str, str]) -> Generator[str]:
    """A Helm repository of the case's own, serving `chart` from `127.0.0.1` at the URL it yields."""
    directory.mkdir()
    archive = packaged(chart)
    _ = (directory / 'probe-0.1.0.tgz').write_bytes(archive)
    _ = (directory / 'index.yaml').write_text(INDEX.format(digest=hashlib.sha256(archive).hexdigest()))

    class Serving(http_server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
            pass

    # The standard library's server on its own thread, the way its own
    # documentation runs one beside a client.
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Serving, directory=str(directory)))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        serving.join()
