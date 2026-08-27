"""The GitHub-secret slot: what it runs, and what it does with the answer.

Two levels, as the config slot's suite has. A recorded runner says which `gh`
invocations a push makes and how their output is used — this repository's own
logic. A real subprocess against a `gh` planted on `PATH` says those invocations
mean what they are believed to mean: that the value travels on standard input
and never in `argv`, that the token reaches the child under both names, and that
a refusal comes back naming what an operator would go and fix. No test here
reaches GitHub.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest
from fake_gh import RecordedGh

from kluster.scripts.credentials import github_secrets
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.pulumi_config import SlotRefused

REPOSITORY = 'Aetf/kluster'
SECRET = 'a-recovered-passphrase'

#: A `gh` that answers instead of talking to GitHub. Everything it is handed is
#: written down so the test can read it back: the arguments, the environment
#: `gh` authenticates from, and whatever arrived on standard input.
FAKE_GH = """#!/bin/sh
{
  printf 'args:%s\\n' "$*"
  printf 'gh-token:%s\\n' "$GH_TOKEN"
  printf 'github-token:%s\\n' "$GITHUB_TOKEN"
  printf 'stdin:'
  cat
  printf '\\n'
} >> "$RECORD"
echo '[]'
"""

#: The other half: a `gh` that refuses the way the API does, on stderr.
REFUSING_GH = """#!/bin/sh
echo 'HTTP 404: Not Found (https://api.github.com/repos/Aetf/kluster)' >&2
exit 1
"""


def _plant(directory: Path, script: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a `gh` of our own first on `PATH`, so the real one is never reached.

    First rather than alone: the script is a shell script and needs the tools a
    shell script uses, and a `PATH` holding nothing but this directory would
    have it fail in ways that have nothing to do with what is under test.
    """
    executable = directory / 'gh'
    _ = executable.write_text(script)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv('PATH', f'{directory}{os.pathsep}{os.environ["PATH"]}')


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

    with pytest.raises(SlotRefused, match='not JSON'):
        _ = forge.listing(Slot(REPOSITORY, 'PULUMI_CONFIG_PASSPHRASE', 'dns'))


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
    record = tmp_path / 'record'
    monkeypatch.setenv('RECORD', str(record))
    # The ambient value a shell inside this checkout would already carry: the
    # run must authenticate as what it was handed, not as what it inherited.
    monkeypatch.setenv('GITHUB_TOKEN', 'the-ambient-token')

    _ = github_secrets.run_gh(['secret', 'set', 'PULUMI_CONFIG_PASSPHRASE'], token='the-account-root', stdin=SECRET)

    written = record.read_text()
    assert 'args:secret set PULUMI_CONFIG_PASSPHRASE' in written
    # Both names, because `gh` prefers `GH_TOKEN` and reads `GITHUB_TOKEN` when
    # it is absent — leaving either alone would let the shell decide who this is.
    assert 'gh-token:the-account-root' in written
    assert 'github-token:the-account-root' in written
    assert f'stdin:{SECRET}' in written


def test_a_refusal_says_where_an_operator_would_fix_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(tmp_path, REFUSING_GH, monkeypatch)

    with pytest.raises(SlotRefused, match='no such repository or Environment'):
        _ = github_secrets.run_gh(['secret', 'list', '--repo', REPOSITORY], token='the-account-root', stdin=None)


def test_a_missing_tool_says_so_rather_than_raising_an_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))

    with pytest.raises(SlotRefused, match='is not installed'):
        _ = github_secrets.run_gh(['secret', 'list'], token='the-account-root', stdin=None)


def test_the_child_keeps_the_ambient_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `gh` needs a home directory and a PATH like any other tool, so the token
    # is overlaid on the caller's environment rather than replacing it. The
    # planted script proves it from the inside: it writes to a path it can only
    # learn from a variable this call did not set.
    _plant(tmp_path, FAKE_GH, monkeypatch)
    record = tmp_path / 'record'
    monkeypatch.setenv('RECORD', str(record))

    _ = github_secrets.run_gh(['secret', 'list'], token='the-account-root', stdin=None)

    assert record.is_file()
