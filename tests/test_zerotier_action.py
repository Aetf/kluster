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
lines they print. Every job that joins takes the join's reading again as its
last step when it fails, and the cases find those jobs in the workflows and
run that step after the action's own reading, as the runner would.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import process_sessions
import pytest
from peer_listings import PEERS, addresses, peer, zerotier_stand_ins
from workflow_files import GITHUB, github_name, mapping, read_workflow, workflow_jobs, workflows_and_actions

from kluster import conventions
from kluster.scripts import zt_reading

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


def _environment(
    tmp_path: Path, step: str, env: dict[str, str], *, started: int | None, bin_files: dict[str, str] | None
) -> tuple[list[str], dict[str, str], Path]:
    """The command and environment that run a step the way the runner does, and the log its `sudo` writes.

    `sudo` is replaced by a fake that records its arguments. `started=None` is
    the empty `DEADLINE` an unrun clock step leaves behind; otherwise the
    deadline is `started` plus the clock step's own seconds. `bin_files` are
    further stand-ins put on `PATH`, by name and script.
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
    # A path the action names relative to itself resolves against the action
    # here as it does on the runner.
    declared = {key: value.replace('${{ github.action_path }}', str(ACTION.parent)) for key, value in declared.items()}
    return (
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        {
            **declared,
            **env,
            'DEADLINE': '' if started is None else str(started + _deadline()),
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        },
        log,
    )


def _sudo(log: Path) -> list[str]:
    """Every command the fake `sudo` was handed, in order."""
    return log.read_text().splitlines() if log.exists() else []


def _run(
    tmp_path: Path, step: str, env: dict[str, str], *, started: int | None, bin_files: dict[str, str] | None = None
) -> Waited:
    """Run a step to its end, as `_environment` describes."""
    argv, environment, log = _environment(tmp_path, step, env, started=started, bin_files=bin_files)
    done = process_sessions.run(argv, env=environment, text=True, timeout=30)
    return Waited(done.returncode, done.stdout, _sudo(log))


def _targets(every: tuple[str, ...], any_of: tuple[str, ...]) -> dict[str, str]:
    """The wait step's two inputs, both set, so that neither is left as the expression the step declares."""
    return {'TARGETS': ' '.join(every), 'ANY_TARGETS': ' '.join(any_of)}


def _wait(tmp_path: Path, *every: str, any_of: tuple[str, ...] = (), started: int | None) -> Waited:
    """Run the wait step over `every`, each of which has to answer, and `any_of`, one of which has to."""
    return _run(tmp_path, 'Wait for the target', _targets(every, any_of), started=started)


def test_a_target_that_answers_is_reported_with_its_attempts_and_seconds(tmp_path: Path) -> None:
    """The success line is read by physical/gateway-cutover.md §8.3, so its whole form is held."""
    with _listening() as target:
        waited = _wait(tmp_path, target, started=int(time.time()))

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(target)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


def test_the_wait_passes_only_once_the_last_target_has_answered(tmp_path: Path) -> None:
    """A `physical` run dials the gateway and the homelab host, and the join returns only once both answer.

    The second target refuses through the first round and listens from the
    second on. The stand-in for `sleep` holds the step between the rounds
    until the case has made it listen, so the round it answers in is set by
    the case rather than by a clock. A wait that ended on the first target to
    answer, or tried only the first, returns before it ever sleeps; one that
    stopped trying a target once it had answered is what reports each line
    once. The first target's line is the one §8.3 reads, and it still prints.
    """
    held = '#!/bin/sh\necho "sleep $*" >&2\nread -r _ || true\n'
    with (
        _listening() as first,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as late,
    ):
        late.bind(('127.0.0.1', 0))
        last = f'127.0.0.1:{late.getsockname()[1]}'
        argv, environment, log = _environment(
            tmp_path,
            'Wait for the target',
            _targets((first, last), ()),
            started=int(time.time()),
            bin_files={'sleep': held},
        )
        with process_sessions.started(
            argv, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        ) as command:
            assert command.stderr is not None
            # The step's first sleep: the first round is over, and the last
            # target refused in it.
            between = command.stderr.readline()
            assert between.startswith('sleep '), f'the step never slept between rounds: {between!r}'
            late.listen()
            stdout, _ = command.communicate('\n', timeout=30)
            status = command.returncode

    assert status == 0, stdout
    assert re.fullmatch(
        rf'{re.escape(first)} reachable after 1 attempts, \d+ s\n{re.escape(last)} reachable after 2 attempts, \d+ s\n',
        stdout,
    ), stdout
    assert _sudo(log) == []


def test_the_first_of_several_any_targets_to_answer_ends_the_wait(tmp_path: Path) -> None:
    """A caller names equivalent services, the two resolvers, so one that refuses does not fail the join.

    The refusing target is listed first, so a wait that tried only the first,
    or required every one, fails here.
    """
    with _refusing() as refused, _listening() as answering:
        waited = _wait(tmp_path, any_of=(refused, answering), started=int(time.time()))

    assert waited.status == 0
    assert re.fullmatch(rf'{re.escape(answering)} reachable after 1 attempts, \d+ s\n', waited.stdout), waited.stdout


@pytest.mark.parametrize('variable', ['TARGETS', 'ANY_TARGETS'])
def test_targets_written_one_per_line_are_all_read(tmp_path: Path, variable: str) -> None:
    """A caller that writes either input as a block, one target per line, as it writes `routes`, loses none of them.

    For `wait_for`, two answering targets on two lines are both reported, so a
    wait that read only the first line reports one. For `wait_for_any`, the
    refusing target is on the first line and the answering one on the
    second, so a wait that read only the first line waits out its deadline on
    a target that never answers.
    """
    with _refusing() as refused, _listening() as answering, _listening() as second:
        lines = f'{answering}\n{second}\n' if variable == 'TARGETS' else f'{refused}\n{answering}\n'
        waited = _run(tmp_path, 'Wait for the target', {**_targets((), ()), variable: lines}, started=int(time.time()))

    expected = [answering, second] if variable == 'TARGETS' else [answering]
    assert waited.status == 0
    assert re.fullmatch(
        ''.join(rf'{re.escape(target)} reachable after 1 attempts, \d+ s\n' for target in expected), waited.stdout
    ), waited.stdout


@pytest.mark.parametrize('variable', ['TARGETS', 'ANY_TARGETS'])
def test_a_spent_deadline_ends_the_wait_in_an_error_naming_the_targets_and_the_seconds(
    tmp_path: Path, variable: str
) -> None:
    """The install having used the whole budget, the wait tries once and fails rather than starting over."""
    with _refusing() as first, _refusing() as second:
        waited = _run(
            tmp_path,
            'Wait for the target',
            {**_targets((), ()), variable: f'{first} {second}'},
            started=int(time.time()) - _deadline(),
        )

    assert waited.status == 1
    # The annotation has to carry every target, the attempts and the seconds
    # waited; how it words them is the action's.
    (line,) = (line for line in waited.stdout.splitlines() if line.startswith('::error::'))
    assert re.search(rf'{re.escape(first)}\b', line), line
    assert re.search(rf'{re.escape(second)}\b', line), line
    assert re.search(r'\b1 attempts\b', line), line
    assert re.search(r'\b\d+ s\b', line), line
    assert waited.sudo == ['zerotier-cli listnetworks']


def test_the_error_names_the_target_that_never_answered_and_not_the_one_that_did(tmp_path: Path) -> None:
    """Under `physical`'s two shells, the error is what tells a gateway with no path from a homelab host with none.

    The answering target is listed first and reported as it answers; the
    error that follows names the refusing one alone.
    """
    with _listening() as answering, _refusing() as refused:
        waited = _wait(tmp_path, answering, refused, started=int(time.time()) - _deadline())

    assert waited.status == 1
    lines = waited.stdout.splitlines()
    assert re.fullmatch(rf'{re.escape(answering)} reachable after 1 attempts, \d+ s', lines[0]), waited.stdout
    (line,) = (line for line in lines if line.startswith('::error::'))
    assert re.search(rf'{re.escape(refused)}\b', line), line
    assert answering not in line, line


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


##
## The join's reading
##


def test_the_waits_peer_listing_goes_through_the_filter(tmp_path: Path) -> None:
    """The wait prints the listing every ten rounds while a target has not answered, and the log shows none of it raw.

    The stand-in for `sleep` lets the rounds pass in a moment, so the wait
    reaches its listings well inside the second or two the case leaves it.
    """
    peers = tmp_path / 'peers.txt'
    peers.write_text(PEERS)
    with _refusing() as refused:
        waited = _run(
            tmp_path,
            'Wait for the target',
            _targets((refused,), ()),
            started=int(time.time()) - _deadline() + 2,
            bin_files={
                'sudo': f'#!/bin/sh\n[ "$*" = "zerotier-cli peers" ] && cat {peers}\nexit 0\n',
                'sleep': '#!/bin/sh\nexec /bin/sleep 0.05\n',
            },
        )

    assert waited.status == 1
    assert '<ztaddr>' in waited.stdout, waited.stdout
    listed = '\n'.join(line for line in waited.stdout.splitlines() if not line.startswith('::error::'))
    assert addresses(listed) == [], listed
    assert '<ipv4>' in listed, listed


def test_the_reading_ends_inside_the_headroom_and_leaves_the_wait_half_of_it() -> None:
    """The reading runs after the wait's own error, inside the minute every caller's cap leaves past the deadline."""
    env, script = _step("Take the join's reading")
    bound = re.search(r'timeout "\$\(\(READING_SECONDS \+ (\d+)\)\)"', script)
    assert bound, script

    assert int(env['READING_SECONDS']) + int(bound.group(1)) <= HEADROOM_SECONDS // 2


#: The checkout the action and the workflows run from.
CHECKOUT = GITHUB.parent


def _joining_jobs() -> dict[str, list[dict[str, object]]]:
    """Every workflow job a step of which uses the action, as `<file>: <job>`, with its steps."""
    jobs: dict[str, list[dict[str, object]]] = {}
    for path in workflows_and_actions():
        if path.parent.name != 'workflows':
            continue
        for name, job in workflow_jobs(read_workflow(path), github_name(path)).items():
            where = f'{github_name(path)}: {name}'
            steps = [mapping(step, f'{where} step') for step in cast('list[object]', job.get('steps', []))]
            if any(step.get('uses') == USES for step in steps):
                jobs[where] = steps
    return jobs


def _reading_step(
    tmp_path: Path, inputs: dict[str, str], *, runner: str, mtu: int | None
) -> tuple[str, dict[str, str], Path]:
    """Run the action's reading step as the runner runs it, over a join's `inputs` as the step's environment names them.

    The stand-in daemon lists the gateway's member and gives the runner the
    address `runner`. What it returns is the line the step printed, the
    variables it exported to the job's later steps, and `PATH`'s stand-ins.
    """
    udm = conventions.overlay.member(conventions.overlay.MEMBER_UDM)
    assert isinstance(udm, conventions.overlay.EnrolledMember)
    bin_dir = zerotier_stand_ins(
        tmp_path,
        peers=[peer(udm.node_id, 'LEAF', 23, '192.0.2.1/21032')],
        network={'nwid': conventions.overlay.NETWORK_ID, 'mtu': 1280, 'assignedAddresses': [f'{runner}/16']},
        mtu=mtu,
    )
    exported = tmp_path / 'github_env'
    declared, script = _step("Take the join's reading")
    declared = {key: value.replace('${{ github.action_path }}', str(ACTION.parent)) for key, value in declared.items()}
    done = process_sessions.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        env={
            **declared,
            'NETWORK_ID': conventions.overlay.NETWORK_ID,
            'TARGETS': '',
            'ANY_TARGETS': '',
            'ROUTES': '',
            **inputs,
            'GITHUB_ENV': str(exported),
            'RUNNER_TEMP': str(tmp_path),
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        },
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout
    variables = dict(line.split('=', 1) for line in exported.read_text().splitlines())
    return cast('str', done.stdout), variables, bin_dir


def _joined(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """What a `physical`-shaped join's reading leaves the rest of its job: the variables it exports, and `PATH`'s stand-ins.

    The runner's address is no identity's, so no leg is probed.
    """
    line, variables, bin_dir = _reading_step(
        tmp_path, {'TARGETS': f'{conventions.overlay.UDM}:22'}, runner='192.0.2.200', mtu=1280
    )
    assert re.match(r'zt-reading: member=none .*\budm=DIRECT/23ms/ipv4\b', line), line
    return variables, bin_dir


def test_a_dns_joins_reading_reads_the_gateway_its_routes_go_through_and_the_resolvers_it_waited_for(
    tmp_path: Path,
) -> None:
    """A `dns` join names its targets in `wait_for_any` and reaches them by `routes`, and the step hands the reading both.

    The inputs are a `dns` join's shape: resolvers no member holds, each
    routed through the gateway's overlay address. The gateway's path is read
    only through the route, and the resolvers' legs only through the targets.
    The runner holds the `dns` identity's address, so the legs are the ones
    the reading would probe; `ip` reports no MTU, so it probes none of them
    and the case dials nothing.
    """
    resolvers = conventions.gateway.RESOLVERS
    port = conventions.gateway.ADGUARD_API_PORT
    inputs = {
        'ANY_TARGETS': ' '.join(f'{resolver.address}:{port}' for resolver in resolvers),
        'ROUTES': ''.join(f'{resolver.address}/32 via {conventions.overlay.UDM}\n' for resolver in resolvers),
    }

    line, _, _ = _reading_step(tmp_path, inputs, runner=str(conventions.overlay.CI_DNS), mtu=None)

    fields = dict(field.split('=', 1) for field in line.split()[1:])
    assert fields['member'] == conventions.overlay.MEMBER_CI_DNS, line
    assert fields[conventions.overlay.MEMBER_UDM] == 'DIRECT/23ms/ipv4', line
    first = resolvers[0].name
    assert [fields.get(f'{first}.{probe}') for probe in zt_reading.PROBES] == ['skipped'] * 4, line


def test_every_job_that_joins_takes_the_reading_again_as_its_last_step_once_it_has_failed(tmp_path: Path) -> None:
    """The join's reading shows the paths at the join; a job that failed later shows them again at its end.

    The jobs are every one a step of which uses the action, found in the
    workflows. Each one's last step runs only on a failure, and only where
    the join exported what it read with; run as written after the action's
    own reading, it prints the same members under its own label, and a
    reading with nothing to read from still prints and passes.
    """
    variables, bin_dir = _joined(tmp_path)
    (variable,) = variables
    jobs = _joining_jobs()
    # A read that stopped finding the jobs would pass the loop below on nothing.
    assert jobs, f'no workflow step uses {USES}'

    findings: dict[str, str] = {}
    for where, steps in jobs.items():
        last = steps[-1]
        condition = str(last.get('if', ''))
        if 'failure()' not in condition or f'env.{variable}' not in condition:
            findings[where] = f'its last step runs under {condition!r}, not on a failure after a join'
            continue
        for kept, expected in ((variables[variable], r'.*\budm=DIRECT/23ms/ipv4\b.*'), (str(tmp_path / 'none'), r'.*')):
            done = process_sessions.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', str(last.get('run'))],
                env={
                    variable: kept,
                    'GITHUB_WORKSPACE': str(CHECKOUT),
                    'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                },
                text=True,
                timeout=120,
            )
            line = cast('str', done.stdout)
            if done.returncode != 0 or not re.fullmatch(rf'zt-reading\(at-failure\): {expected}\n', line):
                findings[where] = f'with {kept}, exit {done.returncode}: {line!r}'
    assert findings == {}, findings


def test_the_reading_a_failing_job_takes_again_ends_inside_its_step_and_the_readings_own_deadline_inside_that() -> None:
    """The step's `timeout` lets the reading's own deadline end it, and ends it before the step's cap does."""
    env, _ = _step("Take the join's reading")
    seconds = int(env['READING_SECONDS'])
    for where, steps in _joining_jobs().items():
        last = steps[-1]
        bound = re.search(r'\btimeout (\d+) ', str(last.get('run')))
        assert bound, f'{where}: {last.get("run")!r}'
        cap = last.get('timeout-minutes')
        assert isinstance(cap, int), where
        assert seconds < int(bound.group(1)) < cap * 60, where
