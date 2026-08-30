"""The GitHub-secret slot: an Actions secret in a repository or in one Environment.

One of the register's storage channels (docs/credentials.md §1 rule 6), and the
only one CI itself reads. Three of the channels rule 6 lists are this one under
different names -- a CI Environment secret, an ops-repo secret and the `kluster`
repository secret differ in which repository they sit in and whether they name
an Environment, which is what `Slot` carries.

**Driven through the `gh` CLI, for the same reason the config slot is driven
through `pulumi`.** The API takes a secret as ciphertext -- the collection
publishes a Curve25519 public key, the caller seals the value to it and uploads
the box together with the id of the key it was sealed to -- and `gh secret set`
performs exactly that exchange. Doing it here instead would mean carrying a
second cryptographic runtime for one call site: the construction is
`X25519 -> HSalsa20 -> XSalsa20-Poly1305`, of which `cryptography` implements
only part, so the rest would be hand-written primitives in a repository that
has a standing rule against them (`age.py` states the one exception, and states
why an *encoding* is not a *primitive*). The tool that already ships the correct
implementation is the one to use, and AGENTS.md's "scripts are Python" is
satisfied the way it is by `pulumi_config.py`: the command is Python and the
tool is its subprocess.

The value is handed over on **standard input**, never as an argument, so a
credential never appears in the process table of a shared machine. The token
goes in the subprocess environment and is set explicitly rather than inherited:
`gh` reads `GH_TOKEN` before `GITHUB_TOKEN`, and this repository's own
`mise.toml` materializes the second from a workstation slot -- so a run that set
neither could authenticate as whichever value the ambient shell happened to
carry.

**Who fills these, and why it is neither the stack nor CI.** The pusher
authenticates as the `github` account root (`masters.py`) from the workstation.
The `github` stack declares the *structure* -- which repositories exist, which
Environments, which of them a reviewer gates -- and is applied by hand a few
times a year, while the values here rotate on their own cadence and some of them
are generated in Pulumi state after that stack last ran; a stack cannot push
what did not exist when it was applied. CI is worse: a workflow holding the
credential that writes its own Environment's secrets can rewrite the partition
confining it, which is the one property that partition exists to have.

**Reading a value back is impossible, and that is the API's design.** A pushed
secret is never disclosed again -- not to this script, not to a later run, not
to anyone holding the token. So verification is what the listing can show: the
name is there, and its `updatedAt` moved. That separates "the push happened"
from "the push was refused", which is the failure being guarded against; it
cannot separate a correct value from a corrupted one, and nothing on this
channel can.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess as sp
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: The tool that speaks this channel. Not pinned by this repository: it is an
#: operator's tool like `ssh`, and the two subcommands used here have been
#: stable for years.
GH = 'gh'

#: How long any one `gh` invocation may take. Each is one or two API calls, so
#: a slow one is a broken one rather than a big one.
TIMEOUT = 60


class Runner(Protocol):
    """How a `gh` invocation is made. Substituted in tests."""

    def __call__(self, args: Sequence[str], *, token: str, stdin: str | None) -> str: ...


def _why(completed: sp.CompletedProcess[str]) -> str:
    """One line for a refusal, with the cases worth acting on differently named.

    An operator answers "the token is wrong" and "the Environment is not there"
    in completely different places, and `gh` reports both as an HTTP status
    inside a sentence; naming them here is what keeps the answer in the error
    rather than in a second investigation.
    """
    detail = (completed.stderr or completed.stdout).strip()
    last = detail.splitlines()[-1] if detail else f'exit status {completed.returncode}'
    if 'HTTP 404' in detail:
        return f'{last} - no such repository or Environment; the `github` stack declares them'
    if 'HTTP 401' in detail or 'HTTP 403' in detail:
        return f'{last} - the GitHub account root was refused; it needs `repo` scope on that repository'
    return last


def run_gh(args: Sequence[str], *, token: str, stdin: str | None) -> str:
    """Run one `gh` command as the account root, returning its standard output.

    Both token variables are set: `gh` prefers `GH_TOKEN`, and leaving
    `GITHUB_TOKEN` alone would let whatever the shell already exported decide
    who this run is -- which inside this checkout is the very value `mise.toml`
    materializes from a workstation slot.
    """
    environment = {**os.environ, 'GH_TOKEN': token, 'GITHUB_TOKEN': token}
    try:
        completed = sp.run(
            [GH, *args],
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SlotRefused(f'`{GH}` is not installed; the GitHub-secret slots are pushed through it') from exc
    except sp.TimeoutExpired as exc:
        raise SlotRefused(f'`{GH} {" ".join(args)}` did not finish within {TIMEOUT}s') from exc
    if completed.returncode != 0:
        raise SlotRefused(f'`{GH} {" ".join(args)}` failed: {_why(completed)}')
    return completed.stdout


@dataclass(frozen=True)
class Slot:
    """One GitHub secret: a repository, a name, and optionally an Environment.

    An Environment secret is visible only to a job that names that Environment,
    which is what partitions the credentials by deployment layer (ci.md §3). A
    repository secret is visible to every workflow in the repository, and is
    therefore right only for a value whose whole power is small enough for that.
    """

    #: `owner/name`, as `gh --repo` spells it.
    repository: str
    name: str
    #: `None` for a repository secret.
    environment: str | None = None

    def __str__(self) -> str:
        where = f'{self.repository} ({self.environment})' if self.environment else self.repository
        return f'GitHub secret {where}: {self.name}'

    @property
    def scope(self) -> list[str]:
        """The flags that select this slot's collection.

        `--repo` is always given rather than relying on the working directory:
        the ops repository is a slot target too, and a command whose meaning
        depends on where it was run from is a command that fills the wrong
        repository exactly once.
        """
        scope = ['--repo', self.repository]
        if self.environment is not None:
            scope += ['--env', self.environment]
        return scope


@dataclass(frozen=True)
class Forge:
    """The forge's secret store, as the account root that may write it."""

    token: str
    run: Runner = run_gh

    def listing(self, slot: Slot) -> dict[str, str]:
        """Every secret name in this slot's collection, with when it last changed.

        Names and timestamps only -- the API discloses no value, which is what
        makes this the whole of verification (module docstring).
        """
        raw = self.run(['secret', 'list', *slot.scope, '--json', 'name,updatedAt'], token=self.token, stdin=None)
        try:
            listed: object = json.loads(raw.strip() or '[]')
        except ValueError as exc:
            raise SlotRefused(f'{slot}: the secret listing was not JSON') from exc
        # The shape is checked in the same step as the parse. Valid JSON that
        # is not a list of objects -- which is what `gh` prints when it has
        # something else to say -- would otherwise survive the guard above and
        # die on an index two lines later.
        if not isinstance(listed, list):
            raise SlotRefused(f'{slot}: the secret listing is a {type(listed).__name__}, not a list of secrets')
        found: dict[str, str] = {}
        for index, entry in enumerate(cast('list[object]', listed)):
            name = cast('dict[str, object]', entry).get('name') if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                raise SlotRefused(f'{slot}: entry {index} of the secret listing has no name, and is {entry!r}')
            found[name] = str(cast('dict[str, object]', entry).get('updatedAt', ''))
        return found

    def put(self, slot: Slot, value: str) -> None:
        """Store `value` in the slot; `gh` encrypts it on the way out.

        `--body` is deliberately not passed: it takes the value as an argument
        and has no standard-input spelling, and omitting it is exactly what
        makes `gh` read the value from the pipe. What is piped is written
        verbatim, so a trailing newline would become part of the secret.
        """
        _ = self.run(['secret', 'set', slot.name, *slot.scope], token=self.token, stdin=value)
