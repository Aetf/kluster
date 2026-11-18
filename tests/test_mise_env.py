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

Nothing in `mise.toml` reaches an operator stack: no template reads the
passphrase those stacks are encrypted under, and no task runs `pulumi` against
one. They run through the `operator-stack` driver alone, which sets their
backend and passphrase on the process it starts (framework/pulumi.md §3.3).

The templates are run by mise itself, not re-implemented here. The `[env]`
table is lifted into scratch checkouts outside any repository, so no
`mise.toml` joins in beyond the ones laid out, under a `HOME` of its own, so
no global configuration does either; the values are literal on purpose,
because which layer answered is the subject.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import yaml

from kluster.conventions import identity
from kluster.lib import bundle as bundle_files
from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.scripts.credentials import workstation as credential_slots

MISE = shutil.which('mise')
needs_mise = pytest.mark.skipif(MISE is None, reason='mise is not on PATH')

MISE_TOML = Path(__file__).resolve().parents[1] / 'mise.toml'
WORKFLOWS = Path(__file__).resolve().parents[1] / '.github' / 'workflows'
#: The repository's composite actions, each an `action.yml` whose steps run
#: inside the job that uses it.
ACTIONS = Path(__file__).resolve().parents[1] / '.github' / 'actions'

#: What the caller exported: a scratch backend and passphrases of its own.
EXPORTED = {
    'PULUMI_BACKEND_URL': 'file:///scratch/state',
    'PULUMI_CONFIG_PASSPHRASE': 'exported-passphrase',
    'PGSSLROOTCERT': '/exported/ca.crt',
    'PGSSLCERT': '/exported/client.crt',
    'PGSSLKEY': '/exported/client.key',
}


#: The connection string a filled `state-backend/` slot holds.
SLOT_URL = 'postgres://operator@192.0.2.10:5432/pulumi_state'


def _checkout(checkout: Path) -> Path:
    """`checkout`, created, holding `mise.toml`'s `[env]` table: its templates and its literal values."""
    config = tomllib.loads(MISE_TOML.read_text())
    table = config['env']
    assert all(isinstance(value, str) for value in table.values()), '[env] holds a value that is not a string'
    # Every template is resolved and compared, so a template added without
    # the guard fails here until it carries an exported value and a slot. A
    # value with no template syntax renders to itself and reads no slot.
    templates = {name: value for name, value in table.items() if '{{' in value or '{%' in value}
    assert set(templates) == EXPORTED.keys(), f'[env] templates {sorted(templates)} are not EXPORTED'
    lines = ['[env]']
    for name, value in table.items():
        # A JSON string is a valid TOML basic string.
        lines.append(f'{name} = {json.dumps(value)}')
    checkout.mkdir(parents=True)
    _ = (checkout / 'mise.toml').write_text('\n'.join(lines) + '\n')
    return checkout


def _fill_slots(checkout: Path) -> dict[str, str]:
    """Write every slot `mise.toml` reads into `checkout`; what they resolve to."""
    slots = lib_workstation.slots(checkout)
    bundle = slots / stack_environment.BUNDLE_SLOT
    bundle.mkdir(parents=True)
    _ = (bundle / bundle_files.URL_FILE).write_text(SLOT_URL + '\n')
    _ = (slots / credential_slots.PASSPHRASE).write_text('slot-passphrase\n')
    # The operator passphrase is here as on a workstation with no secret
    # store, and nothing below may resolve it.
    _ = (slots / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text('slot-operator-passphrase\n')
    return {
        # What `pulumi` is given is the bundle's URL with the parameter the
        # driver behind its backend reads (`bundle.pulumi_url`); the three
        # files are named as they are for every client.
        'PULUMI_BACKEND_URL': f'{SLOT_URL}?binary_parameters=yes',
        'PULUMI_CONFIG_PASSPHRASE': 'slot-passphrase',
        'PGSSLROOTCERT': str(bundle / bundle_files.CA_FILE),
        'PGSSLCERT': str(bundle / bundle_files.CERT_FILE),
        'PGSSLKEY': str(bundle / bundle_files.KEY_FILE),
    }


def _resolve(
    cwd: Path,
    home: Path,
    *,
    trusted: Path | None = None,
    extra: Mapping[str, str] | None = None,
    names: Collection[str] = EXPORTED.keys(),
) -> dict[str, str]:
    """What a process `mise x` starts in `cwd` sees of `names`, given `EXPORTED`.

    `trusted` is the directory whose configuration mise may render, `cwd`
    unless named; `extra` joins the environment mise itself is started in.
    """
    assert MISE is not None
    result = process_sessions.run(
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
        text=True,
        check=True,
        # Below the 60 s case bound with room for a second resolve in one
        # case, so a hung `mise` is named by its own timeout.
        timeout=20,
    )
    pairs = (entry.split('=', 1) for entry in result.stdout.split('\0') if '=' in entry)
    return {name: value for name, value in pairs if name in names}


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


@needs_mise
@pytest.mark.parametrize('layout', ['checkout without slots', 'checkout with slots', 'workspace under .claude'])
def test_every_run_mise_starts_writes_the_state_compressed_and_without_backups(tmp_path: Path, layout: str) -> None:
    # In each layout a `pulumi` run is started from -- CI's checkout, which
    # holds the slots it materializes, a workstation's, and a `jj` workspace
    # under one, where two `mise.toml` render -- the process is given every
    # switch `mise.toml` sets, as it sets it, and a caller's own setting of
    # one does not turn it off. What mise renders is held to what the Python
    # side reads out of the same `[env]` (`stack_environment.state_switches`),
    # which is what an operator stack in the estate's backend is given, so
    # the two kinds of writer agree. What the pinned `pulumi` writes under
    # each switch is the comment above it in `mise.toml`'s `[env]`.
    primary = _checkout(tmp_path / 'kluster')
    cwd = primary
    if layout != 'checkout without slots':
        _ = _fill_slots(primary)
    if layout == 'workspace under .claude':
        cwd = _checkout(primary / '.claude' / 'workspaces' / 'w')
    expected = stack_environment.state_switches()
    assert expected, 'mise.toml sets no literal PULUMI_ variable, so nothing would be checked'
    caller = dict.fromkeys(expected, 'false')

    resolved = _resolve(cwd, tmp_path / 'home', trusted=primary, extra=caller, names=expected.keys())

    assert resolved == expected


#: The shapes a slot's connection string comes in: what `state-backend bundle`
#: writes, one with no query, one that names its certificate files in the
#: query as an older bundle does, an empty file, which names no backend, and
#: a backend of another scheme.
SLOT_URLS = {
    'as written': f'{SLOT_URL}?sslmode=verify-full',
    'no query': SLOT_URL,
    'file parameters': f'{SLOT_URL}?sslmode=verify-full&sslrootcert=/b/ca.crt&sslcert=/b/client.crt&sslkey=/b/client.key',
    'empty': '',
    'another scheme': 'file:///scratch/state',
}


@needs_mise
@pytest.mark.parametrize('written', list(SLOT_URLS.values()), ids=list(SLOT_URLS))
def test_mise_gives_pulumi_the_url_the_python_side_gives_it(tmp_path: Path, written: str) -> None:
    # Two spellings of one rule: the template `mise.toml` renders for every
    # run it starts, CI's included, and `bundle.pulumi_url`, which every run
    # the Python side starts from a slot is given. Whatever the slot holds,
    # they agree.
    checkout = _checkout(tmp_path / 'kluster')
    _ = _fill_slots(checkout)
    url_file = lib_workstation.slots(checkout) / stack_environment.BUNDLE_SLOT / bundle_files.URL_FILE
    _ = url_file.write_text(written + '\n')

    resolved = _resolve(checkout, tmp_path / 'home', names={'PULUMI_BACKEND_URL'})

    assert resolved == {'PULUMI_BACKEND_URL': bundle_files.pulumi_url(written)}


#: Where a command word stands in a line of shell: at its start, or after
#: whitespace -- not inside a quoted string, such as a commit message naming
#: `pulumi install`.
PULUMI_COMMAND = re.compile(r'(?:^|(?<=\s))pulumi(?=\s|$)')
#: What has to come right before it: `mise x`, with any tools it is handed.
THROUGH_MISE = re.compile(r'mise x(?: [\w:/.@-]+)* -- $')


def _bare_pulumi(script: str) -> list[str]:
    """The lines of a step's script that start `pulumi` other than through `mise x`; comments are not commands."""
    return [
        line.strip()
        for line in script.splitlines()
        if not line.lstrip().startswith('#')
        for found in PULUMI_COMMAND.finditer(line)
        if not THROUGH_MISE.search(line[: found.start()])
    ]


def test_the_bare_pulumi_predicate_finds_each_way_to_start_one() -> None:
    """The check below can only pass if it can fail: a `pulumi` started outside `mise x` is found."""
    for bare in (
        'pulumi up --stack dns --yes',
        'PULUMI_DIY_BACKEND_ZSTD=false pulumi preview --stack apps',
        'set -e; pulumi refresh --stack k8s-base',
        'mise x -- env -u PULUMI_DIY_BACKEND_ZSTD pulumi up --stack dns',
        'mise x -- uv run python && pulumi stack ls',
    ):
        assert _bare_pulumi(bare) == [bare], bare
    for through in (
        'mise x -- pulumi up --stack dns --yes --diff',
        'mise x uv -- pulumi preview --stack apps',
        '# pulumi preview is what this step runs',
        "-m 'pulumi install, from the Pulumi.yaml this branch carries'",
    ):
        assert _bare_pulumi(through) == [], through


def _steps(name: str, document: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Every step of a workflow's jobs, or of a composite action, with where it is."""
    if 'runs' in document:
        return [(name, step) for step in document['runs'].get('steps', [])]
    return [
        (f'{name} {job_name}', step)
        for job_name, job in document.get('jobs', {}).items()
        for step in job.get('steps', [])
    ]


def _outside_mise(name: str, document: Mapping[str, Any]) -> list[str]:
    """The steps of `document` that start `pulumi` other than through `mise x`, or use an action that runs it."""
    found: list[str] = []
    for where, step in _steps(name, document):
        if 'pulumi' in str(step.get('uses', '')).lower():
            found.append(f'{where}: {step["uses"]}')
        found += [f'{where}: {line}' for line in _bare_pulumi(str(step.get('run', '')))]
    return found


def test_the_step_walk_finds_a_bare_pulumi_in_a_workflow_and_in_a_composite_action() -> None:
    """The check below can only pass if it can fail: both shapes of document are walked."""
    bare = {'run': 'pulumi up --stack dns --yes'}
    action = {'uses': 'pulumi/actions@v6'}
    workflow = {'jobs': {'deploy': {'steps': [{'run': 'mise x -- pulumi up --stack dns'}, bare, action]}}}
    composite = {'runs': {'using': 'composite', 'steps': [{'shell': 'bash', **bare}, action]}}

    assert len(_outside_mise('workflow.yml', workflow)) == 2
    assert len(_outside_mise('action.yml', composite)) == 2


def test_every_workflow_starts_pulumi_through_mise() -> None:
    """Every job that runs `pulumi` runs it under `mise.toml`'s `[env]`, so every writer CI has carries the switches.

    A step running `pulumi` itself is the one way a job could reach a stack
    without them, and a step using an action that runs it is the other. A
    composite action's steps run inside the job that uses it, so they are
    read too.
    """
    documents = [
        *((path.name, path) for path in sorted(WORKFLOWS.glob('*.yml'))),
        *((path.parent.name, path) for path in sorted(ACTIONS.glob('*/action.yml'))),
    ]
    assert any(path.parent == ACTIONS / name for name, path in documents), 'no composite action was read'

    outside = [line for name, path in documents for line in _outside_mise(name, yaml.safe_load(path.read_text()))]

    assert outside == [], outside


def _runs_an_operator_stack(task: object) -> bool:
    """Whether a task's definition runs `pulumi` against an operator stack, or the driver for one."""
    return any(
        re.search(rf'(?:--stack|-s)[\s=]+{re.escape(stack)}\b|operator-stack\s+{re.escape(stack)}\b', str(task))
        for stack in identity.OPERATOR_STACKS
    )


def test_the_operator_stack_predicate_finds_each_spelling_of_one() -> None:
    """The check below can only pass if it can fail: each way a task could name an operator stack is found."""
    assert identity.OPERATOR_STACKS
    for stack in identity.OPERATOR_STACKS:
        for run in (f'pulumi up --stack {stack}', f'pulumi preview -s {stack}', f'pulumi refresh --stack={stack}'):
            assert _runs_an_operator_stack({'run': run}), run
        assert _runs_an_operator_stack({'run': f'operator-stack {stack} plan'})
    assert not _runs_an_operator_stack({'run': 'pulumi preview --stack dns'})


def test_nothing_in_mise_toml_reaches_an_operator_stack() -> None:
    """No template reads the operator passphrase, and no task runs `pulumi` against one.

    Either would be a second way to an operator stack beside the driver, and
    one that sets neither the stack's own backend nor its checks: the
    `[env]` table is rendered for every `mise x`, and a task for every
    `mise run`.
    """
    config = tomllib.loads(MISE_TOML.read_text())
    reading = [
        name for name, template in config['env'].items() if stack_environment.OPERATOR_PASSPHRASE_SLOT in str(template)
    ]
    running = [name for name, task in config.get('tasks', {}).items() if _runs_an_operator_stack(task)]

    assert reading == [], f'[env] templates reading the operator passphrase: {reading}'
    assert running == [], f'mise tasks running an operator stack: {running}'
