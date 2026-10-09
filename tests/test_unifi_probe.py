"""The UniFi pre-window probe, as the cutover runbook writes it (physical/gateway-cutover.md §3).

The probe is a throwaway Pulumi project the operator writes out of two
heredocs in the runbook and runs from the checkout root. Nothing else in the
repository holds it, so these cases read both files out of the runbook itself,
and a change to either is a change to what they run:

-   **its project file runs a program in the checkout's environment.** The
    `.venv` that `uv sync` makes holds no `pip`, which the language host asks
    for unless the toolchain is `uv`; under `uv` it asks for the checkout's
    `pyproject.toml` instead, two directories up. The project file names
    `uv`, as the root one does. The language host also picks `uv` on its own
    when it finds a `uv.lock` above the project, which a checkout has, so
    the case holds what the file and the tree do together rather than which
    of the two chose the toolchain. The case runs the pinned CLI
    on that file, verbatim, in a tree of the checkout's shape, against a
    `file://` backend and a home and `TMPDIR` of its own
    (`scratch_projects.cli_directories`). The program is a stand-in: the
    probe's own one configures the UniFi provider, which dials the controller,
    and fetches that provider's plugin to do it.
-   **its program declares what the runbook's pass reading names**, under the
    mocks, so no controller is dialed: every object through the provider the
    window's own `SiteFirewall` construction builds, and every object the
    console shows carrying the name the runbook's cleanup finds it by.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import site
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import process_sessions
import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with
from scratch_projects import PASSPHRASE, PYPROJECT, SETUP_TIMEOUT, cli_directories, scrubbed

RUNBOOK = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway-cutover.md'

#: Where the runbook puts the probe, relative to the checkout root it runs
#: from (`PROBE=$PWD/.claude/unifi-probe`), and the project's name in it.
PROBE = Path('.claude') / 'unifi-probe'
PROJECT = 'unifi-probe'

#: The bound of each `pulumi` command. The whole engine case, three commands
#: and the environment's set-up, took under 2 s on an idle 24-core machine;
#: the slowest real-CLI case testing.md §8 measured took 39 s with four busy
#: processes per core, and this is three times that. A stop-loss, which
#: nothing asserts on.
COMMAND_TIMEOUT = 120

#: The case bound, above the three commands and the environment's two `uv`
#: steps, each at its own bound; a stop-loss, which nothing asserts on.
CASE_TIMEOUT = 3 * COMMAND_TIMEOUT + 2 * SETUP_TIMEOUT


def heredoc(name: str) -> str:
    """The file the runbook writes into the probe's directory as `name`, as it lands there."""
    match = re.search(
        r'cat > "\$PROBE/' + re.escape(name) + r"\" <<'EOF'\n(?P<body>.*?)\n[ ]*EOF\n",
        RUNBOOK.read_text(),
        re.DOTALL,
    )
    assert match is not None, f'{RUNBOOK} writes no {name} for the probe'
    return textwrap.dedent(match['body']) + '\n'


# -- the project file ---------------------------------------------------------


#: Stands in for the probe's program: it runs, and says which environment it
#: ran in, without configuring a provider.
STAND_IN = """\
import os
import pathlib
import sys

_ = pathlib.Path(os.environ['PROBE_RAN_IN']).write_text(sys.prefix)
"""


def checkout(root: Path) -> Path:
    """A tree of the checkout's shape under `root`: a `uv` project, its `.venv`, and the probe's directory.

    The `.venv` is a `uv` environment, so it holds no `pip`, and its `.pth`
    file reaches the test run's packages, so nothing is fetched. The project
    is the scratch one rather than this repository's, which would lock and
    sync every dependency the repository has.
    """
    root.mkdir()
    _ = (root / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = process_sessions.run(['uv', 'lock', '-q', '--offline'], cwd=root, timeout=SETUP_TIMEOUT, check=True)
    venv = root / '.venv'
    _ = process_sessions.run(
        ['uv', 'venv', '-q', '--python', sys.executable, str(venv)], timeout=SETUP_TIMEOUT, check=True
    )
    (site_packages,) = venv.glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    probe = root / PROBE
    probe.mkdir(parents=True)
    return probe


@pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None,
    reason='the pinned pulumi CLI or uv is not on PATH',
)
@pytest.mark.timeout(CASE_TIMEOUT)
def test_the_probes_project_runs_its_program_from_a_checkout(tmp_path: Path) -> None:
    probe = checkout(tmp_path / 'checkout')
    _ = (probe / 'Pulumi.yaml').write_text(heredoc('Pulumi.yaml'))
    _ = (probe / '__main__.py').write_text(STAND_IN)
    (tmp_path / 'state').mkdir()
    ran_in = tmp_path / 'ran-in'
    env = (
        scrubbed(os.environ)
        | cli_directories(tmp_path)
        | {
            'PULUMI_BACKEND_URL': (tmp_path / 'state').as_uri(),
            'PULUMI_CONFIG_PASSPHRASE': PASSPHRASE,
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
            'UV_OFFLINE': '1',
            'PROBE_RAN_IN': str(ran_in),
        }
    )

    def pulumi_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=probe, env=env, text=True, timeout=COMMAND_TIMEOUT
        )

    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = pulumi_cli('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = pulumi_cli('stack', 'init', 'probe')
    assert initialized.returncode == 0, initialized.stderr

    previewed = pulumi_cli('preview', '--json')

    # Planned, by a program that ran in the checkout's own environment.
    assert previewed.returncode == 0, previewed.stdout + previewed.stderr
    steps: list[dict[str, Any]] = json.loads(previewed.stdout)['steps']
    (stack,) = [step for step in steps if step['urn'].endswith(f'::{PROJECT}-probe')]
    assert stack['op'] == 'create'
    assert Path(ran_in.read_text()) == probe.parent.parent / '.venv'


# -- the program --------------------------------------------------------------


#: Where the probe's provider dials, which no case reaches: a documentation
#: address (RFC 5737).
GATEWAY_HOST = '192.0.2.1'

#: The objects the runbook's pass reading names, each by the type its
#: program declares it as: the provider, then the zone, the groups, the
#: policies, the order and the forward.
PROVIDER = 'pulumi:providers:unifi'
NAMED = {
    'unifi:index/firewallZone:FirewallZone',
    'unifi:index/firewallGroup:FirewallGroup',
    'unifi:index/firewallZonePolicy:FirewallZonePolicy',
    'unifi:index/portForward:PortForward',
}
ORDER = 'unifi:index/firewallZonePolicyOrder:FirewallZonePolicyOrder'


class Controller(Recorder):
    """The controller, as far as the probe's one lookup needs it: the `External` zone exists."""

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'unifi:index/getFirewallZone:getFirewallZone':
            return {'id': 'external-zone', 'name': 'External'}
        return {}


@pytest.mark.asyncio
async def test_the_probes_program_declares_what_the_runbook_reads_back() -> None:
    controller = await run_with(Controller(), stack='probe', project=PROJECT)
    pulumi.runtime.set_all_config(
        {f'{PROJECT}:gatewayHost': GATEWAY_HOST, f'{PROJECT}:unifiApiKey': 'not-a-key'},
        secret_keys=[f'{PROJECT}:unifiApiKey'],
    )

    async with declaring():
        exec(compile(heredoc('__main__.py'), str(PROBE / '__main__.py'), 'exec'), {'__name__': '__main__'})

    # The pass reading: one provider, and one create for each object of the
    # kinds it names -- and nothing else.
    assert controller.types == {PROVIDER, ORDER, *NAMED}
    (provider,) = controller.of_type(PROVIDER)
    # The construction `SiteFirewall` builds its own provider with, which is
    # what makes the probe's answer the window's (`controller_provider`).
    assert provider.inputs['apiUrl'] == f'https://{GATEWAY_HOST}'
    for declaration in controller.declared:
        if declaration.typ != PROVIDER:
            # A provider reference is the provider's URN, then its id.
            assert declaration.provider.rsplit('::', 1)[0].endswith(f'{PROVIDER}::{provider.name}'), declaration
        # The cleanup after a failure removes, in the console, whatever
        # `destroy` could not, by this name.
        if declaration.typ in NAMED:
            assert str(declaration.inputs['name']).startswith('kluster-probe'), declaration
