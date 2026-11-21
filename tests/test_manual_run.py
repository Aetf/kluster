"""CI's Helm environment and the per-stack environment, as `manual_run` gives them.

The caller of each case carries a Helm configuration of its own, the way a
workstation's shell does: homes elsewhere, and variables that name one of
Helm's paths outright. What a child started under `ci_helm` is handed holds
none of them, and the pinned `helm` itself, asked where it keeps every path
it reads, names one under the run's own directory. `update_crds`'s `helm`
runs are held to the same reading, through the function that starts them.
A `pulumi` run's environment (`Environments`) carries the stack's own
passphrase and the caller's backend, and one that names no backend is
refused.

The main check, `manual_run.require_main`, the attached run and the refusal
of a word that names a stack are held where their callers are:
`test_operator_stack.py` and `test_ci_stack.py`, over a scratch forge and its
clones, and `test_drill.py`.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import pinned_tools
import process_sessions
import pytest

from kluster.conventions import identity
from kluster.scripts import manual_run
from kluster.scripts.update_crds import sources

#: A caller's Helm configuration: homes of its own, a variable naming
#: `repositories.yaml` outright, which wins over any home, and the
#: Kubernetes provider's override of the repository cache.
CALLER = {
    'HELM_CONFIG_HOME': '/caller/helm-config',
    'HELM_CACHE_HOME': '/caller/helm-cache',
    'HELM_DATA_HOME': '/caller/helm-data',
    'HELM_REPOSITORY_CONFIG': '/caller/repositories.yaml',
    'HELM_REPOSITORY_CACHE': '/caller/repository-cache',
    'HELM_KUBECONTEXT': 'the-callers-context',
    'PULUMI_K8S_HELM_REPOSITORY_CACHE': '/caller/provider-cache',
    'PULUMI_K8S_HELM_REPOSITORY_CONFIG_PATH': '/caller/provider-repositories.yaml',
}

#: The variables of `helm env` that name a path Helm reads or writes.
HELM_PATHS = (
    'HELM_CACHE_HOME',
    'HELM_CONFIG_HOME',
    'HELM_DATA_HOME',
    'HELM_PLUGINS',
    'HELM_REGISTRY_CONFIG',
    'HELM_REPOSITORY_CACHE',
    'HELM_REPOSITORY_CONFIG',
)

#: A child that prints the environment it was started with.
PRINT_ENVIRONMENT = 'import json, os; print(json.dumps(dict(os.environ)))'


@pytest.fixture(scope='module')
def pinned_helm() -> Path:
    """The pinned `helm`, refused when missing rather than skipped."""
    return Path(pinned_tools.located(sources.HELM))


def _helm_env(printed: str) -> dict[str, str]:
    """`helm env`'s `NAME="value"` lines, by name."""
    found: dict[str, str] = {}
    for line in printed.splitlines():
        name, separator, value = line.partition('=')
        if separator:
            found[name] = json.loads(value)
    return found


def _under(path: str, directory: Path) -> bool:
    return Path(path).is_relative_to(directory)


def test_a_child_under_ci_helm_is_handed_none_of_the_callers_helm_settings(tmp_path: Path) -> None:
    directory = tmp_path / 'run'
    directory.mkdir()

    environment = manual_run.ci_helm(os.environ | CALLER, directory)
    handed = json.loads(
        process_sessions.run(
            [sys.executable, '-c', PRINT_ENVIRONMENT], env=environment, text=True, check=True, timeout=60
        ).stdout
    )

    assert {name: value for name, value in handed.items() if value in CALLER.values()} == {}
    assert handed['PATH'] == os.environ['PATH']
    homes = [Path(handed[name]) for name in ('HELM_CONFIG_HOME', 'HELM_CACHE_HOME', 'HELM_DATA_HOME')]
    assert all(home.parent == directory for home in homes)
    assert len(set(homes)) == len(homes)


def test_the_pinned_helm_under_ci_helm_keeps_every_path_under_the_runs_directory(
    pinned_helm: Path, tmp_path: Path
) -> None:
    # The pinned release is the one the Kubernetes provider's plugin embeds
    # (framework/pulumi.md §4), so its reading is the provider's too.
    directory = tmp_path / 'run'
    directory.mkdir()

    printed = process_sessions.run(
        [str(pinned_helm), 'env'],
        env=manual_run.ci_helm(os.environ | CALLER, directory),
        text=True,
        check=True,
        timeout=60,
    ).stdout

    paths = {name: _helm_env(printed)[name] for name in HELM_PATHS}
    assert {name: path for name, path in paths.items() if not _under(path, directory)} == {}


def test_update_crds_runs_helm_under_ci_helm(
    pinned_helm: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in CALLER.items():
        monkeypatch.setenv(name, value)
    workdir = tmp_path / 'work'
    workdir.mkdir()

    printed = sources._helm(pinned_helm, ['env'], workdir=workdir)  # pyright: ignore[reportPrivateUsage]
    paths = {name: _helm_env(printed)[name] for name in HELM_PATHS}

    assert {name: path for name, path in paths.items() if not _under(path, workdir)} == {}


#: What `mise x` starts a run with: the stack passphrase and the estate's
#: backend, a passphrase file a shell exported, and a Helm configuration.
BASE = {
    'PULUMI_CONFIG_PASSPHRASE': 'the-stack-passphrase',
    'PULUMI_CONFIG_PASSPHRASE_FILE': '/elsewhere/passphrase',
    'PULUMI_BACKEND_URL': 'postgres://operator@192.0.2.10:5432/pulumi_state',
    'HELM_REPOSITORY_CONFIG': '/caller/repositories.yaml',
    'PULUMI_K8S_HELM_REPOSITORY_CACHE': '/caller/cache',
}


def test_every_command_runs_under_cis_helm_environment_and_its_stacks_passphrase(tmp_path: Path) -> None:
    helm = tmp_path / 'helm'
    environments = manual_run.Environments(base=BASE, helm=helm, physical=lambda: 'physicals-own-passphrase')
    names = identity.STACK_NAMES

    handed = {stack: environments.pulumi(stack) for stack in (names.physical, names.dns, names.apps)}

    assert {stack: env.get('PULUMI_CONFIG_PASSPHRASE') for stack, env in handed.items()} == {
        names.physical: 'physicals-own-passphrase',
        names.dns: 'the-stack-passphrase',
        names.apps: 'the-stack-passphrase',
    }
    assert ['PULUMI_CONFIG_PASSPHRASE_FILE' in env for env in handed.values()] == [False] * len(handed)
    assert {env['PULUMI_BACKEND_URL'] for env in handed.values()} == {BASE['PULUMI_BACKEND_URL']}
    for env in [*handed.values(), environments.tool()]:
        assert {name for name in env if name.startswith(manual_run.HELM_STEERING)} == set(manual_run.HELM_HOMES)
        assert all(Path(env[name]).is_relative_to(helm) for name in manual_run.HELM_HOMES)


@pytest.mark.parametrize('backend', [None, ''], ids=['absent', 'empty'])
def test_a_pulumi_environment_that_names_no_backend_is_refused(tmp_path: Path, backend: str | None) -> None:
    base = {name: value for name, value in BASE.items() if name != 'PULUMI_BACKEND_URL'}
    if backend is not None:
        base['PULUMI_BACKEND_URL'] = backend
    environments = manual_run.Environments(base=base, helm=tmp_path, physical=lambda: 'physicals-own-passphrase')

    for stack in (identity.STACK_NAMES.physical, identity.STACK_NAMES.dns):
        with pytest.raises(manual_run.NoBackend, match='names no PULUMI_BACKEND_URL'):
            _ = environments.pulumi(stack)


def test_helms_homes_are_an_empty_directory_of_the_runs_own_removed_with_it(tmp_path: Path) -> None:
    with manual_run.helm_homes(tmp_path / 'checkout', prefix='case-') as directory:
        assert list(directory.iterdir()) == []
        _ = (directory / 'repositories.yaml').write_text('written by the run\n')

    assert not directory.exists()


def test_helms_homes_inside_the_checkout_are_refused_leaving_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / 'checkout'
    inside = checkout / 'tmp'
    inside.mkdir(parents=True)
    monkeypatch.setenv('TMPDIR', str(inside))
    monkeypatch.setattr(tempfile, 'tempdir', None)

    with (
        pytest.raises(manual_run.HelmInCheckout, match='set TMPDIR outside the checkout'),
        manual_run.helm_homes(checkout, prefix='case-'),
    ):
        pass

    assert list(inside.iterdir()) == []


@pytest.mark.parametrize(
    'args',
    [
        ['--color', 'never', 'config', 'copy', '-d', 'dns'],
        ['-v', '1', 'config', 'cp', '--dest', 'dns'],
        ['config', 'copy', '--dest=dns'],
    ],
    ids=['global-copy-d', 'global-cp-dest', 'copy-dest='],
)
def test_a_config_copy_destination_is_a_stack_word_wherever_global_flags_put_config(args: list[str]) -> None:
    assert manual_run.stack_word(args) is not None


@pytest.mark.parametrize(('code', 'status'), [(0, 0), (1, 1), (-signal.SIGTERM, 128 + signal.SIGTERM)])
def test_a_child_killed_by_signal_n_has_the_exit_status_128_plus_n(code: int, status: int) -> None:
    assert manual_run.exit_status(code) == status
