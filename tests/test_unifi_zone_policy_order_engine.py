"""What a zone pair's order rests on in the engine, held by the pinned `pulumi` itself.

`ZonePolicyOrder` tells a policy in transit from a foreign one by the old
inputs an `update` is handed, and it reports drift through what `read` leaves
in state. Both are the engine's to answer: that a replacement is created, the
order updated and the original deleted, in that order; that the update's old
bag still holds the list a refresh left alone; that a list refreshed clean
plans nothing; and that a reported drift plans an update and prints in a
refreshed preview. No mock plans a step, so each case runs the CLI `mise.toml`
pins over a `file://` backend and a `PULUMI_HOME` of the case's own, against
a program of stand-in policies and the real resource, dialing the controller
stand-in on `127.0.0.1`. The controller's refusals are the ones
`unifi_controller.ZonePolicies` keeps, so a batch that left the original out
fails the run here as it does on the device.

The stand-in policy creates and deletes through two routes the case's server
adds beside the v2 API, and is replaced create-first, as a census policy is.
"""

from __future__ import annotations

import http.server as http_server
import json
import shutil
import subprocess as sp
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

import process_sessions
import pytest
from scratch_projects import scratch_project
from unifi_controller import ControllerSession, ZonePolicies

import kluster
from kluster.providers.unifi_zone_policy_order import API_KEY_CONFIG

#: How long one case may take: its project's set-up and every command it runs
#: against the engine, at the bounds of `tests/test_device_directory_engine.py`,
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
SOURCE = 'zone-source'
DESTINATION = 'zone-destination'
STACK = 'probe'

PROGRAM = """\
import os

import pulumi
import requests
from pulumi.dynamic import CreateResult, Resource, ResourceProvider

from kluster.providers.unifi_zone_policy_order import ZonePolicyOrder

CONTROLLER = os.environ['PROBE_CONTROLLER']


class Policy(ResourceProvider):
    def create(self, props):
        response = requests.post(f'{CONTROLLER}/policy/{props["title"]}', timeout=10)
        response.raise_for_status()
        return CreateResult(id_=response.text, outs=dict(props))

    def delete(self, id_, _props):
        requests.delete(f'{CONTROLLER}/policy/{id_}', timeout=10).raise_for_status()


class Stand(Resource):
    def __init__(self, name):
        super().__init__(Policy(), name, {'title': name})


ZonePolicyOrder(
    'order',
    api_url=CONTROLLER,
    allow_insecure=False,
    site='default',
    source_zone_id='zone-source',
    destination_zone_id='zone-destination',
    policy_ids=[Stand(name).id for name in os.environ['PROBE_POLICIES'].split(',')],
)
"""


class Serving(http_server.BaseHTTPRequestHandler):
    """The controller stand-in behind a socket, plus the stand-in policy's two routes."""

    controller: ZonePolicies

    def _answer(self, status: int, text: str) -> None:
        self.send_response(status)
        self.end_headers()
        _ = self.wfile.write(text.encode())

    def _v2(self, method: str, body: Any) -> None:
        session = ControllerSession(self.controller)
        session.headers = dict(self.headers.items())
        answered = self.controller.answer(session, method, self.path, body)
        text = answered.text if answered.payload is None else json.dumps(answered.payload)
        self._answer(answered.status_code, text)

    def do_GET(self) -> None:
        self._v2('GET', None)

    def do_PUT(self) -> None:
        self._v2('PUT', json.loads(self.rfile.read(int(self.headers['Content-Length']))))

    def do_POST(self) -> None:
        self._answer(200, self.controller.add(self.path.rpartition('/')[2], SOURCE, DESTINATION))

    def do_DELETE(self) -> None:
        self.controller.delete(self.path.rpartition('/')[2])
        self._answer(200, '')

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass


@dataclass
class Engine:
    """One scratch project, the controller it dials, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    controller: ZonePolicies

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def up(self, *args: str) -> sp.CompletedProcess[str]:
        return self.pulumi('up', '--yes', '--skip-preview', *args)

    def refreshed_preview(self) -> sp.CompletedProcess[str]:
        return self.pulumi('preview', '--refresh', '--expect-no-changes', '--diff')

    def urn(self, name: str) -> str:
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        resources = cast('list[dict[str, Any]]', json.loads(exported.stdout)['deployment']['resources'])
        (urn,) = [str(resource['urn']) for resource in resources if str(resource['urn']).endswith(f'::{name}')]
        return urn

    def ids(self) -> list[str]:
        """The pair's custom policies, by rank."""
        return [policy['_id'] for policy in self.controller.custom(SOURCE, DESTINATION)]


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def controller() -> Iterator[tuple[ZonePolicies, str]]:
    """The stand-in, served on a port of the case's own, and where it answers."""
    served = ZonePolicies(key=KEY, site=SITE)
    served.pair(SOURCE, DESTINATION)
    handler = type('Handler', (Serving,), {'controller': served})
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served, f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def launched(tmp_path: Path, controller: tuple[ZonePolicies, str]) -> Engine:
    """A fresh stack after its first `up`: two policies, ranked."""
    served, url = controller
    scratch = scratch_project(tmp_path, PROGRAM)
    # A program importing this repository's code names `src/` itself: an
    # editable install's own `.pth` is not read through the case's `.pth`.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'kluster.pth').write_text(f'{Path(kluster.__file__).parent.parent}\n')
    engine = Engine(
        project=scratch.directory,
        env=scratch.env | {'PROBE_CONTROLLER': url, 'PROBE_POLICIES': 'first,second'},
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
    assert served.ranked(SOURCE, DESTINATION) == ['first', 'second']
    return engine


def test_a_replaced_policy_is_carried_through_the_order_it_stopped_being_named_by(launched: Engine) -> None:
    """Create, then the order's update with the original still on the pair, then the delete.

    The update's batch names the original, which only the old list can have
    told it, and the run ends with a pair the refresh finds clean.
    """
    original, second = launched.ids()

    replaced = launched.up('--replace', launched.urn('first'))

    assert replaced.returncode == 0, _ran(replaced)
    (replacement, kept) = launched.ids()
    assert replacement != original
    assert kept == second
    assert launched.controller.batches[-1]['before_predefined_ids'] == [replacement, second, original]
    clean = launched.refreshed_preview()
    assert clean.returncode == 0, _ran(clean)


def test_a_dropped_policy_is_carried_and_then_deleted(launched: Engine) -> None:
    """The delete comes after the update that stopped naming it."""
    first, dropped = launched.ids()
    launched.env['PROBE_POLICIES'] = 'first'

    result = launched.up()

    assert result.returncode == 0, _ran(result)
    assert launched.ids() == [first]
    assert launched.controller.batches[-1]['before_predefined_ids'] == [first, dropped]
    clean = launched.refreshed_preview()
    assert clean.returncode == 0, _ran(clean)


def test_a_foreign_policy_shows_in_a_refreshed_preview_and_refuses_the_update_it_proposes(launched: Engine) -> None:
    """Drift is a planned update, its reason is printed, and the run that tries it names the policy.

    Written into state by the refresh of a run that then fails, it comes off
    again by the refresh after the policy is gone, with no write.
    """
    foreign = launched.controller.add('Hand rule', SOURCE, DESTINATION)
    written = len(launched.controller.batches)

    previewed = launched.refreshed_preview()

    assert previewed.returncode != 0, _ran(previewed)
    assert 'Hand rule' in _ran(previewed)
    result = launched.up('--refresh')
    assert result.returncode != 0, _ran(result)
    assert 'Hand rule' in _ran(result)
    assert len(launched.controller.batches) == written
    assert launched.controller.ranked(SOURCE, DESTINATION) == ['first', 'second', 'Hand rule']

    launched.controller.delete(foreign)
    clean = launched.refreshed_preview()
    assert clean.returncode == 0, _ran(clean)


def test_a_census_policy_out_of_place_is_ranked_first_again_by_the_refreshed_run(launched: Engine) -> None:
    """A plain `up` trusts state; `up --refresh` reads the rank and restores it."""
    first, second = launched.ids()
    launched.controller.move(second, 1)

    plain = launched.up()
    assert plain.returncode == 0, _ran(plain)
    assert launched.ids() == [second, first], 'a run with no refresh reads nothing'

    refreshed = launched.up('--refresh')

    assert refreshed.returncode == 0, _ran(refreshed)
    assert launched.ids() == [first, second]
    clean = launched.refreshed_preview()
    assert clean.returncode == 0, _ran(clean)
