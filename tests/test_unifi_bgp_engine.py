"""The routing session's password stays out of the backend and out of a preview, held by the pinned `pulumi` itself.

`BgpConfiguration` carries the routing session's password inside its text, and
it marks the text secret itself, as a secret input and through
`additionalSecretOutputs`. Which of the two keeps the text out of the state
file and out of what a preview prints is not documented engine behavior: the
engine leaves an `additionalSecretOutputs` input in the clear
(`test_operator_stack.py`). So the case runs the CLI `mise.toml` pins over a
`file://` backend and a `PULUMI_HOME` of the case's own, against a program
holding the real resource, dialing the controller stand-in
(`unifi_bgp_controller.BgpConfigs`) on `127.0.0.1`, and looks for the text in
both places.
"""

from __future__ import annotations

import http.server as http_server
import json
import re
import shutil
import subprocess as sp
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import process_sessions
import pytest
from scratch_projects import scratch_project
from unifi_bgp_controller import BgpConfigs, BgpSession

import kluster
from kluster.lib import state_only
from kluster.providers.unifi_bgp import API_KEY_CONFIG

#: How long one case may take: its project's set-up and every command it runs
#: against the engine, at the bounds of `tests/test_unifi_zone_policy_order_engine.py`,
#: whose handful of commands these are. A stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

KEY = 'unifi-api-key'
SITE = 'default'
STACK = 'probe'
PASSWORD = 'a-session-password-5c1d'
CONFIG = f'router bgp 65000\n neighbor 192.0.2.10 remote-as 65001\n neighbor 192.0.2.10 password {PASSWORD}\n!\n'

#: The text arrives in plain, as a rendered string does before anything marks
#: it: the resource is what makes it a secret.
PROGRAM = """\
import os

from kluster.providers.unifi_bgp import BgpConfiguration

BgpConfiguration(
    'bgp',
    api_url=os.environ['PROBE_CONTROLLER'],
    allow_insecure=False,
    site='default',
    config=os.environ['PROBE_CONFIG'],
    description='Managed by the probe.',
    file_name='probe-frr.conf',
)
"""


class Serving(http_server.BaseHTTPRequestHandler):
    """The controller stand-in behind a socket."""

    controller: BgpConfigs

    def _v2(self, method: str) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        session = BgpSession(self.controller)
        session.headers = dict(self.headers.items())
        answered = self.controller.answer(session, method, self.path, body)
        text = answered.text if answered.payload is None else json.dumps(answered.payload)
        self.send_response(answered.status_code)
        self.end_headers()
        _ = self.wfile.write(text.encode())

    def do_GET(self) -> None:
        self._v2('GET')

    def do_POST(self) -> None:
        self._v2('POST')

    def do_DELETE(self) -> None:
        self._v2('DELETE')

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass


@dataclass
class Engine:
    """One scratch project, the controller it dials, and the commands a case runs against it."""

    project: Path
    state: Path
    env: dict[str, str]
    controller: BgpConfigs

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def up(self, *args: str) -> sp.CompletedProcess[str]:
        return self.pulumi('up', '--yes', '--skip-preview', *args)

    def plain_preview(self) -> sp.CompletedProcess[str]:
        """A preview with no refresh, which reads nothing of the controller unless `diff` does."""
        return self.pulumi('preview', '--expect-no-changes', '--diff')

    def stored(self) -> str:
        """Every file the backend holds, as one text: the state, its history and its backups."""
        return '\n'.join(path.read_text(errors='replace') for path in sorted(self.state.rglob('*')) if path.is_file())


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def controller() -> Iterator[tuple[BgpConfigs, str]]:
    """The stand-in, served on a port of the case's own, and where it answers."""
    served = BgpConfigs(key=KEY, site=SITE)
    handler = type('Handler', (Serving,), {'controller': served})
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served, f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def launched(tmp_path: Path, controller: tuple[BgpConfigs, str]) -> Engine:
    """A fresh stack after its first `up`: the site holds the configuration."""
    served, url = controller
    scratch = scratch_project(tmp_path, PROGRAM)
    # A program importing this repository's code names `src/` itself: an
    # editable install's own `.pth` is not read through the case's `.pth`.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'kluster.pth').write_text(f'{Path(kluster.__file__).parent.parent}\n')
    engine = Engine(
        project=scratch.directory,
        state=tmp_path / 'state',
        env=scratch.env | {'PROBE_CONTROLLER': url, 'PROBE_CONFIG': CONFIG},
        controller=served,
    )
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    configured = engine.pulumi('config', 'set', '--secret', API_KEY_CONFIG, KEY)
    assert configured.returncode == 0, configured.stderr
    first = engine.up()
    assert first.returncode == 0, _ran(first)
    assert served.held is not None
    assert served.held['frr_bgpd_config'] == CONFIG
    return engine


def test_the_text_is_in_no_state_file_and_no_output(launched: Engine) -> None:
    """The text was handed over in plain, and neither the backend nor a preview shows it.

    The backend holds the text only as ciphertext. A plain preview of a
    console edit plans an update and prints the resource's inputs, the text
    among them as `[secret]`; the edited text stays inside the provider's
    `diff`.

    The positive controls are the stand-in, which holds the password, the
    file name stored beside the text in the clear, which the same search finds
    in the backend, and the preview's refusal of the plan with the CLI's
    `--expect-no-changes` code, with `[secret]` printed where the text would
    be: a search that cannot find the password is one that could have.
    """
    assert PASSWORD in str(launched.controller.held)
    assert 'probe-frr.conf' in launched.stored()
    shown = launched.pulumi('stack', 'export')
    assert shown.returncode == 0, shown.stderr

    assert PASSWORD not in launched.stored()
    assert 'remote-as' not in launched.stored()
    assert PASSWORD not in _ran(shown)

    assert launched.controller.held is not None
    launched.controller.held['frr_bgpd_config'] = CONFIG.replace('65001', '65002')
    previewed = launched.plain_preview()

    assert previewed.returncode == state_only.REFUSED, _ran(previewed)
    assert re.search(r'^\s+config\s+: \[secret\]$', previewed.stdout, re.M), _ran(previewed)
    assert PASSWORD not in _ran(previewed)
    assert 'remote-as' not in _ran(previewed)
