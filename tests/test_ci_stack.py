"""`ci-stack`: what a run by hand of a stack CI deploys is given, refused and handed back.

Every case runs a stand-in `pulumi` found on `PATH`, written by the case: one
that records its arguments and the environment it was started with, one that
prints a value, one that answers ^C the way the real one does. The main check
is the real one (`manual_run.require_main`) over a scratch repository -- a
bare repository standing for the forge, a clone of it standing for the
checkout, and a second clone that pushes past it -- with `git` kept to the
case's own repositories and configuration. Where the subject is what reaches
the terminal, the command line itself runs in a process of its own, with the
checkout it reads pointed at the scratch clone.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess as sp
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest

from kluster.conventions import identity
from kluster.scripts import ci_stack, manual_run

#: What `mise x` starts `ci-stack` with, beside the caller's own: the stack
#: passphrase and the estate's backend from the checkout's slots, a
#: passphrase file a shell exported, and a Helm configuration of the
#: workstation's.
AMBIENT = {
    'PULUMI_CONFIG_PASSPHRASE': 'the-stack-passphrase',
    'PULUMI_CONFIG_PASSPHRASE_FILE': '/elsewhere/stack.passphrase',
    'PULUMI_BACKEND_URL': 'postgres://operator@192.0.2.10:5432/pulumi_state',
    'HELM_CONFIG_HOME': '/caller/helm-config',
    'HELM_REPOSITORY_CONFIG': '/caller/repositories.yaml',
    'PULUMI_K8S_HELM_REPOSITORY_CACHE': '/caller/provider-cache',
}

PHYSICAL_PASSPHRASE = 'physicals-own-passphrase'

GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 'probe',
    'GIT_AUTHOR_EMAIL': 'probe@example.invalid',
    'GIT_COMMITTER_NAME': 'probe',
    'GIT_COMMITTER_EMAIL': 'probe@example.invalid',
}

#: How long a command line run in a process of its own may take: a stop-loss
#: far above what it does, which nothing asserts on.
COMMAND_TIMEOUT = 60


@pytest.fixture(autouse=True)
def isolated_vcs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `git` the main check runs, kept to this case's own repositories and configuration.

    No `GIT_` variable of the caller's reaches it, and it never looks above
    the case's directory, which may sit inside a `jj` workspace's checkout.
    """
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')


def _git(*args: str, cwd: Path) -> None:
    _ = process_sessions.run(['git', *args], cwd=cwd, env=os.environ | GIT_ENV, text=True, check=True, timeout=60)


@dataclass(frozen=True)
class Repository:
    """A forge and two clones of it: the checkout a run starts from, and another that pushes past it."""

    checkout: Path
    other: Path

    def move_past(self) -> None:
        """A commit on the forge's `main`, fetched into the checkout and not under its working copy."""
        _ = (self.other / 'landed.txt').write_text('a change that landed\n')
        _git('add', 'landed.txt', cwd=self.other)
        _git('commit', '-q', '-m', 'landed', cwd=self.other)
        _git('push', '-q', 'origin', 'main', cwd=self.other)
        _git('fetch', '-q', 'origin', cwd=self.checkout)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    forge = tmp_path / 'forge.git'
    _git('init', '-q', '--bare', '-b', 'main', str(forge), cwd=tmp_path)
    checkout = tmp_path / 'kluster'
    _git('clone', '-q', str(forge), str(checkout), cwd=tmp_path)
    _git('checkout', '-q', '-b', 'main', cwd=checkout)
    _ = (checkout / 'Pulumi.yaml').write_text('name: kluster\n')
    _git('add', 'Pulumi.yaml', cwd=checkout)
    _git('commit', '-q', '-m', 'first', cwd=checkout)
    _git('push', '-q', 'origin', 'main', cwd=checkout)
    other = tmp_path / 'other'
    _git('clone', '-q', str(forge), str(other), cwd=tmp_path)
    return Repository(checkout=checkout, other=other)


@dataclass(frozen=True)
class StandIn:
    """A `pulumi` on `PATH` that records what it was started with, at `record`, and exits `exit`."""

    bin: Path
    record: Path

    @property
    def started(self) -> bool:
        return self.record.exists()

    def read(self) -> dict[str, Any]:
        return cast('dict[str, Any]', json.loads(self.record.read_text()))

    @property
    def env(self) -> dict[str, str]:
        """The environment of a run that hands it this stand-in: the ambient one, with the stand-in first on `PATH`."""
        return os.environ | AMBIENT | {'PATH': f'{self.bin}:{os.environ["PATH"]}'}


#: The recording stand-in: its arguments, its environment, and what the
#: directory its Helm homes are under held when it started.
RECORDING = """\
import json, os, sys
from pathlib import Path
helm = Path(os.environ['HELM_CONFIG_HOME']).parent
Path({record!r}).write_text(json.dumps({{
    'argv': sys.argv[1:], 'env': dict(os.environ), 'helm': str(helm), 'held': sorted(os.listdir(helm)),
}}))
sys.exit({exit})
"""


def _write_pulumi(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    pulumi = bin_dir / 'pulumi'
    _ = pulumi.write_text(f'#!{sys.executable} -I\n{body}')
    pulumi.chmod(0o755)


def _recording(tmp_path: Path, *, exit: int = 0) -> StandIn:  # noqa: A002 -- the stand-in's exit status
    stand_in = StandIn(bin=tmp_path / 'bin', record=tmp_path / 'started.json')
    _write_pulumi(stand_in.bin, RECORDING.format(record=str(stand_in.record), exit=exit))
    return stand_in


@pytest.fixture
def stand_in(tmp_path: Path) -> StandIn:
    return _recording(tmp_path)


def _run(stand_in: StandIn, stack: str, *args: str, checkout: Path, main: bool = False) -> int:
    """`ci-stack <stack> <args>` in this process, its main check the real one where `main`, and skipped otherwise."""
    return ci_stack.run(
        stack,
        args,
        checkout=checkout,
        base=stand_in.env,
        physical=lambda: PHYSICAL_PASSPHRASE,
        require_main=manual_run.require_main if main else lambda _: None,
    )


# --------------------------------------------------------------------------
# The stacks it runs.
# --------------------------------------------------------------------------


def test_the_stacks_it_runs_are_the_census_less_the_operator_stacks() -> None:
    assert set(ci_stack.stacks()) | set(identity.OPERATOR_STACKS) == set(identity.STACK_NAMES.names())
    assert set(ci_stack.stacks()) & set(identity.OPERATOR_STACKS) == set()


def test_an_operator_stack_is_refused_naming_operator_stack(stand_in: StandIn, tmp_path: Path) -> None:
    stack = next(iter(identity.OPERATOR_STACKS))

    with pytest.raises(ci_stack.Refused, match=f'`operator-stack {stack}`'):
        _ = _run(stand_in, stack, 'preview', checkout=tmp_path)

    assert not stand_in.started


def test_the_command_line_exits_125_on_a_refusal_and_on_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_stack.main([next(iter(identity.OPERATOR_STACKS)), 'preview']) == ci_stack.REFUSED
    with pytest.raises(SystemExit) as usage:
        _ = ci_stack.main([])

    assert usage.value.code == ci_stack.REFUSED
    assert capsys.readouterr().out == ''


# --------------------------------------------------------------------------
# What is refused before pulumi starts.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args',
    [
        ['preview', '--stack', 'dns'],
        ['preview', '--stack=dns'],
        ['preview', '-s', 'dns'],
        ['up', '-ys', 'dns'],
        ['config', 'cp', '-d', 'dns'],
        ['config', '--color', 'never', 'cp', '--dest=dns'],
        # A global flag and its value ahead of the command's words.
        ['--color', 'never', 'config', 'cp', '-d', 'dns'],
        ['--color', 'never', 'config', 'cp', '--dest', 'dns'],
        ['--color=never', 'config', 'cp', '--dest=dns'],
        # `config copy` is the command, `cp` its alias.
        ['config', 'copy', '-d', 'dns'],
        ['config', 'copy', '--dest=dns'],
        ['--color', 'never', 'config', 'copy', '-d', 'dns'],
    ],
    ids=[
        'stack',
        'stack=',
        's',
        'ys',
        'config-cp-d',
        'config-cp-dest',
        'global-config-cp-d',
        'global-config-cp-dest',
        'global=-config-cp-dest=',
        'config-copy-d',
        'config-copy-dest=',
        'global-config-copy-d',
    ],
)
def test_a_word_that_names_a_stack_is_refused_naming_it(stand_in: StandIn, tmp_path: Path, args: list[str]) -> None:
    named = next(arg for arg in args if arg.startswith('-') and not arg.startswith('--color'))

    with pytest.raises(ci_stack.Refused, match=f'`{named}` names a stack'):
        _ = _run(stand_in, identity.STACK_NAMES.physical, *args, checkout=tmp_path)

    assert not stand_in.started


@pytest.mark.parametrize(
    ('args', 'named'),
    [
        (['up', '-C', '/elsewhere/old-clone'], '-C'),
        (['up', '-yC', '/elsewhere/old-clone'], '-yC'),
        (['up', '--cwd', '/elsewhere/old-clone'], '--cwd'),
        (['up', '--cwd=/elsewhere/old-clone'], '--cwd=/elsewhere/old-clone'),
        (['up', '--config-file', 'Pulumi.apps.yaml'], '--config-file'),
        (['up', '--config-file=Pulumi.apps.yaml'], '--config-file=Pulumi.apps.yaml'),
        (['up', '--client', '127.0.0.1:50051'], '--client'),
        (['up', '--client=127.0.0.1:50051'], '--client=127.0.0.1:50051'),
    ],
    ids=['C', 'yC', 'cwd', 'cwd=', 'config-file', 'config-file=', 'client', 'client='],
)
def test_a_word_that_points_pulumi_at_another_program_or_configuration_is_refused_naming_it(
    stand_in: StandIn, tmp_path: Path, args: list[str], named: str
) -> None:
    with pytest.raises(ci_stack.Refused, match=f'`{re.escape(named)}` points pulumi at another program'):
        _ = _run(stand_in, 'k8s-base', *args, checkout=tmp_path)

    assert not stand_in.started


def test_a_run_that_names_no_pulumi_command_is_refused(stand_in: StandIn, tmp_path: Path) -> None:
    with pytest.raises(ci_stack.Refused, match='name the pulumi command'):
        _ = _run(stand_in, 'dns', checkout=tmp_path)

    assert not stand_in.started


def test_the_stack_goes_ahead_of_a_double_dash_and_words_after_it_are_the_callers(
    stand_in: StandIn, tmp_path: Path
) -> None:
    assert _run(stand_in, 'dns', 'config', 'set', 'key', '--', '-s', checkout=tmp_path) == 0

    assert stand_in.read()['argv'] == ['config', 'set', 'key', '--stack', 'dns', '--', '-s']


def test_a_run_under_claude_is_refused(stand_in: StandIn, tmp_path: Path) -> None:
    checkout = tmp_path / 'kluster' / '.claude' / 'workspaces' / 'probe'
    checkout.mkdir(parents=True)

    with pytest.raises(ci_stack.Refused, match=r'under a \.claude/ directory'):
        _ = _run(stand_in, 'dns', 'preview', checkout=checkout)

    assert not stand_in.started


@pytest.mark.parametrize('backend', [None, ''], ids=['absent', 'empty'])
def test_an_environment_that_names_no_backend_is_refused(
    stand_in: StandIn, tmp_path: Path, backend: str | None
) -> None:
    base = {name: value for name, value in stand_in.env.items() if name != 'PULUMI_BACKEND_URL'}
    if backend is not None:
        base['PULUMI_BACKEND_URL'] = backend

    with pytest.raises(manual_run.NoBackend, match='names no PULUMI_BACKEND_URL'):
        _ = ci_stack.run('dns', ['preview'], checkout=tmp_path, base=base, require_main=lambda _: None)

    assert not stand_in.started


@pytest.mark.parametrize('args', [['stack', 'export'], ['up', '--yes']], ids=['read', 'up'])
def test_a_working_copy_behind_the_forges_main_is_refused_before_pulumi_starts(
    stand_in: StandIn, repository: Repository, args: list[str]
) -> None:
    repository.move_past()

    with pytest.raises(manual_run.CheckRefused) as refused:
        _ = _run(stand_in, 'dns', *args, checkout=repository.checkout, main=True)

    assert refused.value.why is manual_run.Unready.BEHIND_MAIN
    assert not stand_in.started


def test_a_working_copy_on_the_forges_main_starts_pulumi(stand_in: StandIn, repository: Repository) -> None:
    assert _run(stand_in, 'dns', 'stack', 'export', checkout=repository.checkout, main=True) == 0

    assert stand_in.started


# --------------------------------------------------------------------------
# What pulumi is started with.
# --------------------------------------------------------------------------


def test_physical_runs_under_its_own_passphrase_and_cis_helm_environment(stand_in: StandIn, tmp_path: Path) -> None:
    assert _run(stand_in, identity.STACK_NAMES.physical, 'preview', '--diff', checkout=tmp_path) == 0

    started = stand_in.read()
    env = cast('dict[str, str]', started['env'])
    assert env['PULUMI_CONFIG_PASSPHRASE'] == PHYSICAL_PASSPHRASE
    assert 'PULUMI_CONFIG_PASSPHRASE_FILE' not in env
    assert env['PULUMI_BACKEND_URL'] == AMBIENT['PULUMI_BACKEND_URL']
    assert {name: value for name, value in env.items() if value in AMBIENT.values() and 'HELM' in name} == {}
    helm = Path(started['helm'])
    assert all(Path(env[name]).parent == helm for name in manual_run.HELM_HOMES)
    assert started['held'] == [], "the Helm homes' directory held something when pulumi started"
    assert not helm.exists(), "the Helm homes' directory outlived the run"
    assert started['argv'] == ['preview', '--diff', '--stack', identity.STACK_NAMES.physical]


def test_every_other_stack_runs_under_the_callers_stack_passphrase(stand_in: StandIn, tmp_path: Path) -> None:
    assert _run(stand_in, identity.STACK_NAMES.dns, 'preview', checkout=tmp_path) == 0

    env = cast('dict[str, str]', stand_in.read()['env'])
    assert env['PULUMI_CONFIG_PASSPHRASE'] == AMBIENT['PULUMI_CONFIG_PASSPHRASE']
    assert 'PULUMI_CONFIG_PASSPHRASE_FILE' not in env


@pytest.mark.parametrize('code', [0, 1, 7])
def test_the_exit_is_pulumis(tmp_path: Path, code: int) -> None:
    stand_in = _recording(tmp_path, exit=code)

    assert _run(stand_in, 'dns', 'preview', checkout=tmp_path) == code


#: A `pulumi` killed by SIGTERM before it exits by itself.
KILLED = """\
import os, signal
os.kill(os.getpid(), signal.SIGTERM)
"""


def test_a_pulumi_killed_by_a_signal_exits_128_plus_the_signal(tmp_path: Path) -> None:
    bin_dir = tmp_path / 'bin'
    _write_pulumi(bin_dir, KILLED)
    stand_in = StandIn(bin=bin_dir, record=tmp_path / 'unused')

    assert _run(stand_in, 'dns', 'up', checkout=tmp_path) == 128 + signal.SIGTERM


def test_a_pulumi_not_on_path_exits_127(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setitem(AMBIENT, 'PATH', str(empty))

    assert ci_stack.run(
        'dns', ['preview'], checkout=tmp_path, base=os.environ | AMBIENT, require_main=lambda _: None
    ) == (ci_stack.NOT_FOUND)


#: A `pulumi` that writes, to the file its environment names, the disposition
#: of SIGINT it started with: `SIG_IGN` where it inherited an ignored one,
#: Python's own handler where it started at the default.
SIGINT_AT_START = """\
import os, signal
open(os.environ['SIGINT_RECORD'], 'w').write(str(signal.getsignal(signal.SIGINT)))
"""


def test_pulumi_starts_with_sigint_at_its_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / 'bin'
    _write_pulumi(bin_dir, SIGINT_AT_START)
    written = tmp_path / 'sigint'
    monkeypatch.setitem(AMBIENT, 'SIGINT_RECORD', str(written))
    stand_in = StandIn(bin=bin_dir, record=written)

    assert _run(stand_in, 'dns', 'preview', checkout=tmp_path) == 0

    assert written.read_text() == str(signal.default_int_handler)


# --------------------------------------------------------------------------
# The command line, in a process of its own.
# --------------------------------------------------------------------------

#: `ci-stack`'s own command line, reading the checkout it is started in
#: rather than the one its source sits in.
COMMAND_LINE = """\
import sys
from pathlib import Path
from kluster.lib import workstation
from kluster.scripts import ci_stack
workstation.repo_root = lambda *_: Path.cwd()
sys.exit(ci_stack.main(sys.argv[1:]))
"""

#: A `pulumi` that prints a value, as `stack output --show-secrets` does.
PRINTING = """\
import sys
sys.stdout.write('a-value-a-read-prints\\n')
"""


def test_standard_output_is_pulumis_alone_and_ci_stacks_lines_are_on_standard_error(
    repository: Repository, tmp_path: Path
) -> None:
    bin_dir = tmp_path / 'bin'
    _write_pulumi(bin_dir, PRINTING)
    env = os.environ | AMBIENT | {'PATH': f'{bin_dir}:{os.environ["PATH"]}'}

    done = process_sessions.run(
        [sys.executable, '-c', COMMAND_LINE, 'dns', 'stack', 'output', 'value', '--show-secrets'],
        cwd=repository.checkout,
        env=env,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == 'a-value-a-read-prints\n'
    assert "checking the working copy against the forge's main" in done.stderr


#: How long the stand-in below runs before it ends by itself: a bound on a
#: stand-in whose case can no longer kill it. A stop-loss, far above the
#: case, and nothing waits on it: the ^C ends it first.
STAND_IN_LIFETIME = 300

#: A `pulumi` that answers ^C the way the real one does, by finishing what it
#: has in flight before it exits, and says so once it has. Everything is in
#: place before it says it has started, so a ^C at any moment after that line
#: meets its handler.
PULUMI_ANSWERING_SIGINT = f"""\
import signal, sys, time
def cancelled(*_):
    time.sleep(1)
    print('finished', flush=True)
    sys.exit(3)
signal.signal(signal.SIGINT, cancelled)
print('started', flush=True)
time.sleep({STAND_IN_LIFETIME})
sys.exit(4)
"""

#: How long the case waits on `ci-stack` once it has sent ^C: a stop-loss
#: below the case bound, which nothing asserts on.
SIGINT_WAIT = 30


def test_a_sigint_during_a_run_is_pulumis_to_answer(repository: Repository, tmp_path: Path) -> None:
    # The terminal sends ^C to the whole foreground process group; `ci-stack`
    # lets `pulumi` finish answering it, and exits as `pulumi` does. It leads
    # a POSIX session of its own, and the stand-in runs in its process group,
    # so `killpg` on the session's id is that ^C.
    bin_dir = tmp_path / 'bin'
    _write_pulumi(bin_dir, PULUMI_ANSWERING_SIGINT)
    env = os.environ | AMBIENT | {'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    with process_sessions.started(
        [sys.executable, '-c', COMMAND_LINE, 'dns', 'up'],
        cwd=repository.checkout,
        env=env,
        stdout=process_sessions.PIPE,
        stderr=sp.DEVNULL,
        text=True,
    ) as running:
        assert running.stdout is not None
        # An event: the stand-in's own line. It meets EOF instead if
        # `ci-stack` ends before starting it, and the case fails here by name.
        assert running.stdout.readline() == 'started\n', 'the stand-in never started'
        os.killpg(running.pid, signal.SIGINT)
        code = running.wait(timeout=SIGINT_WAIT)
        answered = running.stdout.read()

    assert answered == 'finished\n', 'pulumi was killed before it finished answering ^C'
    assert code == 3
