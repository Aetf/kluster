"""Running the `pulumi` CLI from outside a stack program.

Two callers run it: the `credentials` commands, which write a stack's
committed configuration, and the appliance's restore, which asks a backend
which stacks it serves (`kluster.lib.state_backend.state`). Both run the pinned
CLI (`mise.toml`) rather than the automation API, and both need the checkout
that holds `Pulumi.yaml`, so the runner and that lookup live here, below both.

A refusal is `PulumiRefused`. Each caller says what a refusal means in its own
terms: the `credentials` package turns it into a slot that would not take the
value (`SlotRefused`), the restore into a step that did not happen.
"""

from __future__ import annotations

import os
import subprocess as sp
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from kluster.lib import workstation

#: How long any one `pulumi` invocation may take. Every command talks to the
#: state backend, so this is a network timeout rather than a formality.
TIMEOUT = 120


class PulumiRefused(RuntimeError):
    """A `pulumi` run failed, or the checkout it runs in could not be found."""


class Runner(Protocol):
    """How a `pulumi` invocation is made. Substituted in tests."""

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str: ...


def run_pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
    """Run one `pulumi` command, returning its standard output.

    `env` is overlaid on the caller's environment rather than replacing it:
    `pulumi` needs a home directory and a PATH like any other tool, and what
    the caller adds is the backend URL and the passphrase that open the state.
    A secret value travels on `stdin`: an argument would put it in the process
    table of a shared machine.
    """
    completed = sp.run(
        ['pulumi', *args, '--non-interactive'],
        cwd=cwd,
        env={**os.environ, **env},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise PulumiRefused(f'`pulumi {" ".join(args)}` failed: {detail[-1] if detail else completed.returncode}')
    return completed.stdout


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
