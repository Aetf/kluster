"""The Pulumi config-secret slot: what it runs, and what it does with the answer.

Two levels, because they answer different questions. A recorded runner says
which `pulumi` invocations a push makes and how their output is used — that is
this repository's logic. A real `pulumi` against a temporary file backend says
those invocations mean what they are believed to mean: that a secret handed
over on standard input arrives whole, that reading it back decrypts, and that
creating a stack twice is not an error. The second is skipped where the pinned
CLI is not installed, which is neither CI nor a workstation with `mise`.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from credentials_command_tree import named_leaves
from fake_pulumi import RecordedPulumi

from kluster.conventions import identity
from kluster.lib import pulumi_cli, stack_environment, workstation
from kluster.scripts.credentials import escrow, pulumi_config, slots

STACK = 'dns'
SECRET = 'a-minted-token'
#: A qualified key, standing for any of them: what this suite exercises is the
#: machinery every push shares, so it names no register row of its own.
QUALIFIED_KEY = 'example:aSecret'
#: The throwaway project the live tests write into. Its name is the namespace
#: `pulumi` gives an unqualified config key, so the tests read it from here
#: rather than assuming this repository's own project name.
PROJECT = 'slot-probe'


#: The bound every case here that starts the pinned `pulumi` runs under, set
#: from measured durations rather than from the suite's per-case bound
#: (testing.md §8), and above `pulumi_cli.TIMEOUT`, the bound each command
#: carries, so a stalled command fails as a `TimeoutExpired` naming it. On
#: four cores under the suite's four workers the slowest took 11 s, and 39 s
#: with twelve busy processes beside them. A stop-loss; nothing asserts on
#: elapsed time.
CLI_CASE_TIMEOUT = 360
#: The mark every case here that starts the pinned `pulumi` carries.
real_cli = pytest.mark.timeout(CLI_CASE_TIMEOUT)
#: The fixtures that hand a case a real CLI's project and backend; a case
#: taking one runs the CLI, and carries `real_cli`.
REAL_CLI_FIXTURES = frozenset({'live_stack', 'physical_project'})
#: The cases that start the CLI with no such fixture, named one by one.
REAL_CLI_CASES = frozenset({'test_a_failing_invocation_names_the_command'})


def test_every_case_that_starts_the_real_cli_carries_the_cli_bound() -> None:
    """The bound is read off the case.

    A case that takes a real-CLI fixture is held to it without an edit here;
    one that starts the CLI by itself is named in `REAL_CLI_CASES`.
    """
    module = sys.modules[__name__]
    cases = {name: case for name, case in inspect.getmembers(module, inspect.isfunction) if name.startswith('test_')}
    taking = [
        (name, case)
        for name, case in cases.items()
        if name in REAL_CLI_CASES or REAL_CLI_FIXTURES & set(inspect.signature(case).parameters)
    ]

    assert not REAL_CLI_CASES - set(cases), f'no such case: {sorted(REAL_CLI_CASES - set(cases))}'
    assert taking, 'no case here takes a real-CLI fixture'
    for name, case in taking:
        bounds = [mark.args for mark in getattr(case, 'pytestmark', []) if mark.name == 'timeout']
        assert bounds == [(CLI_CASE_TIMEOUT,)], f'{name} runs the real CLI under {bounds or "the suite bound"}'


@pytest.fixture
def recorded(tmp_path: Path) -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return pulumi_config.Stack(name=STACK, directory=tmp_path, run=runner), runner


def test_a_missing_stack_is_created_and_not_selected(recorded: tuple[pulumi_config.Stack, RecordedPulumi]) -> None:
    stack, runner = recorded

    stack.ensure()

    # `--no-select` because which stack the operator had selected is theirs;
    # a credentials run must not change it under them.
    assert runner.stacks == [STACK]
    assert ['stack', 'init', STACK, '--no-select'] in runner.invocations


def test_an_existing_stack_is_left_alone(recorded: tuple[pulumi_config.Stack, RecordedPulumi]) -> None:
    stack, runner = recorded
    runner.stacks.append(STACK)

    stack.ensure()

    # Idempotence at the level that matters for a re-run: a second push into a
    # live stack must not try to create it again.
    assert [args for args in runner.invocations if args[:2] == ['stack', 'init']] == []


def test_a_secret_is_written_and_read_back(recorded: tuple[pulumi_config.Stack, RecordedPulumi]) -> None:
    stack, runner = recorded

    stack.set_secret(QUALIFIED_KEY, SECRET)

    # The file gains ciphertext whatever happens, so decrypting it again is
    # the only thing that distinguishes a delivered credential from a lost one.
    assert runner.config[QUALIFIED_KEY] == SECRET
    assert ['config', 'get', QUALIFIED_KEY, '--stack', STACK] in runner.invocations


def test_a_slot_that_does_not_keep_the_value_is_a_failure(
    recorded: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    stack, runner = recorded
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused, match='does not decrypt'):
        stack.set_secret(QUALIFIED_KEY, SECRET)


def test_an_output_dump_that_is_not_json_is_refused_without_being_quoted(tmp_path: Path) -> None:
    """`stack output --show-secrets` is a dump whose values are secrets by design.

    A dump the CLI cut short is still that dump up to where it stopped, and the
    refusal it produces is logged by the sync that asked for it. So the refusal
    says how much arrived and where the parse gave up, and quotes none of it.
    """
    truncated = '{"token": "' + SECRET + '", "other": "val'

    def cut_short(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        assert list(args[:2]) == ['stack', 'output']
        return truncated

    stack = pulumi_config.Stack(name=STACK, directory=tmp_path, run=cut_short)

    with pytest.raises(pulumi_config.SlotRefused) as refused:
        _ = stack.outputs()

    message = str(refused.value)
    assert SECRET not in message, message
    assert 'token' not in message, message
    assert f'{len(truncated)} characters' in message
    assert 'line 1 column' in message


@dataclass(frozen=True)
class Side:
    """One side of the runner's boundary, and the refusal it raises."""

    project_dir: Callable[[], Path]
    run_pulumi: pulumi_cli.Runner
    refusal: type[Exception]


#: The runner `kluster.lib` holds for every caller refuses as `PulumiRefused`,
#: which `state_backend.state` translates into its own refusal; this package's
#: entry points refuse as `SlotRefused`, which every `credentials` caller
#: catches.
SIDES = pytest.mark.parametrize(
    'side',
    [
        Side(pulumi_cli.project_dir, pulumi_cli.run_pulumi, pulumi_cli.PulumiRefused),
        Side(pulumi_config.project_dir, pulumi_config.run_pulumi, pulumi_config.SlotRefused),
    ],
    ids=['runner', 'credentials'],
)


@SIDES
def test_a_checkout_that_cannot_be_found_is_refused_as_that_side_refuses(
    side: Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The checkout's own error is handed over as the refusal each side
    # translates, rather than passing through untranslated.
    def nowhere() -> Path:
        raise workstation.WorkstationError('no mise.toml above this package')

    monkeypatch.setattr(workstation, 'repo_root', nowhere)

    with pytest.raises(side.refusal, match=re.escape('no mise.toml above')):
        _ = side.project_dir()


@SIDES
def test_a_checkout_without_pulumi_yaml_is_refused(side: Side, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)

    with pytest.raises(side.refusal, match=re.escape('no Pulumi.yaml')):
        _ = side.project_dir()


@pytest.fixture
def live_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pulumi_config.Stack:
    """A stack in a throwaway project on a file backend, driven by the real CLI."""
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')
    project = tmp_path / 'project'
    project.mkdir()
    _ = (project / 'Pulumi.yaml').write_text(f'name: {PROJECT}\nruntime: nodejs\ndescription: slot probe\n')
    state = tmp_path / 'state'
    state.mkdir()
    # A home of its own: nothing here may touch the operator's own credentials
    # file or their selected stack. It goes in the ambient environment because
    # it is not part of what a `credentials` run knows -- `run_pulumi` overlays
    # the stack's own variables on whatever is already there.
    monkeypatch.setenv('PULUMI_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    return pulumi_config.Stack(
        name=STACK,
        directory=project,
        # The passphrase and the URL go in as the shape a `credentials` run
        # builds, so the probe exercises the resolution the real callers use:
        # the stack picks its own passphrase out of this by its own name.
        environment=pulumi_config.BackendEnvironment(passphrase='probe-passphrase', url=state.as_uri()),
    )


@real_cli
def test_the_real_cli_takes_a_secret_on_standard_input(live_stack: pulumi_config.Stack) -> None:
    live_stack.ensure()
    live_stack.ensure()

    live_stack.set_secret(QUALIFIED_KEY, SECRET)
    live_stack.set('anIdentifier', 'account-1')

    # What the operator commits: ciphertext for the token, plain text for the
    # identifier, in the stack file this repository carries. An unqualified key
    # lands under the project's own namespace, which is why this repository
    # writes one rather than spelling the project name out.
    committed = (live_stack.directory / f'Pulumi.{STACK}.yaml').read_text()
    assert 'secure:' in committed
    assert SECRET not in committed
    assert f'{PROJECT}:anIdentifier: account-1' in committed
    assert live_stack.get(QUALIFIED_KEY) == SECRET


@real_cli
@SIDES
def test_a_failing_invocation_names_the_command(side: Side, tmp_path: Path) -> None:
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')

    # No Pulumi.yaml here, so the CLI refuses: a push that fails must say so
    # rather than report a slot nobody filled.
    with pytest.raises(side.refusal, match='stack ls'):
        _ = side.run_pulumi(
            ['stack', 'ls', '--json'],
            cwd=tmp_path,
            env={'PULUMI_HOME': str(tmp_path / 'home'), 'PULUMI_BACKEND_URL': (tmp_path / 'state').as_uri()},
            stdin=None,
        )


def test_a_stack_whose_state_is_committed_is_pointed_at_the_checkouts_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pulumi config set` against such a stack needs its backend as much as an
    # `up` does, so a `credentials` push reaches it through the same mapping
    # the `operator-stack` driver uses, whatever URL this machine holds for the
    # estate's backend.
    monkeypatch.setattr(
        identity, 'OPERATOR_STACKS', {**identity.OPERATOR_STACKS, 'probe': identity.StateHome.COMMITTED}
    )
    environment = pulumi_config.BackendEnvironment(url='postgres://operator@192.0.2.10/pulumi_state')

    committed = pulumi_config.Stack(name='probe', directory=tmp_path, environment=environment).env
    estate = pulumi_config.Stack(name=STACK, directory=tmp_path, environment=environment).env

    assert (
        committed[pulumi_config.BACKEND_URL_ENV]
        == f'{(tmp_path / stack_environment.CHECKPOINTS).as_uri()}?metadata=skip'
    )
    assert committed[stack_environment.DISABLE_BACKUPS_ENV] == 'true'
    assert estate[pulumi_config.BACKEND_URL_ENV] == 'postgres://operator@192.0.2.10/pulumi_state'
    assert stack_environment.DISABLE_BACKUPS_ENV not in estate


def test_only_the_operator_stacks_are_given_the_operator_passphrase() -> None:
    """The operator passphrase goes to the stacks in `APART` and to no other, and is not looked for otherwise.

    A stack outside that census is handed the stack passphrase without the
    finder being called at all, so a command that never points at an operator
    stack never reaches the chain; an operator stack is handed what the finder
    answers, never the stack passphrase.
    """
    asked: list[str] = []

    def find() -> str:
        asked.append('asked')
        return 'the-operator-passphrase'

    environment = pulumi_config.BackendEnvironment(passphrase='the-stack-passphrase', operator=find)
    assert pulumi_config.APART, 'nothing to exercise: no stack is encrypted apart from the others'
    operator_stack = next(iter(pulumi_config.APART))

    estate = environment.variables(STACK)

    assert STACK not in pulumi_config.APART
    assert estate[pulumi_config.PASSPHRASE_ENV] == 'the-stack-passphrase'
    assert asked == []
    assert environment.variables(operator_stack)[pulumi_config.PASSPHRASE_ENV] == 'the-operator-passphrase'
    assert asked == ['asked']


def test_only_physical_is_given_its_own_passphrase() -> None:
    """`physical` is handed what its finder answers and never the stack passphrase; no other stack asks.

    The stack passphrase is in every Environment a pull request can reach, so
    handing it to `physical` would be the one way `physical`'s configuration
    reopens to a pull request's runs.
    """
    asked: list[str] = []

    def find() -> str:
        asked.append('asked')
        return 'the-physical-passphrase'

    environment = pulumi_config.BackendEnvironment(
        passphrase='the-stack-passphrase', operator=lambda: 'the-operator-passphrase', physical=find
    )

    assert environment.variables(STACK)[pulumi_config.PASSPHRASE_ENV] == 'the-stack-passphrase'
    for stack in pulumi_config.APART:
        assert environment.variables(stack)[pulumi_config.PASSPHRASE_ENV] == 'the-operator-passphrase'
    assert asked == []
    assert environment.variables(pulumi_config.PHYSICAL)[pulumi_config.PASSPHRASE_ENV] == 'the-physical-passphrase'
    assert asked == ['asked']


def test_physical_is_refused_where_nothing_gives_its_passphrase() -> None:
    environment = pulumi_config.BackendEnvironment(passphrase='the-stack-passphrase')

    with pytest.raises(pulumi_config.PassphraseMissing) as refusal:
        _ = environment.variables(pulumi_config.PHYSICAL)
    assert ('derived', pulumi_config.PHYSICAL_ROW, 'generate') in named_leaves(str(refusal.value))


def test_physical_is_refused_with_the_reason_its_finder_gives() -> None:
    def find() -> str:
        raise stack_environment.EnvironmentRefused('nothing escrowed for the label')

    environment = pulumi_config.BackendEnvironment(passphrase='the-stack-passphrase', physical=find)

    with pytest.raises(pulumi_config.PassphraseMissing, match='nothing escrowed for the label'):
        _ = environment.variables(pulumi_config.PHYSICAL)


def test_physical_names_the_row_the_register_carries() -> None:
    # The row a refusal names is the slot map's and the escrow's own spelling.
    assert pulumi_config.PHYSICAL_ROW in slots.ROWS
    assert escrow.row_name(escrow.PHYSICAL_PASSPHRASE) == pulumi_config.PHYSICAL_ROW


#: The passphrases the re-encryption cases move between.
FORMER = 'the-stack-passphrase'
OWN = 'the-own-passphrase'
#: The secret the probe program's state holds once it has been applied.
STATE_SECRET = 'a-state-secret'


@pytest.fixture
def physical_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A throwaway project and file backend for a stack named `physical`, driven by the real CLI."""
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')
    project = tmp_path / 'project'
    project.mkdir()
    # A YAML program with no resource and one secret output: an `up` puts a
    # secret into the state with no provider to install, so the state has
    # something of its own to move.
    _ = (project / 'Pulumi.yaml').write_text(
        f'name: {PROJECT}\nruntime: yaml\ndescription: slot probe\noutputs:\n  held:\n    fn::secret: {STATE_SECRET}\n'
    )
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('PULUMI_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    return project, state.as_uri()


def _physical_under(project: Path, url: str, own: str, *, former: str | None = None) -> pulumi_config.Stack:
    """`physical` as a `credentials` run opens it: its own passphrase is `own`."""
    return pulumi_config.Stack(
        name=pulumi_config.PHYSICAL,
        directory=project,
        environment=pulumi_config.BackendEnvironment(passphrase=former, url=url, physical=lambda: own),
    )


def _salt(stack: pulumi_config.Stack) -> str:
    """The stack file's salt, read as the plain line it is."""
    found = re.search(r'^encryptionsalt:\s*(\S+)', _stack_file(stack).read_text(), re.M)
    assert found is not None
    return found.group(1)


def _stack_file(stack: pulumi_config.Stack) -> Path:
    return stack.directory / f'Pulumi.{stack.name}.yaml'


def _under(stack: pulumi_config.Stack, passphrase: str, *args: str) -> str:
    """One `pulumi` command against `stack`, under `passphrase` whatever the stack's own is."""
    return stack.run(
        [*args, '--stack', stack.name],
        cwd=stack.directory,
        env={**stack.env, 'PULUMI_CONFIG_PASSPHRASE': passphrase},
        stdin=None,
    )


def _state_secret(stack: pulumi_config.Stack, passphrase: str) -> str:
    """The secret output the applied program left in the state, decrypted under `passphrase`."""
    exported = json.loads(_under(stack, passphrase, 'stack', 'export', '--show-secrets'))
    (output,) = (
        resource['outputs']['held']
        for resource in exported['deployment']['resources']
        if resource['type'] == 'pulumi:pulumi:Stack'
    )
    # `--show-secrets` writes a secret as an object whose `plaintext` holds
    # its JSON encoding; the text is enough for a containment check.
    return json.dumps(output)


def _applied_under_former(project: Path, url: str) -> pulumi_config.Stack:
    """`physical` under the former passphrase, with a secret in its configuration and one in its state."""
    stack = _physical_under(project, url, FORMER)
    stack.ensure()
    stack.set_secret(QUALIFIED_KEY, SECRET)
    _ = _under(stack, FORMER, 'up', '--yes', '--skip-preview')
    assert STATE_SECRET in _state_secret(stack, FORMER)
    return stack


@real_cli
def test_the_real_cli_moves_a_stack_onto_its_own_passphrase(physical_project: tuple[Path, str]) -> None:
    """Configuration and state both move: the new passphrase opens them, the old one no longer does."""
    project, url = physical_project
    made_under_former = _applied_under_former(project, url)
    salt_before = _salt(made_under_former)
    moving = _physical_under(project, url, OWN, former=FORMER)

    assert moving.re_encrypt(former=[FORMER]) is True

    assert moving.get(QUALIFIED_KEY) == SECRET
    assert STATE_SECRET in _state_secret(moving, OWN)
    with pytest.raises(pulumi_config.SlotRefused, match='incorrect passphrase'):
        _ = made_under_former.get(QUALIFIED_KEY)
    with pytest.raises(pulumi_config.SlotRefused, match='incorrect passphrase'):
        _ = _state_secret(moving, FORMER)
    assert _salt(moving) != salt_before
    assert SECRET not in _stack_file(moving).read_text()


@real_cli
def test_a_configuration_with_no_secret_is_moved_rather_than_called_moved(physical_project: tuple[Path, str]) -> None:
    """Plain keys decrypt under any passphrase, so the former ones are asked first and the stack is moved.

    Asked the other way round, such a stack would be reported as already
    under its own passphrase and left under the former one, and the first
    secret written under its own would end in `incorrect passphrase`.
    """
    project, url = physical_project
    plain = _physical_under(project, url, FORMER)
    plain.ensure()
    plain.set('anIdentifier', 'account-1')
    salt_before = _salt(plain)
    moving = _physical_under(project, url, OWN, former=FORMER)

    assert moving.re_encrypt(former=[FORMER]) is True

    assert _salt(moving) != salt_before
    moving.set_secret(QUALIFIED_KEY, SECRET)
    assert moving.get(QUALIFIED_KEY) == SECRET


@real_cli
def test_a_move_that_did_not_take_is_refused(physical_project: tuple[Path, str]) -> None:
    """The check after the move: a `change-secrets-provider` that exits 0 and moves nothing is not a move."""
    project, url = physical_project
    _ = _applied_under_former(project, url)

    def swallowing(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        if list(args[:2]) == ['stack', 'change-secrets-provider']:
            return ''
        return pulumi_config.run_pulumi(args, cwd=cwd, env=env, stdin=stdin)

    moving = pulumi_config.Stack(
        name=pulumi_config.PHYSICAL,
        directory=project,
        environment=pulumi_config.BackendEnvironment(passphrase=FORMER, url=url, physical=lambda: OWN),
        run=swallowing,
    )

    with pytest.raises(pulumi_config.SlotRefused, match='does not read back under its own passphrase'):
        _ = moving.re_encrypt(former=[FORMER])


@real_cli
def test_a_stack_already_under_its_own_passphrase_is_left_alone(physical_project: tuple[Path, str]) -> None:
    project, url = physical_project
    already = _physical_under(project, url, OWN, former=FORMER)
    already.ensure()
    already.set_secret(QUALIFIED_KEY, SECRET)
    _ = _under(already, OWN, 'up', '--yes', '--skip-preview')
    committed = _stack_file(already).read_text()

    assert already.re_encrypt(former=[FORMER]) is False

    assert _stack_file(already).read_text() == committed
    assert already.get(QUALIFIED_KEY) == SECRET


@real_cli
def test_a_stack_is_moved_from_whichever_former_passphrase_opens_it(physical_project: tuple[Path, str]) -> None:
    """A rotation's case: the stack is under an earlier generation, not the first candidate tried."""
    project, url = physical_project
    earlier = _physical_under(project, url, 'an-earlier-generation')
    earlier.ensure()
    earlier.set_secret(QUALIFIED_KEY, SECRET)
    moving = _physical_under(project, url, OWN, former=FORMER)

    assert moving.re_encrypt(former=[FORMER, 'an-earlier-generation']) is True
    assert moving.get(QUALIFIED_KEY) == SECRET


@real_cli
def test_a_stack_under_a_passphrase_nobody_named_is_refused(physical_project: tuple[Path, str]) -> None:
    project, url = physical_project
    stranger = _physical_under(project, url, 'a-passphrase-nobody-holds')
    stranger.ensure()
    stranger.set_secret(QUALIFIED_KEY, SECRET)
    committed = _stack_file(stranger).read_text()

    with pytest.raises(pulumi_config.SlotRefused, match='opens under none'):
        _ = _physical_under(project, url, OWN, former=FORMER).re_encrypt(former=[FORMER])

    assert _stack_file(stranger).read_text() == committed


@real_cli
def test_a_stack_the_backend_does_not_hold_is_refused(physical_project: tuple[Path, str]) -> None:
    project, url = physical_project

    with pytest.raises(pulumi_config.SlotRefused, match='holds no physical stack'):
        _ = _physical_under(project, url, OWN, former=FORMER).re_encrypt(former=[FORMER])


#: The stack passphrase's generations the rotation cases move between, and
#: `physical`'s own, which no rotation of the stack passphrase may touch.
EARLIER_GENERATION = 'an-earlier-stack-passphrase'
NEWEST_GENERATION = 'the-newest-stack-passphrase'
PHYSICAL_OWN = 'physicals-own-passphrase'
#: The stack whose configuration holds no secret, as `main`'s `k8s-base`
#: holds only a plain setting, and the setting it holds.
PLAIN_ONLY = identity.STACK_NAMES.k8s_base
PLAIN_KEY = 'example:aSetting'


def test_the_stacks_under_the_stack_passphrase_are_the_ones_a_run_hands_it_to(tmp_path: Path) -> None:
    """The census by definition: every stack whose run is given the stack passphrase, and no other.

    Asked of `BackendEnvironment.variables`, the one place a run's passphrase
    is chosen, so an operator stack or `physical` cannot be moved onto the
    stack passphrase, and a stack added to the census is moved without a
    second list to edit.
    """
    environment = pulumi_config.BackendEnvironment(
        passphrase='the-stack-passphrase', url='file:///nowhere', operator=lambda: 'operator', physical=lambda: 'own'
    )
    handed = [
        name
        for name in identity.STACK_NAMES.names()
        if environment.variables(name, checkout=tmp_path).get(pulumi_config.PASSPHRASE_ENV) == 'the-stack-passphrase'
    ]

    assert handed
    assert list(pulumi_config.ON_STACK_PASSPHRASE) == handed


def _on_stack_passphrase(url: str, passphrase: str) -> pulumi_config.BackendEnvironment:
    """What a `credentials` run hands the stacks: `passphrase` as the stack passphrase, `physical` its own."""
    return pulumi_config.BackendEnvironment(passphrase=passphrase, url=url, physical=lambda: PHYSICAL_OWN)


def _rotation_estate(project: Path, url: str) -> dict[str, pulumi_config.Stack]:
    """Every stack under the stack passphrase made under its earlier generation, and `physical` under its own.

    Each holds a secret in its configuration but `PLAIN_ONLY`, which holds a
    plain setting alone, as on `main`: a configuration with nothing to
    decrypt opens under any passphrase, so it is the one whose generation a
    decryption cannot tell. That the state moves with it is
    `Stack.re_encrypt`'s own proof, held with a secret in the state by the
    cases above; these cases are about which stacks are moved.
    """
    estate: dict[str, pulumi_config.Stack] = {}
    for name in (*pulumi_config.ON_STACK_PASSPHRASE, pulumi_config.PHYSICAL):
        stack = pulumi_config.Stack(
            name=name, directory=project, environment=_on_stack_passphrase(url, EARLIER_GENERATION)
        )
        stack.ensure()
        if name == PLAIN_ONLY:
            stack.set(PLAIN_KEY, 'a-public-value')
        else:
            stack.set_secret(QUALIFIED_KEY, SECRET)
        estate[name] = stack
    return estate


def _moved(project: Path, url: str, name: str) -> pulumi_config.Stack:
    return pulumi_config.Stack(name=name, directory=project, environment=_on_stack_passphrase(url, NEWEST_GENERATION))


@real_cli
def test_every_stack_under_the_stack_passphrase_is_moved_and_no_other(physical_project: tuple[Path, str]) -> None:
    """Each moves onto the newest generation and off the earlier one; `physical`, under its own, is not touched."""
    project, url = physical_project
    estate = _rotation_estate(project, url)
    physical_file = _stack_file(estate[pulumi_config.PHYSICAL]).read_text()

    moved = pulumi_config.re_encrypt_on_stack_passphrase(
        _on_stack_passphrase(url, NEWEST_GENERATION), former=[EARLIER_GENERATION], directory=project
    )

    assert moved == list(pulumi_config.ON_STACK_PASSPHRASE)
    for name in pulumi_config.ON_STACK_PASSPHRASE:
        if name == PLAIN_ONLY:
            # Nothing in it to decrypt, so the salt is what moved: a secret
            # written under the earlier generation is refused, and one under
            # the newest is taken.
            with pytest.raises(pulumi_config.SlotRefused, match='incorrect passphrase'):
                estate[name].set_secret(QUALIFIED_KEY, SECRET)
            _moved(project, url, name).set_secret(QUALIFIED_KEY, SECRET)
            continue
        assert _moved(project, url, name).get(QUALIFIED_KEY) == SECRET, name
        with pytest.raises(pulumi_config.SlotRefused, match='incorrect passphrase'):
            _ = estate[name].get(QUALIFIED_KEY)
    assert _stack_file(estate[pulumi_config.PHYSICAL]).read_text() == physical_file


@real_cli
def test_a_stack_already_on_the_newest_generation_is_left_alone(physical_project: tuple[Path, str]) -> None:
    """A re-run after a stop part-way moves what is left and nothing twice, and a run after that moves nothing.

    `PLAIN_ONLY` holds no secret, so it is the stack a run that read the
    generation off a decryption would move again every time.
    """
    project, url = physical_project
    _ = _rotation_estate(project, url)
    first, *rest = pulumi_config.ON_STACK_PASSPHRASE
    assert _moved(project, url, first).re_encrypt(former=[EARLIER_GENERATION]) is True
    committed = _stack_file(_moved(project, url, first)).read_text()

    moved = pulumi_config.re_encrypt_on_stack_passphrase(
        _on_stack_passphrase(url, NEWEST_GENERATION), former=[EARLIER_GENERATION], directory=project
    )

    assert moved == rest
    assert _stack_file(_moved(project, url, first)).read_text() == committed
    files = {name: _stack_file(_moved(project, url, name)).read_text() for name in pulumi_config.ON_STACK_PASSPHRASE}

    again = pulumi_config.re_encrypt_on_stack_passphrase(
        _on_stack_passphrase(url, NEWEST_GENERATION), former=[EARLIER_GENERATION], directory=project
    )

    assert again == []
    assert {name: _stack_file(_moved(project, url, name)).read_text() for name in files} == files


@real_cli
def test_a_rotation_whose_move_did_not_take_is_refused(physical_project: tuple[Path, str]) -> None:
    """The proof after each move holds here too: a `change-secrets-provider` that exits 0 and moves nothing."""
    project, url = physical_project
    _ = _rotation_estate(project, url)

    def swallowing(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        if list(args[:2]) == ['stack', 'change-secrets-provider']:
            return ''
        return pulumi_config.run_pulumi(args, cwd=cwd, env=env, stdin=stdin)

    with pytest.raises(pulumi_config.SlotRefused, match='does not read back under its own passphrase'):
        _ = pulumi_config.re_encrypt_on_stack_passphrase(
            _on_stack_passphrase(url, NEWEST_GENERATION),
            former=[EARLIER_GENERATION],
            directory=project,
            run=swallowing,
        )


@real_cli
def test_a_refusal_stops_the_rotation_at_that_stack(physical_project: tuple[Path, str]) -> None:
    """The stacks before it stay moved, and the ones after it are not reached: neither file nor state is touched.

    A stack moved ahead of a failure is one more state a run from `main`
    meets under the wrong generation until the merge, so a run that carried
    on past the refusal and reported it at the end would leave more to undo.
    """
    project, url = physical_project
    estate = _rotation_estate(project, url)
    first, failing, *after = pulumi_config.ON_STACK_PASSPHRASE
    assert after

    def state(name: str) -> str:
        return pulumi_config.run_pulumi(
            ['stack', 'export', '--stack', name], cwd=project, env=estate[name].env, stdin=None
        )

    untouched = {name: (_stack_file(estate[name]).read_text(), state(name)) for name in after}

    def failing_import(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        if list(args[:2]) == ['stack', 'change-secrets-provider'] and failing in args:
            raise pulumi_config.SlotRefused('the state import failed')
        return pulumi_config.run_pulumi(args, cwd=cwd, env=env, stdin=stdin)

    with pytest.raises(pulumi_config.SlotRefused, match='the state import failed'):
        _ = pulumi_config.re_encrypt_on_stack_passphrase(
            _on_stack_passphrase(url, NEWEST_GENERATION),
            former=[EARLIER_GENERATION],
            directory=project,
            run=failing_import,
        )

    assert _moved(project, url, first).get(QUALIFIED_KEY) == SECRET
    assert {name: (_stack_file(estate[name]).read_text(), state(name)) for name in after} == untouched


@real_cli
def test_a_census_stack_the_backend_does_not_hold_stops_the_rotation_naming_what_makes_it(
    physical_project: tuple[Path, str],
) -> None:
    project, url = physical_project
    first = pulumi_config.ON_STACK_PASSPHRASE[0]

    with pytest.raises(
        pulumi_config.SlotRefused,
        match=f'holds no {first} stack.*{re.escape(f"`mise x -- pulumi stack init {first} --no-select`")}',
    ):
        _ = pulumi_config.re_encrypt_on_stack_passphrase(
            _on_stack_passphrase(url, NEWEST_GENERATION), former=[EARLIER_GENERATION], directory=project
        )


@real_cli
def test_a_state_written_back_under_the_earlier_passphrase_is_refused_and_the_named_recovery_finishes_it(
    physical_project: tuple[Path, str],
) -> None:
    """A run from `main`'s checkout after the move, before the merge, on a state that held no secret.

    Its `up` succeeds and writes the state back under the earlier
    passphrase, while the checkout holds the moved file. The refusal names
    the file `main` held before the move, not the committed one, and
    restoring it lets the next run move the stack.
    """
    project, url = physical_project
    earlier = _physical_under(project, url, FORMER)
    earlier.ensure()
    earlier.set_secret(QUALIFIED_KEY, SECRET)
    before = _stack_file(earlier).read_text()
    moving = _physical_under(project, url, OWN, former=FORMER)
    assert moving.re_encrypt(former=[FORMER]) is True
    moved = _stack_file(moving).read_text()

    _ = _stack_file(earlier).write_text(before)
    _ = _under(earlier, FORMER, 'up', '--yes', '--skip-preview')
    _ = _stack_file(moving).write_text(moved)
    assert STATE_SECRET in _state_secret(moving, FORMER)

    name = pulumi_config.PHYSICAL
    with pytest.raises(
        pulumi_config.SlotRefused,
        match=re.escape(f'`git -C {project} checkout <that commit> -- Pulumi.{name}.yaml`) and run this again'),
    ):
        _ = moving.re_encrypt(former=[FORMER])

    _ = _stack_file(moving).write_text(before)
    assert moving.re_encrypt(former=[FORMER]) is True
    assert moving.get(QUALIFIED_KEY) == SECRET
    assert STATE_SECRET in _state_secret(moving, OWN)


def _read_only(tree: Path, *, writable: bool) -> None:
    for path in [tree, *tree.rglob('*')]:
        mode = path.stat().st_mode
        path.chmod(mode | 0o200 if writable else mode & ~0o222)


@pytest.mark.skipif(
    os.geteuid() == 0, reason='root writes a tree whatever its mode, so the refusal under test cannot happen'
)
@real_cli
def test_an_interrupted_move_names_the_recovery_and_the_recovery_finishes_it(
    physical_project: tuple[Path, str], tmp_path: Path
) -> None:
    """A run stopped between the stack file and the state: each refusal names the way forward, and it works.

    The import of the state is made to fail by a backend that cannot be
    written, after the stack file is already rewritten.
    """
    project, url = physical_project
    made_under_former = _applied_under_former(project, url)
    committed = _stack_file(made_under_former).read_text()
    moving = _physical_under(project, url, OWN, former=FORMER)
    backend = tmp_path / 'state' / '.pulumi'

    _read_only(backend, writable=False)
    try:
        with pytest.raises(
            pulumi_config.SlotRefused, match=re.escape(f'may already be rewritten: {_recovery(project)}')
        ):
            _ = moving.re_encrypt(former=[FORMER])
    finally:
        _read_only(backend, writable=True)
    assert _stack_file(moving).read_text() != committed

    with pytest.raises(pulumi_config.SlotRefused, match=f'its state is not.*{re.escape(_recovery(project))}'):
        _ = moving.re_encrypt(former=[FORMER])

    _ = _stack_file(moving).write_text(committed)
    assert moving.re_encrypt(former=[FORMER]) is True
    assert moving.get(QUALIFIED_KEY) == SECRET
    assert STATE_SECRET in _state_secret(moving, OWN)


@real_cli
def test_a_finished_move_whose_stack_file_was_lost_is_recognized_as_moved(physical_project: tuple[Path, str]) -> None:
    """The move ran, and the stack file it wrote was lost before it was committed: a second run finishes it.

    The second run opens the restored file under the former passphrase,
    rewrites it, and stops at the state, which the first run already moved;
    the stack it leaves opens whole under its own passphrase, and a third
    run has nothing to do.
    """
    project, url = physical_project
    made_under_former = _applied_under_former(project, url)
    committed = _stack_file(made_under_former).read_text()
    moving = _physical_under(project, url, OWN, former=FORMER)
    assert moving.re_encrypt(former=[FORMER]) is True

    _ = _stack_file(moving).write_text(committed)

    assert moving.re_encrypt(former=[FORMER]) is True
    assert moving.get(QUALIFIED_KEY) == SECRET
    assert STATE_SECRET in _state_secret(moving, OWN)
    assert moving.re_encrypt(former=[FORMER]) is False


def _recovery(project: Path) -> str:
    """The recovery a refusal names, with the checkout it ran in, so it works from any directory."""
    name = pulumi_config.PHYSICAL
    return f'restore the committed Pulumi.{name}.yaml (`git -C {project} checkout -- Pulumi.{name}.yaml`)'


@pytest.mark.skipif(
    os.geteuid() == 0, reason='root writes a tree whatever its mode, so the refusal under test cannot happen'
)
@real_cli
def test_a_move_that_wrote_nothing_is_not_counted_because_nothing_holds_a_secret(
    physical_project: tuple[Path, str],
) -> None:
    """Plain keys and an empty state decrypt under anything, so only a new salt says the run moved the stack.

    The move is stopped before it writes by a stack file, and a directory,
    that cannot be written.
    """
    project, url = physical_project
    plain = _physical_under(project, url, FORMER)
    plain.ensure()
    plain.set('anIdentifier', 'account-1')
    committed = _stack_file(plain).read_text()
    moving = _physical_under(project, url, OWN, former=FORMER)

    _read_only(project, writable=False)
    try:
        with pytest.raises(pulumi_config.SlotRefused, match='was not rewritten, so nothing moved'):
            _ = moving.re_encrypt(former=[FORMER])
    finally:
        _read_only(project, writable=True)

    assert _stack_file(moving).read_text() == committed


@real_cli
def test_a_stack_file_left_between_its_two_saves_is_refused_naming_the_recovery(
    physical_project: tuple[Path, str],
) -> None:
    """A new salt beside ciphertexts under the old one: `pulumi`'s own refusal, with the way forward added.

    That is the file a run killed between `change-secrets-provider`'s two
    saves of it leaves; it is made here from the files before and after a
    move.
    """
    project, url = physical_project
    made_under_former = _physical_under(project, url, FORMER)
    made_under_former.ensure()
    made_under_former.set_secret(QUALIFIED_KEY, SECRET)
    before = _stack_file(made_under_former).read_text()
    moving = _physical_under(project, url, OWN, former=FORMER)
    assert moving.re_encrypt(former=[FORMER]) is True
    new_salt = _salt(moving)
    _ = _stack_file(moving).write_text(re.sub(r'(?m)^encryptionsalt:.*$', f'encryptionsalt: {new_salt}', before))

    with pytest.raises(pulumi_config.SlotRefused, match=re.escape(_recovery(project))):
        _ = moving.re_encrypt(former=[FORMER])
