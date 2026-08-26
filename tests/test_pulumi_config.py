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

import shutil
from pathlib import Path

import pytest
from fake_pulumi import RecordedPulumi

from kluster.scripts.credentials import pulumi_config

STACK = 'dns'
SECRET = 'a-minted-token'


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

    stack.set_secret('cloudflare:apiToken', SECRET)

    # The file gains ciphertext whatever happens, so decrypting it again is
    # the only thing that distinguishes a delivered credential from a lost one.
    assert runner.config['cloudflare:apiToken'] == SECRET
    assert ['config', 'get', 'cloudflare:apiToken', '--stack', STACK] in runner.invocations


def test_a_slot_that_does_not_keep_the_value_is_a_failure(
    recorded: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    stack, runner = recorded
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused, match='does not decrypt'):
        stack.set_secret('cloudflare:apiToken', SECRET)


def test_the_project_directory_is_the_checkout_holding_pulumi_yaml() -> None:
    # The command writes a file in this repository, so it works from any
    # working directory rather than from the one the operator stands in.
    assert (pulumi_config.project_dir() / 'Pulumi.yaml').is_file()


@pytest.fixture
def live_stack(tmp_path: Path) -> pulumi_config.Stack:
    """A stack in a throwaway project on a file backend, driven by the real CLI."""
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')
    project = tmp_path / 'project'
    project.mkdir()
    _ = (project / 'Pulumi.yaml').write_text('name: slot-probe\nruntime: nodejs\ndescription: slot probe\n')
    state = tmp_path / 'state'
    state.mkdir()
    return pulumi_config.Stack(
        name=STACK,
        directory=project,
        # A home of its own: nothing here may touch the operator's own
        # credentials file or their selected stack.
        env={
            'PULUMI_HOME': str(tmp_path / 'home'),
            'PULUMI_BACKEND_URL': state.as_uri(),
            'PULUMI_CONFIG_PASSPHRASE': 'probe-passphrase',
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
        },
    )


def test_the_real_cli_takes_a_secret_on_standard_input(live_stack: pulumi_config.Stack) -> None:
    live_stack.ensure()
    live_stack.ensure()

    live_stack.set_secret('cloudflare:apiToken', SECRET)
    live_stack.set('kluster:cloudflareAccountId', 'account-1')

    # What the operator commits: ciphertext for the token, plain text for the
    # identifier, in the stack file this repository carries.
    committed = (live_stack.directory / f'Pulumi.{STACK}.yaml').read_text()
    assert 'secure:' in committed
    assert SECRET not in committed
    assert 'kluster:cloudflareAccountId: account-1' in committed
    assert live_stack.get('cloudflare:apiToken') == SECRET


def test_a_failing_invocation_names_the_command(tmp_path: Path) -> None:
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')

    # No Pulumi.yaml here, so the CLI refuses: a push that fails must say so
    # rather than report a slot nobody filled.
    with pytest.raises(pulumi_config.SlotRefused, match='stack ls'):
        _ = pulumi_config.run_pulumi(
            ['stack', 'ls', '--json'],
            cwd=tmp_path,
            env={'PULUMI_HOME': str(tmp_path / 'home'), 'PULUMI_BACKEND_URL': (tmp_path / 'state').as_uri()},
            stdin=None,
        )
