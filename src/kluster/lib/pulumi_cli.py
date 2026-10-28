"""Running the `pulumi` CLI from outside a stack program.

Two callers run it: the `credentials` commands, which write a stack's
committed configuration, and the appliance's restore, which asks a backend
which stacks it serves (`kluster.lib.state_backend.state`). Both run the pinned
CLI (`mise.toml`) rather than the automation API, and both need the checkout
that holds `Pulumi.yaml`, so the runner and that lookup live here, below both.

A refusal is `PulumiRefused`, a run that outlasts its bound included, which is
refused as its own kind of it (`StoppedAtBound`). Each caller says what a
refusal means in its own terms: the `credentials` package turns it into a
slot that would not take the value (`SlotRefused`), and a run stopped at its
bound into a backend that did not answer in time; the restore turns either
into a step that did not happen.
"""

from __future__ import annotations

import logging
import os
import subprocess as sp
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from kluster.lib import workstation

log = logging.getLogger(__name__)

#: How long any one `pulumi` invocation may take unless its caller says
#: otherwise. Every command talks to the state backend, so this is a network
#: timeout rather than a formality: against a backend whose traffic is
#: dropped -- an unroutable address, a box that is down behind a firewall that
#: does not reject -- the CLI gives up only once TCP does, later than this.
TIMEOUT = 120

#: The CLI's own variable naming the backend a run reads and writes.
BACKEND_URL_ENV = 'PULUMI_BACKEND_URL'


class PulumiRefused(RuntimeError):
    """A `pulumi` run failed, or the checkout it runs in could not be found."""


class StoppedAtBound(PulumiRefused):
    """A `pulumi` run still going at its bound, and stopped there.

    Its own type because it says nothing about the stack the run asked about:
    no answer came back at all, which is how a backend whose traffic is
    dropped looks from here. Asking that backend anything else is another
    wait of the same length for the same silence, so a caller that would ask
    again after a refusal tells this one apart by type.
    """


class Runner(Protocol):
    """How a `pulumi` invocation is made. Substituted in tests."""

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str: ...


def run_pulumi(
    args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None, timeout: float | None = None
) -> str:
    """Run one `pulumi` command, returning its standard output.

    `env` is overlaid on the caller's environment rather than replacing it:
    `pulumi` needs a home directory and a PATH like any other tool, and what
    the caller adds is the backend URL and the passphrase that open the state.
    A secret value travels on `stdin`: an argument would put it in the process
    table of a shared machine.

    **A run still going after `timeout` seconds is killed and refused**, as
    `StoppedAtBound` naming the command, the backend and the bound, which is
    how a backend whose traffic is dropped looks from here. The line logged
    before the run names the same three, so the wait has an explanation
    while it lasts. With no `timeout` the bound is `TIMEOUT`, read when the
    run starts, so a harness that replaces the constant bounds every run.
    """
    bound = TIMEOUT if timeout is None else timeout
    command = f'`pulumi {" ".join(args)}`'
    environment = {**os.environ, **env}
    backend = backend_named(environment.get(BACKEND_URL_ENV))
    log.info('running %s against %s, stopped if it has not finished in %gs', command, backend, bound)
    try:
        completed = sp.run(
            ['pulumi', *args, '--non-interactive'],
            cwd=cwd,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=bound,
            check=False,
        )
    except sp.TimeoutExpired as exc:
        raise StoppedAtBound(
            f'{command} against {backend} did not finish within its bound of {bound:g}s and was stopped; a '
            'backend whose traffic is dropped -- an unroutable address, a box that is down behind a firewall that '
            'does not reject -- ends a run this way'
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise PulumiRefused(f'{command} failed: {detail[-1] if detail else completed.returncode}')
    return completed.stdout


def backend_named(url: str | None) -> str:
    """The backend `url` names, as a log line or a refusal may say it.

    The scheme and the host, with its port: the parts that say which backend
    it was and that carry no credential. Everything else is left out, since
    a URL can carry a password beside its user and a query can carry one
    too (libpq reads `password=` there). A `file://` backend has no host, so
    it is named by the directory it is rooted at, a leading `~` or `.`
    included, which the URL reader takes for a host.

    **A URL whose host cannot be told apart from its user information is
    named by nothing in it.** The host ends at the first `/`, `?` or `#`, so
    a password holding one of them puts the `@` after it, and what reads as
    the host is the user and the start of the password. Any `@` past the
    host is taken for that, an `@` a query merely carries included. An empty
    value names no backend, which is how a shell leaves one unset as often
    as by leaving it out.
    """
    if not url:
        return f"the backend the CLI's own login names, no {BACKEND_URL_ENV} being set"
    try:
        parts = urlsplit(url)
    except ValueError:
        return f'a {BACKEND_URL_ENV} that does not parse as a URL'
    past_host = parts.path + parts.query + parts.fragment
    if '@' in past_host or (parts.scheme == 'file' and '@' in parts.netloc):
        return f'a {BACKEND_URL_ENV} whose host cannot be told apart from its user information'
    if parts.scheme == 'file':
        return f'file://{parts.netloc}{parts.path}'
    return f'{parts.scheme}://{parts.netloc.rpartition("@")[2]}'


def project_dir() -> Path:
    """The checkout holding `Pulumi.yaml` — where a stack's configuration lives.

    The checkout this package runs from (`workstation.repo_root`), so a command
    works from any working directory: the file it reads or writes is a file in
    *this* repository, not in whatever tree the operator happens to stand in.
    Refused as `PulumiRefused` whichever way it fails, because that is the one
    refusal a caller of this module translates into its own.
    """
    try:
        root = workstation.repo_root()
    except workstation.WorkstationError as exc:
        raise PulumiRefused(
            f'the config slots live in a checkout of this repository, and none was found: {exc}'
        ) from exc
    if not (root / 'Pulumi.yaml').is_file():
        raise PulumiRefused(f'no Pulumi.yaml in the checkout at {root}; the config slots live beside it')
    return root
