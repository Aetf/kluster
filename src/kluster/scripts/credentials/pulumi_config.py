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


class PassphraseMissing(SlotRefused):
    """A stack encrypted apart from the estate, on a machine holding no passphrase for it.

    Its own type because it is the one refusal here that is about the *machine*
    rather than about the stack's contents, and a caller that dresses a refusal
    up in its own words has to let this one through unchanged: "the credential
    is not in the config" and "this machine cannot read that config at all" send
    an operator to different places.
    """


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

#: The stack whose committed configuration is encrypted apart from the rest.
GITHUB_STACK = 'github'

#: Every stack of this project, by the name `pulumi stack select` takes. The
#: dispatch table in `kluster.stacks` is the authority; this is its restatement
#: for a layer that may import no stack program (the layering contract in
#: `pyproject.toml`), and a test holds the two equal (`tests/test_derived.py`).
#: What it is for is a delivery that takes a stack by name: a name outside
#: this set is refused before anything is minted, because the alternative is
#: `stack init` creating the misspelling in the backend and the mint filling
#: it while retiring the real stack's live credential by name (`derived`).
STACKS: frozenset[str] = frozenset({'physical', 'dns', 'k8s-base', 'apps', GITHUB_STACK})

#: Every stack that is **not** on the estate passphrase, and the register row
#: (§3) the one it *is* on comes from. A census rather than a consequence of
#: whatever a run happened to recover: a stack named here and handed no
#: passphrase of its own is refused *here*, by name, instead of being run under
#: the estate's — which `pulumi` would answer with `error: incorrect
#: passphrase`, a refusal that names neither the stack nor the fix and arrives
#: at the far end of whatever command was in progress.
#:
#: The value is the row rather than a sentence about it, so the refusal below
#: composes the command and a test can follow the row into the slot map. That
#: is what holds this honest: a stack cannot be taken off the estate passphrase
#: without a register row that generates and escrows one.
APART: Mapping[str, str] = {GITHUB_STACK: 'github-passphrase'}


@dataclass(frozen=True)
class BackendEnvironment:
    """What this machine can tell a `pulumi` run about the state backend.

    A closed shape rather than a mapping, because the key set is closed and
    because a caller reads the URL by name — a workstation without a client
    bundle has no URL, and that is a state to be handled rather than a key
    that happens to be missing from a bag. `variables()` is the one place it
    becomes the environment a process is started with, so an absent half
    is an absent variable rather than an empty one.

    **`variables` takes the stack it is building an environment for**, because
    `PULUMI_CONFIG_PASSPHRASE` is process-global while this installation has
    more than one: the `github` stack's configuration is encrypted under a
    passphrase of its own, which reaches no CI Environment and is what confines
    that stack to the workstation (credentials.md §2.2). A caller therefore
    cannot build "the environment" — only the environment for a named stack —
    and `Stack` derives it from its own name so that no call site can pair one
    stack with another's passphrase.
    """

    #: The estate passphrase. Neither it nor the mapping below prints: both
    #: hold passphrases, and the URL is what a repr is read for.
    passphrase: str | None = field(default=None, repr=False)
    url: str | None = None
    #: Stacks whose config is encrypted under a passphrase of their own, by
    #: stack name. A stack absent from here takes the estate's. A mapping and
    #: not a second field, so adding another such stack is a row rather than a
    #: branch — and so `apart` is the whole answer to "which stacks are not on
    #: the estate passphrase", which a test can read.
    apart: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)

    def variables(self, stack: str) -> dict[str, str]:
        passphrase = self.apart.get(stack)
        if passphrase is None and (row := APART.get(stack)) is not None:
            raise PassphraseMissing(
                f"the {stack} stack's configuration is encrypted under a passphrase of its own and this "
                f'machine holds none: run `credentials derived {row} generate`, or `credentials derived '
                f'{row} recover` on a machine that already holds the kit. The estate passphrase is '
                f'deliberately not used here — every CI Environment holds that one, and this stack is the '
                f'one nothing in CI may read (framework/github.md §1).'
            )
        values: dict[str, str] = {}
        if (chosen := passphrase or self.passphrase) is not None:
            values[PASSPHRASE_ENV] = chosen
        if self.url is not None:
            values[BACKEND_URL_ENV] = self.url
        return values


@dataclass(frozen=True)
class Stack:
    """One stack's committed configuration, as a slot that takes values.

    The environment every invocation runs with is derived **here**, from this
    stack's own name, rather than handed in ready-made. That is the whole
    guard against the trap `BackendEnvironment` describes: a passphrase is
    process-global, so a caller that built the variables itself could hand
    this one another stack's, and a `pulumi` run under the wrong passphrase is
    a class of bug worth making unreachable rather than merely unlikely.

    It would not be a *silent* bug — `encryptionsalt` is a verifier, so
    `pulumi` answers `error: incorrect passphrase` and exits non-zero without
    touching the file — but the refusal names neither the stack nor the fix,
    and a run that fails at the far end of a bring-up is expensive to read.
    """

    name: str
    directory: Path
    #: What this machine can say about the state backend, which the caller
    #: derives from the kit rather than expecting in the ambient environment.
    #: The stack's own passphrase is picked out of it by name below.
    environment: BackendEnvironment = field(default_factory=lambda: BackendEnvironment())
    run: Runner = run_pulumi

    @property
    def env(self) -> Mapping[str, str]:
        """The variables a `pulumi` run against *this* stack is started with."""
        return self.environment.variables(self.name)

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
        except json.JSONDecodeError as exc:
            # The size and where the parse stopped, never the text: this is a
            # dump whose values are secrets by design, and the refusal is
            # logged. Not `str(exc)` either -- two of the decoder's messages
            # quote the character they stopped on.
            raise SlotRefused(
                f'`pulumi stack output --json` did not print JSON: {len(raw)} characters, '
                f'and parsing stopped at line {exc.lineno} column {exc.colno}'
            ) from exc
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
