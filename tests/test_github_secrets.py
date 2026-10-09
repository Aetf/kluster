"""The GitHub-secret slot: what it runs, and what it does with the answer.

Two levels, as the config slot's suite has. A recorded runner says which `gh`
invocations a push makes and how their output is used — this repository's own
logic. A real subprocess against a `gh` planted on `PATH` says those invocations
mean what they are believed to mean: that the value travels on standard input
and never in `argv`, that the token reaches the child under both names, that a
refusal comes back naming what an operator would go and fix, and that a `gh`
that never finishes is a refusal at its own bound. No test here reaches GitHub.

`run_gh` is production's runner, and it kills the `gh` it started and nothing
else (framework/testing.md §1.2), so the planted scripts are written to leave
nothing behind that kill: none starts a process that could block, and the one
that blocks does it in the shell's own open of a FIFO.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from credentials_command_tree import Leaf, named_leaves
from fake_gh import RecordedGh

from kluster.scripts.credentials import devices, github_secrets
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.pulumi_config import SlotRefused

REPOSITORY = 'Aetf/kluster'
SECRET = 'a-recovered-passphrase'

#: The name the planted script answers to on `PATH`, which is the name `run_gh` runs.
PLANTED = 'gh'

#: `github_secrets.TIMEOUT` while a planted `gh` runs. Below the case bound
#: (`timeout` in `pyproject.toml`), so a planted `gh` that stalls fails as its
#: own `TimeoutExpired`, naming the command, rather than as the case bound; and
#: far above what a planted `gh` takes, which is one shell writing one file.
COMMAND_TIMEOUT = 10

#: The variables through which an operator's shell would choose a host or an
#: enterprise credential for `gh` (`gh help environment`), cleared while a
#: planted `gh` runs.
UNSET_FOR_GH = ('GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST', 'GH_REPO')

#: A `gh` that answers instead of talking to GitHub. Everything it is handed is
#: written down so the test can read it back: the arguments, the environment
#: `gh` authenticates from (and, beside the record, the whole environment it
#: was started with), and, for `secret set`, whatever arrived on standard
#: input. Only `secret set` reads it, because that is the one command whose
#: real `gh` does: any other inherits the test process's standard input from
#: `run_gh`, which under `-s` is a terminal nobody types into.
FAKE_GH = """#!/bin/sh
{
  printf 'args:%s\\n' "$*"
  printf 'gh-token:%s\\n' "$GH_TOKEN"
  printf 'github-token:%s\\n' "$GITHUB_TOKEN"
  if [ "$1 $2" = 'secret set' ]; then
    printf 'stdin:'
    cat
    printf '\\n'
  fi
} >> "$RECORD"
env -0 > "$RECORD.environment"
echo '[]'
"""

#: A `gh` that never finishes. It blocks in the shell's own open of the FIFO
#: `$STALL`, which nobody writes, so it starts no child: the kill `run_gh`
#: makes at its bound reaches the whole of it.
STALLING_GH = """#!/bin/sh
read -r line < "$STALL"
"""


def _refusing(status: str) -> str:
    """A `gh` that refuses the way the API does, on stderr."""
    return f"""#!/bin/sh
echo '{status} (https://api.github.com/repos/{REPOSITORY})' >&2
exit 1
"""


def _plant(directory: Path, script: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a `gh` of our own first on `PATH`, so the real one is never reached.

    First rather than alone: the script is a shell script and needs the tools a
    shell script uses, and a `PATH` holding nothing but this directory would
    have it fail in ways that have nothing to do with what is under test. Its
    run is bounded at `COMMAND_TIMEOUT`, below the case bound.

    A real `gh` reached anyway -- by a change to `run_gh` that loses `PATH`,
    which resolves `gh` through the default path instead -- finds no
    configuration of the operator's: `GH_CONFIG_DIR`, where `gh` keeps its
    hosts and any credential it stores in a file, is an empty directory of the
    case's, and the variables that pick a host or an enterprise credential are
    gone (`gh help environment`). A change that drops the whole environment
    drops this directory with it, and leaves the token `run_gh` sets, which
    takes precedence over any credential `gh` has stored, in a keyring too.
    """
    executable = directory / PLANTED
    _ = executable.write_text(script)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('PATH', f'{directory}{os.pathsep}{os.environ["PATH"]}')
    configuration = directory / 'gh-config'
    configuration.mkdir()
    monkeypatch.setenv('GH_CONFIG_DIR', str(configuration))
    for name in UNSET_FOR_GH:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github_secrets, 'TIMEOUT', COMMAND_TIMEOUT)


def test_an_environment_secret_names_its_environment_and_a_repository_one_does_not() -> None:
    # `--repo` is always given rather than relying on the working directory:
    # the ops repository is a target too, and a command whose meaning depends
    # on where it was run from fills the wrong repository exactly once.
    assert Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns').scope == ['--repo', REPOSITORY, '--env', 'dns']
    assert Slot(REPOSITORY, 'HAOS_DEPLOY_WEBHOOK_URL').scope == ['--repo', REPOSITORY]


def test_a_listing_reads_names_and_timestamps() -> None:
    recorded = RecordedGh(collections={(REPOSITORY, 'dns'): {'PULUMI_CONFIG_PASSPHRASE': '2026-08-01T00:00:00Z'}})
    forge = Forge(token='a-token', run=recorded)

    listing = forge.listing(Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns'))

    # Names and timestamps are the whole of what the API discloses, which is
    # what makes them the whole of verification.
    assert listing == {'PULUMI_CONFIG_PASSPHRASE': '2026-08-01T00:00:00Z'}
    assert ['secret', 'list', '--repo', REPOSITORY, '--env', 'dns', '--json', 'name,updatedAt'] in recorded.invocations


def _answering(output: str) -> github_secrets.Runner:
    """A `gh` that says the same thing whatever it is asked."""

    def run(args: Sequence[str], *, token: str, stdin: str | None) -> str:
        return output

    return run


def test_an_empty_collection_lists_nothing_rather_than_failing() -> None:
    forge = Forge(token='a-token', run=_answering(''))

    assert forge.listing(Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns')) == {}


def test_a_listing_that_is_not_json_is_a_refusal_naming_the_slot() -> None:
    forge = Forge(token='a-token', run=_answering('not json at all'))
    slot = Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns')

    with pytest.raises(SlotRefused, match=re.escape(str(slot))):
        _ = forge.listing(slot)


def test_the_value_travels_on_standard_input() -> None:
    recorded = RecordedGh()
    slot = Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns')

    Forge(token='a-token', run=recorded).put(slot, SECRET)

    # `--body` would put the credential in the process table of a shared
    # machine; omitting it is exactly what makes `gh` read the pipe.
    assert recorded.values[(REPOSITORY, 'dns', 'PULUMI_CONFIG_PASSPHRASE')] == SECRET
    assert ['secret', 'set', 'PULUMI_CONFIG_PASSPHRASE', '--repo', REPOSITORY, '--env', 'dns'] in recorded.invocations
    assert not [args for args in recorded.invocations if SECRET in args]


def test_a_real_subprocess_receives_the_value_and_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(tmp_path, FAKE_GH, monkeypatch)
    # The planted script learns where to write from a variable this call did
    # not set, so a record at all is the proof that the token is overlaid on
    # the caller's environment rather than replacing it: `gh` needs a home
    # directory and a `PATH` like any other tool.
    record = tmp_path / 'record'
    monkeypatch.setenv('RECORD', str(record))
    # The ambient value a shell inside this checkout would already carry: the
    # run must authenticate as what it was handed, not as what it inherited.
    monkeypatch.setenv('GITHUB_TOKEN', 'the-ambient-token')

    _ = github_secrets.run_gh(['secret', 'set', 'PULUMI_CONFIG_PASSPHRASE'], token='the-admin-token', stdin=SECRET)

    written = record.read_text()
    assert 'args:secret set PULUMI_CONFIG_PASSPHRASE' in written
    # Both names, because `gh` prefers `GH_TOKEN` and reads `GITHUB_TOKEN` when
    # it is absent — leaving either alone would let the shell decide who this is.
    assert 'gh-token:the-admin-token' in written
    assert 'github-token:the-admin-token' in written
    assert f'stdin:{SECRET}' in written


def test_a_planted_gh_is_started_with_no_configuration_of_the_operators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What an operator's shell might carry, set before the plant: none of it
    # may reach a `gh` the case starts, the real one reached by mistake included.
    monkeypatch.setenv('GH_CONFIG_DIR', '/nonexistent/the-operators-gh-config')
    for name in UNSET_FOR_GH:
        monkeypatch.setenv(name, f'the-ambient-{name.lower()}')
    _plant(tmp_path, FAKE_GH, monkeypatch)
    record = tmp_path / 'record'
    monkeypatch.setenv('RECORD', str(record))

    _ = github_secrets.run_gh(['secret', 'list', '--repo', REPOSITORY], token='the-admin-token', stdin=None)

    started_with = dict(entry.split('=', 1) for entry in Path(f'{record}.environment').read_text().split('\0') if entry)
    configuration = Path(started_with['GH_CONFIG_DIR'])
    assert configuration.is_relative_to(tmp_path)
    assert not list(configuration.iterdir())
    assert not [name for name in UNSET_FOR_GH if name in started_with]


@contextmanager
def _a_stdin_that_never_ends() -> Generator[None]:
    """Fd 0 a pipe whose writer this process holds for the block: a terminal nobody types into, to a reader.

    pytest's capture and an xdist worker already hold /dev/null there. On the
    way out the writer closes, so a reader the block left behind meets EOF and
    ends without anyone signalling it.
    """
    read, write = os.pipe()
    saved = os.dup(0)
    os.dup2(read, 0)
    os.close(read)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(write)


def test_a_listing_leaves_the_standard_input_it_inherits_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `run_gh` hands a listing no standard input, so the planted `gh` inherits
    # this process's; a planted `gh` that read it would block until its bound.
    _plant(tmp_path, FAKE_GH, monkeypatch)
    record = tmp_path / 'record'
    monkeypatch.setenv('RECORD', str(record))

    with _a_stdin_that_never_ends():
        listed = github_secrets.run_gh(['secret', 'list', '--repo', REPOSITORY], token='the-admin-token', stdin=None)

    assert listed.strip() == '[]'
    assert f'args:secret list --repo {REPOSITORY}' in record.read_text()


@pytest.mark.parametrize(
    ('status', 'remedy', 'commands'),
    [
        pytest.param(
            'HTTP 404: Not Found', r'no such repository or Environment.*not given this repository', [], id='404'
        ),
        # A fine-grained token is refused per permission rather than per
        # scope, so the repair is the set §3 defines, not a scope to add.
        pytest.param(
            'HTTP 403: Resource not accessible',
            r'lacks a permission this call needs \(credentials\.md §3\)',
            [],
            id='403',
        ),
        # A 401 is a token GitHub does not recognize -- revoked or expired --
        # and no permission added to it would help: the repair is a new token,
        # through the admin token's own row. The parser reads the row off the
        # message, so a leaf renamed in the tree fails here while the message
        # still names the old one.
        pytest.param(
            'HTTP 401: Bad credentials',
            'was not accepted',
            [('derived', devices.GITHUB_ADMIN, 'record')],
            id='401',
        ),
    ],
)
def test_a_refusal_says_where_an_operator_would_fix_it(
    status: str, remedy: str, commands: list[Leaf], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path, _refusing(status), monkeypatch)

    with pytest.raises(SlotRefused, match=remedy) as refused:
        _ = github_secrets.run_gh(['secret', 'list', '--repo', REPOSITORY], token='the-admin-token', stdin=None)
    assert named_leaves(str(refused.value)) == commands


def _read_by_anyone(fifo: Path) -> bool:
    """Whether any process holds `fifo` open for reading, or waits in its open to.

    A writer's non-blocking open of a FIFO with no reader is refused with
    ENXIO (fifo(7)). Where there is one, the open succeeds and is closed at
    once, and the reader, with no writer left, meets EOF and ends.
    """
    try:
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as refused:
        if refused.errno == errno.ENXIO:
            return False
        raise
    os.close(writer)
    return True


def test_a_gh_that_never_finishes_is_refused_at_its_own_bound_and_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _plant(tmp_path, STALLING_GH, monkeypatch)
    stall = tmp_path / 'stall'
    os.mkfifo(stall)
    monkeypatch.setenv('STALL', str(stall))

    with pytest.raises(SlotRefused) as refused:
        _ = github_secrets.run_gh(['secret', 'list', '--repo', REPOSITORY], token='the-admin-token', stdin=None)

    timed_out = refused.value.__cause__
    assert isinstance(timed_out, subprocess.TimeoutExpired)
    assert timed_out.cmd == [PLANTED, 'secret', 'list', '--repo', REPOSITORY]
    # The bound that fired is below the one the case runs under, which is
    # what let the case end here, naming the command, at all.
    assert timed_out.timeout < float(str(request.config.getini('timeout')))
    # `run_gh` killed and reaped the planted `gh` before it raised. What the
    # stall was blocked on is the FIFO, so a process the script had started
    # to block for it would be holding it still.
    assert not _read_by_anyone(stall)
