"""The Pulumi config-secret slot: a value delivered into a stack's committed configuration.

One of the register's storage channels (docs/credentials.md §1 rule 6), and the
narrower of the two Pulumi ones: `Pulumi.<stack>.yaml` is committed, so its
ciphertext is public the moment the repository is, and only credentials a
program needs *before* it can run belong here. What lands is ciphertext under
the state passphrase, which is itself derived from the kit (§2.2) — so a slot
written here opens from the kit and from nothing else.

Driven through the `pulumi` CLI rather than the automation API because that is
what writes the file the operator then commits, and because the CLI is already
the pinned tool every other Pulumi step in this repository uses (`mise.toml`).
A secret value is handed over on standard input: an argument would put the
credential in the process table of a shared machine.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess as sp
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: How long any one `pulumi` invocation may take. Config commands talk to the
#: state backend, so this is a network timeout rather than a formality.
TIMEOUT = 120


class SlotRefused(RuntimeError):
    """A slot would not take the value — the push failed, so nothing consumed it."""


class Runner(Protocol):
    """How a `pulumi` invocation is made. Substituted in tests."""

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str: ...


def run_pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
    """Run one `pulumi` command, returning its standard output.

    `env` is overlaid on the caller's environment rather than replacing it:
    `pulumi` needs a home directory and a PATH like any other tool, and what
    the caller adds is the backend URL and the passphrase that open the state.
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
        raise SlotRefused(f'`pulumi {" ".join(args)}` failed: {detail[-1] if detail else completed.returncode}')
    return completed.stdout


def project_dir() -> Path:
    """The checkout holding `Pulumi.yaml` — where a stack's configuration lives.

    Found by walking up from this module, so the command works from any working
    directory: the file it writes is a file in *this* repository, not in
    whatever tree the operator happens to stand in.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / 'Pulumi.yaml').is_file():
            return candidate
    raise SlotRefused('no Pulumi.yaml above this module; the config slots live in a checkout of this repository')


@dataclass(frozen=True)
class Stack:
    """One stack's committed configuration, as a slot that takes values."""

    name: str
    directory: Path
    #: `PULUMI_BACKEND_URL` and `PULUMI_CONFIG_PASSPHRASE`, which the caller
    #: derives from the kit rather than expecting in the ambient environment.
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    run: Runner = run_pulumi

    def _pulumi(self, *args: str, stdin: str | None = None) -> str:
        return self.run([*args], cwd=self.directory, env=self.env, stdin=stdin)

    def exists(self) -> bool:
        listing = list[dict[str, Any]](json.loads(self._pulumi('stack', 'ls', '--json')))
        return any(str(stack.get('name')) == self.name for stack in listing)

    def ensure(self) -> None:
        """Create the stack if the backend has none of that name.

        `--no-select` because a credentials run is not a development session:
        which stack the operator had selected is theirs, and a push must not
        change it under them.
        """
        log.info('checking the state backend for the %s stack', self.name)
        if self.exists():
            return
        log.info('no %s stack yet; creating it', self.name)
        _ = self._pulumi('stack', 'init', self.name, '--no-select')

    def get(self, key: str) -> str:
        return self._pulumi('config', 'get', key, '--stack', self.name).strip()

    def set(self, key: str, value: str) -> None:
        """Write a non-secret key, in plain text in the committed file."""
        log.info('setting %s on the %s stack', key, self.name)
        _ = self._pulumi('config', 'set', key, value, '--stack', self.name)

    def set_secret(self, key: str, value: str) -> None:
        """Write a secret key, and read it back to prove the slot holds it.

        The read-back is what makes the push verifiable at all: the file gains
        ciphertext either way, and the only thing that distinguishes a delivered
        credential from a corrupted one is decrypting it again.
        """
        log.info('encrypting %s into the %s stack config', key, self.name)
        _ = self._pulumi('config', 'set', key, '--secret', '--stack', self.name, stdin=value)
        if self.get(key) != value:
            raise SlotRefused(f'{key} on the {self.name} stack does not decrypt to what was just written')
