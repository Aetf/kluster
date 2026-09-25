"""Which layer answers `mise.toml`'s `[env]`: the checkout's slot, or the caller.

`mise.toml` resolves what a `pulumi` run needs from the workstation slots under
`.credentials/` and, where a slot is absent, from the environment it was
started in (credentials.md §1 rule 6). Nothing else may answer: a location
outside the checkout would outrank an exported value in exactly the checkout
that has no slot, which is where a scratch probe exports a `file://` backend
of its own (framework/testing.md §5.1).

The slots answer for the checkout itself and not for anything under its
`.claude/`, which is where a `jj` workspace sits (framework/dispatch.md
§1.2). A workspace is a checkout of its own, with its own `mise.toml`, and mise
renders every `mise.toml` from the working directory up, so the enclosing
checkout's templates run first and what they yield is the environment the
workspace's templates fall back to (kluster-ops#387). That nesting is laid out
here as it stands on a workstation.

The templates are run by mise itself, not re-implemented here. The `[env]`
table is lifted into scratch checkouts outside any repository, so no
`mise.toml` joins in beyond the ones laid out, under a `HOME` of its own, so no
global configuration does either; the values are literal on purpose, because
which layer answered is the subject.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

MISE = shutil.which('mise')
needs_mise = pytest.mark.skipif(MISE is None, reason='mise is not on PATH')

MISE_TOML = Path(__file__).resolve().parents[1] / 'mise.toml'

#: What the caller exported: a scratch backend and passphrases of its own.
EXPORTED = {
    'PULUMI_BACKEND_URL': 'file:///scratch/state',
    'PULUMI_CONFIG_PASSPHRASE': 'exported-passphrase',
    'KLUSTER_GITHUB_PASSPHRASE': 'exported-github-passphrase',
    'PGSSLROOTCERT': '/exported/ca.crt',
    'PGSSLCERT': '/exported/client.crt',
    'PGSSLKEY': '/exported/client.key',
}

#: The connection string a filled `state-backend/` slot holds.
SLOT_URL = 'postgres://operator@192.0.2.10:5432/pulumi_state'


def _checkout(checkout: Path) -> Path:
    """`checkout`, created, holding `mise.toml`'s `[env]` table and nothing else."""
    table = tomllib.loads(MISE_TOML.read_text())['env']
    # Every key is resolved and compared, so a template added without the
    # guard fails here until it carries an exported value and a slot.
    assert set(table) == EXPORTED.keys(), f'[env] keys {sorted(table)} are not EXPORTED'
    lines = ['[env]']
    for name, template in table.items():
        assert isinstance(template, str), f'{name} is not a template string'
        # A JSON string is a valid TOML basic string.
        lines.append(f'{name} = {json.dumps(template)}')
    checkout.mkdir(parents=True)
    _ = (checkout / 'mise.toml').write_text('\n'.join(lines) + '\n')
    return checkout


def _fill_slots(checkout: Path) -> dict[str, str]:
    """Write every slot `mise.toml` reads into `checkout`; what they resolve to."""
    slots = checkout / '.credentials'
    bundle = slots / 'state-backend'
    bundle.mkdir(parents=True)
    _ = (bundle / 'backend-url').write_text(SLOT_URL + '\n')
    _ = (slots / 'pulumi.passphrase').write_text('slot-passphrase\n')
    _ = (slots / 'github.passphrase').write_text('slot-github-passphrase\n')
    return {
        'PULUMI_BACKEND_URL': SLOT_URL,
        'PULUMI_CONFIG_PASSPHRASE': 'slot-passphrase',
        'KLUSTER_GITHUB_PASSPHRASE': 'slot-github-passphrase',
        'PGSSLROOTCERT': str(bundle / 'ca.crt'),
        'PGSSLCERT': str(bundle / 'client.crt'),
        'PGSSLKEY': str(bundle / 'client.key'),
    }


def _resolve(
    cwd: Path,
    home: Path,
    *,
    trusted: Path | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """What a process `mise x` starts in `cwd` sees, given `EXPORTED`.

    `trusted` is the directory whose configuration mise may render, `cwd`
    unless named; `extra` joins the environment mise itself is started in.
    """
    assert MISE is not None
    result = subprocess.run(
        [MISE, 'x', '--', 'env', '-0'],
        cwd=cwd,
        env={
            'PATH': str(Path(MISE).parent) + ':/usr/bin:/bin',
            'HOME': str(home),
            'MISE_TRUSTED_CONFIG_PATHS': str(trusted or cwd),
            'MISE_OFFLINE': '1',
            **(extra or {}),
            **EXPORTED,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    pairs = (entry.split('=', 1) for entry in result.stdout.split('\0') if '=' in entry)
    return {name: value for name, value in pairs if name in EXPORTED}


@needs_mise
def test_a_checkout_without_slots_resolves_what_the_caller_exported(tmp_path: Path) -> None:
    # Copies left where these values lived before `.credentials/` -- the
    # passphrase file at the checkout's root, the client bundle under the
    # home directory -- are inert: the caller's values come through untouched.
    checkout = _checkout(tmp_path / 'checkout')
    home = tmp_path / 'home'
    bundle = home / '.config' / 'kluster' / 'state-backend'
    bundle.mkdir(parents=True)
    _ = (bundle / 'backend-url').write_text(SLOT_URL + '\n')
    _ = (checkout / '.pulumi.secret').write_text('old-passphrase\n')

    assert _resolve(checkout, home) == EXPORTED


@needs_mise
def test_the_slot_outranks_what_the_caller_exported(tmp_path: Path) -> None:
    # The slot is the source, so in a checkout that holds one an exported
    # value redirects nothing -- and the four state-backend values come from
    # the one bundle together.
    checkout = _checkout(tmp_path / 'checkout')
    slots = _fill_slots(checkout)

    assert _resolve(checkout, tmp_path / 'home') == slots


@needs_mise
@pytest.mark.parametrize('inside', ['.claude/workspaces/w', '.claude/workspaces/w/src', '.claude'])
def test_under_the_checkouts_claude_directory_the_caller_answers(tmp_path: Path, inside: str) -> None:
    # The enclosing checkout holds every slot and the workspace none, as on a
    # workstation: the workspace's `mise.toml` is the tracked file, and
    # `.credentials/` is never copied into it. Anywhere under `.claude/`, the
    # enclosing checkout's slots yield nothing and the caller's values reach
    # the process -- the backend URL above all, which is how a scratch probe
    # names a `file://` backend of its own.
    primary = _checkout(tmp_path / 'kluster')
    _ = _fill_slots(primary)
    _ = _checkout(primary / '.claude' / 'workspaces' / 'w')
    cwd = primary / inside
    cwd.mkdir(parents=True, exist_ok=True)

    assert _resolve(cwd, tmp_path / 'home', trusted=primary) == EXPORTED


@needs_mise
@pytest.mark.parametrize(
    'inside',
    [
        # A name that merely begins with `.claude`.
        '.claude-notes',
        # The checkout's whole path again, `.claude/` included, deeper down
        # rather than at the front: under the checkout, not under its
        # `.claude/`.
        'vendor/{checkout}/.claude/workspaces/w',
    ],
)
def test_beside_the_checkouts_claude_directory_the_slot_answers(tmp_path: Path, inside: str) -> None:
    # The templates ask whether the working directory lies under
    # `config_root/.claude/` -- a prefix, separator included -- so a path that
    # merely contains those characters is still the checkout's own.
    checkout = _checkout(tmp_path / 'kluster')
    slots = _fill_slots(checkout)
    cwd = checkout / inside.format(checkout=str(checkout).lstrip('/'))
    cwd.mkdir(parents=True)

    assert _resolve(cwd, tmp_path / 'home', trusted=checkout) == slots


@needs_mise
def test_a_sibling_whose_name_extends_the_checkouts_is_not_under_it(tmp_path: Path) -> None:
    # A checkout at `…/kluster` rendered for a working directory in
    # `…/kluster2/.claude/`: the checkout's path is a prefix of the working
    # directory's, its `.claude/` is not. mise renders a configuration whose
    # root is not an ancestor of the working directory only as the global
    # one, whose `config_root` is `HOME` -- so the checkout is made the home
    # directory and its `mise.toml` the global configuration.
    checkout = _checkout(tmp_path / 'kluster')
    slots = _fill_slots(checkout)
    cwd = tmp_path / 'kluster2' / '.claude' / 'workspaces' / 'w'
    cwd.mkdir(parents=True)

    resolved = _resolve(
        cwd,
        checkout,
        trusted=checkout,
        extra={'MISE_GLOBAL_CONFIG_FILE': str(checkout / 'mise.toml')},
    )

    assert resolved == slots
