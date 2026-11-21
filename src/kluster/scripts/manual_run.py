"""What a run a person starts with this repository's tools shares with a CI job's (framework/pulumi.md §3.3, §3.4).

A CI job starts from a fresh checkout of the forge's `main`, under a runner
whose home holds no Helm configuration, with the stack's own passphrase and
the estate's backend set on the step that runs `pulumi`. A run started on a
workstation has none of these for free, and each tool that starts one takes
them from here:

-   **`require_main`**: the working copy holds the forge's current `main`.
    A run from an older one plans against older code, and for a stack whose
    state is committed, against an older checkpoint too.
-   **`ci_helm`**: the environment a child is started with carries none of
    the caller's Helm settings, and Helm's homes are under a directory of
    the run's own (`helm_homes`), empty when the run starts. Left to the
    caller's, a repository that a workstation's `repositories.yaml` names
    with no cache reaches every chart a preview renders; CI's runner has
    neither.
-   **`Environments`**: the environment of a `pulumi` run against a stack CI
    deploys, chosen per stack by `credentials`'s `BackendEnvironment`, the
    one place that maps a stack to its passphrase, under `ci_helm`. A run
    whose environment names no backend is refused (`NoBackend`).
-   **`attached`**: a command on the operator's terminal, its standard
    input, output and error the terminal's, with a ^C left to it.
-   **`stack_word`**: a word of a passed-through `pulumi` command that names
    a stack, which a tool that runs one stack refuses; and `elsewhere_word`,
    one that points `pulumi` at another program or configuration.

`operator-stack` applies the main check and the Helm half around every run,
and `update_crds` the Helm half around every `helm` it starts. `ci-stack`
applies all of them around every run of a stack CI deploys, the drill around
each command of a rebuild, and `perf record` takes the environment and the
main check for its read of `physical`.
"""

from __future__ import annotations

import contextlib
import enum
import shlex
import shutil
import signal
import subprocess as sp
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kluster.lib import stack_environment
from kluster.scripts.credentials import oci_recovery, pulumi_config

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


class HelmInCheckout(stack_environment.EnvironmentRefused):
    """The temporary directory for Helm's homes resolves inside the checkout, where `jj` would snapshot it."""


@contextlib.contextmanager
def helm_homes(checkout: Path, *, prefix: str) -> Generator[Path]:
    """A directory of the run's own for Helm's homes: empty, outside `checkout`, and removed on the way out.

    One that resolves inside `checkout` is refused (`HelmInCheckout`), since
    what Helm writes there would enter the next `jj` snapshot. The directory
    goes on every way out the caller lives through; a caller ended by a signal
    it does not handle (SIGKILL, SIGTERM, a hang-up) leaves it in the
    temporary directory.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        if directory.resolve().is_relative_to(checkout.resolve()):
            raise HelmInCheckout(
                f'the temporary directory {directory} is inside {checkout}, where `jj` would snapshot what Helm '
                'writes: set TMPDIR outside the checkout'
            )
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------
# The environment of a `pulumi` run, per stack.
# --------------------------------------------------------------------------

#: The variable that names a passphrase file, which `pulumi` refuses beside
#: `PULUMI_CONFIG_PASSPHRASE`, and which a caller's shell may export.
PASSPHRASE_FILE_ENV = 'PULUMI_CONFIG_PASSPHRASE_FILE'


class NoBackend(stack_environment.EnvironmentRefused):
    """A `pulumi` run's environment names no backend, so `pulumi` would fall back to the one its own login names."""


@dataclass
class Environments:
    """The environment of each command a run starts: the caller's own, per stack for `pulumi`, under CI's Helm.

    `base` is the environment `mise x` started the caller with, whose
    `PULUMI_CONFIG_PASSPHRASE` is the stack passphrase and whose
    `PULUMI_BACKEND_URL` is the estate's backend, both from the checkout's
    slots. `helm` is the run's own directory for Helm's homes, empty when
    the run starts (`helm_homes`).
    """

    base: Mapping[str, str] = field(repr=False)
    helm: Path
    #: Where `physical`'s passphrase is read: its workstation slot, refused
    #: with the command that fills it where it holds none.
    physical: Callable[[], str] = oci_recovery.slot_passphrase

    def backend(self) -> pulumi_config.BackendEnvironment:
        passphrase = self.base.get(stack_environment.PASSPHRASE_ENV)
        return pulumi_config.BackendEnvironment(
            passphrase=(lambda: passphrase) if passphrase else None, physical=self.physical
        )

    def pulumi(self, stack: str) -> dict[str, str]:
        """The environment of a `pulumi` run against `stack`; `NoBackend` where it names no backend.

        `physical`'s passphrase where the stack is `physical`, the caller's
        stack passphrase for every other stack CI deploys, and never a
        passphrase file of the caller's, which `pulumi` would refuse beside
        the passphrase. An empty `PULUMI_BACKEND_URL` is none: it is what
        `mise.toml` renders where the checkout holds no backend slot.
        """
        own = {name: value for name, value in self.base.items() if name != PASSPHRASE_FILE_ENV}
        env = ci_helm(own | self.backend().variables(stack), self.helm)
        if not env.get(stack_environment.BACKEND_URL_ENV):
            raise NoBackend(
                f'the environment of a run against {stack} names no {stack_environment.BACKEND_URL_ENV}, so '
                "`pulumi` would fall back to the backend its own login names. `mise x` sets it from the checkout's "
                'state-backend slot: run through `mise x`, from the checkout that holds .credentials/'
            )
        return env

    def tool(self) -> dict[str, str]:
        """The environment of every other command."""
        return ci_helm(self.base, self.helm)


# --------------------------------------------------------------------------
# A command on the operator's terminal.
# --------------------------------------------------------------------------


def attached(argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> int:
    """Run `argv` on the terminal, its standard streams this process's, and return its exit.

    **A ^C is the command's to answer.** The terminal sends SIGINT to the
    whole foreground process group, the command included, and `pulumi`
    answers the first one by cancelling gracefully: it finishes the steps in
    flight, records them, and releases its lock. So while the command runs,
    this process ignores SIGINT and waits for it rather than killing it
    partway through that cancel. The command gets SIGINT back at its default
    before it starts (`default_sigint`), since an ignored disposition is
    inherited across `exec`: a program that installs no handler of its own
    would otherwise never see the ^C.

    A command killed by signal N exits 128+N, as a shell reports it, rather
    than `subprocess`'s -N, which a caller's own `sys.exit` would turn into
    256-N (`exit_status`). A command not found raises `FileNotFoundError`, as `subprocess`
    does.
    """
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with sp.Popen(list(argv), cwd=cwd, env=dict(env), preexec_fn=default_sigint) as child:
            return exit_status(child.wait())
    finally:
        _ = signal.signal(signal.SIGINT, previous)


def exit_status(code: int) -> int:
    """`subprocess`'s return code as the exit status a shell reports: 128+N for a child killed by signal N."""
    return 128 - code if code < 0 else code


def default_sigint() -> None:
    """In a child about to `exec`: SIGINT back at its default, whatever disposition it inherited."""
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


# --------------------------------------------------------------------------
# A passed-through command that names a stack.
# --------------------------------------------------------------------------


def stack_word(args: Sequence[str]) -> str | None:
    """The first word of a passed-through `pulumi` command that names a stack, or may; None where none does.

    A tool that runs one stack takes it as its own argument and adds it, so a
    word that names a stack is refused rather than overridden: a caller who
    wrote one meant some other stack. Such a word is `--stack` in either
    spelling, a single-dash cluster holding an `s` -- `-s dev`, `-sdev` and
    `-ys dev` all reach the `-s` shorthand -- and, in a `config` command, a
    `--dest` or a cluster holding its `-d` shorthand: `config copy` (alias
    `cp`) names the stack it copies into with it. Only the words ahead of a
    `--` are read, past which a word is an argument rather than a flag.

    **The `config` command is found among its words wherever it stands**,
    since `pulumi` takes a flag anywhere among them, global ones ahead of the
    command included. A flag's value written as a word of its own
    (`--color never`) is among the words too, because which flags take a
    value is `pulumi`'s to know. And the destination is refused under any
    `config` subcommand rather than under a list of `copy`'s names, so an
    alias a release adds does not reopen it: no other `config` subcommand
    takes either flag.
    """
    flags = _flags(args)
    copies = 'config' in (arg for arg in flags if not arg.startswith('-'))
    for arg in flags:
        if (
            _short_cluster(arg, 's')
            or _long(arg, '--stack')
            or (copies and (_short_cluster(arg, 'd') or _long(arg, '--dest')))
        ):
            return arg
    return None


def elsewhere_word(args: Sequence[str]) -> str | None:
    """The first word of a passed-through `pulumi` command that points it at another program or configuration.

    `-C`/`--cwd` runs `pulumi` as if started in another directory, so on
    whatever program and stack files that directory holds, which the main
    check of this checkout says nothing about; `--config-file` reads a
    stack's configuration from another file, another stack's among them; and
    `--client` points the engine at a language host already running, so the
    program is whatever that host runs. A single-dash cluster holding a `C`
    reaches the `-C` shorthand. Only the words ahead of a `--` are read.
    """
    for arg in _flags(args):
        if _short_cluster(arg, 'C') or any(_long(arg, name) for name in ('--cwd', '--config-file', '--client')):
            return arg
    return None


def with_stack(args: Sequence[str], stack: str) -> list[str]:
    """`args` with `--stack <stack>` ahead of any `--`, past which a word is not a flag."""
    args = list(args)
    at = args.index('--') if '--' in args else len(args)
    return [*args[:at], '--stack', stack, *args[at:]]


def _flags(args: Sequence[str]) -> list[str]:
    """The words of `args` ahead of a `--`."""
    return list(args[: args.index('--')] if '--' in args else args)


def _long(arg: str, name: str) -> bool:
    return arg == name or arg.startswith(f'{name}=')


def _short_cluster(arg: str, letter: str) -> bool:
    return arg.startswith('-') and not arg.startswith('--') and letter in arg[1:]
