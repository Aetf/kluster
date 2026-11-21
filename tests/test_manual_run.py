"""CI's Helm environment, as `manual_run.ci_helm` gives it and as the pinned Helm reads it.

The caller of each case carries a Helm configuration of its own, the way a
workstation's shell does: homes elsewhere, and variables that name one of
Helm's paths outright. What a child started under `ci_helm` is handed holds
none of them, and the pinned `helm` itself, asked where it keeps every path
it reads, names one under the run's own directory. `update_crds`'s `helm`
runs are held to the same reading, through the function that starts them.

The main check, `manual_run.require_main`, is held where its callers are:
`test_operator_stack.py`, over a scratch forge and its clones.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pinned_tools
import process_sessions
import pytest

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
