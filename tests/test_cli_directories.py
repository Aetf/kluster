"""A run of the pinned CLI keeps its temporary files in the case's own directory, a run killed mid-download included.

`pulumi` downloads a plugin into a temporary file it creates before it asks
for the plugin, and removes it once the install ends -- unless the process
is killed first, which a case's bound, a session's end or a mutation run all
do. Left in the host's temporary directory, those tarballs pile up run after
run. `scratch_projects.cli_directories` gives every run a `TMPDIR` under the
case's directory, so what a killed run leaves is the case's, and goes with it.

The case plants the kill at a known moment, with no sleep. A server on the
loopback answers the plugin's request by holding it open, and the request
reaching the server is the event that the tarball exists, since the CLI
creates it first. The block then ends, and with it the CLI, mid-download.
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pinned_tools
import process_sessions
import pytest
from scratch_projects import LEFT_BEHIND, cli_directories

#: How long the case waits for the CLI's request, and the server holds it: a
#: stop-loss below the case bound, which nothing asserts on.
WAIT = 30


class _Stopped(Exception):
    """The case leaving its block on purpose, with the download in flight."""


def test_a_run_killed_mid_download_leaves_its_tarball_in_the_cases_own_directory(tmp_path: Path) -> None:
    pulumi = pinned_tools.located('pulumi')
    requested, release = threading.Event(), threading.Event()

    class Holding(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.set()
            _ = release.wait(timeout=WAIT)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
            pass

    # The standard library's server on its own thread, the way its own
    # documentation runs one beside a client.
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Holding)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    # The directory a run would make its temporary files in without its own.
    outer = tmp_path / 'outer'
    outer.mkdir()
    directories = cli_directories(tmp_path / 'run')
    env = (
        {name: value for name, value in os.environ.items() if not name.startswith('PULUMI_')}
        | {'TMPDIR': str(outer)}
        | directories
        | {'PULUMI_SKIP_UPDATE_CHECK': 'true'}
    )
    try:
        with (
            pytest.raises(_Stopped),
            process_sessions.started(
                [
                    pulumi,
                    'plugin',
                    'install',
                    'resource',
                    'probe',
                    '1.0.0',
                    '--server',
                    f'http://127.0.0.1:{server.server_port}',
                ],
                env=env,
                stdout=process_sessions.DEVNULL,
                stderr=process_sessions.DEVNULL,
            ),
        ):
            assert requested.wait(timeout=WAIT), 'the CLI never asked for the plugin'
            raise _Stopped
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=WAIT)

    escaped = sorted(name for name in os.listdir(outer) if name.startswith(LEFT_BEHIND))
    kept = sorted(name for name in os.listdir(env['TMPDIR']) if name.startswith(LEFT_BEHIND))
    assert escaped == []
    # Not vacuous: the killed download did leave its tarball, in the case's own directory.
    assert [name for name in kept if name.startswith('pulumi-plugin-tar')], kept
