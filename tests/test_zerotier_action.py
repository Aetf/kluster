"""The ZeroTier action installs the routes it is given and waits for its targets, each ending in its own error.

The action joins the overlay; given `routes`, it installs each once its next
hop is on a link of the runner; given `wait_for`, it waits for the first of
those hosts and ports to answer before it returns. Both waits run against one
wall-clock deadline set by the action's first step, so the install and the
waits share one budget, and the deadline has to pass early enough that a
wait's own error -- which names what it waited for and how long -- prints
before the caller's cap ends the step without one. The cases here hold that
relation between the deadline the action declares and every cap a workflow
puts on a step that uses it, both read out of the files and neither restated;
and they run the route and wait steps as they are written, against stand-ins
for `ip` and `sudo` and against ports on this host, for what they do and the
lines they print.
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
#: The clock reference a waiting step's `DEADLINE` is written as.
CLOCK = re.compile(r'^\$\{\{\s*steps\.([\w-]+)\.outputs\.deadline\s*\}\}$')


def _steps() -> list[dict[str, object]]:
    runs = mapping(read_workflow(ACTION).get('runs'), 'zerotier runs:')
    return [mapping(step, 'zerotier step') for step in cast('list[object]', runs.get('steps'))]


def _step(name: str) -> tuple[dict[str, str], str]:
    """The environment and script of the step named `name`."""
    (step,) = (step for step in _steps() if step.get('name') == name)
    env = mapping(step.get('env', {}), f'the {name!r} step env:')
    return {key: str(value) for key, value in env.items()}, str(step.get('run'))


def _deadline() -> int:
    """The seconds the clock step gives the join, from its own start."""
    env, _ = _step("Start the join's clock")
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


def test_every_wait_reads_the_deadline_the_actions_first_step_sets() -> None:
    """The install shares the deadline only if the clock every waiting step reads starts before it."""
    waiting = [step for step in _steps() if 'DEADLINE' in mapping(step.get('env', {}), 'a step env:')]
    # A read that stopped finding the waits would pass the loop below on nothing.
    assert {step.get('name') for step in waiting} >= {"Install the run's routes", 'Wait for the target'}
    for step in waiting:
        reference = str(mapping(step.get('env'), 'a step env:')['DEADLINE'])
        clock = CLOCK.match(reference)
        assert clock, f'{step.get("name")}: DEADLINE is {reference!r}, not a step output'
        assert _steps()[0].get('id') == clock.group(1), step.get('name')


@dataclass(frozen=True)
class Waited:
    """What a run of a step left behind: its status, its output, and every command it ran under `sudo`."""

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


def _run(
    tmp_path: Path, step: str, env: dict[str, str], *, started: int | None, bin_files: dict[str, str] | None = None
) -> Waited:
    """Run a step the way the runner does, with `sudo` replaced by a fake that records its arguments.

    `started=None` is the empty `DEADLINE` an unrun clock step leaves behind;
    otherwise the deadline is `started` plus the clock step's own seconds.
    `bin_files` are further stand-ins put on `PATH`, by name and script.
    """
    if shutil.which('bash') is None:
        pytest.fail('bash is not on PATH, which the step runs on')
    log = tmp_path / 'sudo.log'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, script in {'sudo': f'#!/bin/sh\necho "$*" >>{log}\n', **(bin_files or {})}.items():
        fake = bin_dir / name
        fake.write_text(script)
        fake.chmod(0o755)

    declared, script = _step(step)
    done = process_sessions.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        env={
            **declared,
            **env,
            'DEADLINE': '' if started is None else str(started + _deadline()),
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        },
        text=True,
        timeout=30,
    )
    return Waited(done.returncode, done.stdout, log.read_text().splitlines() if log.exists() else [])


def _wait(tmp_path: Path, *targets: str, started: int | None) -> Waited:
    """Run the wait step over `targets`."""
    return _run(tmp_path, 'Wait for the target', {'TARGETS': ' '.join(targets)}, started=started)


def test_a_target_that_answers_is_reported_with_its_attempts_and_seconds(tmp_path: Path) -> None:
    """The success line is read by physical/gateway-cutover.md §8.3, so its whole form is held."""
    with _listening() as target:
        waited = _wait(tmp_path, target, started=int(time.time()))

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(target)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


def test_the_first_of_several_targets_to_answer_ends_the_wait(tmp_path: Path) -> None:
    """A caller names equivalent services, the two resolvers, so one that refuses does not fail the join.

    The refusing target is listed first, so a wait that tried only the first,
    or required every one, fails here.
    """
    with _refusing() as refused, _listening() as answering:
        waited = _wait(tmp_path, refused, answering, started=int(time.time()))

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(answering)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


def test_targets_written_one_per_line_are_all_tried(tmp_path: Path) -> None:
    """A caller that writes `wait_for` as a block, one target per line, as it writes `routes`, loses none of them.

    The refusing target is on the first line and the answering one on the
    second, so a wait that read only the first line waits out its deadline
    on a target that never answers.
    """
    with _refusing() as refused, _listening() as answering:
        waited = _run(
            tmp_path, 'Wait for the target', {'TARGETS': f'{refused}\n{answering}\n'}, started=int(time.time())
        )

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(answering)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


def test_a_spent_deadline_ends_the_wait_in_an_error_naming_the_targets_and_the_seconds(tmp_path: Path) -> None:
    """The install having used the whole budget, the wait tries once and fails rather than starting over."""
    with _refusing() as first, _refusing() as second:
        waited = _wait(tmp_path, first, second, started=int(time.time()) - _deadline())

    assert waited.status == 1
    # The annotation has to carry every target, the attempts and the seconds
    # waited; how it words them is the action's.
    (line,) = (line for line in waited.stdout.splitlines() if line.startswith('::error::'))
    assert re.search(rf'{re.escape(first)}\b', line), line
    assert re.search(rf'{re.escape(second)}\b', line), line
    assert re.search(r'\b1 attempts\b', line), line
    assert re.search(r'\b\d+ s\b', line), line
    assert waited.sudo == ['zerotier-cli listnetworks']


def test_a_wait_with_no_deadline_refuses_by_name_instead_of_blaming_the_target(tmp_path: Path) -> None:
    """A skipped clock step, or one writing another output name, hands the wait an empty `DEADLINE`."""
    with _listening() as target:
        waited = _wait(tmp_path, target, started=None)

    assert waited.status == 1
    assert re.fullmatch(r'::error::.*\bclock\b.*\n', waited.stdout), waited.stdout
    assert target not in waited.stdout
    assert waited.sudo == []


##
## The run's own routes
##

#: The two routes the cases hand the step, written as a caller writes them.
ROUTES = '192.0.2.3/32 via 198.51.100.1\n192.0.2.4/32 via 198.51.100.1\n'


def _ip(tmp_path: Path, *, off_link: int | None) -> str:
    """A stand-in for `ip -4 route get <hop>`, answering as the kernel does for the hop it is asked about.

    It says the hop is reached through a gateway of the host's own for its
    first `off_link` calls, and on a connected link after that -- with the
    trailing `cache` line the real command prints -- or, with `off_link=None`,
    that the host has no route to it at all.
    """
    count = tmp_path / 'ip.calls'
    if off_link is None:
        return '#!/bin/sh\necho "RTNETLINK answers: Network is unreachable" >&2\nexit 2\n'
    return (
        '#!/bin/sh\n'
        '[ "$1 $2 $3" = "-4 route get" ] || { echo "unexpected: $*" >&2; exit 64; }\n'
        f'n=$(($(cat {count} 2>/dev/null || echo 0) + 1)); echo "$n" >{count}\n'
        f'if [ "$n" -le {off_link} ]; then echo "$4 via 192.0.2.254 dev eth0 src 192.0.2.10 uid 1000"; '
        'else echo "$4 dev zt0 src 10.144.2.2 uid 1000"; fi\n'
        'echo "    cache"\n'
    )


def _install(tmp_path: Path, routes: str, *, off_link: int | None, started: int) -> Waited:
    """Run the route step over `routes`, against the `ip` stand-in."""
    return _run(
        tmp_path,
        "Install the run's routes",
        {'ROUTES': routes},
        started=started,
        bin_files={'ip': _ip(tmp_path, off_link=off_link)},
    )


def test_every_route_given_is_installed_through_its_next_hop(tmp_path: Path) -> None:
    """What the kernel is asked for is each route handed in, target and next hop, in order."""
    installed = _install(tmp_path, ROUTES, off_link=0, started=int(time.time()))

    assert installed.status == 0, installed.stdout
    assert installed.sudo == [
        'ip route replace 192.0.2.3/32 via 198.51.100.1',
        'ip route replace 192.0.2.4/32 via 198.51.100.1',
    ]


def test_a_route_waits_for_its_next_hop_to_be_on_a_link_of_this_host(tmp_path: Path) -> None:
    """The member holds no overlay address for a while after the join, and the kernel takes a `via` only on-link.

    The stand-in reports the next hop as reached through the host's own
    gateway twice before it is on a link, so a step that installed at once,
    or read the trailing `cache` line as an answer, would run the install while
    the hop was still off-link.
    """
    installed = _install(tmp_path, ROUTES, off_link=2, started=int(time.time()))

    assert installed.status == 0, installed.stdout
    assert int((tmp_path / 'ip.calls').read_text()) >= 3
    assert installed.sudo[0] == 'ip route replace 192.0.2.3/32 via 198.51.100.1'


@pytest.mark.parametrize('off_link', [10_000, None], ids=['through-a-gateway', 'no-route'])
def test_a_next_hop_on_no_link_by_the_deadline_fails_the_job_naming_it(tmp_path: Path, off_link: int | None) -> None:
    """The join fails at this step, before the stack runs, and installs nothing."""
    installed = _install(tmp_path, ROUTES, off_link=off_link, started=int(time.time()) - _deadline())

    assert installed.status == 1
    (line,) = (line for line in installed.stdout.splitlines() if line.startswith('::error::'))
    assert '198.51.100.1' in line, line
    assert '192.0.2.3/32' in line, line
    assert installed.sudo == ['zerotier-cli listnetworks']


def test_a_line_that_is_not_a_route_is_refused_by_its_text_before_any_route_is_installed(tmp_path: Path) -> None:
    """A malformed second line would otherwise leave the first installed and the second silently dropped."""
    installed = _install(
        tmp_path, '192.0.2.3/32 via 198.51.100.1\n192.0.2.4/32 198.51.100.1\n', off_link=0, started=int(time.time())
    )

    assert installed.status == 1
    (line,) = (line for line in installed.stdout.splitlines() if line.startswith('::error::'))
    assert '192.0.2.4/32 198.51.100.1' in line, line
    assert installed.sudo == []


def test_routes_with_no_deadline_refuse_by_name_instead_of_blaming_the_next_hop(tmp_path: Path) -> None:
    """An empty `DEADLINE` would read as the epoch, and the wait for the next hop would fail at once."""
    installed = _run(tmp_path, "Install the run's routes", {'ROUTES': ROUTES}, started=None)

    assert installed.status == 1
    assert re.fullmatch(r'::error::.*\bclock\b.*\n', installed.stdout), installed.stdout
    assert installed.sudo == []
