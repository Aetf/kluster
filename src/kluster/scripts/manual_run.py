"""What a run a person starts with this repository's tools shares with a CI job's (framework/pulumi.md §3.3).

A CI job starts from a fresh checkout of the forge's `main`, under a runner
whose home holds no Helm configuration. A run started on a workstation has
neither for free, and each tool that starts one takes both from here:

-   **`require_main`**: the working copy holds the forge's current `main`.
    A run from an older one plans against older code, and for a stack whose
    state is committed, against an older checkpoint too.
-   **`ci_helm`**: the environment a child is started with carries none of
    the caller's Helm settings, and Helm's homes are under a directory of
    the run's own, empty when the run starts. Left to the caller's, a
    repository that a workstation's `repositories.yaml` names with no cache
    reaches every chart a preview renders; CI's runner has neither.

`operator-stack` applies both around every run, and `update_crds` the Helm
half around every `helm` it starts.
"""

from __future__ import annotations

import enum
import shlex
import subprocess as sp
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Where the forge's `main` is read from, as `git ls-remote` names it.
REMOTE = 'origin'
MAIN = 'refs/heads/main'

#: How long one `git` or `jj` query may take. `ls-remote` talks to the forge.
TIMEOUT = 60

#: The prefixes of the caller's variables that steer Helm: Helm's own, read
#: by the CLI and by the SDK the Kubernetes provider embeds (`helmcli.New()`),
#: and the provider's overrides of the SDK's paths and driver.
HELM_STEERING = ('HELM_', 'PULUMI_K8S_HELM_')

#: The variables that put Helm's homes somewhere, each a directory under the
#: run's own, by the name `ci_helm` gives it. Every path Helm keeps --
#: `repositories.yaml`, the repository cache, the registry configuration,
#: the plugins -- defaults to one under them.
HELM_HOMES = {'HELM_CONFIG_HOME': 'config', 'HELM_CACHE_HOME': 'cache', 'HELM_DATA_HOME': 'data'}


class Unready(enum.Enum):
    """Why a working copy is not one a run may start from."""

    #: The forge's `main` answered with no commit.
    NO_MAIN = 'no main'
    #: The forge's `main` is not in this repository.
    MAIN_NOT_FETCHED = 'main not fetched'
    #: The forge's `main` is fetched and the working copy is not on it.
    BEHIND_MAIN = 'behind main'
    #: A file the run reads holds conflict markers: a committed checkpoint,
    #: which `operator_stack.checkpoint` checks after the ancestry.
    CONFLICTED = 'conflicted'
    #: A `git` or `jj` query failed, a stale working copy among them.
    UNREADABLE = 'unreadable'


#: A remedy: the commands that clear a refusal, in the order they run.
Remedy = tuple[tuple[str, ...], ...]


def spelled(remedy: Remedy) -> str:
    """`remedy` as a refusal's sentence names it: each command in backticks, in order."""
    return ', then '.join(f'`{shlex.join(command)}`' for command in remedy)


class CheckRefused(RuntimeError):
    """The working copy is not one a run may start from.

    `why` says which, and `remedy` is what clears it, as commands rather than
    words, so a reader can run them and a case can hold them to the tool's own
    grammar. The message names the same commands, spelled from `remedy`.
    """

    def __init__(self, message: str, *, why: Unready, remedy: Remedy = ()) -> None:
        super().__init__(message)
        self.why: Unready = why
        self.remedy: Remedy = remedy


# --------------------------------------------------------------------------
# The working copy holds the forge's main.
# --------------------------------------------------------------------------


def require_main(checkout: Path) -> None:
    """The forge's `main`, read without writing a ref, must be an ancestor of the working copy.

    `git merge-base --is-ancestor` answers 0 when it is, 1 when it is not, and
    128 when the commit is not in this repository at all — the forge's `main`
    never fetched. Every answer but 0 is a refusal, and the remedy is the
    step that is missing. A forge that cannot be read is a refusal too
    (`Unready.UNREADABLE`): a run that cannot say which `main` is current
    does not start.
    """
    listed = _run(['git', 'ls-remote', REMOTE, MAIN], checkout, what="reading the forge's main")
    fields = listed.split()
    if not fields:
        raise CheckRefused(
            f'`git ls-remote {REMOTE} {MAIN}` names no commit, so there is no main to start from', why=Unready.NO_MAIN
        )
    forge_main = fields[0]
    head = working_copy(checkout)
    answer = sp.run(
        ['git', 'merge-base', '--is-ancestor', forge_main, head],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    # Onto the forge's main as the remote bookmark names it, which is what was
    # checked: a local `main` that does not track it would leave the working
    # copy where it is.
    rebase = ('jj', 'rebase', '-b', '@', '-d', f'main@{REMOTE}')
    if answer.returncode == 1:
        remedy: Remedy = (rebase,)
        raise CheckRefused(
            f"the forge's main ({forge_main[:12]}) is fetched but the working copy is not on it: "
            f'{spelled(remedy)}, then run again',
            why=Unready.BEHIND_MAIN,
            remedy=remedy,
        )
    if answer.returncode != 0:
        remedy = (('jj', 'git', 'fetch'), rebase)
        raise CheckRefused(
            f"the forge's main ({forge_main[:12]}) is not in this repository: {spelled(remedy)}, then run again",
            why=Unready.MAIN_NOT_FETCHED,
            remedy=remedy,
        )


def working_copy(checkout: Path) -> str:
    """The commit the working copy is: `jj`'s `@` in a `jj` checkout, git's `HEAD` in a plain clone.

    `jj` snapshots the working copy first, as every `jj` command does, and
    refuses one that is stale — one whose `@` another workspace rewrote, by a
    rebase or by a fetch that abandoned what `@` sat on. The files on disk are
    then not `@`'s, and they are what the run reads, so a stale working copy
    is refused here rather than read past.
    """
    if (checkout / '.jj').is_dir():
        command = ['jj', 'log', '--no-graph', '-r', '@', '-T', 'commit_id']
    else:
        command = ['git', 'rev-parse', 'HEAD']
    return _run(command, checkout, what='reading the working copy').strip()


def _run(command: Sequence[str], cwd: Path, *, what: str) -> str:
    completed = sp.run(list(command), cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if completed.returncode != 0:
        raise CheckRefused(
            f'{what} failed: `{" ".join(command)}`: {completed.stderr.strip() or completed.returncode}',
            why=Unready.UNREADABLE,
        )
    return completed.stdout


# --------------------------------------------------------------------------
# CI's Helm environment.
# --------------------------------------------------------------------------


def ci_helm(base: Mapping[str, str], directory: Path) -> dict[str, str]:
    """`base` less every variable that steers Helm, with Helm's homes under `directory`.

    `directory` is the run's own, made empty for it and removed with it, so
    Helm finds no configuration, no repository and no cache there that the
    run did not write: what a CI runner's empty home gives a job. The caller's
    Helm variables go as well as its homes, because a variable that names one
    path outright -- `HELM_REPOSITORY_CONFIG`, or the provider's
    `PULUMI_K8S_HELM_REPOSITORY_CACHE` -- wins over the home that path would
    otherwise be under.
    """
    kept = {name: value for name, value in base.items() if not name.startswith(HELM_STEERING)}
    return kept | {name: str(directory / under) for name, under in HELM_HOMES.items()}
