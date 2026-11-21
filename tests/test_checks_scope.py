"""`checks-scope` answers `stack-files` for the one shape of change, and `full` for everything else.

Each case builds a scratch repository with a base commit and a head commit and
asks for the verdict on the head, as the `Scope` step of `checks.yml` asks for
it on a pull request's merge commit. The project in the scratch repository is
not this one, so a key spelled with this repository's project name would miss
the scratch files' keys and turn a `stack-files` case `full`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import process_sessions
import pytest

from kluster.conventions import sealed
from kluster.conventions.backup import ETCD_DRILL_RECIPIENT
from kluster.scripts import checks_scope
from kluster.scripts.checks_scope import FULL, RECIPIENT_FILE, STACK_FILES
from kluster.scripts.credentials import slots

ROOT = Path(__file__).parent.parent

#: Each git call's bound: a stop-loss, which nothing asserts on.
GIT_TIMEOUT = 10

PROJECT = 'scope-probe'
SEALED = f'{PROJECT}:{sealed.CONFIG_KEY}'
KUBECONFIG = f'{PROJECT}:{slots.KUBECONFIG_KEY}'

#: Recipients of the form `age-keygen` draws: `age1` and no second `1`.
RECIPIENT = 'age1' + 'q' * 58
OTHER_RECIPIENT = 'age1' + 'p' * 58


def stack_file(
    *,
    salt: str = 'v1:c2FsdA==:v1:a:b',
    disabled: str = 'kubernetes',
    kubeconfig: str = '\n    secure: v1:AAAA',
    sealed_values: str = '\n    authelia:\n      jwt: AgBfirst',
) -> str:
    """A stack file in the form the CLI writes, each part replaceable."""
    return (
        f'encryptionsalt: {salt}\n'
        'config:\n'
        f'  pulumi:disable-default-providers:\n    - {disabled}\n'
        f'  {KUBECONFIG}:{kubeconfig}\n'
        f'  {SEALED}:{sealed_values}\n'
    )


BASE = {
    'Pulumi.yaml': f'name: {PROJECT}\nruntime: python\n',
    'Pulumi.k8s-base.yaml': stack_file(),
    'Pulumi.apps.yaml': stack_file(disabled='cloudflare'),
    'main.py': 'print("a program")\n',
    str(RECIPIENT_FILE): f'# the restore-verify recipient\n{RECIPIENT}\n',
}


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty scratch repository, with the caller's git kept out of it.

    No `GIT_*` variable of the caller's reaches git, an inherited `GIT_DIR`
    would point it at another repository, and neither does their global or
    system configuration. `checks_scope` runs git in the process's own
    environment, so the process's is the one cleared.
    """
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('HOME', str(tmp_path))
    for role in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{role}_NAME', 'scope probe')
        monkeypatch.setenv(f'GIT_{role}_EMAIL', 'probe@example.invalid')
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    return repo


def git(repo: Path, *arguments: str) -> None:
    _ = process_sessions.run(['git', *arguments], cwd=repo, check=True, timeout=GIT_TIMEOUT)


def commit(repo: Path, files: Mapping[str, str | bytes | None]) -> None:
    """Write `files` (a `None` deletes the path) and commit everything."""
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(text if isinstance(text, bytes) else text.encode())
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'a commit')


def scope(repo: Path, head: Mapping[str, str | bytes | None], *, event: str = 'pull_request') -> str:
    """The verdict on a head that changes `head` over `BASE`."""
    commit(repo, BASE)
    commit(repo, head)
    return checks_scope.verdict(repo, event=event, base='HEAD^1').scope


@pytest.mark.parametrize(
    'head',
    [
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(sealed_values='\n    authelia:\n      jwt: AgBsecond')}),
        pytest.param(
            {
                'Pulumi.k8s-base.yaml': stack_file(
                    sealed_values='\n    authelia:\n      jwt: AgBfirst\n    exim:\n      password: AgBthird'
                )
            },
        ),
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(kubeconfig='\n    secure: v1:BBBB')}),
        pytest.param(
            {
                'Pulumi.k8s-base.yaml': stack_file(kubeconfig='\n    secure: v1:BBBB'),
                'Pulumi.apps.yaml': stack_file(disabled='cloudflare', sealed_values='\n    authelia:\n      jwt: AgBx'),
            },
        ),
        pytest.param(
            {
                'Pulumi.k8s-base.yaml': stack_file(sealed_values='\n    authelia:\n      jwt: AgBsecond'),
                str(RECIPIENT_FILE): f'# the restore-verify recipient\n{OTHER_RECIPIENT}\n',
            },
        ),
        pytest.param({str(RECIPIENT_FILE): f'{OTHER_RECIPIENT}\n'}),
    ],
    ids=[
        'a-ciphertext-changed',
        'a-sealed-name-added',
        'a-kubeconfig-copy-changed',
        'two-stack-files',
        'a-stack-file-and-the-recipient',
        'the-recipient-alone',
    ],
)
def test_a_change_to_what_the_stack_files_hold_sealed_takes_the_fast_path(
    repository: Path, head: dict[str, str | None]
) -> None:
    assert scope(repository, head) == STACK_FILES


def test_a_kubeconfig_copy_added_to_a_stack_that_had_none_takes_the_fast_path(repository: Path) -> None:
    without = stack_file().replace(f'  {KUBECONFIG}:\n    secure: v1:AAAA\n', '')
    commit(repository, {**BASE, 'Pulumi.apps.yaml': without})
    commit(repository, {'Pulumi.apps.yaml': stack_file()})

    assert checks_scope.verdict(repository, event='pull_request', base='HEAD^1').scope == STACK_FILES


@pytest.mark.parametrize(
    'head',
    [
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(salt='v1:b3RoZXI=:v1:a:b')}),
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(disabled='cloudflare')}),
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(kubeconfig=' "apiVersion: v1"')}),
        pytest.param({'Pulumi.k8s-base.yaml': stack_file(kubeconfig='\n    secure: v1:BBBB\n    other: x')}),
        pytest.param({'Pulumi.yaml': f'name: {PROJECT}\nruntime: python\ndescription: changed\n'}),
        pytest.param({'Pulumi.dns.yaml': stack_file()}),
        pytest.param({'Pulumi.apps.yaml': None}),
        pytest.param(
            {
                'Pulumi.k8s-base.yaml': stack_file(sealed_values='\n    authelia:\n      jwt: AgBsecond'),
                'main.py': 'print("another program")\n',
            },
        ),
        pytest.param({'Pulumi.k8s-base.yaml': 'config: [\n'}),
        pytest.param({'Pulumi.k8s-base.yaml': '- a list\n'}),
        pytest.param({str(RECIPIENT_FILE): f'{RECIPIENT}\n{OTHER_RECIPIENT}\n'}),
        pytest.param({str(RECIPIENT_FILE): 'AGE-SECRET-KEY-1QQQQ\n'}),
    ],
    ids=[
        'the-salt-changed',
        'disable-default-providers-changed',
        'a-kubeconfig-copy-in-the-clear',
        'a-kubeconfig-copy-beside-another-key',
        'the-project-file',
        'a-stack-file-added',
        'a-stack-file-deleted',
        'a-python-file-beside-a-stack-file',
        'a-stack-file-that-does-not-parse',
        'a-stack-file-that-is-not-a-mapping',
        'two-recipients',
        'an-identity-where-the-recipient-goes',
    ],
)
def test_any_other_change_runs_in_full(repository: Path, head: dict[str, str | None]) -> None:
    assert scope(repository, head) == FULL


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('1', 'true'),
        ('false', '0'),
        ('1', '1.0'),
        ('1', '1e0'),
        ('1', '0x1'),
        ('017', '17'),
        ('1', '"1"'),
        ('2001-12-14 21:59:43 -5', '2001-12-15 02:59:43 +0'),
        ('{1: a}', '{"1": a}'),
    ],
    ids=[
        'an-int-to-a-bool',
        'a-bool-to-an-int',
        'an-int-to-a-float',
        'an-int-to-an-exponent',
        'an-int-to-hex',
        'an-octal-to-its-decimal',
        'an-int-to-a-string',
        'a-timestamp-to-the-same-instant',
        'an-int-key-to-a-string-key',
    ],
)
def test_a_value_beside_the_two_keys_changed_to_one_a_loader_holds_equal_runs_in_full(
    repository: Path, before: str, after: str
) -> None:
    """The comparison is of what the file says, each scalar its tag and its text, not of what a loader makes of it.

    Pulumi hands the program a configuration value as its text, so `017` and
    `17` are two values to the program however a YAML loader reads them.
    """
    setting = f'  {PROJECT}:replicas: {{}}\n'
    commit(repository, {**BASE, 'Pulumi.apps.yaml': stack_file() + setting.format(before)})
    commit(repository, {'Pulumi.apps.yaml': stack_file() + setting.format(after)})

    assert checks_scope.verdict(repository, event='pull_request', base='HEAD^1').scope == FULL


@pytest.mark.parametrize(
    'head',
    [
        pytest.param({'Pulumi.k8s-base.yaml': stack_file().encode() + b'  scope-probe:x: \xff\n'}),
        pytest.param({str(RECIPIENT_FILE): f'{RECIPIENT[:-1]}'.encode() + b'\xff\n'}),
        pytest.param({'Pulumi.k8s-base.yaml': stack_file() + f'  {PROJECT}:deep: {"[" * 3000}{"]" * 3000}\n'}),
    ],
    ids=['a-stack-file-not-utf-8', 'a-recipient-not-utf-8', 'a-stack-file-nested-past-the-recursion-limit'],
)
def test_an_error_nothing_foresees_runs_in_full_rather_than_failing(
    repository: Path, head: Mapping[str, str | bytes | None], caplog: pytest.LogCaptureFixture
) -> None:
    assert scope(repository, head) == FULL
    assert any(record.exc_info for record in caplog.records), 'the error is logged with its traceback'


def test_a_renamed_stack_file_runs_in_full(repository: Path) -> None:
    commit(repository, BASE)
    git(repository, 'mv', 'Pulumi.apps.yaml', 'Pulumi.web.yaml')
    git(repository, 'commit', '-q', '-m', 'a rename')

    assert checks_scope.verdict(repository, event='pull_request', base='HEAD^1').scope == FULL


def test_a_mode_change_runs_in_full(repository: Path) -> None:
    commit(repository, BASE)
    (repository / 'Pulumi.apps.yaml').chmod(0o755)
    commit(repository, {'Pulumi.k8s-base.yaml': stack_file(sealed_values='\n    authelia:\n      jwt: AgBsecond')})

    assert checks_scope.verdict(repository, event='pull_request', base='HEAD^1').scope == FULL


def test_a_push_runs_in_full(repository: Path) -> None:
    head = {'Pulumi.k8s-base.yaml': stack_file(sealed_values='\n    authelia:\n      jwt: AgBsecond')}

    assert scope(repository, head, event='push') == FULL


@pytest.mark.parametrize('base', ['HEAD^1', ''], ids=['no-parent', 'none'])
def test_a_base_that_is_not_a_commit_runs_in_full(repository: Path, base: str) -> None:
    commit(repository, BASE)

    assert checks_scope.verdict(repository, event='pull_request', base=base).scope == FULL


def test_a_head_that_changes_nothing_runs_in_full(repository: Path) -> None:
    commit(repository, BASE)
    git(repository, 'commit', '-q', '--allow-empty', '-m', 'nothing')

    assert checks_scope.verdict(repository, event='pull_request', base='HEAD^1').scope == FULL


def test_the_command_writes_the_verdict_as_the_step_output(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commit(repository, BASE)
    commit(repository, {'Pulumi.k8s-base.yaml': stack_file(kubeconfig='\n    secure: v1:BBBB')})
    output = tmp_path / 'github-output'
    _ = output.write_text('earlier=step\n')
    monkeypatch.chdir(repository)

    code = checks_scope.main(['--event', 'pull_request', '--base', 'HEAD^1', '--github-output', str(output)])

    assert code == 0
    assert output.read_text() == f'earlier=step\nscope={STACK_FILES}\n'
    (line,) = capsys.readouterr().out.splitlines()
    assert STACK_FILES in line


def test_the_recipient_path_is_the_file_the_stack_program_reads() -> None:
    assert (ROOT / RECIPIENT_FILE).samefile(ETCD_DRILL_RECIPIENT)
