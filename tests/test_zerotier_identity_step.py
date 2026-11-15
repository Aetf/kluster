"""The ZeroTier action joins only once the service it just started answers its control port.

`systemctl start` returns before the ZeroTier service listens on its local
control port, and a `zerotier-cli` command that reaches no listener fails at
once. So the identity step waits for `zerotier-cli info` to answer before it
joins, under the deadline the action's first step sets, and a service that
never answers ends the step in an error that says so. The cases run the step
as it is written, against a stand-in for `sudo` that plays the service: it
answers every `zerotier-cli` command from a given contact on and refuses each
one before it, so a join sent before the service answered fails here the way
it fails on a runner.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import process_sessions
import pytest
from workflow_files import GITHUB, mapping, read_workflow

ACTION = GITHUB / 'actions' / 'zerotier' / 'action.yml'
STEP = 'Install the member identity'
NETWORK_ID = '0123456789abcdef'
#: What `zerotier-cli join` prints when the service accepts the join.
JOIN_OK = '200 join OK'


def _step(name: str) -> tuple[dict[str, str], str]:
    """The environment and script of the action's step named `name`."""
    runs = mapping(read_workflow(ACTION).get('runs'), 'zerotier runs:')
    steps = [mapping(step, 'zerotier step') for step in cast('list[object]', runs.get('steps'))]
    (step,) = (step for step in steps if step.get('name') == name)
    env = mapping(step.get('env', {}), f'the {name!r} step env:')
    return {key: str(value) for key, value in env.items()}, str(step.get('run'))


def _deadline() -> int:
    """The seconds the clock step gives the join, from its own start."""
    env, _ = _step("Start the join's clock")
    return int(env['JOIN_DEADLINE'])


@dataclass(frozen=True)
class Ran:
    """What a run of the step left behind: its status, its output, and every command it ran under `sudo`."""

    status: int
    stdout: str
    sudo: list[str]


def _service(tmp_path: Path, *, answers_from: int | None, join_answer: str = JOIN_OK) -> str:
    """A `sudo` that records its arguments and plays the ZeroTier service behind `zerotier-cli`.

    The service answers every `zerotier-cli` command from contact
    `answers_from` on, counting from 1, and refuses each one before it the way
    the real CLI does when nothing listens: a message and status 1. `None`
    is a service that never answers. Once it answers, a join prints
    `join_answer` and succeeds only if that is `200 join OK`.
    """
    log, contacts = tmp_path / 'sudo.log', tmp_path / 'contacts'
    first = answers_from if answers_from is not None else 0
    return f"""#!/bin/sh
echo "$*" >>{log}
case "$1" in
  tee) cat >/dev/null ;;
  zerotier-cli)
    n=$(( $(cat {contacts} 2>/dev/null || echo 0) + 1 ))
    echo "$n" >{contacts}
    if [ {first} -eq 0 ] || [ "$n" -lt {first} ]; then
      echo "0 $2 connection failed"
      exit 1
    fi
    case "$2" in
      info) echo '200 info 0123456789 1.16.2 OFFLINE' ;;
      join) echo '{join_answer}'; [ '{join_answer}' = '{JOIN_OK}' ] ;;
    esac ;;
esac
"""


def _run(tmp_path: Path, sudo: str, *, started: int | None) -> Ran:
    """Run the identity step the way the runner does, with `sudo` replaced by `sudo`.

    `started=None` is the empty `DEADLINE` an unrun clock step leaves behind;
    otherwise the deadline is `started` plus the clock step's own seconds.
    """
    if shutil.which('bash') is None:
        pytest.fail('bash is not on PATH, which the step runs on')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'sudo'
    fake.write_text(sudo)
    fake.chmod(0o755)

    declared, script = _step(STEP)
    done = process_sessions.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        env={
            **declared,
            'IDENTITY': 'an identity',
            'NETWORK_ID': NETWORK_ID,
            'DEADLINE': '' if started is None else str(started + _deadline()),
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        },
        text=True,
        timeout=30,
    )
    log = tmp_path / 'sudo.log'
    return Ran(done.returncode, done.stdout, log.read_text().splitlines() if log.exists() else [])


def test_the_join_waits_for_the_service_to_answer_its_control_port(tmp_path: Path) -> None:
    """A service that refuses its first contacts after its start is joined once it answers, and not before."""
    ran = _run(tmp_path, _service(tmp_path, answers_from=3), started=int(time.time()))

    assert ran.status == 0, ran.stdout
    started = ran.sudo.index('systemctl start zerotier-one')
    assert ran.sudo[started + 1 :] == [
        'zerotier-cli info',
        'zerotier-cli info',
        'zerotier-cli info',
        f'zerotier-cli join {NETWORK_ID}',
    ]
    # The step says what it waits for before it waits, and how long the wait
    # took once it is over; how it words either is the action's.
    lines = ran.stdout.splitlines()
    assert re.search(r'\bwaiting\b.*\bcontrol port\b', lines[0]), lines
    assert re.search(r'\bcontrol port\b.*\b3 attempts, \d+ s\b', lines[1]), lines
    assert lines[2:] == [JOIN_OK]


def test_a_service_that_never_answers_fails_the_step_naming_it_and_never_joins(tmp_path: Path) -> None:
    """The install having used the whole budget, the wait tries once and fails rather than starting over."""
    ran = _run(tmp_path, _service(tmp_path, answers_from=None), started=int(time.time()) - _deadline())

    assert ran.status == 1
    (line,) = (line for line in ran.stdout.splitlines() if line.startswith('::error::'))
    assert re.search(r'\bZeroTier service\b.*\bcontrol port\b', line), line
    assert re.search(r'\b1 attempts\b', line), line
    assert re.search(r'\b\d+ s\b', line), line
    assert not [command for command in ran.sudo if command.startswith('zerotier-cli join')], ran.sudo


def test_a_join_the_service_refuses_fails_the_step_with_its_answer(tmp_path: Path) -> None:
    refusal = '400 join invalid network id'
    ran = _run(tmp_path, _service(tmp_path, answers_from=1, join_answer=refusal), started=int(time.time()))

    assert ran.status == 1
    (line,) = (line for line in ran.stdout.splitlines() if line.startswith('::error::'))
    assert refusal in line, line


def test_a_step_with_no_deadline_refuses_by_name_before_it_touches_the_service(tmp_path: Path) -> None:
    """A skipped clock step, or one writing another output name, hands the step an empty `DEADLINE`."""
    ran = _run(tmp_path, _service(tmp_path, answers_from=1), started=None)

    assert ran.status == 1
    assert re.fullmatch(r'::error::.*\bclock\b.*\n', ran.stdout), ran.stdout
    assert ran.sudo == []
