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

The `github` task is what a `pulumi` run against that stack goes through: it
fixes the stack and hands `pulumi` the stack's own passphrase, which `[env]`
exports under a name of its own (kluster-ops#388). It is held here to that
pair -- the `github` stack and `KLUSTER_GITHUB_PASSPHRASE`, never the estate
passphrase or a stack the caller names -- by running it against a stub
`pulumi` that records what it was given, so no run reaches a backend.

The templates and the task are run by mise itself, not re-implemented here.
The `[env]` table and the task are lifted into scratch checkouts outside any
repository, so no `mise.toml` joins in beyond the ones laid out, under a
`HOME` of its own, so no global configuration does either; the values are
literal on purpose, because which layer answered is the subject.
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
    """`checkout`, created, holding `mise.toml`'s `[env]` table and `github` task."""
    config = tomllib.loads(MISE_TOML.read_text())
    table = config['env']
    # Every key is resolved and compared, so a template added without the
    # guard fails here until it carries an exported value and a slot.
    assert set(table) == EXPORTED.keys(), f'[env] keys {sorted(table)} are not EXPORTED'
    lines = ['[env]']
    for name, template in table.items():
        assert isinstance(template, str), f'{name} is not a template string'
        # A JSON string is a valid TOML basic string.
        lines.append(f'{name} = {json.dumps(template)}')
    lines.append('[tasks.github]')
    for name, value in config['tasks']['github'].items():
        # JSON's strings and booleans are TOML's too.
        assert isinstance(value, str | bool), f'tasks.github.{name} is neither a string nor a boolean'
        lines.append(f'{name} = {json.dumps(value)}')
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


#: The words `mise run github` is given, and what `pulumi` must receive for
#: them. The stack goes after every flag the caller gave and ahead of a `--`,
#: past which it would be an argument. A leading `--` is mise's own, and it is
#: what brings a later `--`, or a `--help`, through every mise release whole.
TASK_ARGS = [
    (['preview'], ['preview', '--stack', 'github']),
    (['config', 'get', 'githubAdminToken'], ['config', 'get', 'githubAdminToken', '--stack', 'github']),
    (
        ['up', '--yes', '--message', "it's $HOME; `id`"],
        ['up', '--yes', '--message', "it's $HOME; `id`", '--stack', 'github'],
    ),
    # Past a `--` an `-s` is a value, not a stack.
    (
        ['--', 'config', 'set', 'key', '--', '-s', 'dev'],
        ['config', 'set', 'key', '--stack', 'github', '--', '-s', 'dev'],
    ),
    (['--', 'up', '--help'], ['up', '--help', '--stack', 'github']),
]


def _run_github(
    cwd: Path,
    tmp_path: Path,
    args: list[str],
    *,
    exported: Mapping[str, str],
    trusted: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str] | None, str | None]:
    """`mise run github <args>` in `cwd`, against a stub `pulumi`.

    What the stub was handed comes back beside the run: its argument vector
    and `PULUMI_CONFIG_PASSPHRASE`, or `None` for both where it never ran.
    """
    assert MISE is not None
    record = tmp_path / 'record'
    record.mkdir()
    stub = tmp_path / 'bin' / 'pulumi'
    stub.parent.mkdir()
    _ = stub.write_text(
        '#!/bin/sh\n'
        f"printf '%s\\0' \"$@\" > '{record}/argv'\n"
        f"printf '%s' \"$PULUMI_CONFIG_PASSPHRASE\" > '{record}/passphrase'\n"
    )
    stub.chmod(0o755)
    result = subprocess.run(
        [MISE, 'run', '--quiet', 'github', *args],
        cwd=cwd,
        env={
            # The stub is ahead of anything else named `pulumi`.
            'PATH': f'{stub.parent}:{Path(MISE).parent}:/usr/bin:/bin',
            'HOME': str(tmp_path / 'home'),
            'MISE_TRUSTED_CONFIG_PATHS': str(trusted or cwd),
            'MISE_OFFLINE': '1',
            **exported,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    argv = record / 'argv'
    if not argv.exists():
        return result, None, None
    return result, argv.read_text().split('\0')[:-1], (record / 'passphrase').read_text()


@needs_mise
@pytest.mark.parametrize(('args', 'expected'), TASK_ARGS)
def test_the_github_task_pairs_the_github_stack_with_its_own_passphrase(
    tmp_path: Path, args: list[str], expected: list[str]
) -> None:
    # A checkout holding every slot, the estate passphrase among them: the
    # one `pulumi` receives is the `github` stack's, and the stack is
    # `github`.
    checkout = _checkout(tmp_path / 'kluster')
    slots = _fill_slots(checkout)

    result, argv, passphrase = _run_github(checkout, tmp_path, args, exported=EXPORTED)

    assert result.returncode == 0, result.stderr
    assert argv == expected
    assert passphrase == slots['KLUSTER_GITHUB_PASSPHRASE']


@needs_mise
def test_without_slots_the_github_task_takes_the_callers_github_passphrase(tmp_path: Path) -> None:
    # The caller's value comes through the template's fallback, and it is the
    # caller's `github` passphrase that answers, not its estate one.
    checkout = _checkout(tmp_path / 'kluster')

    result, argv, passphrase = _run_github(checkout, tmp_path, ['preview'], exported=EXPORTED)

    assert result.returncode == 0, result.stderr
    assert argv == ['preview', '--stack', 'github']
    assert passphrase == EXPORTED['KLUSTER_GITHUB_PASSPHRASE']


@needs_mise
@pytest.mark.parametrize(
    'args',
    [
        ['preview', '-s', 'dev'],
        ['preview', '-sdev'],
        # `-y` then the `-s` shorthand, in one cluster.
        ['up', '-ys', 'dev'],
        ['preview', '--stack', 'dev'],
        ['preview', '--stack=dev'],
        # Even the right stack: the task names it, and nothing else does.
        ['preview', '--stack', 'github'],
    ],
)
def test_a_stack_the_caller_names_is_refused(tmp_path: Path, args: list[str]) -> None:
    checkout = _checkout(tmp_path / 'kluster')
    _ = _fill_slots(checkout)

    result, argv, _ = _run_github(checkout, tmp_path, args, exported=EXPORTED)

    assert result.returncode != 0
    assert argv is None, 'pulumi ran'
    assert 'names a stack' in result.stderr, result.stderr


@needs_mise
def test_a_run_naming_no_command_is_refused(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / 'kluster')
    _ = _fill_slots(checkout)

    result, argv, _ = _run_github(checkout, tmp_path, [], exported=EXPORTED)

    assert result.returncode != 0
    assert argv is None, 'pulumi ran'
    assert 'name a pulumi command' in result.stderr, result.stderr


#: What the caller exported, less a `github` passphrase.
WITHOUT_GITHUB = {name: value for name, value in EXPORTED.items() if name != 'KLUSTER_GITHUB_PASSPHRASE'}


@needs_mise
@pytest.mark.parametrize('where', ['no github slot', 'workspace'])
def test_an_empty_github_passphrase_is_refused(tmp_path: Path, where: str) -> None:
    # The two places `KLUSTER_GITHUB_PASSPHRASE` resolves empty while the
    # estate passphrase does not: a checkout holding every slot but the
    # `github` one, and a workspace under a checkout that holds them all.
    # Either way `pulumi` never starts, so neither the empty value nor the
    # estate passphrase reaches it.
    primary = _checkout(tmp_path / 'kluster')
    _ = _fill_slots(primary)
    if where == 'workspace':
        cwd = _checkout(primary / '.claude' / 'workspaces' / 'w')
    else:
        (primary / '.credentials' / 'github.passphrase').unlink()
        cwd = primary

    result, argv, _ = _run_github(cwd, tmp_path, ['preview'], exported=WITHOUT_GITHUB, trusted=primary)

    assert result.returncode != 0
    assert argv is None, 'pulumi ran'
    assert 'mise run github: KLUSTER_GITHUB_PASSPHRASE is empty' in result.stderr, result.stderr
