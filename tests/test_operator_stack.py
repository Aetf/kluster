"""The `operator-stack` driver: what a run of a stack no CI job runs is given, refused and checked.

Three levels, because they answer different questions:

-   **A fake `pulumi`** that records what it is started with and answers from
    fixtures says what the driver asks for and when: the environment, the
    arguments, whether an `up` is made at all, and what the checks make of a
    checkpoint.
-   **A scratch repository** — a bare repository standing for the forge and a
    clone of it standing for the checkout — says the working-copy checks read
    git's own answers the way they are believed to. The checks' `git` and
    `jj` run with none of the caller's configuration or `GIT_` variables,
    and `git` looks no higher than the case's own directory.
-   **The real engine over a scratch `file://` backend**, with a program
    whose one resource is a dynamic one that needs no credential and so
    reaches no network, says the driver's handling of a committed checkpoint
    holds against what the pinned CLI actually writes: that the preview's
    plan is read from its streamed events, that the apply writes to the
    driver's own output, a terminal's or a pipe's, and is still read from
    the events and the state, that no `up` runs over nothing planned, that
    a write over an unchanged deployment keeps the file's bytes, that no
    switch of the caller's moves the state out of the file the checks read,
    that the check needing no value finds a secret in the clear at each
    place the engine marks one, and that a held step is named by the
    driver's progress lines, in the preview and in an apply under a pipe.
    The driver reaches the engine there through `Bounded`, each command in
    a POSIX session of its own and bounded below the case
    (framework/testing.md §1.2, §8); a case whose subject is the
    production `Cli` runs the driver in a session `started` ends instead.
    Skipped where the pinned CLI or `uv` is not installed.

The cases that need a committed stack add `probe` to the census for their own
duration, so they run no program of the census's own and hold no stack's real
configuration.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import io
import itertools
import json
import logging
import os
import pty
import re
import selectors
import shutil
import signal
import site
import struct
import subprocess as sp
import sys
import termios
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pinned_tools
import process_sessions
import pytest
from credentials_command_tree import named_leaves
from memory_keyring import MemoryKeyring, installed
from scratch_projects import cli_directories

from kluster.conventions import identity
from kluster.lib import acquisition, bundle, pulumi_cli, stack_environment, workstation
from kluster.lib.state_backend import permission, settings, state
from kluster.scripts.credentials import derived, escrow
from kluster.scripts.credentials import workstation as credential_slots
from kluster.scripts.operator_stack import appliance, checkpoint, cli, driver

#: The stack a committed-state case adds to the census.
PROBE = 'probe'
OPERATOR_PASSPHRASE = 'the-operator-passphrase'
#: What the process the driver runs in carries: the estate's stack passphrase
#: and a backend of its own, as `mise.toml` and a scratch probe leave them,
#: and switches that would move or reroute the state.
AMBIENT = {
    'PULUMI_CONFIG_PASSPHRASE': 'an-ambient-stack-passphrase',
    'PULUMI_CONFIG_PASSPHRASE_FILE': '/ambient/stack.passphrase',
    'PULUMI_BACKEND_URL': 'file:///ambient/state',
    'PULUMI_ACCESS_TOKEN': 'an-ambient-token',
    'PULUMI_DIY_BACKEND_GZIP': 'true',
    'PULUMI_RETAIN_CHECKPOINTS': 'true',
    'PULUMI_HOME': '/ambient-home/.pulumi',
    'PULUMI_SKIP_UPDATE_CHECK': 'true',
    'AWS_ACCESS_KEY_ID': 'an-ambient-key',
    'PGPASSWORD': 'an-ambient-password',
    'PGSSLROOTCERT': '/ambient/ca.crt',
    'PGSSLCERT': '/ambient/client.crt',
    'PGSSLKEY': '/ambient/client.key',
    'PATH': os.environ.get('PATH', ''),
}
#: What the driver sets on a run itself, beside what the caller may keep.
SET_BY_THE_DRIVER = {
    'PULUMI_CONFIG_PASSPHRASE',
    'PULUMI_BACKEND_URL',
    'PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS',
    driver.STREAMING_JSON_ENV,
    bundle.CA_ENV,
    bundle.CERT_ENV,
    bundle.KEY_ENV,
}
SLOT_URL = 'postgres://operator@192.0.2.10:5432/pulumi_state'
SECRET_SIG = {checkpoint.SECRET_SIG: checkpoint.SECRET_MARK}

GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 'probe',
    'GIT_AUTHOR_EMAIL': 'probe@example.invalid',
    'GIT_COMMITTER_NAME': 'probe',
    'GIT_COMMITTER_EMAIL': 'probe@example.invalid',
}


# --------------------------------------------------------------------------
# Fixtures.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_vcs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `git` and `jj` the code under test runs, kept to this case's own repositories and configuration.

    The checks call both with this process's environment. A `GIT_DIR` or
    `GIT_WORK_TREE` the caller carries would point them elsewhere, and so
    would a case directory inside a repository -- a `--basetemp` under a
    `jj` workspace sits inside the primary checkout, whose forge `git` would
    then ask. So no `GIT_` variable of the caller's reaches them, `git`
    never looks above the case's directory, and neither tool reads the
    operator's configuration.
    """
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    # `git` does not move up *into* a ceiling, so the case's own directory is
    # the last one it looks in.
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('JJ_CONFIG', os.devnull)


@pytest.fixture(autouse=True)
def secret_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """An empty secret store in place of the operator's, and nobody at a terminal.

    The driver finds the operator passphrase through the acquisition chain,
    whose first layer is the desktop secret store and whose last is a prompt
    where standard input is a terminal: without this a case would read the
    operator's store, and one run with `-s` would wait at a prompt.
    """
    monkeypatch.setattr('sys.stdin', io.StringIO())
    backend = MemoryKeyring()
    with installed(backend):
        yield backend


@dataclass
class FakePulumi:
    """A `pulumi` that runs nothing, records what it was started with, and answers from its fields."""

    #: What a preview's summary counts, by operation.
    changes: dict[str, int] = field(default_factory=lambda: {'same': 3})
    #: What `stack export --show-secrets` prints, one answer per call.
    exports: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    #: Called for every streamed command other than a preview: a stand-in for
    #: what the command writes.
    effect: Callable[[list[str]], None] = lambda _args: None
    #: The export call, counted from one, that fails as a backend that stopped
    #: answering would.
    failing_export: int | None = None
    #: What a preview prints, one engine event per line, where a case writes
    #: the events out; otherwise a summary counting `changes`.
    printed: str | None = None
    #: What a preview asked to replace a resource prints, where it differs.
    replacing: str | None = None
    #: What a streamed command exits with.
    code: int = 0
    streamed: list[list[str]] = field(default_factory=list[list[str]])
    captured: list[list[str]] = field(default_factory=list[list[str]])
    envs: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    #: What each streamed command and preview was said to be doing, in order.
    doings: list[str | None] = field(default_factory=list[str | None])

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None) -> int:
        args = list(args)
        self.streamed.append(args)
        self.doings.append(doing)
        self.envs.append(dict(env))
        self.effect(args)
        return self.code

    def events(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None
    ) -> tuple[int, str]:
        args = list(args)
        self.streamed.append(args)
        self.doings.append(doing)
        self.envs.append(dict(env))
        assert args[:3] == ['preview', '--refresh', '--json'], args
        assert env[driver.STREAMING_JSON_ENV] == 'true'
        if self.replacing is not None and '--replace' in args:
            return 0, self.replacing
        if self.printed is not None:
            return 0, self.printed
        return 0, json.dumps({'summaryEvent': {'resourceChanges': self.changes}}) + '\n'

    def capture(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        args = list(args)
        self.captured.append(args)
        self.envs.append(dict(env))
        if args[:2] == ['stack', 'export']:
            if sum(1 for call in self.captured if call[:2] == ['stack', 'export']) == self.failing_export:
                raise driver.Refused('`pulumi stack export` failed: the backend did not answer')
            return json.dumps(self.exports.pop(0) if len(self.exports) > 1 else self.exports[0])
        if args[0] == 'config':
            return json.dumps(self.config)
        raise AssertionError(f'unexpected query {args}')

    def ups(self) -> list[list[str]]:
        return [args for args in self.streamed if args[0] == 'up']


def _git(*args: str, cwd: Path) -> str:
    # Through a POSIX session of its own: a push, a fetch or a clone starts
    # processes of its own (`git-receive-pack`, `git-upload-pack`).
    return process_sessions.run(
        ['git', *args], cwd=cwd, env=os.environ | GIT_ENV, text=True, check=True, timeout=60
    ).stdout


@dataclass(frozen=True)
class Repository:
    """A forge and two clones of it: the checkout a run starts from, and another that pushes past it."""

    checkout: Path
    other: Path

    def advance_the_forge(self) -> None:
        """A commit on the forge's `main` that the checkout has not fetched."""
        _ = (self.other / 'landed.txt').write_text('a change that landed\n')
        _ = _git('add', 'landed.txt', cwd=self.other)
        _ = _git('commit', '-q', '-m', 'landed', cwd=self.other)
        _ = _git('push', '-q', 'origin', 'main', cwd=self.other)

    def fetch(self) -> None:
        _ = _git('fetch', '-q', 'origin', cwd=self.checkout)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    forge = tmp_path / 'forge.git'
    _ = _git('init', '-q', '--bare', '-b', 'main', str(forge), cwd=tmp_path)
    checkout = tmp_path / 'kluster'
    _ = _git('clone', '-q', str(forge), str(checkout), cwd=tmp_path)
    _ = _git('checkout', '-q', '-b', 'main', cwd=checkout)
    _ = (checkout / 'README.md').write_text('a checkout\n')
    _ = _git('add', 'README.md', cwd=checkout)
    _ = _git('commit', '-q', '-m', 'first', cwd=checkout)
    _ = _git('push', '-q', 'origin', 'main', cwd=checkout)
    other = tmp_path / 'other'
    _ = _git('clone', '-q', str(forge), str(other), cwd=tmp_path)
    _fill_slots(checkout)
    return Repository(checkout=checkout, other=other)


def _fill_slots(checkout: Path) -> None:
    slots = checkout / workstation.DIRECTORY
    (slots / stack_environment.BUNDLE_SLOT).mkdir(parents=True)
    _ = (slots / stack_environment.BUNDLE_SLOT / bundle.URL_FILE).write_text(SLOT_URL + '\n')
    _ = (slots / credential_slots.PASSPHRASE).write_text('a-slot-stack-passphrase\n')
    _ = (slots / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text(OPERATOR_PASSPHRASE + '\n')


@pytest.fixture
def committed(monkeypatch: pytest.MonkeyPatch) -> str:
    """`probe`, in the census as a stack whose state is committed, for this case alone."""
    monkeypatch.setattr(identity, 'OPERATOR_STACKS', {**identity.OPERATOR_STACKS, PROBE: identity.StateHome.COMMITTED})
    return PROBE


def _checkpoint(checkout: Path, resources: list[dict[str, Any]]) -> Path:
    """A committed checkpoint of `probe` holding `resources`, as `pulumi` lays one out."""
    path = checkout / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks' / 'probe' / f'{PROBE}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        'version': 3,
        'checkpoint': {'stack': PROBE, 'latest': {'manifest': {'time': 't0'}, 'resources': resources}},
    }
    _ = path.write_text(json.dumps(document))
    return path


def _export(resources: list[dict[str, Any]], *, time: str = 't0') -> dict[str, Any]:
    return {'version': 3, 'deployment': {'manifest': {'time': time}, 'resources': resources}}


def _ciphertext(text: str) -> dict[str, str]:
    return {**SECRET_SIG, 'ciphertext': f'v1:{len(text)}:not-a-real-ciphertext'}


def _plaintext(value: object) -> dict[str, str]:
    return {**SECRET_SIG, 'plaintext': json.dumps(value)}


# --------------------------------------------------------------------------
# The environment a run is given.
# --------------------------------------------------------------------------


def test_the_estate_backend_stack_runs_under_its_slots_and_nothing_its_caller_carries(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    assert run.plan() == driver.NOTHING_PLANNED

    slot = checkout / workstation.DIRECTORY / stack_environment.BUNDLE_SLOT
    for env in fake.envs:
        assert env['PULUMI_CONFIG_PASSPHRASE'] == OPERATOR_PASSPHRASE
        assert env['PULUMI_BACKEND_URL'] == SLOT_URL
        assert {name: env[name] for name in (bundle.CA_ENV, bundle.CERT_ENV, bundle.KEY_ENV)} == bundle.ssl_env(slot)
        # Of the caller's variables that steer `pulumi` or its backend, only
        # the allowed ones reach the process.
        steering = {name for name in env if name.startswith(stack_environment.STEERING)}
        assert steering <= SET_BY_THE_DRIVER | stack_environment.ALLOWED, steering - SET_BY_THE_DRIVER
        assert env['PULUMI_HOME'] == AMBIENT['PULUMI_HOME']
        # And no value the caller carried for any other of them does.
        kept = {'PATH', *stack_environment.ALLOWED}
        leaked = {name for name, value in AMBIENT.items() if name not in kept and env.get(name) == value}
        assert leaked == set()
    assert fake.envs, 'the driver started no pulumi, so the environment was never looked at'


def test_the_committed_stack_runs_against_the_checkouts_checkpoints(repository: Repository, committed: str) -> None:
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.plan() == driver.NOTHING_PLANNED

    (env,) = fake.envs
    assert env['PULUMI_BACKEND_URL'] == f'{(repository.checkout / "checkpoints").as_uri()}?metadata=skip'
    assert env['PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS'] == 'true'
    assert env['PULUMI_CONFIG_PASSPHRASE'] == OPERATOR_PASSPHRASE
    assert not [value for value in env.values() if 'stack-passphrase' in value or '/ambient/' in value]


def test_a_machine_without_the_operator_passphrase_is_refused_naming_the_command(tmp_path: Path) -> None:
    """No layer of the chain holds it and nobody is at a terminal: the refusal names the command that fills it."""
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    (checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()

    with pytest.raises(stack_environment.EnvironmentRefused) as refusal:
        _ = driver.Run.open('github', checkout, pulumi=FakePulumi(), base=AMBIENT)

    assert named_leaves(str(refusal.value)) == [('derived', stack_environment.OPERATOR_PASSPHRASE_ROW, 'recover')]
    assert stack_environment.OPERATOR_PASSPHRASE_ENV in str(refusal.value)


#: Each layer of the chain holding a value of its own, in the chain's order;
#: the prompt is the last, standing for a terminal that answers.
LAYERS = ('store', 'slot', 'variable', 'prompt')


@pytest.mark.parametrize('first', LAYERS)
def test_the_operator_passphrase_is_found_by_the_chain_in_its_order(
    first: str, secret_store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every layer from `first` on holds a different value, and the first of them is the one a run is given.

    Walked from each layer in turn, so each is shown both answering and being
    passed over for the layer before it: the store over the slot, the slot
    over the variable, the variable over the prompt, and the prompt asked only
    when nothing above it holds a value.
    """
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    slot = checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT
    slot.unlink()
    held = LAYERS[LAYERS.index(first) :]
    if 'store' in held:
        secret_store.items[(acquisition.KEYRING_SERVICE, stack_environment.OPERATOR_PASSPHRASE_ACCOUNT)] = 'from-store'
    if 'slot' in held:
        _ = slot.write_text('from-slot\n')
    if 'variable' in held:
        monkeypatch.setenv(stack_environment.OPERATOR_PASSPHRASE_ENV, 'from-variable')
    asked: list[str] = []

    def ask(question: str) -> str:
        asked.append(question)
        return 'from-prompt'

    found = stack_environment.operator_passphrase(checkout, ask)

    assert found == f'from-{first}'
    assert (asked != []) == (first == 'prompt')


def test_an_empty_layer_is_passed_over_rather_than_handed_to_pulumi(
    secret_store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank store entry and a blank slot are absent layers, not an empty passphrase."""
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    secret_store.items[(acquisition.KEYRING_SERVICE, stack_environment.OPERATOR_PASSPHRASE_ACCOUNT)] = '  '
    _ = (checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text('\n')
    monkeypatch.setenv(stack_environment.OPERATOR_PASSPHRASE_ENV, 'from-variable')

    assert stack_environment.operator_passphrase(checkout, lambda _question: None) == 'from-variable'


def test_an_empty_answer_at_the_prompt_is_refused(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    (checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()

    with pytest.raises(stack_environment.EnvironmentRefused, match='nobody answered the prompt') as refusal:
        _ = stack_environment.operator_passphrase(checkout, lambda _question: '  ')

    assert named_leaves(str(refusal.value)) == [('derived', stack_environment.OPERATOR_PASSPHRASE_ROW, 'recover')]


class _Terminal(io.StringIO):
    """Standard input that answers as a terminal does."""

    def isatty(self) -> bool:
        return True


def test_a_run_at_a_terminal_is_asked_for_the_operator_passphrase_the_chain_does_not_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain's last layer as a run meets it: a prompt where standard input is a terminal, and its answer used."""
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    (checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()
    monkeypatch.setattr('sys.stdin', _Terminal())
    asked: list[str] = []

    def typed(question: str) -> str:
        asked.append(question)
        return 'typed-at-the-terminal'

    monkeypatch.setattr('getpass.getpass', typed)
    fake = FakePulumi()

    assert driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT).plan() == driver.NOTHING_PLANNED

    assert len(asked) == 1
    assert fake.envs, 'the driver started no pulumi, so the passphrase was never handed on'
    assert {env['PULUMI_CONFIG_PASSPHRASE'] for env in fake.envs} == {'typed-at-the-terminal'}


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads a file whatever its mode')
def test_an_unreadable_slot_is_refused_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot that is there and cannot be read is the operator's to repair, not a layer to skip or a traceback."""
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    slot = checkout / workstation.DIRECTORY / stack_environment.OPERATOR_PASSPHRASE_SLOT
    slot.chmod(0)
    monkeypatch.setenv(stack_environment.OPERATOR_PASSPHRASE_ENV, 'from-variable')

    try:
        with pytest.raises(stack_environment.EnvironmentRefused) as refusal:
            _ = driver.Run.open('github', checkout, pulumi=FakePulumi(), base=AMBIENT)
    finally:
        slot.chmod(0o600)

    assert str(slot) in str(refusal.value)


def test_the_slots_the_driver_reads_are_the_ones_the_credentials_commands_write() -> None:
    """The bundle's slot and the passphrase's row, each spelled on both sides; the passphrase's slot is spelled once."""
    assert stack_environment.BUNDLE_SLOT == credential_slots.BUNDLE
    assert escrow.row_name(escrow.OPERATOR_PASSPHRASE) == stack_environment.OPERATOR_PASSPHRASE_ROW


# --------------------------------------------------------------------------
# What a run refuses.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('args', 'naming'),
    [
        (['preview', '--stack', 'dev'], '--stack'),
        (['preview', '--stack=dev'], '--stack=dev'),
        (['preview', '-s', 'dev'], '-s'),
        (['preview', '-sdev'], '-sdev'),
        (['up', '-ys', 'dev'], '-ys'),
        # `config cp` names the stack it writes with `--dest`.
        (['config', 'cp', '--dest', 'dev'], '--dest'),
        (['config', 'cp', '--dest=dev'], '--dest=dev'),
        (['config', 'cp', '-d', 'dev'], '-d'),
        # A flag, or a flag and its value, between the command's words.
        (['config', '--color=never', 'cp', '--dest', 'dev'], '--dest'),
        (['config', '--color', 'never', 'cp', '-d', 'dev'], '-d'),
    ],
)
def test_an_argument_naming_another_stack_is_refused_naming_it(tmp_path: Path, args: list[str], naming: str) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused) as refused:
        _ = run.passthrough(args)
    assert f'`{naming}`' in str(refused.value)
    assert fake.streamed == []


def test_the_stack_goes_ahead_of_a_double_dash_and_words_after_it_are_the_callers(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['config', 'set', 'key', '--', '-s', 'dev']) == 0

    assert fake.streamed == [['config', 'set', 'key', '--stack', 'github', '--', '-s', 'dev']]


@pytest.mark.parametrize('inside', ['.claude/workspaces/w', '.claude'])
def test_a_run_under_claude_is_refused(tmp_path: Path, inside: str) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    workspace = checkout / inside
    workspace.mkdir(parents=True, exist_ok=True)
    _fill_slots(workspace)

    with pytest.raises(driver.Refused) as refused:
        _ = driver.Run.open('github', workspace, pulumi=FakePulumi(), base=AMBIENT)
    assert str(workspace) in str(refused.value)


def test_the_command_line_refuses_a_stack_outside_the_census(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        _ = cli.main(['dns', 'plan'])

    assert refused.value.code == 2
    assert "invalid choice: 'dns'" in capsys.readouterr().err


@pytest.mark.parametrize(
    'args',
    [
        ['import', 'oci:Core/instance:Instance', 'box', 'ocid1.instance.oc1..x'],
        ['stack', 'import', '--file', 'edited.json'],
        # A flag, or a flag and its value, between the command's words.
        ['stack', '--color=never', 'import', '--file', 'edited.json'],
        ['stack', '--color', 'never', 'import', '--file', 'edited.json'],
    ],
)
def test_a_passed_through_import_is_refused_for_a_committed_stack(
    repository: Repository, committed: str, args: list[str]
) -> None:
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused) as refused:
        _ = run.passthrough(args)
    assert ' '.join(args) in str(refused.value)
    assert fake.streamed == []


def test_a_passed_through_import_reaches_pulumi_for_a_backend_stack(tmp_path: Path) -> None:
    # The refusal is about a committed state; the estate's backend is not
    # published, and `framework/github.md` §3.1's adoption runs this way.
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['import', 'github:index/repository:Repository', 'kluster', 'kluster']) == 0
    assert fake.streamed[0][0] == 'import'


# --------------------------------------------------------------------------
# `plan` and `up`.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('changes', 'expected'),
    [
        ({'same': 4}, driver.NOTHING_PLANNED),
        ({'same': 3, 'read': 1}, driver.NOTHING_PLANNED),
        ({'same': 3, 'update': 1}, driver.PLANNED),
        ({'create': 1}, driver.PLANNED),
    ],
)
def test_plan_previews_with_a_refresh_and_makes_no_up(tmp_path: Path, changes: dict[str, int], expected: int) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi(changes=changes)
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    assert run.plan() == expected

    (preview,) = fake.streamed
    assert preview[:2] == ['preview', '--refresh']
    assert preview[-2:] == ['--stack', 'github']
    assert fake.ups() == []


def test_up_with_nothing_planned_makes_no_up(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    assert run.up(yes=True) == driver.NOTHING_PLANNED
    assert fake.ups() == []


@dataclass
class Marking(FakePulumi):
    """A `FakePulumi` that logs a mark as each streamed command or preview starts, so the log orders the runs."""

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None) -> int:
        logging.getLogger(MARKS).info('run')
        return super().stream(args, cwd=cwd, env=env, doing=doing)

    def events(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None
    ) -> tuple[int, str]:
        logging.getLogger(MARKS).info('run')
        return super().events(args, cwd=cwd, env=env, doing=doing)


#: The logger `Marking` marks each run on.
MARKS = 'test_operator_stack.runs'


def test_a_plan_and_an_up_ask_for_their_runs_progress_and_a_passthrough_for_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # What each run is said to be doing is a phrase the driver logged since
    # the run before it; the narration itself is `Cli`'s (`progress`).
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = Marking(changes={'same': 2, 'update': 1})
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    with caplog.at_level(logging.INFO):
        _ = run.plan()
        _ = run.up(yes=True)
        _ = run.passthrough(['stack', 'ls'])

    # The plan's preview, the up's own preview, the apply; then the passthrough.
    assert [doing is not None for doing in fake.doings] == [True, True, True, False]
    said: list[list[str]] = [[]]
    for record in caplog.records:
        if record.name == MARKS:
            said.append([])
        else:
            said[-1].append(record.getMessage())
    for doing, before in zip(fake.doings, said, strict=False):
        assert doing is None or doing in before, (doing, before)


@pytest.mark.parametrize(('yes', 'answer', 'ups'), [(True, False, 1), (False, True, 1), (False, False, 0)])
def test_up_applies_what_is_planned_once_confirmed(tmp_path: Path, yes: bool, answer: bool, ups: int) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi(changes={'same': 2, 'update': 1})
    asked: list[str] = []

    def confirm(question: str) -> bool:
        asked.append(question)
        return answer

    run = driver.Run.open('github', checkout, pulumi=fake, confirm=confirm, base=AMBIENT)

    assert run.up(yes=yes) == (0 if ups else driver.PLANNED)

    assert fake.ups() == [['up', '--refresh', '--yes', '--skip-preview', '--stack', 'github']] * ups
    assert len(asked) == (0 if yes else 1)


def _events(*events: dict[str, Any]) -> str:
    return ''.join(json.dumps(event) + '\n' for event in events)


def _step(op: str) -> dict[str, Any]:
    return {'resourcePreEvent': {'metadata': {'op': op, 'urn': 'urn:box', 'type': 'probe:index:Box'}}}


@pytest.mark.parametrize(
    ('old', 'new', 'planned'),
    [
        ({}, {'protect': True}, True),
        ({'protect': False}, {'protect': True}, True),
        ({'retainOnDelete': True}, {}, True),
        ({'provider': 'urn:a::id'}, {'provider': 'urn:b::id'}, True),
        ({}, {'protect': False}, False),
        ({'protect': True}, {'protect': True}, False),
    ],
)
def test_a_same_step_whose_options_change_is_planned(
    old: dict[str, object], new: dict[str, object], planned: bool
) -> None:
    same = {'resourcePreEvent': {'metadata': {'op': 'same', 'urn': 'urn:box', 'old': old, 'new': new}}}

    preview = driver.read_preview(_events(same, {'summaryEvent': {'resourceChanges': {'same': 2}}}))

    assert preview.planned is planned


@pytest.mark.parametrize(
    ('printed', 'refusal'),
    [
        (_events(_step('same')), 'no summary'),
        (_events(_step('same'), {'summaryEvent': {'result': 'succeeded'}}), 'no summary'),
        (_events({'summaryEvent': {'resourceChanges': {}}}), 'no step at all'),
        (_events({'summaryEvent': {'resourceChanges': ['same']}}), 'not a count by operation'),
        (_events(_step('update'), {'summaryEvent': {'resourceChanges': {'same': 2}}}), 'summary counts none'),
        ('Previewing update (probe):\n', 'not an engine event'),
    ],
)
def test_a_preview_whose_plan_cannot_be_read_is_refused(printed: str, refusal: str) -> None:
    # "Nothing planned" is the answer that stays quiet, so every way the
    # events stop saying what was planned refuses instead.
    with pytest.raises(driver.Refused, match=refusal):
        _ = driver.read_preview(printed)


# --------------------------------------------------------------------------
# The working copy, over a scratch repository.
# --------------------------------------------------------------------------


def test_a_forge_main_never_fetched_is_refused_naming_the_fetch(repository: Repository, committed: str) -> None:
    repository.advance_the_forge()
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match='not in this repository: `jj git fetch`'):
        _ = run.plan()
    assert fake.streamed == []


def test_a_forge_main_fetched_but_not_under_the_working_copy_is_refused_naming_the_rebase(
    repository: Repository, committed: str
) -> None:
    repository.advance_the_forge()
    repository.fetch()
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match=r'fetched but the working copy is not on it: `jj rebase -b @ -d main`'):
        _ = run.plan()
    assert fake.streamed == []


CONFLICTED = '<<<<<<< Conflict 1 of 1\n+++++++ side #1\n{"version":3}\n+++++++ side #2\n{"version":3}\n>>>>>>> Conflict 1 of 1 ends\n'


def test_a_conflicted_checkpoint_is_refused_naming_the_reconcile(repository: Repository, committed: str) -> None:
    path = _checkpoint(repository.checkout, [])
    _ = path.write_text(CONFLICTED)
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match=r'is conflicted.*jj restore --from main checkpoints/'):
        _ = run.plan()
    assert fake.streamed == []


def test_behind_the_forge_and_conflicted_the_ancestry_is_what_is_refused(
    repository: Repository, committed: str
) -> None:
    path = _checkpoint(repository.checkout, [])
    _ = path.write_text(CONFLICTED)
    repository.advance_the_forge()
    repository.fetch()
    run = driver.Run.open(committed, repository.checkout, pulumi=FakePulumi(), base=AMBIENT)

    with pytest.raises(driver.Refused, match='jj rebase') as refused:
        _ = run.plan()
    assert 'conflicted' not in str(refused.value)


@pytest.fixture(scope='module')
def pinned_jj() -> None:
    """The pinned `jj`, refused when missing rather than skipped."""
    pinned_tools.require('jj')


needs_jj = pytest.mark.usefixtures('pinned_jj')


@needs_jj
def test_in_a_jj_checkout_the_working_copy_is_jjs(repository: Repository, committed: str) -> None:
    # `@` in a colocated checkout is a commit git's HEAD is the parent of; the
    # forge's main is checked against it, as it is where a run's checkpoint
    # sits.
    jj_env = os.environ | {'JJ_CONFIG': os.devnull, 'JJ_USER': 'probe', 'JJ_EMAIL': 'probe@example.invalid'}
    _ = process_sessions.run(
        ['jj', 'git', 'init', '--colocate'], cwd=repository.checkout, env=jj_env, check=True, timeout=60
    )
    at = process_sessions.run(
        ['jj', 'log', '--no-graph', '-r', '@', '-T', 'commit_id'],
        cwd=repository.checkout,
        env=jj_env,
        text=True,
        check=True,
        timeout=60,
    ).stdout

    assert checkpoint.working_copy(repository.checkout) == at
    assert driver.Run.open(committed, repository.checkout, pulumi=FakePulumi(), base=AMBIENT).plan() == 0

    repository.advance_the_forge()
    repository.fetch()
    with pytest.raises(driver.Refused, match='jj rebase'):
        _ = driver.Run.open(committed, repository.checkout, pulumi=FakePulumi(), base=AMBIENT).plan()


def _jj(*args: str, cwd: Path) -> str:
    # Through a POSIX session of its own: `jj git fetch` runs `git fetch`,
    # which starts processes of its own.
    return process_sessions.run(['jj', *args], cwd=cwd, text=True, check=True, timeout=60).stdout


@needs_jj
def test_a_primary_working_copy_another_workspace_rewrote_is_refused(
    repository: Repository, committed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The primary's `@` holds its own run's checkpoint when a landed one is
    # fetched and `@` is rebased onto it from a second workspace. The files on
    # disk are still the primary's older checkpoint; `@` is now on `main`.
    # Read past the staleness, the ancestry passes and the run starts from the
    # older file; refused, it cannot.
    monkeypatch.setenv('JJ_CONFIG', os.devnull)
    monkeypatch.setenv('JJ_USER', 'probe')
    monkeypatch.setenv('JJ_EMAIL', 'probe@example.invalid')
    primary = repository.checkout
    _ = _jj('git', 'init', '--colocate', cwd=primary)
    ours = _checkpoint(primary, [])
    _ = ours.write_text('{"c":1}')
    _ = _jj('describe', '-m', 'our run', cwd=primary)
    theirs = repository.other / ours.relative_to(primary)
    theirs.parent.mkdir(parents=True)
    _ = theirs.write_text('{"c":2}')
    _ = _git('add', str(theirs.relative_to(repository.other)), cwd=repository.other)
    _ = _git('commit', '-q', '-m', 'a landed run', cwd=repository.other)
    _ = _git('push', '-q', 'origin', 'main', cwd=repository.other)
    _ = _jj('git', 'fetch', cwd=primary)
    second = primary.parent / 'second'
    _ = _jj('workspace', 'add', str(second), cwd=primary)
    _ = _jj('rebase', '-b', 'default@', '-d', 'main@origin', cwd=second)

    with pytest.raises(checkpoint.CheckRefused, match='stale'):
        checkpoint.require_current(primary, ours)


#: How long the stand-in below runs before it ends by itself: a bound on a
#: stand-in whose case can no longer kill it, since an outright kill of the
#: test process runs no clean-up. A stop-loss, far above the case, and
#: nothing waits on it: the ^C ends it first.
STAND_IN_LIFETIME = 300

#: A `pulumi` that answers ^C the way the real one does, by finishing what it
#: has in flight before it exits: it takes a second over it -- longer than the
#: quarter-second after which `subprocess.run` would kill it, which is the
#: regression the case catches -- and says so once it has. Its lifetime is a
#: `sleep` it starts in the background, which a shell without job control
#: starts with ^C ignored, so the trap ends it: left running, it would hold
#: the driver's standard output, and the case's read after the ^C with it.
#: Everything is in place before it says it has started, on the standard
#: output the driver hands it, so a ^C at any moment after that line meets
#: the trap.
PULUMI_ANSWERING_SIGINT = f"""\
#!/bin/sh
sleep {STAND_IN_LIFETIME} &
lifetime=$!
trap 'kill $lifetime 2>/dev/null; sleep 1; echo finished; exit 3' INT
echo started
wait $lifetime
exit 4
"""

#: The driver's `Cli` streaming a run of it, in a process of its own.
STREAMING = """\
import os, sys
from pathlib import Path
from kluster.scripts.operator_stack import driver
sys.exit(driver.Cli().stream(['up'], cwd=Path.cwd(), env=dict(os.environ)))
"""

#: How long the case waits on the driver once it has sent ^C: a stop-loss
#: below the case bound, which nothing asserts on.
SIGINT_WAIT = 30


def test_a_sigint_during_a_run_is_pulumis_to_answer(tmp_path: Path) -> None:
    # The terminal sends ^C to the whole foreground process group; the driver
    # lets `pulumi` finish answering it, and returns what `pulumi` exits with.
    # The driver leads a POSIX session of its own, and the stand-in runs in
    # its process group, so `killpg` on the session's id is that ^C.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stand_in = bin_dir / 'pulumi'
    _ = stand_in.write_text(PULUMI_ANSWERING_SIGINT)
    stand_in.chmod(0o755)
    env = os.environ | {'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    with process_sessions.started(
        [sys.executable, '-c', STREAMING], cwd=tmp_path, env=env, stdout=process_sessions.PIPE, text=True
    ) as running:
        assert running.stdout is not None
        # An event: the stand-in's own line. It meets EOF instead if the
        # driver ends before starting it, and the case fails here by name.
        assert running.stdout.readline() == 'started\n', 'the stand-in never started'
        os.killpg(running.pid, signal.SIGINT)
        code = running.wait(timeout=SIGINT_WAIT)
        answered = running.stdout.read()

    assert answered == 'finished\n', 'pulumi was killed before it finished answering ^C'
    assert code == 3


# --------------------------------------------------------------------------
# What a committed checkpoint may publish.
# --------------------------------------------------------------------------

TOKEN = 'a-token-the-program-marks-secret'
KEY = 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nVJTUt9Us8cKjMzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dV\n'


def _write_run(checkout: Path, fake: FakePulumi, resources: list[dict[str, Any]]) -> Callable[[list[str]], None]:
    """What a writing command leaves behind: the checkpoint rewritten to hold `resources`."""

    def effect(_args: list[str]) -> None:
        _ = _checkpoint(checkout, resources)

    return effect


@pytest.mark.parametrize(
    ('resource', 'config', 'property_'),
    [
        # A secret value, from the configuration, in an output of another name.
        (
            {'urn': 'urn:box', 'inputs': {}, 'outputs': {'echo': TOKEN}},
            {'probe:token': {'value': TOKEN, 'secret': True}},
            'urn:box outputs.echo',
        ),
        # One line of a multi-line secret, re-wrapped inside another string.
        (
            {'urn': 'urn:box', 'inputs': {}, 'outputs': {'userData': 'key: |\n  ' + KEY.splitlines()[1] + '\n'}},
            {'probe:key': {'value': KEY, 'secret': True}},
            'urn:box outputs.userData',
        ),
    ],
)
def test_a_secret_value_in_the_clear_fails_the_run_naming_the_property(
    repository: Repository,
    committed: str,
    caplog: pytest.LogCaptureFixture,
    resource: dict[str, Any],
    config: dict[str, Any],
    property_: str,
) -> None:
    fake = FakePulumi(exports=[_export([resource])], config=config)
    fake.effect = _write_run(repository.checkout, fake, [resource])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['state', 'unprotect', 'urn:box', '--yes']) == driver.FAILED

    path = stack_environment.checkpoint(repository.checkout, committed)
    assert path is not None
    record = checkpoint.record(repository.checkout, committed).read_text()
    assert property_ in record
    assert property_ in caplog.text
    # The value itself is named nowhere a finding goes.
    assert TOKEN not in record + caplog.text
    assert KEY.splitlines()[1] not in record + caplog.text


def test_a_secret_value_from_the_state_is_searched_for_too(repository: Repository, committed: str) -> None:
    resource = {
        'urn': 'urn:key',
        'inputs': {},
        'outputs': {'secret': _ciphertext(TOKEN), 'copy': TOKEN},
    }
    exported = {**resource, 'outputs': {'secret': _plaintext(TOKEN), 'copy': TOKEN}}
    fake = FakePulumi(exports=[_export([exported])])
    fake.effect = _write_run(repository.checkout, fake, [resource])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['refresh', '--yes']) == driver.FAILED
    path = stack_environment.checkpoint(repository.checkout, committed)
    assert path is not None
    assert (
        'urn:key outputs.copy: holds the value of urn:key outputs.secret in the clear'
        in checkpoint.record(repository.checkout, committed).read_text()
    )


@pytest.mark.parametrize(
    ('resource', 'property_'),
    [
        (
            {'urn': 'urn:key', 'additionalSecretOutputs': ['secret'], 'inputs': {}, 'outputs': {'secret': 'k'}},
            'urn:key outputs.secret: is named in additionalSecretOutputs',
        ),
        (
            {'urn': 'urn:box', 'inputs': {'metadata': _ciphertext('m')}, 'outputs': {'metadata': 'm'}},
            'urn:box outputs.metadata: holds ciphertext as an input',
        ),
    ],
)
def test_a_declared_secret_property_in_the_clear_fails_the_run_naming_it(
    repository: Repository, committed: str, resource: dict[str, Any], property_: str
) -> None:
    fake = FakePulumi(exports=[_export([resource])])
    fake.effect = _write_run(repository.checkout, fake, [resource])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['refresh', '--yes']) == driver.FAILED
    path = stack_environment.checkpoint(repository.checkout, committed)
    assert path is not None
    assert property_ in checkpoint.record(repository.checkout, committed).read_text()


def test_a_checkpoint_holding_its_secrets_as_ciphertext_passes(repository: Repository, committed: str) -> None:
    resource = {
        'urn': 'urn:box',
        'additionalSecretOutputs': ['secret'],
        'inputs': {'metadata': _ciphertext('m')},
        'outputs': {'metadata': _ciphertext('m'), 'secret': _ciphertext(TOKEN), 'plain': 'an identifier'},
    }
    exported = {
        **resource,
        'inputs': {'metadata': _plaintext('m')},
        'outputs': {'metadata': _plaintext('m'), 'secret': _plaintext(TOKEN), 'plain': 'an identifier'},
    }
    fake = FakePulumi(exports=[_export([exported])], config={'probe:token': {'value': TOKEN, 'secret': True}})
    fake.effect = _write_run(repository.checkout, fake, [resource])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['refresh', '--yes']) == 0
    path = stack_environment.checkpoint(repository.checkout, committed)
    assert path is not None
    assert not checkpoint.record(repository.checkout, committed).exists()


def test_a_write_over_an_unchanged_deployment_keeps_the_files_bytes(repository: Repository, committed: str) -> None:
    resource = {'urn': 'urn:box', 'inputs': {}, 'outputs': {'plain': 'an identifier'}}
    path = _checkpoint(repository.checkout, [resource])
    before = path.read_bytes()
    # The engine's rewrite: a new timestamp, the same deployment.
    fake = FakePulumi(exports=[_export([resource], time='t0'), _export([resource], time='t1')])

    def rewrite(_args: list[str]) -> None:
        _ = path.write_text(
            json.dumps(
                {
                    'version': 3,
                    'checkpoint': {'stack': PROBE, 'latest': {'manifest': {'time': 't1'}, 'resources': [resource]}},
                }
            )
        )

    fake.effect = rewrite
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['state', 'unprotect', 'urn:box', '--yes']) == 0
    assert path.read_bytes() == before


def test_a_secret_that_lost_its_marking_in_the_run_fails_it(repository: Repository, committed: str) -> None:
    # A generated secret with no input of the same name: once the command
    # drops its only marking, only the state before the command says it was
    # secret.
    before = {'urn': 'urn:dumpkey', 'inputs': {}, 'outputs': {'key': _plaintext(TOKEN)}}
    after = {'urn': 'urn:dumpkey', 'inputs': {}, 'outputs': {'key': TOKEN}}
    _ = _checkpoint(repository.checkout, [{**before, 'outputs': {'key': _ciphertext(TOKEN)}}])
    fake = FakePulumi(exports=[_export([before]), _export([after])])
    fake.effect = _write_run(repository.checkout, fake, [after])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['up', '--yes', '--skip-preview']) == driver.FAILED
    assert (
        'urn:dumpkey outputs.key: holds the value of urn:dumpkey outputs.key before the command in the clear'
        in checkpoint.record(repository.checkout, committed).read_text()
    )


@pytest.mark.parametrize('stray', ['probe.json.gz', 'probe.json.zst', 'probe.json.1790591454000000000'])
def test_a_file_for_the_stack_beside_its_checkpoint_fails_the_run_naming_it(
    repository: Repository, committed: str, stray: str
) -> None:
    resource = {'urn': 'urn:box', 'inputs': {}, 'outputs': {'plain': 'an identifier'}}
    fake = FakePulumi(exports=[_export([resource])])

    def effect(_args: list[str]) -> None:
        path = _checkpoint(repository.checkout, [resource])
        _ = (path.parent / stray).write_text('a copy of the state the checks never read')

    fake.effect = effect
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.passthrough(['up', '--yes', '--skip-preview']) == driver.FAILED
    assert (
        f'checkpoints/.pulumi/stacks/probe/{stray}: is not a file the checks read'
        in checkpoint.record(repository.checkout, committed).read_text()
    )


def test_a_run_stopped_before_its_checks_leaves_the_record_and_the_next_up_checks_again(
    repository: Repository, committed: str
) -> None:
    resource = {'urn': 'urn:box', 'inputs': {}, 'outputs': {'plain': 'an identifier'}}
    _ = _checkpoint(repository.checkout, [resource])
    # The export before the write answers and the one after it does not.
    fake = FakePulumi(exports=[_export([resource])], failing_export=2)
    fake.effect = _write_run(repository.checkout, fake, [resource])
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match='stack export'):
        _ = run.passthrough(['refresh', '--yes'])

    record = checkpoint.record(repository.checkout, committed)
    assert record.read_text().startswith('unchecked: `pulumi refresh --yes`')
    fake.failing_export = None
    assert run.up(yes=True) == 0
    assert fake.ups() == [['up', '--refresh', '--yes', '--skip-preview', '--stack', committed]]
    assert not record.exists()


def test_each_export_around_a_write_is_announced_before_it_starts(
    repository: Repository, committed: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Each reads the backend under a network timeout, so a slow one is a
    # silence the line before it explains.
    caplog.set_level(logging.INFO)
    resource = {'urn': 'urn:box', 'inputs': {}, 'outputs': {'plain': 'an identifier'}}
    _ = _checkpoint(repository.checkout, [resource])
    fake = FakePulumi(exports=[_export([resource])])
    answer = fake.capture
    # At each export: how many lines had been logged, and the last of them.
    announced: list[tuple[int, str]] = []

    def announcing(args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        if list(args[:2]) == ['stack', 'export']:
            announced.append((len(caplog.records), caplog.messages[-1]))
        return answer(args, cwd=cwd, env=env)

    fake.capture = announcing
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)
    before = len(caplog.records)

    assert run.passthrough(['state', 'unprotect', 'urn:box', '--yes']) == 0
    # A line of its own ahead of each export, logged since the one before it,
    # and naming the stack whose state the export reads.
    counts = [before, *(count for count, _ in announced)]
    assert len(announced) == 2
    assert all(earlier < later for earlier, later in itertools.pairwise(counts)), counts
    assert all(committed in line for _, line in announced), announced


def test_one_place_holding_a_secret_is_one_finding_naming_every_source() -> None:
    document = {'checkpoint': {'latest': {'resources': [{'urn': 'urn:box', 'outputs': {'echo': TOKEN}}]}}}

    findings = checkpoint.cleartext(document, {'config probe:token': TOKEN, 'urn:key outputs.secret': TOKEN})

    assert [str(finding) for finding in findings] == [
        'urn:box outputs.echo: holds the value of config probe:token, urn:key outputs.secret in the clear'
    ]


def test_a_failed_git_is_quoted_whole(tmp_path: Path) -> None:
    # Outside a repository `git ls-remote` explains itself over several lines,
    # and the one that says what failed is the first.
    with pytest.raises(checkpoint.CheckRefused, match=r'fatal: .*\n'):
        checkpoint.require_current(tmp_path, None)


# --------------------------------------------------------------------------
# The checks as functions.
# --------------------------------------------------------------------------


def test_an_export_that_moved_only_its_timestamp_and_resource_order_is_the_same_deployment() -> None:
    first = {'urn': 'urn:a', 'outputs': {'s': _plaintext('x')}}
    second: dict[str, Any] = {'urn': 'urn:b', 'outputs': {}}

    assert checkpoint.same_deployment(_export([first, second], time='t0'), _export([second, first], time='t1'))
    # A property that became secret is a change, although its value is not.
    assert not checkpoint.same_deployment(
        _export([{'urn': 'urn:a', 'outputs': {'s': 'x'}}]),
        _export([{'urn': 'urn:a', 'outputs': {'s': _plaintext('x')}}]),
    )


def test_a_value_too_short_to_identify_and_pem_armor_are_not_searched_for() -> None:
    found = checkpoint.needles(
        {'config k': '-----BEGIN KEY-----\nshort\n' + 'x' * 40 + '\n-----END KEY-----\n', 'config t': 'true'}
    )

    assert {needle for needle, _ in found} == {
        '-----BEGIN KEY-----\nshort\n' + 'x' * 40 + '\n-----END KEY-----\n',
        'x' * 40,
    }


# --------------------------------------------------------------------------
# The real engine, over a scratch `file://` backend.
# --------------------------------------------------------------------------

needs_pulumi = pytest.mark.skipif(shutil.which('pulumi') is None, reason='the pinned pulumi CLI is not on PATH')

#: The bounds a case that runs the real engine runs under, set from measured
#: durations rather than from the suite's per-case bound (testing.md §8). On a
#: four-core machine the slowest whole case here -- its stack's set-up and
#: every command -- took 9 s idle, 25 s with twice as many busy processes as
#: cores, and 40 s with four times as many. One command is bounded at three
#: times that worst case, and the case at twice the command, so a stalled
#: command fails by its own `TimeoutExpired`, naming it, before the case's
#: bound fires; both are stop-losses, and nothing asserts on elapsed time.
ENGINE_COMMAND_TIMEOUT = 120
ENGINE_CASE_TIMEOUT = 240
engine_bound = pytest.mark.timeout(ENGINE_CASE_TIMEOUT)

#: A program with one real resource and no credential: a dynamic resource
#: whose `gen` input replaces nothing and updates in place, and the stack's
#: own outputs, one of them secret so that every write re-encrypts something.
#: Its `update` or its `read`, as the run's environment says, can be held.
#: What it declares is read from `settings.json` beside it, so a case changes
#: the program's answer without changing the program.
PROGRAM = """\
import json
import os
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, ReadResult, Resource, ResourceProvider, UpdateResult


def held(op):
    # The operation `PROBE_HOLD_OP` names waits for the case to open the FIFO
    # `PROBE_HOLD` names: a step held as long as the case needs, as an event.
    if os.environ.get('PROBE_HOLD_OP') == op:
        open(os.environ['PROBE_HOLD']).read()


class Box(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='box', outs=dict(props))

    def diff(self, _id, olds, news):
        return DiffResult(changes=olds.get('gen') != news.get('gen'))

    def update(self, _id, _olds, news):
        held('update')
        return UpdateResult(outs=dict(news))

    def read(self, id_, props):
        held('read')
        return ReadResult(id_=id_, outs=props)


class BoxResource(Resource):
    def __init__(self, name, gen, opts):
        super().__init__(Box(), name, {'gen': gen}, opts)


settings = json.loads(pathlib.Path('settings.json').read_text())
BoxResource('box', settings['gen'], pulumi.ResourceOptions(protect=settings['protect']))
pulumi.export('plain', settings['plain'])
pulumi.export('token', pulumi.Output.secret(settings['secret']))
"""

#: The project around it: a Python program run under the `uv` toolchain in a
#: virtual environment of the case's own, whose `.pth` file reaches the test
#: run's own packages, so nothing is installed and nothing is fetched.
PROJECT = """\
name: probe
runtime:
  name: python
  options:
    toolchain: uv
    virtualenv: {venv}
"""
PYPROJECT = """\
[project]
name = "probe"
version = "0"
requires-python = ">={major}.{minor}"
dependencies = []

[tool.uv]
package = false
"""


@dataclass
class Scratch:
    run: driver.Run
    checkout: Path
    base: dict[str, str]

    @property
    def path(self) -> Path:
        path = stack_environment.checkpoint(self.checkout, PROBE)
        assert path is not None
        return path

    def history(self) -> int:
        """How many records of an update the backend holds: the engine adds a pair for each one it runs."""
        return len(list((self.checkout / stack_environment.CHECKPOINTS / '.pulumi' / 'history').rglob('*.json')))

    def settings(
        self,
        *,
        gen: str = 'g1',
        plain: str = 'hello',
        secret: str = 'a-secret-the-probe-outputs',
        protect: bool = False,
    ) -> None:
        _ = (self.checkout / 'settings.json').write_text(
            json.dumps({'gen': gen, 'plain': plain, 'secret': secret, 'protect': protect})
        )

    def files(self) -> set[str]:
        return {path.name for path in self.path.parent.iterdir()}


class Bounded:
    """The driver's `Pulumi` for a case: the pinned CLI, each command in a POSIX session of its own.

    The production `Cli` waits on an operator's run with no bound, which is
    right at a terminal and wrong in a case: a stalled command would hang the
    case past its bound. Here every command goes through
    `process_sessions.run` at `ENGINE_COMMAND_TIMEOUT`, so a stall fails as a
    `TimeoutExpired` naming it, and nothing the command started -- the
    language host, a dynamic provider -- outlives the call. What the driver
    reads it reads the same way: `events` hands back standard output,
    `capture` refuses a failure naming the command and its standard error,
    and `stream`'s output is the case's. What each run is said to be doing
    is taken and left unused: the production `Cli` is what narrates.
    """

    def _run(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> sp.CompletedProcess[str]:
        return process_sessions.run(['pulumi', *args], cwd=cwd, env=env, text=True, timeout=ENGINE_COMMAND_TIMEOUT)

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None) -> int:
        ran = self._run(args, cwd=cwd, env=env)
        _ = sys.stdout.write(ran.stdout)
        _ = sys.stderr.write(ran.stderr)
        return ran.returncode

    def events(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None
    ) -> tuple[int, str]:
        ran = self._run(args, cwd=cwd, env=env)
        _ = sys.stderr.write(ran.stderr)
        return ran.returncode, ran.stdout

    def capture(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        ran = self._run([*args, '--non-interactive'], cwd=cwd, env=env)
        if ran.returncode != 0:
            raise driver.Refused(f'`pulumi {" ".join(args[:2])}` failed: {ran.stderr.strip() or ran.returncode}')
        return ran.stdout


def _initialized(repository: Repository, stack: str, tmp_path: Path, program: str) -> Scratch:
    """`program` as `probe`'s, with its stack initialized and nothing applied."""
    if shutil.which('pulumi') is None or shutil.which('uv') is None:
        pytest.skip('the pinned pulumi CLI or uv is not on PATH')
    venv = tmp_path / 'venv'
    # `uv` keeps what it learns of an interpreter in its cache, which is the
    # case's own here, as it is for the language host's `uv` below.
    uv_env = os.environ | {'UV_CACHE_DIR': str(tmp_path / 'uv-cache')}
    _ = process_sessions.run(
        ['uv', 'venv', '-q', '--python', sys.executable, str(venv)], env=uv_env, timeout=60, check=True
    )
    (site_packages,) = venv.glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    checkout = repository.checkout
    _ = (checkout / '__main__.py').write_text(program)
    _ = (checkout / 'Pulumi.yaml').write_text(PROJECT.format(venv=venv))
    _ = (checkout / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = process_sessions.run(['uv', 'lock', '-q', '--offline'], cwd=checkout, env=uv_env, timeout=60, check=True)
    # The CLI's home and its temporary directory are the case's
    # (`scratch_projects.cli_directories`), so what a run leaves goes with it.
    base = (
        dict(os.environ)
        | cli_directories(tmp_path)
        | {
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
            'UV_OFFLINE': '1',
            'UV_CACHE_DIR': uv_env['UV_CACHE_DIR'],
        }
    )
    made = Scratch(run=driver.Run.open(stack, checkout, base=base, pulumi=Bounded()), checkout=checkout, base=base)
    assert made.run.passthrough(['stack', 'init']) == 0
    return made


@pytest.fixture
def scratch(repository: Repository, committed: str, tmp_path: Path) -> Iterator[Scratch]:
    made = _initialized(repository, committed, tmp_path, PROGRAM)
    made.settings()
    assert made.run.passthrough(['up', '--yes', '--skip-preview']) == 0
    yield made


@needs_pulumi
@engine_bound
def test_the_real_preview_is_read_for_what_it_plans(scratch: Scratch) -> None:
    assert scratch.run.plan() == driver.NOTHING_PLANNED

    # A resource that changes is a step the summary counts.
    scratch.settings(gen='g2')
    assert scratch.run.plan() == driver.PLANNED

    # A plain stack output that changes is no step, and still planned.
    scratch.settings(plain='changed')
    assert scratch.run.plan() == driver.PLANNED


@needs_pulumi
@engine_bound
def test_a_real_change_to_a_resources_protect_alone_is_planned_and_applied(scratch: Scratch) -> None:
    # The engine counts the step `same`; the state it writes carries the
    # option all the same, and `protect` refuses a replacement only once the
    # state holds it.
    scratch.settings(protect=True)

    assert scratch.run.plan() == driver.PLANNED
    assert scratch.run.up(yes=True) == 0

    (box,) = [
        r
        for r in json.loads(scratch.path.read_text())['checkpoint']['latest']['resources']
        if r['urn'].endswith('::box')
    ]
    assert box.get('protect') is True
    assert scratch.run.plan() == driver.NOTHING_PLANNED


@needs_pulumi
@engine_bound
def test_the_real_up_over_nothing_planned_writes_nothing(scratch: Scratch) -> None:
    before, history = scratch.path.read_bytes(), scratch.history()

    assert scratch.run.up(yes=True) == driver.NOTHING_PLANNED

    assert scratch.path.read_bytes() == before
    # No update ran at all, rather than one whose bytes were put back.
    assert scratch.history() == history


@needs_pulumi
@engine_bound
def test_a_real_write_over_an_unchanged_deployment_keeps_the_files_bytes(scratch: Scratch) -> None:
    before, history = scratch.path.read_bytes(), scratch.history()

    assert scratch.run.passthrough(['up', '--yes', '--skip-preview']) == 0

    # The engine did write, and what it wrote differed only in its timestamp
    # and its ciphertexts.
    assert scratch.history() > history
    assert scratch.path.read_bytes() == before


@needs_pulumi
@engine_bound
def test_a_real_up_over_nothing_planned_runs_while_a_failed_check_is_recorded_and_clears_it(scratch: Scratch) -> None:
    record = checkpoint.record(scratch.checkout, PROBE)
    _ = record.write_text('urn:box outputs.metadata: holds ciphertext as an input and is not ciphertext as an output\n')
    history = scratch.history()

    assert scratch.run.up(yes=True) == 0

    assert scratch.history() > history
    assert not record.exists()


#: The driver's `up`, run from a process of its own whose standard streams
#: the case chooses: the stack in the census as the fixture `committed` puts
#: it there, an empty secret store as the fixture `secret_store` installs, and
#: the driver's log on standard error as `operator-stack` configures it.
UP_IN_A_PROCESS = """\
import json, logging, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from memory_keyring import MemoryKeyring, installed
from scratch_projects import cli_directories
from kluster.conventions import identity
from kluster.scripts.operator_stack import driver
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
identity.OPERATOR_STACKS = {{**identity.OPERATOR_STACKS, {stack!r}: identity.StateHome.COMMITTED}}
with installed(MemoryKeyring()):
    sys.exit(driver.Run.open({stack!r}, Path({checkout!r}), base=json.loads({base!r})).up(yes=True))
"""

#: A cursor moved up a line or more: how `pulumi`'s interactive display
#: redraws its tree in place, and something its non-interactive display,
#: which only appends lines, never writes.
REDRAW = re.compile(rb'\x1b\[\d+A')

#: A colour: what `pulumi` writes only where its standard output is a
#: terminal, whichever display it then draws. The driver's own lines carry none.
COLOUR = re.compile(rb'\x1b\[38;5;\d+m')


def _drained(reader: int, command: process_sessions.Command) -> bytes:
    """Everything written to `reader` until its last writer is gone, or a `TimeoutExpired` naming the run.

    The bound is on the whole read, `ENGINE_COMMAND_TIMEOUT` from its start,
    not on a silence: a stalled `pulumi` is not silent, since its display
    redraws at a terminal, and under a pipe the driver says what is in
    flight every `progress.INTERVAL`. The
    `started` block the timeout is raised in then ends the run's whole
    session.
    """
    read = b''
    deadline = time.monotonic() + ENGINE_COMMAND_TIMEOUT
    with selectors.DefaultSelector() as selector:
        _ = selector.register(reader, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(timeout=left):
                raise sp.TimeoutExpired(command.args, ENGINE_COMMAND_TIMEOUT, output=read)
            try:
                chunk = os.read(reader, 65536)
            except OSError:  # a pty whose last writer is gone reads as EIO
                return read
            if not chunk:
                return read
            read += chunk


@needs_pulumi
@engine_bound
@pytest.mark.parametrize('terminal', [True, False], ids=['terminal', 'pipe'])
def test_a_real_up_hands_pulumi_the_drivers_own_output_and_reads_what_it_needs_from_the_events(
    scratch: Scratch, terminal: bool
) -> None:
    # Pins what the driver does today, as a tripwire against capturing the
    # apply's output: with a terminal of real dimensions `pulumi` draws its
    # interactive display, whose elapsed times tick, and under a pipe its
    # non-interactive one. Either way the plan, the apply and the checkpoint's
    # checks are read from the events and the state, never from the display.
    scratch.settings(gen='g2')
    # `pulumi` reads a CI system's own variables -- `GITHUB_ACTIONS` on this
    # repository's runner -- as a reason to draw its non-interactive display
    # even at a terminal, so the run carries none, as an operator's terminal
    # does not.
    env = {name: value for name, value in scratch.base.items() if name != 'GITHUB_ACTIONS'} | {'TERM': 'xterm-256color'}
    program = UP_IN_A_PROCESS.format(
        tests=str(Path(__file__).parent), stack=PROBE, checkout=str(scratch.checkout), base=json.dumps(env)
    )
    # The driver's standard streams: a terminal of real dimensions, or a pipe.
    if terminal:
        reader, writer = pty.openpty()
        fcntl.ioctl(writer, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    else:
        reader, writer = os.pipe()
    try:
        with process_sessions.started(
            [sys.executable, '-c', program],
            env=env,
            stdin=writer if terminal else process_sessions.DEVNULL,
            stdout=writer,
            stderr=process_sessions.STDOUT,
        ) as command:
            # The driver holds its own copy; with this one closed, the read
            # meets its end once every writer in the run is gone.
            os.close(writer)
            writer = -1
            shown = _drained(reader, command)
            code = command.wait(timeout=ENGINE_COMMAND_TIMEOUT)
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)

    assert code == 0, shown.decode()
    # The driver's own log, in the format the program above gives it, reaches
    # the same stream.
    assert b'INFO: ' in shown, shown.decode()
    assert bool(COLOUR.search(shown)) is terminal, shown.decode()
    assert bool(REDRAW.search(shown)) is terminal, shown.decode()
    (box,) = [
        r
        for r in json.loads(scratch.path.read_text())['checkpoint']['latest']['resources']
        if r['urn'].endswith('::box')
    ]
    assert box['outputs']['gen'] == 'g2'
    assert not checkpoint.record(scratch.checkout, PROBE).exists()
    assert scratch.run.plan() == driver.NOTHING_PLANNED


#: The driver's `plan` or `up` in a process of its own, as `UP_IN_A_PROCESS`
#: runs it, with the production `Cli`, ticking every `PROGRESS_INTERVAL`.
DRIVEN_IN_A_PROCESS = """\
import json, logging, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from memory_keyring import MemoryKeyring, installed
from kluster.conventions import identity
from kluster.scripts.operator_stack import driver, progress
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
progress.INTERVAL = {interval!r}
identity.OPERATOR_STACKS = {{**identity.OPERATOR_STACKS, {stack!r}: identity.StateHome.COMMITTED}}
with installed(MemoryKeyring()):
    run = driver.Run.open({stack!r}, Path({checkout!r}), base=json.loads({base!r}))
    sys.exit(run.up(yes=True) if {how!r} == 'up' else run.plan())
"""

#: How often the driver in those cases says what is in flight: often, so the
#: line the case waits for comes soon after the step is held. A display rate,
#: which nothing asserts on.
PROGRESS_INTERVAL = 1.0


def _read_until(reader: int, wanted: Callable[[bytes], bool], awaited: str, command: process_sessions.Command) -> bytes:
    """What `reader` gives until a complete line is `wanted`, or a `TimeoutExpired` naming the run.

    `awaited` says what the line is, for the failure. The bound is on the
    whole read, as in `_drained`.
    """
    read = b''
    deadline = time.monotonic() + ENGINE_COMMAND_TIMEOUT
    with selectors.DefaultSelector() as selector:
        _ = selector.register(reader, selectors.EVENT_READ)
        while not any(wanted(line) for line in read.split(b'\n')[:-1]):
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(timeout=left):
                expired = sp.TimeoutExpired(command.args, ENGINE_COMMAND_TIMEOUT, output=read)
                expired.add_note(f'waiting for {awaited}')
                raise expired
            chunk = os.read(reader, 65536)
            assert chunk, f'the driver ended before {awaited}:\n{read.decode()}'
            read += chunk
    return read


def _held_run(scratch: Scratch, how: str, op: str) -> tuple[int, bytes, bytes]:
    """The driver's `how` with `op` held: what it said until it named the held step, its code, and the rest.

    The step is named when a line of the driver's log -- `INFO: `, the
    prefix `DRIVEN_IN_A_PROCESS` gives it -- holds the step's operation and
    the box's URN as the state records it. The prefix is what tells it from
    `pulumi`'s own planned-step line, which holds both as well.
    """
    (urn,) = [
        r['urn'] for r in checkpoint.resources(json.loads(scratch.path.read_text())) if r['urn'].endswith('::box')
    ]
    step = (b'update' if op == 'update' else b'refresh', urn.encode())

    def named(line: bytes) -> bool:
        return line.startswith(b'INFO: ') and step[0] in line.split() and step[1] in line

    hold = scratch.checkout.parent / 'hold'
    os.mkfifo(hold)
    env = scratch.base | {'PROBE_HOLD': str(hold), 'PROBE_HOLD_OP': op}
    program = DRIVEN_IN_A_PROCESS.format(
        tests=str(Path(__file__).parent),
        interval=PROGRESS_INTERVAL,
        stack=PROBE,
        checkout=str(scratch.checkout),
        base=json.dumps(env),
        how=how,
    )
    with process_sessions.started(
        [sys.executable, '-c', program],
        env=env,
        stdout=process_sessions.PIPE,
        stderr=process_sessions.STDOUT,
    ) as command:
        assert command.stdout is not None
        reader = command.stdout.fileno()
        said = _read_until(reader, named, f'a line of the driver naming {step[0].decode()} {urn}', command)
        # The step has been said; the program's provider waits on the FIFO.
        with hold.open('w'):
            pass
        rest = _drained(reader, command)
        code = command.wait(timeout=ENGINE_COMMAND_TIMEOUT)
    return code, said, rest


@needs_pulumi
@engine_bound
def test_a_real_apply_under_a_pipe_says_which_step_it_waits_on(scratch: Scratch) -> None:
    # The non-interactive display prints a step when it starts and when it
    # ends; between, the driver says the step is still in flight, read from
    # the engine's events, and the display carries no dots. A pulumi bump that
    # drops `--event-log` or its file form fails here, at the bound, naming the
    # line it waited for: the apply's narration rests on both
    # (`progress`, pulumi/pulumi#11139).
    scratch.settings(gen='g2')

    code, said, rest = _held_run(scratch, 'up', 'update')

    shown = said + rest
    assert code == 0, shown.decode()
    assert b'box updating' in shown and b'box updated' in shown, shown.decode()
    assert b'@ updating' not in shown, shown.decode()
    assert not checkpoint.record(scratch.checkout, PROBE).exists()
    (box,) = [r for r in checkpoint.resources(json.loads(scratch.path.read_text())) if r['urn'].endswith('::box')]
    assert box['outputs']['gen'] == 'g2'


@needs_pulumi
@engine_bound
def test_a_real_plan_says_which_refresh_it_waits_on_and_then_plans_as_before(scratch: Scratch) -> None:
    # The preview's standard output is the events the plan is read from, so
    # pulumi shows nothing of it; the driver says what the refresh is on.
    code, _, _ = _held_run(scratch, 'plan', 'read')

    assert code == driver.NOTHING_PLANNED


@needs_pulumi
@engine_bound
@pytest.mark.parametrize(
    'switch',
    [
        {'PULUMI_DIY_BACKEND_GZIP': 'true'},
        {'PULUMI_SELF_MANAGED_STATE_GZIP': 'true'},
        {'PULUMI_DIY_BACKEND_ZSTD': 'true'},
        {'PULUMI_RETAIN_CHECKPOINTS': 'true'},
    ],
)
def test_a_real_run_under_a_callers_state_switch_writes_the_checkpoint_the_checks_read(
    scratch: Scratch, switch: dict[str, str]
) -> None:
    # Each switch would write the stack's state as a file beside the
    # checkpoint, or in its place, where the checks never look.
    run = driver.Run.open(PROBE, scratch.checkout, base=scratch.base | switch, pulumi=Bounded())
    scratch.settings(gen='g2')

    assert run.passthrough(['up', '--yes', '--skip-preview']) == 0

    assert scratch.files() == {'probe.json', 'probe.json.bak'}


#: A program whose one resource echoes its inputs as its outputs, with a
#: secret placed where `settings.json`'s `shape` says: inside an object; as
#: an array inside an object; inside an object the provider hands back as a
#: string; or as the input of a property `additional_secret_outputs` names,
#: plain or, as `marked`, secret. Where `copy` names a path, the create copies
#: the stack's checkpoint file, `checkpoint`, there before it returns: the file
#: the engine wrote for that create in flight.
ECHO = """\
import json
import pathlib
import shutil

import pulumi
from pulumi.dynamic import CreateResult, Resource, ResourceProvider


class Echo(ResourceProvider):
    def create(self, props):
        if props.get('copy'):
            shutil.copyfile(props['checkpoint'], props['copy'])
        outs = dict(props)
        if props.get('reshape'):
            outs['deep'] = json.dumps(props['deep'], sort_keys=True)
        return CreateResult(id_='echo', outs=outs)


class EchoResource(Resource):
    def __init__(self, name, props, opts):
        super().__init__(Echo(), name, props, opts)


settings = json.loads(pathlib.Path('settings.json').read_text())
secret = pulumi.Output.secret(settings['secret'])
names = []
if settings['shape'] in ('object', 'reshaped'):
    props = {'deep': {'a': secret, 'b': 'plain'}, 'reshape': settings['shape'] == 'reshaped'}
elif settings['shape'] == 'array':
    props = {'box': {'keys': ['k1', secret]}}
elif settings['shape'] == 'additional':
    props, names = {'x': settings['secret']}, ['x']
else:
    props, names = {'x': secret}, ['x']
if settings['copy']:
    props['checkpoint'], props['copy'] = settings['checkpoint'], settings['copy']
EchoResource('echo', props, pulumi.ResourceOptions(additional_secret_outputs=names))
"""
ECHOED = 'a-secret-the-echo-carries'


@pytest.fixture
def echo(repository: Repository, committed: str, tmp_path: Path) -> Scratch:
    return _initialized(repository, committed, tmp_path, ECHO)


def _echo_settings(scratch: Scratch, shape: str, copy: Path | None = None) -> None:
    _ = (scratch.checkout / 'settings.json').write_text(
        json.dumps(
            {
                'shape': shape,
                'secret': ECHOED,
                'checkpoint': str(scratch.path),
                'copy': str(copy) if copy else None,
            }
        )
    )


#: `pulumi up` of `probe`, run outside the driver, whose own checks would
#: refuse some of these files: what is read is what the engine wrote.
UP = ('pulumi', 'up', '--yes', '--skip-preview', '--non-interactive', '--stack', PROBE)


def _echo_up(scratch: Scratch, shape: str, copy: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The checkpoint the engine writes for `shape`, and the echo resource in it."""
    _echo_settings(scratch, shape, copy)
    up = process_sessions.run(UP, cwd=scratch.checkout, env=scratch.run.env, text=True, timeout=ENGINE_COMMAND_TIMEOUT)
    assert up.returncode == 0, up.stdout + up.stderr
    document = json.loads(scratch.path.read_text())
    (resource,) = [r for r in checkpoint.resources(document) if r['urn'].endswith('::echo')]
    return document, resource


def _at(values: dict[str, Any], path: str) -> Any:
    for key in path.split('.'):
        values = values[key]
    return values


@needs_pulumi
@engine_bound
@pytest.mark.parametrize(
    ('shape', 'place', 'input_whole', 'value'),
    [
        ('object', 'outputs.deep.a', True, ECHOED),
        ('array', 'outputs.box.keys', True, ['k1', ECHOED]),
        ('reshaped', 'outputs.deep', False, json.dumps({'a': ECHOED, 'b': 'plain'}, sort_keys=True)),
    ],
    ids=['inside-an-object', 'array-inside-an-object', 'object-returned-as-a-string'],
)
def test_a_marking_the_real_engine_makes_below_the_top_is_found_once_lost(
    echo: Scratch, shape: str, place: str, input_whole: bool, value: object
) -> None:
    document, resource = _echo_up(echo, shape)
    parent, _, key = place.rpartition('.')

    # The engine marks the place and nothing above it, and the check passes
    # the file it wrote. Where the provider handed the object back
    # as a string, the input of that name is an object with a secret inside
    # it rather than an envelope: the output is marked for a secret anywhere
    # under the input.
    assert checkpoint.envelope(_at(resource, place))
    assert not checkpoint.envelope(_at(resource, parent))
    assert checkpoint.envelope(_at(resource, place.replace('outputs', 'inputs', 1))) is input_whole
    assert checkpoint.undeclared(document) == []

    _at(resource, parent)[key] = value

    # The remedy names the resource's input the place is under.
    top = place.split('.')[1]
    assert [str(finding) for finding in checkpoint.undeclared(document)] == [
        f'{resource["urn"]} {place}: holds ciphertext as an input and is not ciphertext as an output; '
        f'pass the input `{top}` whole as `pulumi.Output.secret(...)`'
    ]


@needs_pulumi
@engine_bound
def test_the_real_engine_leaves_an_additional_secret_outputs_input_in_the_clear_and_it_is_found(
    echo: Scratch,
) -> None:
    document, resource = _echo_up(echo, 'additional')

    # What the pinned engine writes for an ordinary program: the output
    # marked, the input of the same name as the program passed it.
    assert checkpoint.envelope(resource['outputs']['x'])
    assert resource['inputs']['x'] == ECHOED
    assert [str(finding) for finding in checkpoint.undeclared(document)] == [
        f'{resource["urn"]} inputs.x: is named in additionalSecretOutputs and is not ciphertext; '
        'pass the input `x` whole as `pulumi.Output.secret(...)`'
    ]

    # The way on: the program marks the input secret as well, and the next
    # write holds it as ciphertext.
    document, resource = _echo_up(echo, 'marked')

    assert checkpoint.envelope(resource['inputs']['x'])
    assert checkpoint.undeclared(document) == []


@needs_pulumi
@engine_bound
def test_a_real_operation_left_pending_is_checked_like_a_resource(echo: Scratch, tmp_path: Path) -> None:
    # The copy the echo's create takes is the file a run killed at that moment
    # would leave. The engine saves a step's begin mutation, which records the
    # operation as pending, before it calls the provider, and saves each
    # earlier step's end before the steps that wait on it begin, so the copy
    # holds the echo's create pending and nothing else -- not the stack's, nor
    # the default provider's, each of which the file holds pending for a while
    # earlier in the run. `check` and `diff` run before the begin mutation is
    # saved, which is why the copy is taken in `create`. The run's environment
    # carries no `PULUMI_SKIP_CHECKPOINTS`, which would hold every write back
    # until the run ends. A release that stopped writing the file before each
    # provider call fails here, and should: the driver's checks read that file.
    copy = tmp_path / 'pending.json'
    _, resource = _echo_up(echo, 'additional', copy)
    pending = json.loads(copy.read_text())

    operations = pending['checkpoint']['latest']['pending_operations']
    assert [operation['resource']['urn'] for operation in operations] == [resource['urn']]
    assert not [r for r in checkpoint.resources(pending) if r['urn'] == resource['urn']]
    assert [str(finding) for finding in checkpoint.undeclared(pending)] == [
        f'pending_operations[0] {resource["urn"]} inputs.x: '
        'is named in additionalSecretOutputs and is not ciphertext; '
        'pass the input `x` whole as `pulumi.Output.secret(...)`'
    ]


# --------------------------------------------------------------------------
# The `state-backend` stack's gate, over the fake `pulumi`.
# --------------------------------------------------------------------------

STATE_BACKEND = appliance.STACK
#: The type tokens the OCI provider registers the box and the reserved address
#: under, which every step event about them names and the URNs in state are
#: keyed by: the provider's contract, written here as it spells it rather than
#: read off the gate's own constants.
INSTANCE_TYPE = 'oci:Core/instance:Instance'
ADDRESS_TYPE = 'oci:Core/publicIp:PublicIp'
INSTANCE_URN = (
    f'urn:pulumi:{STATE_BACKEND}::kluster-py::kluster:state_backend:StateBackend${INSTANCE_TYPE}::state-backend-vm'
)
ADDRESS_URN = (
    f'urn:pulumi:{STATE_BACKEND}::kluster-py::kluster:state_backend:StateBackend${ADDRESS_TYPE}::state-backend-ip'
)
#: The box's bill of materials before a change and after it, one digest moved.
BEFORE = {'butane': 'aaaa', 'operator_keys': 'bbbb', 'postgres_image': 'cccc'}
AFTER = {'butane': 'aaaa', 'operator_keys': 'bbbb', 'postgres_image': 'dddd'}


def _box(op: str, *, before: dict[str, str] | None = BEFORE, after: dict[str, str] = AFTER) -> dict[str, Any]:
    """One step of the instance: `op`, with the bill of materials it starts from and the one it ends at."""
    metadata: dict[str, Any] = {
        'op': op,
        'urn': INSTANCE_URN,
        'type': INSTANCE_TYPE,
        'new': {'inputs': {appliance.BILL_OF_MATERIALS: after}},
        'diffs': ['metadata', appliance.BILL_OF_MATERIALS],
    }
    if before is not None:
        metadata['old'] = {'inputs': {appliance.BILL_OF_MATERIALS: before}}
    return {'resourcePreEvent': {'metadata': metadata}}


def _replacement(*, before: dict[str, str] = BEFORE, after: dict[str, str] = AFTER) -> list[dict[str, Any]]:
    """A replacement of the instance as the engine emits it under `delete_before_replace`.

    `delete-replaced` first, carrying the old state alone, then `replace` and
    `create-replacement` with both: the shapes the pinned CLI emits, which
    `test_a_real_replacement_of_the_box_names_only_the_digest_that_moved_and_waits_for_force`
    reads off the engine itself.
    """
    gone = {
        'op': 'delete-replaced',
        'urn': INSTANCE_URN,
        'type': INSTANCE_TYPE,
        'old': {'inputs': {appliance.BILL_OF_MATERIALS: before}},
        'new': None,
        'diffs': None,
    }
    return [
        {'resourcePreEvent': {'metadata': gone}},
        _box('replace', before=before, after=after),
        _box('create-replacement', before=before, after=after),
    ]


def _address(assigned: str) -> list[dict[str, Any]]:
    """The reserved address, refreshed: assigned to `assigned`, or to nothing where that is empty."""
    outputs = {'ipAddress': settings.ADDRESS, 'assignedEntityId': assigned, 'privateIpId': assigned}
    refreshed = {'op': 'refresh', 'urn': ADDRESS_URN, 'type': ADDRESS_TYPE, 'new': {'outputs': outputs}}
    same = {'op': 'same', 'urn': ADDRESS_URN, 'type': ADDRESS_TYPE, 'old': {'outputs': outputs}}
    return [{'resOutputsEvent': {'metadata': refreshed}}, {'resourcePreEvent': {'metadata': same}}]


def _expiring(at: dt.datetime) -> dict[str, Any]:
    """The stack's own outputs as the program computes them: the server certificate's expiry."""
    outputs = {settings.CERTIFICATE_EXPIRY_OUTPUT: at.isoformat()}
    metadata = {
        'op': 'same',
        'urn': 'urn:stack',
        'type': driver.STACK_TYPE,
        'old': {'outputs': outputs},
        'new': {'outputs': outputs},
    }
    return {'resOutputsEvent': {'metadata': metadata}}


NOW = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
#: An expiry well outside the renewal margin, which nothing names.
LATER = NOW + settings.RENEWAL_MARGIN + dt.timedelta(days=400)


def _planned(*events: dict[str, Any], expiry: dt.datetime = LATER) -> str:
    """A preview's events: these steps, the stack's expiry, and a summary counting the steps."""
    counts: dict[str, int] = {'same': 1}
    for event in events:
        if (pre := event.get('resourcePreEvent')) is not None:
            op = pre['metadata']['op']
            counts[op] = counts.get(op, 0) + 1
    return _events(*events, _expiring(expiry), {'summaryEvent': {'resourceChanges': counts}})


@dataclass
class Backend:
    """The estate's backend as the gate asks it: the stacks it serves, or no answer at all."""

    stacks: list[str] = field(default_factory=lambda: ['organization/kluster-py/physical'])
    answers: bool = True
    asked: int = 0

    def served(self) -> list[str]:
        self.asked += 1
        if not self.answers:
            raise appliance.Silent('postgres://operator@192.0.2.10:5432/pulumi_state did not answer')
        return list(self.stacks)


def _appliance(repository: Repository, fake: FakePulumi, backend: Backend | None = None) -> driver.Run:
    gate = appliance.Gate(served=(backend or Backend()).served, now=lambda: NOW)
    return driver.Run.open(STATE_BACKEND, repository.checkout, pulumi=fake, base=AMBIENT, gate=gate)


def test_a_pending_replacement_without_force_makes_no_up_and_names_what_moved(
    repository: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakePulumi(printed=_planned(*_replacement(), *_address('ocid1.privateip.box')))

    assert _appliance(repository, fake).up(yes=True) == driver.PLANNED

    assert fake.ups() == []
    (refusal,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    # The component whose digest moved and not the one that did not, and the
    # run that replaces the box.
    assert 'postgres_image' in refusal
    assert 'butane' not in refusal
    assert permission.REMEDY in refusal


def test_force_grants_the_permission_on_the_up_alone(repository: Repository) -> None:
    fake = FakePulumi(printed=_planned(*_replacement(), *_address('ocid1.privateip.box')))

    assert _appliance(repository, fake).up(yes=True, force=True) == driver.NOTHING_PLANNED

    (up,) = fake.ups()
    assert up == ['up', '--refresh', '--yes', '--skip-preview', '--stack', STATE_BACKEND]
    granted = [env.get(permission.ENV) for env in fake.envs]
    # The preview carries none: it runs no hook, and grants nothing to anyone.
    assert granted == [None, permission.GRANTED]


def test_replace_passes_the_instance_urn_and_grants_the_permission(repository: Repository) -> None:
    # Nothing moved: `--replace` is the replacement asked for regardless.
    fake = FakePulumi(
        printed=_planned(_box('same', after=BEFORE), *_address('ocid1.privateip.box')),
        replacing=_planned(*_replacement(after=BEFORE), *_address('ocid1.privateip.box')),
    )

    assert _appliance(repository, fake).up(yes=True, replace=True) == driver.NOTHING_PLANNED

    first, again, up = fake.streamed
    assert '--replace' not in first
    assert again[again.index('--replace') + 1] == INSTANCE_URN
    assert up[up.index('--replace') + 1] == INSTANCE_URN
    assert fake.envs[-1][permission.ENV] == permission.GRANTED


@pytest.mark.parametrize('force', [False, True], ids=['plain', 'force'])
def test_a_create_beside_a_held_address_is_refused_whatever_the_flags(repository: Repository, force: bool) -> None:
    # The refreshed state holds no box, and the refreshed address points at
    # one: another workstation's launch, or a box the stack never declared.
    fake = FakePulumi(printed=_planned(_box('create', before=None), *_address('ocid1.privateip.elsewhere')))

    with pytest.raises(driver.Refused) as refused:
        _ = _appliance(repository, fake).up(yes=True, force=force)
    assert 'ocid1.privateip.elsewhere' in str(refused.value)

    assert fake.ups() == []


def test_a_create_while_the_address_points_at_nothing_is_a_launch(repository: Repository) -> None:
    # A first launch, or one after the box is lost: the termination deleted the
    # private address the reservation pointed at.
    fake = FakePulumi(printed=_planned(_box('create', before=None), *_address('')))

    assert _appliance(repository, fake).up(yes=True, force=True) == driver.NOTHING_PLANNED

    assert len(fake.ups()) == 1


def test_plan_names_a_create_beside_a_held_address_and_applies_nothing(
    repository: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakePulumi(printed=_planned(_box('create', before=None), *_address('ocid1.privateip.elsewhere')))

    assert _appliance(repository, fake).plan() == driver.PLANNED

    assert any('ocid1.privateip.elsewhere' in message for message in caplog.messages)
    assert fake.ups() == []


#: What an unknown value serializes as in an event's properties
#: (`computedValuePlaceholder`, pkg/resource/stack/deployment.go at 3.267.0).
UNKNOWN = '04da6b54-80e4-46f7-96ec-b56ff0331ba9'


def _imported_address(assigned: str, *, repointed: bool) -> list[dict[str, Any]]:
    """The reserved address imported rather than refreshed, in the events the pinned engine emits for it.

    The state holds no address, so the refresh reads none, and the preview
    imports it (Pulumi 3.267.0):

    -   The import's `resourcePreEvent` comes before the step applies: `old`
        is null, and `new` is the program's goal, with no outputs yet
        (`NewImportStep`, pkg/resource/deploy/step.go; `makeStepEventMetadata`,
        pkg/engine/events.go).
    -   Its `resOutputsEvent` comes after: the step's Read has set `new`'s
        outputs, and an `old` made up with the same ones (`ImportStep.Apply`),
        and a preview emits that event for every step it applies
        (`previewActions.OnResourceStepPost`, pkg/engine/update.go).
    -   Where the program's inputs differ from what was read, an `update`
        follows, from the imported state to the goal
        (`continueResourceImportEvent`, pkg/resource/deploy/step_generator.go):
        re-pointing `privateIpId` at a box being created, which is unknown.
    """
    outputs = {'ipAddress': settings.ADDRESS, 'assignedEntityId': assigned, 'privateIpId': assigned}
    inputs = {'lifetime': 'RESERVED', 'privateIpId': assigned}
    goal = {'lifetime': 'RESERVED', 'privateIpId': UNKNOWN if repointed else assigned}
    imported = {'inputs': inputs, 'outputs': outputs}
    events: list[dict[str, Any]] = [
        {
            'resourcePreEvent': {
                'metadata': {
                    'op': 'import',
                    'urn': ADDRESS_URN,
                    'type': ADDRESS_TYPE,
                    'old': None,
                    'new': {'inputs': goal, 'outputs': None},
                }
            }
        },
        {
            'resOutputsEvent': {
                'metadata': {'op': 'import', 'urn': ADDRESS_URN, 'type': ADDRESS_TYPE, 'old': imported, 'new': imported}
            }
        },
    ]
    if repointed:
        update = {
            'op': 'update',
            'urn': ADDRESS_URN,
            'type': ADDRESS_TYPE,
            'old': imported,
            'new': {'inputs': goal, 'outputs': outputs | {'privateIpId': UNKNOWN}},
            'diffs': ['privateIpId'],
        }
        events += [{'resourcePreEvent': {'metadata': update}}, {'resOutputsEvent': {'metadata': update}}]
    return events


@pytest.mark.parametrize('repointed', [True, False], ids=['import-then-update', 'import-alone'])
def test_a_create_beside_an_imported_held_address_is_named_by_plan_and_refused_by_up_force(
    repository: Repository, caplog: pytest.LogCaptureFixture, repointed: bool
) -> None:
    # The address is adopted on the run that creates the box: what the import
    # read is all that says the old box still holds it.
    events = (_box('create', before=None), *_imported_address('ocid1.privateip.elsewhere', repointed=repointed))
    fake = FakePulumi(printed=_planned(*events))
    run = _appliance(repository, fake)

    assert run.plan() == driver.PLANNED
    assert any('ocid1.privateip.elsewhere' in message for message in caplog.messages)
    with pytest.raises(driver.Refused) as refused:
        _ = run.up(yes=True, force=True)
    assert 'ocid1.privateip.elsewhere' in str(refused.value)

    assert fake.ups() == []


def test_a_create_beside_an_imported_address_assigned_to_nothing_is_a_launch(repository: Repository) -> None:
    fake = FakePulumi(printed=_planned(_box('create', before=None), *_imported_address('', repointed=True)))

    assert _appliance(repository, fake).up(yes=True, force=True) == driver.NOTHING_PLANNED

    assert len(fake.ups()) == 1


@pytest.mark.parametrize(
    ('expiry', 'named'),
    [
        (NOW + settings.RENEWAL_MARGIN - dt.timedelta(days=1), 'expires on'),
        (NOW - dt.timedelta(days=1), 'expired on'),
        (LATER, None),
    ],
    ids=['inside-the-margin', 'expired', 'outside-the-margin'],
)
def test_an_expiry_inside_the_margin_is_named(
    repository: Repository, caplog: pytest.LogCaptureFixture, expiry: dt.datetime, named: str | None
) -> None:
    fake = FakePulumi(printed=_planned(*_address('ocid1.privateip.box'), expiry=expiry))

    _ = _appliance(repository, fake).plan()

    said = [message for message in caplog.messages if expiry.date().isoformat() in message]
    if named is None:
        assert said == []
    else:
        (message,) = said
        assert named in message
        assert expiry.date().isoformat() in message
        assert ('derived', derived.STATE_BACKEND_SERVER_ROW, 'issue') in named_leaves(message)


@pytest.mark.parametrize(
    ('backend', 'expected'),
    [
        (Backend(), driver.NOTHING_PLANNED),
        (Backend(stacks=[]), driver.RESTORE_OWED),
        (Backend(answers=False), driver.BACKEND_SILENT),
    ],
    ids=['serving', 'empty', 'silent'],
)
def test_plan_answers_from_the_estate_backend(repository: Repository, backend: Backend, expected: int) -> None:
    fake = FakePulumi(printed=_planned(*_address('ocid1.privateip.box')))

    assert _appliance(repository, fake, backend).plan() == expected

    assert backend.asked == 1
    assert fake.ups() == []


def test_an_up_whose_restore_hook_failed_exits_3_over_an_empty_backend(repository: Repository) -> None:
    # The readiness hook found no dump of its own and a backend holding no
    # stack, failed, and `pulumi` exited non-zero: the run's answer is the
    # restore owed, not the failure.
    fake = FakePulumi(printed=_planned(*_replacement(), *_address('ocid1.privateip.box')), code=255)

    assert _appliance(repository, fake, Backend(stacks=[])).up(yes=True, force=True) == driver.RESTORE_OWED


def test_the_callers_permission_never_reaches_a_run(repository: Repository) -> None:
    # A shell that exported the permission grants nothing: not to the preview,
    # not to an `up` with no `--force`, not to a passed-through `up`, which the
    # instance's hooks then refuse.
    fake = FakePulumi(
        printed=_planned(
            *_address('ocid1.privateip.box'),
            {
                'resourcePreEvent': {
                    'metadata': {'op': 'update', 'urn': 'urn:list', 'type': 'oci:Core/securityList:SecurityList'}
                }
            },
        )
    )
    gate = appliance.Gate(served=Backend().served, now=lambda: NOW)
    run = driver.Run.open(
        STATE_BACKEND,
        repository.checkout,
        pulumi=fake,
        base=AMBIENT | {permission.ENV: permission.GRANTED},
        gate=gate,
    )

    _ = run.plan()
    _ = run.up(yes=True)
    _ = run.passthrough(['up', '--yes'])

    assert len(fake.ups()) == 2
    assert [env.get(permission.ENV) for env in fake.envs] == [None] * len(fake.envs)


def test_force_and_replace_are_the_appliances_alone(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    for flags in ({'force': True}, {'replace': True}):
        with pytest.raises(driver.Refused) as refused:
            _ = run.up(yes=True, **flags)
        assert 'github' in str(refused.value)
    assert fake.streamed == []


def test_the_gate_reads_a_silent_backend_apart_from_a_missing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / 'kluster'
    gate = appliance.Gate.for_checkout(checkout)

    # No bundle in the slot is this checkout's to repair, and the run refuses.
    with pytest.raises(appliance.Refused, match='state-backend bundle operator'):
        _ = gate.backend()

    _fill_slots(checkout)

    def unanswered(_target: state.Connection) -> list[str]:
        raise state.StateError('`pulumi stack ls` failed: connection refused')

    monkeypatch.setattr(state, 'stacks', unanswered)
    assert gate.backend() is appliance.Backend.SILENT
    monkeypatch.setattr(state, 'stacks', _served([]))
    assert gate.backend() is appliance.Backend.EMPTY
    monkeypatch.setattr(state, 'stacks', _served(['organization/kluster-py/physical']))
    assert gate.backend() is appliance.Backend.SERVING


def _served(stacks: list[str]) -> Callable[[state.Connection], list[str]]:
    def answer(target: state.Connection) -> list[str]:
        assert target.url == SLOT_URL
        return stacks

    return answer


def test_the_command_line_carries_force_and_replace_to_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Recorded:
        def up(self, *, yes: bool, force: bool, replace: bool) -> int:
            seen.update(yes=yes, force=force, replace=replace)
            return 0

    def opened(_cls: type[driver.Run], *_args: object, **_kwargs: object) -> Recorded:
        return Recorded()

    monkeypatch.setattr(driver.Run, 'open', classmethod(opened))

    assert cli.main([STATE_BACKEND, 'up', '--force']) == 0
    assert seen == {'yes': False, 'force': True, 'replace': False}
    assert cli.main([STATE_BACKEND, 'up', '--replace', '--yes']) == 0
    assert seen == {'yes': True, 'force': False, 'replace': True}


#: A program whose one resource stands in for the appliance's box: a dynamic
#: resource declared as the instance is, `delete_before_replace` with a change
#: of its bill of materials replacing it, so the engine emits the step events a
#: replacement of the box carries.
BOXES = """\
import json
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider


class Box(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='box-' + props['extendedMetadata']['image'], outs=dict(props))

    def diff(self, _id, olds, news):
        replaces = ['extendedMetadata'] if olds.get('extendedMetadata') != news.get('extendedMetadata') else []
        return DiffResult(changes=bool(replaces), replaces=replaces)

    def delete(self, _id, _props):
        pass


class BoxResource(Resource):
    def __init__(self, name, props, opts):
        super().__init__(Box(), name, props, opts)


settings = json.loads(pathlib.Path('settings.json').read_text())
BoxResource(
    'box',
    {'extendedMetadata': settings['bom']},
    pulumi.ResourceOptions(delete_before_replace=True, replace_on_changes=['extendedMetadata']),
)
"""


@dataclass
class AsTheBox:
    """The pinned `pulumi`, whose preview events name the stand-in `box` as the appliance's instance.

    The type is the one thing the stand-in cannot carry: it is a dynamic
    resource. Every other field of every event is the engine's own.
    """

    cli: driver.Pulumi = field(default_factory=Bounded)
    printed: list[str] = field(default_factory=list[str])

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None) -> int:
        return self.cli.stream(args, cwd=cwd, env=env, doing=doing)

    def capture(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        return self.cli.capture(args, cwd=cwd, env=env)

    def events(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], doing: str | None = None
    ) -> tuple[int, str]:
        code, printed = self.cli.events(args, cwd=cwd, env=env, doing=doing)
        lines: list[str] = []
        for line in printed.splitlines():
            event = cast('dict[str, Any]', json.loads(line)) if line.strip() else None
            for kind in ('resourcePreEvent', 'resOutputsEvent'):
                metadata = cast('dict[str, Any]', (event or {}).get(kind, {}).get('metadata') or {})
                if str(metadata.get('urn', '')).endswith('::box'):
                    metadata['type'] = INSTANCE_TYPE
            lines.append(json.dumps(event) if event is not None else line)
        self.printed.append('\n'.join(lines))
        return code, '\n'.join(lines) + '\n'


@pytest.fixture
def boxes(repository: Repository, committed: str, tmp_path: Path) -> Scratch:
    made = _initialized(repository, committed, tmp_path, BOXES)
    _ = (made.checkout / 'settings.json').write_text(json.dumps({'bom': {'butane': 'a', 'image': 'c'}}))
    assert made.run.passthrough(['up', '--yes', '--skip-preview']) == 0
    return made


@needs_pulumi
@engine_bound
def test_a_real_replacement_of_the_box_names_only_the_digest_that_moved_and_waits_for_force(
    boxes: Scratch, caplog: pytest.LogCaptureFixture
) -> None:
    """The engine's own events for a `delete_before_replace` replacement: `delete-replaced` first, with no new state.

    Read through the gate as the appliance's instance, they hold the run for
    `--force`, name the one digest that moved and not the one that did not, and
    under `--force` apply.
    """
    _ = (boxes.checkout / 'settings.json').write_text(json.dumps({'bom': {'butane': 'a', 'image': 'd'}}))
    pulumi = AsTheBox()
    gate = appliance.Gate(served=Backend().served, now=lambda: NOW)
    run = driver.Run.open(PROBE, boxes.checkout, base=boxes.base, pulumi=pulumi, gate=gate)

    assert run.up(yes=True) == driver.PLANNED

    preview = driver.read_preview(pulumi.printed[-1])
    assert [step.op for step in appliance.box_steps(preview)] == ['delete-replaced', 'replace', 'create-replacement']
    (refusal,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert 'image' in refusal
    assert 'butane' not in refusal

    assert run.up(yes=True, force=True) == driver.NOTHING_PLANNED
    assert run.plan() == driver.NOTHING_PLANNED


def test_a_pending_delete_of_the_box_waits_for_force_and_says_so(
    repository: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    gone = {
        'op': 'delete',
        'urn': INSTANCE_URN,
        'type': INSTANCE_TYPE,
        'old': {'inputs': {appliance.BILL_OF_MATERIALS: BEFORE}},
        'new': None,
    }
    fake = FakePulumi(printed=_planned({'resourcePreEvent': {'metadata': gone}}, *_address('ocid1.privateip.box')))

    assert _appliance(repository, fake).up(yes=True) == driver.PLANNED

    assert fake.ups() == []
    (refusal,) = [message for message in caplog.messages if 'nothing is applied' in message]
    assert 'the program no longer declares the box' in refusal


def test_an_up_over_nothing_planned_answers_from_the_estate_backend(repository: Repository) -> None:
    # The one path that would otherwise report 0 over a backend serving no
    # stack: nothing planned, so no `up` runs, and the backend is still read.
    fake = FakePulumi(printed=_planned(*_address('ocid1.privateip.box')))
    backend = Backend(stacks=[])

    assert _appliance(repository, fake, backend).up(yes=True) == driver.RESTORE_OWED

    assert fake.ups() == []
    assert backend.asked == 1


IMPORTED_URN = f'urn:pulumi:{STATE_BACKEND}::kluster-py::kluster:state_backend:StateBackend$oci:Core/image:Image::state-backend-image'


def _imported_then_replaced() -> list[dict[str, Any]]:
    """An adopted resource the plan would replace: imported, then replaced, as a preview shows it."""
    return [
        {'resourcePreEvent': {'metadata': {'op': op, 'urn': IMPORTED_URN, 'type': 'oci:Core/image:Image'}}}
        for op in ('import', 'create-replacement', 'replace')
    ]


#: What the pinned engine says in a preview of such a run (`step_generator.go`
#: at 3.257.0, L1975–L1999).
ENGINE_WARNING = (
    'previously-imported resources that still specify an ID may not be replaced; please remove the `import` '
    'declaration from your program;\nimageSourceDetails: {} => {objectName: fedora-coreos-1.qcow2}'
)


@pytest.mark.parametrize(
    'events',
    [
        _imported_then_replaced(),
        [
            {'resourcePreEvent': {'metadata': {'op': 'replace', 'urn': IMPORTED_URN, 'type': 'oci:Core/image:Image'}}},
            {'diagnosticEvent': {'urn': IMPORTED_URN, 'severity': 'warning', 'message': ENGINE_WARNING}},
        ],
    ],
    ids=['imported-in-this-run', 'imported-earlier'],
)
def test_a_replacement_of_an_adopted_resource_is_refused_before_up_naming_it(
    repository: Repository, events: list[dict[str, Any]]
) -> None:
    # The engine would fail the `up` at that step, after every step before it
    # -- the terminate of the box among them on the cutover's run.
    fake = FakePulumi(printed=_planned(*events, *_address('')))

    with pytest.raises(driver.Refused) as refused:
        _ = _appliance(repository, fake).up(yes=True, force=True)
    assert IMPORTED_URN in str(refused.value)

    assert fake.ups() == []


@pytest.fixture
def unanswered(monkeypatch: pytest.MonkeyPatch) -> list[Mapping[str, str]]:
    """`pulumi stack ls` against a backend whose traffic is dropped: the runner's own bound runs out."""
    asked: list[Mapping[str, str]] = []

    def run_pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        asked.append(env)
        raise sp.TimeoutExpired(cmd=['pulumi', *args], timeout=120)

    monkeypatch.setattr(state.pulumi_cli, 'run_pulumi', run_pulumi)
    return asked


def test_a_backend_whose_traffic_is_dropped_does_not_answer(
    repository: Repository, unanswered: list[Mapping[str, str]]
) -> None:
    fake = FakePulumi(printed=_planned(*_address('')))
    run = driver.Run.open(STATE_BACKEND, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.plan() == driver.BACKEND_SILENT

    # The connection attempt is bounded inside the runner's own bound, so the
    # question ends at the connection's bound rather than when TCP gives up.
    # The variable is libpq's, which the driver behind Pulumi's Postgres
    # backend reads: its name is that contract's, not this repository's.
    (env,) = unanswered
    assert env['PGCONNECT_TIMEOUT'] == str(state.CONNECT_TIMEOUT)
    assert state.CONNECT_TIMEOUT < pulumi_cli.TIMEOUT


def test_a_checkout_with_no_bundle_is_refused_before_anything_runs(repository: Repository) -> None:
    # The run's hooks connect with the bundle and the driver reads the backend
    # through it, so a run without one would fail only after it had written.
    shutil.rmtree(repository.checkout / workstation.DIRECTORY / stack_environment.BUNDLE_SLOT)
    fake = FakePulumi(printed=_planned(*_address('')))
    run = driver.Run.open(STATE_BACKEND, repository.checkout, pulumi=fake, base=AMBIENT)

    for act in (run.plan, lambda: run.up(yes=True)):
        with pytest.raises(appliance.Refused, match='state-backend bundle operator'):
            _ = act()
    assert fake.streamed == []


def test_a_replacement_that_names_nothing_moved_says_so_rather_than_inventing_a_reason(
    repository: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    # The provider replaces the box and the events name no digest and no
    # input: the refusal says that, and sends the operator to the plan.
    steps = [
        {
            'resourcePreEvent': {
                'metadata': {
                    'op': op,
                    'urn': INSTANCE_URN,
                    'type': INSTANCE_TYPE,
                    'old': {'inputs': {appliance.BILL_OF_MATERIALS: BEFORE}},
                    'new': None if op == 'delete-replaced' else {'inputs': {appliance.BILL_OF_MATERIALS: BEFORE}},
                }
            }
        }
        for op in ('delete-replaced', 'replace', 'create-replacement')
    ]
    fake = FakePulumi(printed=_planned(*steps, *_address('ocid1.privateip.box')))

    assert _appliance(repository, fake).up(yes=True) == driver.PLANNED

    (refusal,) = [message for message in caplog.messages if 'nothing is applied' in message]
    assert appliance.NOTHING_NAMED in refusal
    assert 'asked for' not in refusal
