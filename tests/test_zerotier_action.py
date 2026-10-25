"""The ZeroTier action's wait for its target ends in its own error, inside every cap its callers set.

The action joins the overlay and, given `wait_for`, waits for that host and
port to answer before it returns. The wait runs against a wall-clock deadline
counted from the action's first step, so the install and the wait share one
budget, and the deadline has to pass early enough that the wait's own error --
which names the target and how long it waited -- prints before the caller's
cap ends the step without one. The cases here hold that relation between the
deadline the action declares and every cap a workflow puts on a step that uses
it, both read out of the files and neither restated; and they run the wait
step as it is written, against a port on this host, for the lines it prints.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import process_sessions
import pytest
from workflow_files import GITHUB, github_name, mapping, read_workflow, workflow_jobs, workflows_and_actions

ACTION = GITHUB / 'actions' / 'zerotier' / 'action.yml'
#: How a step names this action in `uses:`.
USES = './.github/actions/zerotier'
#: How far inside a caller's cap the deadline must pass: what the wait step
#: runs once the deadline has passed -- one last connect attempt and the
#: listings, each under its own `timeout` -- has to end in the step's error
#: before the cap ends the step.
HEADROOM_SECONDS = 60
#: GitHub's ceiling for a job on a hosted runner, and so the cap a step under a
#: job with no bound of its own runs to.
HOSTED_JOB_LIMIT_MINUTES = 360
#: The started-clock reference the wait step's `STARTED` is written as.
CLOCK = re.compile(r'^\$\{\{\s*steps\.([\w-]+)\.outputs\.started\s*\}\}$')


def _steps() -> list[dict[str, object]]:
    runs = mapping(read_workflow(ACTION).get('runs'), 'zerotier runs:')
    return [mapping(step, 'zerotier step') for step in cast('list[object]', runs.get('steps'))]


def _wait_step() -> tuple[dict[str, str], str]:
    """The wait step's environment and script."""
    (wait,) = (step for step in _steps() if step.get('name') == 'Wait for the target')
    env = mapping(wait.get('env'), 'the wait step env:')
    return {key: str(value) for key, value in env.items()}, str(wait.get('run'))


def _deadline() -> int:
    env, _ = _wait_step()
    return int(env['JOIN_DEADLINE'])


def _caps() -> dict[str, int]:
    """Every step that uses the action, as `<file>: <job>: step <n>`, with the minutes it may run.

    The smaller of the step's own `timeout-minutes` and its job's, since either
    bound ends the step; the hosted runner's ceiling stands in for one that is
    not set.
    """
    caps: dict[str, int] = {}
    for path in workflows_and_actions():
        if path.parent.name != 'workflows':
            continue
        for name, job in workflow_jobs(read_workflow(path), github_name(path)).items():
            for index, step in enumerate(cast('list[object]', job.get('steps', []))):
                step = mapping(step, f'{github_name(path)}: {name} step {index}')
                if step.get('uses') != USES:
                    continue
                where = f'{github_name(path)}: {name}: step {index}'
                bounds = (
                    step.get('timeout-minutes', HOSTED_JOB_LIMIT_MINUTES),
                    job.get('timeout-minutes', HOSTED_JOB_LIMIT_MINUTES),
                )
                for bound in bounds:
                    assert isinstance(bound, int), f'{where}: timeout-minutes is {bound!r}'
                caps[where] = min(cast('tuple[int, int]', bounds))
    return caps


def test_the_wait_deadline_passes_inside_every_callers_cap() -> None:
    caps = _caps()
    deadline = _deadline()

    # A read that stopped finding callers would pass the loop below on nothing.
    assert caps, f'no workflow step uses {USES}'

    too_tight = {
        where: f'{cap} min, against a {deadline} s deadline and {HEADROOM_SECONDS} s of headroom'
        for where, cap in caps.items()
        if deadline + HEADROOM_SECONDS > cap * 60
    }
    assert too_tight == {}, too_tight


def test_no_composite_action_uses_the_zerotier_action() -> None:
    """The caps read above are a workflow step's; a composite step carries none, so a caller there goes unread."""
    nested = [
        github_name(path)
        for path in workflows_and_actions()
        if path.parent.name != 'workflows' and path != ACTION and USES in path.read_text()
    ]
    assert nested == []


def test_the_deadline_is_counted_from_the_actions_first_step() -> None:
    """The install shares the deadline only if the clock the wait reads starts before it."""
    env, _ = _wait_step()
    clock = CLOCK.match(env['STARTED'])
    assert clock, f'STARTED is {env["STARTED"]!r}, not a step output'
    assert _steps()[0].get('id') == clock.group(1)


@dataclass(frozen=True)
class Waited:
    """What a run of the wait step left behind."""

    status: int
    stdout: str
    sudo: list[str]


@contextmanager
def _listening() -> Generator[str]:
    """A `host:port` on this host that accepts a connection."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        yield f'127.0.0.1:{server.getsockname()[1]}'


@contextmanager
def _refusing() -> Generator[str]:
    """A `host:port` on this host that refuses a connection.

    The socket stays bound and never listens: a connect to it is refused at
    once, and no other process can take the port and listen on it while the
    case runs.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
        bound.bind(('127.0.0.1', 0))
        yield f'127.0.0.1:{bound.getsockname()[1]}'


def _wait(tmp_path: Path, target: str, *, started: int | None) -> Waited:
    """Run the wait step the way the runner does, with `sudo` replaced by a fake that records its arguments.

    `started=None` is the empty `STARTED` an unrun clock step leaves behind.
    """
    if shutil.which('bash') is None:
        pytest.fail('bash is not on PATH, which the step runs on')
    log = tmp_path / 'sudo.log'
    fake = tmp_path / 'bin' / 'sudo'
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$*" >>{log}\n')
    fake.chmod(0o755)

    env, script = _wait_step()
    done = process_sessions.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        env={
            **env,
            'TARGET': target,
            'STARTED': '' if started is None else str(started),
            'PATH': f'{fake.parent}{os.pathsep}{os.environ["PATH"]}',
        },
        text=True,
        timeout=30,
    )
    return Waited(done.returncode, done.stdout, log.read_text().splitlines() if log.exists() else [])


def test_a_target_that_answers_is_reported_with_its_attempts_and_seconds(tmp_path: Path) -> None:
    """The success line is read by physical/gateway-cutover.md §8.3, so its whole form is held."""
    with _listening() as target:
        waited = _wait(tmp_path, target, started=int(time.time()))

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(target)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


def test_a_spent_deadline_ends_the_wait_in_an_error_naming_the_target_and_the_seconds(tmp_path: Path) -> None:
    """The install having used the whole budget, the wait tries once and fails rather than starting over."""
    with _refusing() as target:
        waited = _wait(tmp_path, target, started=int(time.time()) - _deadline())

    assert waited.status == 1
    # The annotation has to carry the target, the attempts and the seconds
    # waited; how it words them is the action's. The deadline is a number of
    # seconds the line may also carry, so the seconds waited are a second one.
    (line,) = (line for line in waited.stdout.splitlines() if line.startswith('::error::'))
    assert re.search(rf'{re.escape(target)}\b', line), line
    assert re.search(r'\b1 attempts\b', line), line
    assert [seconds for seconds in re.findall(r'\b(\d+) s\b', line) if int(seconds) != _deadline()], line
    assert waited.sudo == ['zerotier-cli listnetworks']


def test_a_wait_with_no_start_time_refuses_by_name_instead_of_blaming_the_target(tmp_path: Path) -> None:
    """A skipped clock step, or one writing another output name, hands the wait an empty `STARTED`."""
    with _listening() as target:
        waited = _wait(tmp_path, target, started=None)

    assert waited.status == 1
    assert re.fullmatch(r'::error::.*\bclock\b.*\n', waited.stdout), waited.stdout
    assert target not in waited.stdout
    assert waited.sudo == []
