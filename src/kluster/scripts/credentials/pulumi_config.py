"""The Pulumi config-secret slot: a value delivered into a stack's committed configuration.

One of the register's storage channels (docs/credentials.md §1 rule 6), and the
narrower of the two Pulumi ones: `Pulumi.<stack>.yaml` is committed, so its
ciphertext is public the moment the repository is, and only credentials a
program needs *before* it can run belong here. What lands is ciphertext under
the state passphrase, which is itself recovered with the kit (§2.2) — so a slot
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
from typing import Any, Protocol, cast

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


#: The two variables a `pulumi` run in this repository is given. Named,
#: because both the record below and the slot map spell them.
BACKEND_URL_ENV = 'PULUMI_BACKEND_URL'
PASSPHRASE_ENV = 'PULUMI_CONFIG_PASSPHRASE'


@dataclass(frozen=True)
class BackendEnvironment:
    """What this machine can tell a `pulumi` run about the state backend.

    A closed pair rather than a mapping, because the key set is closed and
    because a caller reads the URL by name — a workstation without a client
    bundle has no URL, and that is a state to be handled rather than a key
    that happens to be missing from a bag. `variables()` is the one place the
    pair becomes the environment a process is started with, so an absent half
    is an absent variable rather than an empty one.
    """

    passphrase: str | None = None
    url: str | None = None

    def variables(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.passphrase is not None:
            values[PASSPHRASE_ENV] = self.passphrase
        if self.url is not None:
            values[BACKEND_URL_ENV] = self.url
        return values


@dataclass(frozen=True)
class Stack:
    """One stack's committed configuration, as a slot that takes values."""

    name: str
    directory: Path
    #: What `BackendEnvironment.variables()` produced, which the caller derives
    #: from the kit rather than expecting in the ambient environment.
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    run: Runner = run_pulumi

    def _pulumi(self, *args: str, stdin: str | None = None) -> str:
        return self.run([*args], cwd=self.directory, env=self.env, stdin=stdin)

    def exists(self) -> bool:
        return self.name in self._stack_names(self._pulumi('stack', 'ls', '--json'))

    @staticmethod
    def _stack_names(printed: str) -> set[str]:
        """The names in `pulumi stack ls --json` output, or a refusal quoting it.

        The boundary for that command. A silent empty answer here reads as "no
        such stack", which makes `ensure` try to create one that exists.
        """
        try:
            listing: object = json.loads(printed or '[]')
        except ValueError as exc:
            raise SlotRefused(f'`pulumi stack ls --json` did not print JSON: {printed[:120]!r}') from exc
        if not isinstance(listing, list):
            raise SlotRefused(f'`pulumi stack ls --json` printed a {type(listing).__name__}, not a list of stacks')
        found: set[str] = set()
        for index, entry in enumerate(cast('list[object]', listing)):
            name = cast('dict[str, object]', entry).get('name') if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                raise SlotRefused(f'`pulumi stack ls --json` entry {index} carries no name, and is {entry!r}')
            found.add(name)
        return found

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

    def outputs(self) -> dict[str, Any]:
        """Every output of the stack's current state, secrets included.

        The other direction of this module: what a *program* generated, rather
        than what one needs to start. `--show-secrets` is what makes reading a
        secret output possible at all — without it the value comes back as the
        literal string `[secret]`, which a caller would deliver as if it were
        the secret.
        """
        raw = self._pulumi('stack', 'output', '--json', '--show-secrets', '--stack', self.name).strip()
        try:
            parsed: object = json.loads(raw or '{}')
        except ValueError as exc:
            raise SlotRefused(f'`pulumi stack output --json` did not print JSON: {raw[:120]!r}') from exc
        if not isinstance(parsed, dict):
            raise SlotRefused(f'`pulumi stack output --json` printed a {type(parsed).__name__}, not an object')
        return cast('dict[str, Any]', parsed)

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

    def fill(self, *, secret: Mapping[str, str], plain: Mapping[str, str], holds: str) -> None:
        """Fill this stack's committed configuration, and say what has to be committed.

        Every register row delivered to a stack closes the same way, and the
        closing is the half an operator acts on: the stack has to exist before
        it has a configuration to set, the secret half goes in encrypted and the
        plain half readable, and the run ends by naming the file that publishes
        the slot. A push that stopped at `pulumi config set` would leave the
        credential live in the provider and invisible to everyone else's
        checkout.
        """
        self.ensure()
        for key, value in secret.items():
            self.set_secret(key, value)
        for key, value in plain.items():
            self.set(key, value)
        log.info('the %s stack holds %s; commit Pulumi.%s.yaml to publish the slot', self.name, holds, self.name)
