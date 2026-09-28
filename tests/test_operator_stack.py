"""The `operator-stack` driver: what a run of a stack no CI job runs is given, refused and checked.

Three levels, because they answer different questions:

-   **A fake `pulumi`** that records what it is started with and answers from
    fixtures says what the driver asks for and when: the environment, the
    arguments, whether an `up` is made at all, and what the checks make of a
    checkpoint.
-   **A scratch repository** — a bare repository standing for the forge and a
    clone of it standing for the checkout — says the working-copy checks read
    git's own answers the way they are believed to.
-   **The real engine over a scratch `file://` backend**, with a program
    whose one resource is a dynamic one that needs no credential and so
    reaches no network, says the driver's handling of a committed checkpoint
    holds against what the pinned CLI actually writes: that the preview's
    plan is read from its streamed events, that no `up` runs over nothing
    planned, that a write over an unchanged deployment keeps the file's
    bytes, and that no switch of the caller's moves the state out of the
    file the checks read. Skipped where the pinned CLI or `uv` is not
    installed.

A stack whose state is committed is not in the census yet, so the cases that
need one add `probe` to it for their own duration.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import site
import subprocess as sp
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from memory_keyring import MemoryKeyring, installed

from kluster.conventions import identity
from kluster.lib import acquisition, bundle, stack_environment
from kluster.scripts.credentials import escrow
from kluster.scripts.credentials import workstation as credential_slots
from kluster.scripts.operator_stack import checkpoint, cli, driver

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
    streamed: list[list[str]] = field(default_factory=list[list[str]])
    captured: list[list[str]] = field(default_factory=list[list[str]])
    envs: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        args = list(args)
        self.streamed.append(args)
        self.envs.append(dict(env))
        self.effect(args)
        return 0

    def events(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[int, str]:
        args = list(args)
        self.streamed.append(args)
        self.envs.append(dict(env))
        assert args[:3] == ['preview', '--refresh', '--json'], args
        assert env[driver.STREAMING_JSON_ENV] == 'true'
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
    return sp.run(
        ['git', *args], cwd=cwd, env=os.environ | GIT_ENV, capture_output=True, text=True, check=True, timeout=60
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
    slots = checkout / '.credentials'
    (slots / 'state-backend').mkdir(parents=True)
    _ = (slots / 'state-backend' / 'backend-url').write_text(SLOT_URL + '\n')
    _ = (slots / 'pulumi.passphrase').write_text('a-slot-stack-passphrase\n')
    _ = (slots / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text(OPERATOR_PASSPHRASE + '\n')


@pytest.fixture
def committed(monkeypatch: pytest.MonkeyPatch) -> str:
    """`probe`, in the census as a stack whose state is committed, for this case alone."""
    monkeypatch.setattr(identity, 'OPERATOR_STACKS', {**identity.OPERATOR_STACKS, PROBE: identity.StateHome.COMMITTED})
    return PROBE


def _checkpoint(checkout: Path, resources: list[dict[str, Any]]) -> Path:
    """A committed checkpoint of `probe` holding `resources`, as `pulumi` lays one out."""
    path = checkout / 'checkpoints' / '.pulumi' / 'stacks' / 'probe' / f'{PROBE}.json'
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

    slot = checkout / '.credentials' / 'state-backend'
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
    (checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()

    with pytest.raises(
        stack_environment.EnvironmentRefused, match='credentials derived operator-passphrase recover'
    ) as refusal:
        _ = driver.Run.open('github', checkout, pulumi=FakePulumi(), base=AMBIENT)

    assert 'no operator passphrase on this machine' in str(refusal.value)
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
    slot = checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT
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
    _ = (checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text('\n')
    monkeypatch.setenv(stack_environment.OPERATOR_PASSPHRASE_ENV, 'from-variable')

    assert stack_environment.operator_passphrase(checkout, lambda _question: None) == 'from-variable'


def test_an_empty_answer_at_the_prompt_is_refused(tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    (checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()

    with pytest.raises(stack_environment.EnvironmentRefused, match='operator-passphrase recover'):
        _ = stack_environment.operator_passphrase(checkout, lambda _question: '  ')


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
    (checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT).unlink()
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
    slot = checkout / '.credentials' / stack_environment.OPERATOR_PASSPHRASE_SLOT
    slot.chmod(0)
    monkeypatch.setenv(stack_environment.OPERATOR_PASSPHRASE_ENV, 'from-variable')

    try:
        with pytest.raises(stack_environment.EnvironmentRefused, match='cannot be read') as refusal:
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
    'args',
    [
        ['preview', '--stack', 'dev'],
        ['preview', '--stack=dev'],
        ['preview', '-s', 'dev'],
        ['preview', '-sdev'],
        ['up', '-ys', 'dev'],
    ],
)
def test_an_argument_naming_a_stack_is_refused(tmp_path: Path, args: list[str]) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match='names a stack'):
        _ = run.passthrough(args)
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

    with pytest.raises(driver.Refused, match=r'under a \.claude/ directory'):
        _ = driver.Run.open('github', workspace, pulumi=FakePulumi(), base=AMBIENT)


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

    with pytest.raises(driver.Refused, match="through the program's `import_`"):
        _ = run.passthrough(args)
    assert fake.streamed == []


@pytest.mark.parametrize(
    'args',
    [
        ['config', 'cp', '--dest', 'dev'],
        ['config', 'cp', '--dest=dev'],
        ['config', 'cp', '-d', 'dev'],
        # A flag, or a flag and its value, between the command's words.
        ['config', '--color=never', 'cp', '--dest', 'dev'],
        ['config', '--color', 'never', 'cp', '-d', 'dev'],
    ],
)
def test_config_cp_into_another_stack_is_refused(tmp_path: Path, args: list[str]) -> None:
    checkout = tmp_path / 'kluster'
    _fill_slots(checkout)
    fake = FakePulumi()
    run = driver.Run.open('github', checkout, pulumi=fake, base=AMBIENT)

    with pytest.raises(driver.Refused, match='names a stack'):
        _ = run.passthrough(args)
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
    assert asked == ([] if yes else ['apply these changes to the github stack?'])


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


def test_a_working_copy_on_the_forges_main_runs(repository: Repository, committed: str) -> None:
    fake = FakePulumi()
    run = driver.Run.open(committed, repository.checkout, pulumi=fake, base=AMBIENT)

    assert run.plan() == driver.NOTHING_PLANNED


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


@pytest.mark.skipif(shutil.which('jj') is None, reason='jj is not on PATH')
def test_in_a_jj_checkout_the_working_copy_is_jjs(repository: Repository, committed: str) -> None:
    # `@` in a colocated checkout is a commit git's HEAD is the parent of; the
    # forge's main is checked against it, as it is where a run's checkpoint
    # sits.
    jj_env = os.environ | {'JJ_CONFIG': os.devnull, 'JJ_USER': 'probe', 'JJ_EMAIL': 'probe@example.invalid'}
    _ = sp.run(
        ['jj', 'git', 'init', '--colocate'],
        cwd=repository.checkout,
        env=jj_env,
        capture_output=True,
        check=True,
        timeout=60,
    )
    at = sp.run(
        ['jj', 'log', '--no-graph', '-r', '@', '-T', 'commit_id'],
        cwd=repository.checkout,
        env=jj_env,
        capture_output=True,
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
    return sp.run(['jj', *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=60).stdout


@pytest.mark.skipif(shutil.which('jj') is None, reason='jj is not on PATH')
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


#: A `pulumi` that answers ^C the way the real one does, by finishing what it
#: has in flight before it exits: it takes a second over it, and says so in a
#: file once it has. One killed part-way through never writes that file.
PULUMI_ANSWERING_SIGINT = """\
#!/bin/sh
trap 'sleep 1; echo finished > "$MARKS/finished"; exit 3' INT
echo started > "$MARKS/started"
while :; do sleep 0.1; done
"""

#: The driver's `Cli` streaming a run of it, in a process of its own.
STREAMING = """\
import os, sys
from pathlib import Path
from kluster.scripts.operator_stack import driver
sys.exit(driver.Cli().stream(['up'], cwd=Path.cwd(), env=dict(os.environ)))
"""


def test_a_sigint_during_a_run_is_pulumis_to_answer(tmp_path: Path) -> None:
    # The terminal sends ^C to the whole foreground process group; the driver
    # lets `pulumi` finish answering it, and returns what `pulumi` exits with.
    bin_dir, marks = tmp_path / 'bin', tmp_path / 'marks'
    bin_dir.mkdir()
    marks.mkdir()
    stand_in = bin_dir / 'pulumi'
    _ = stand_in.write_text(PULUMI_ANSWERING_SIGINT)
    stand_in.chmod(0o755)
    env = os.environ | {'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'MARKS': str(marks)}
    with sp.Popen([sys.executable, '-c', STREAMING], cwd=tmp_path, env=env, start_new_session=True) as running:
        # Stop-loss only: the run is waited on by its own marker.
        for _ in range(600):
            if (marks / 'started').exists() or running.poll() is not None:
                break
            time.sleep(0.05)
        assert (marks / 'started').exists(), 'the stand-in never started'
        os.killpg(running.pid, signal.SIGINT)
        code = running.wait(timeout=30)

    assert (marks / 'finished').exists(), 'pulumi was killed before it finished answering ^C'
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
            'urn:box outputs.metadata: is ciphertext as an input',
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

#: A program with one real resource and no credential: a dynamic resource
#: whose `gen` input replaces nothing and updates in place, and the stack's
#: own outputs, one of them secret so that every write re-encrypts something.
#: What it declares is read from `settings.json` beside it, so a case changes
#: the program's answer without changing the program.
PROGRAM = """\
import json
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult


class Box(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='box', outs=dict(props))

    def diff(self, _id, olds, news):
        return DiffResult(changes=olds.get('gen') != news.get('gen'))

    def update(self, _id, _olds, news):
        return UpdateResult(outs=dict(news))


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
        return len(list((self.checkout / 'checkpoints' / '.pulumi' / 'history').rglob('*.json')))

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


@pytest.fixture
def scratch(repository: Repository, committed: str, tmp_path: Path) -> Iterator[Scratch]:
    if shutil.which('pulumi') is None or shutil.which('uv') is None:
        pytest.skip('the pinned pulumi CLI or uv is not on PATH')
    venv = tmp_path / 'venv'
    _ = sp.run(['uv', 'venv', '-q', '--python', sys.executable, str(venv)], check=True, timeout=60)
    (site_packages,) = venv.glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    checkout = repository.checkout
    _ = (checkout / '__main__.py').write_text(PROGRAM)
    _ = (checkout / 'Pulumi.yaml').write_text(PROJECT.format(venv=venv))
    _ = (checkout / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = sp.run(['uv', 'lock', '-q', '--offline'], cwd=checkout, check=True, timeout=60)
    base = dict(os.environ) | {
        'PULUMI_HOME': str(tmp_path / 'pulumi-home'),
        'PULUMI_SKIP_UPDATE_CHECK': 'true',
        'UV_OFFLINE': '1',
    }
    made = Scratch(run=driver.Run.open(committed, checkout, base=base), checkout=checkout, base=base)
    made.settings()
    assert made.run.passthrough(['stack', 'init']) == 0
    assert made.run.passthrough(['up', '--yes', '--skip-preview']) == 0
    yield made


@needs_pulumi
def test_the_real_preview_is_read_for_what_it_plans(scratch: Scratch) -> None:
    assert scratch.run.plan() == driver.NOTHING_PLANNED

    # A resource that changes is a step the summary counts.
    scratch.settings(gen='g2')
    assert scratch.run.plan() == driver.PLANNED

    # A plain stack output that changes is no step, and still planned.
    scratch.settings(plain='changed')
    assert scratch.run.plan() == driver.PLANNED


@needs_pulumi
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
def test_the_real_up_over_nothing_planned_writes_nothing(scratch: Scratch) -> None:
    before, history = scratch.path.read_bytes(), scratch.history()

    assert scratch.run.up(yes=True) == driver.NOTHING_PLANNED

    assert scratch.path.read_bytes() == before
    # No update ran at all, rather than one whose bytes were put back.
    assert scratch.history() == history


@needs_pulumi
def test_a_real_write_over_an_unchanged_deployment_keeps_the_files_bytes(scratch: Scratch) -> None:
    before, history = scratch.path.read_bytes(), scratch.history()

    assert scratch.run.passthrough(['up', '--yes', '--skip-preview']) == 0

    # The engine did write, and what it wrote differed only in its timestamp
    # and its ciphertexts.
    assert scratch.history() > history
    assert scratch.path.read_bytes() == before


@needs_pulumi
def test_a_real_up_over_nothing_planned_runs_while_a_failed_check_is_recorded_and_clears_it(scratch: Scratch) -> None:
    record = checkpoint.record(scratch.checkout, PROBE)
    _ = record.write_text('urn:box outputs.metadata: is ciphertext as an input and not as an output\n')
    history = scratch.history()

    assert scratch.run.up(yes=True) == 0

    assert scratch.history() > history
    assert not record.exists()


@needs_pulumi
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
    run = driver.Run.open(PROBE, scratch.checkout, base=scratch.base | switch)
    scratch.settings(gen='g2')

    assert run.passthrough(['up', '--yes', '--skip-preview']) == 0

    assert scratch.files() == {'probe.json', 'probe.json.bak'}
