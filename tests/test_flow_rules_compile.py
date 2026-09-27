"""ZeroTier's own rule compiler accepts the overlay's rendered flow rules.

The program reaches ZeroTier Central as the network's `rulesSource`, and Central
compiles it during `up`: a program it refuses is a failed live `up`, for the
physical stack inside a cutover window at worst. `test_flow_rules` holds what
the rules mean; this holds that they are rules at all, in the language as the
release the CI member runs compiles it.

The compiler is ZeroTier's `rule-compiler/`, vendored under
`tests/vendor/zerotier-rule-compiler/` at that release — `source.toml` there
records which, and how to take it again — and run with the `node` `mise.toml`
pins. Nothing here reaches the network. A missing `node` fails rather than
skips: a skip would leave the gate green with nothing compiled.

The program is rendered in both shapes it is handed addresses in: the suite's
literals (`overlay_flow_rules`), and the physical stack's own inputs, read
from `conventions` as the stack reads them. The node addresses it is rendered
over are made-up ten-hex-digit ones either way, since the real ones are minted
in state.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from overlay_flow_rules import NODE_IDS, rules

from kluster import conventions
from kluster.components.overlay.flow_rules import flow_rules

#: The vendored compiler and the record of where it came from.
VENDOR = Path(__file__).parent / 'vendor' / 'zerotier-rule-compiler'
SOURCE = VENDOR / 'source.toml'

#: The one bound a compile runs under. It takes well under a second; this is a
#: hang guard, an order of magnitude above that.
TIMEOUT = 60


def node() -> str:
    """The `node` the compiler runs on: the one `mise.toml` pins, found on the `PATH` `mise x` builds."""
    found = shutil.which('node')
    assert found is not None, 'node is not on PATH; it is pinned in mise.toml, so run the suite through `mise x`'
    return found


def compile_rules(program: str, directory: Path) -> subprocess.CompletedProcess[str]:
    """Run the vendored `cli.js` over `program`, as Central compiles a `rulesSource`."""
    source = directory / 'flow-rules.zt'
    source.write_text(program)
    return subprocess.run(
        [node(), str(VENDOR / 'cli.js'), str(source)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def stack_program() -> str:
    """The program over the addresses the physical stack hands `flow_rules`."""
    return flow_rules(
        gateway_overlay_address=conventions.overlay.UDM,
        homelab_overlay_address=conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address,
        resolver_site_addresses=[service.address for service in conventions.gateway.RESOLVERS],
    ).render(NODE_IDS)


@pytest.mark.parametrize('program', [rules, stack_program], ids=['literals', 'stack'])
def test_zerotier_compiles_the_rendered_flow_rules(program: Callable[[], str], tmp_path: Path) -> None:
    compiled = compile_rules(program(), tmp_path)

    assert compiled.returncode == 0, compiled.stderr or compiled.stdout
    # Not vacuous: the program compiled to rules, not to an empty network.
    assert json.loads(compiled.stdout)['config']['rules']


def test_a_program_zerotier_refuses_fails_by_line(tmp_path: Path) -> None:
    """The control: the harness reads the compiler's refusal, so a green case above is a compiled program."""
    broken = rules().replace('accept;', 'acept;')
    assert broken != rules()

    compiled = compile_rules(broken, tmp_path)

    assert compiled.returncode != 0
    assert 'line' in compiled.stderr, compiled.stderr


def test_the_vendored_compiler_is_the_copy_its_record_names() -> None:
    """A copy edited by hand, or taken from another tag without its record, fails by name."""
    source = tomllib.loads(SOURCE.read_text())
    recorded = cast('dict[str, str]', source['sha256'])
    on_disk = {path.name for path in VENDOR.iterdir() if path.name != SOURCE.name}

    assert set(recorded) == on_disk
    for name, digest in recorded.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name
