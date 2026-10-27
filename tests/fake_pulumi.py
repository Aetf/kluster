"""A `pulumi` CLI that runs nothing and remembers everything.

Its own named module because more than one suite pushes into a config slot,
and because `conftest` is not a name an import can aim at (see `memory_kit`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kluster.scripts.credentials.pulumi_config import SlotRefused


@dataclass
class RecordedPulumi:
    """A `pulumi` that runs nothing and remembers everything."""

    stacks: list[str] = field(default_factory=list[str])
    config: dict[str, str] = field(default_factory=dict[str, str])
    invocations: list[list[str]] = field(default_factory=list[list[str]])
    #: What `config get` returns instead of what was set, for the one case
    #: that matters: a slot that took the value and did not keep it.
    corrupts: bool = False
    #: The keys of `config` written as secrets, which the key listing names
    #: without their values.
    secrets: set[str] = field(default_factory=set[str])
    #: Keys the stack holds whose `config get` is refused, as the CLI refuses a
    #: secret read under a passphrase that does not open it. Listed all the
    #: same: the listing decrypts nothing.
    undecryptable: set[str] = field(default_factory=set[str])

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        self.invocations.append(list(args))
        match list(args):
            case ['stack', 'ls', '--json']:
                return json.dumps([{'name': name} for name in self.stacks])
            case ['stack', 'init', name, '--no-select']:
                self.stacks.append(name)
                return ''
            case ['config', 'set', key, '--secret', '--stack', _]:
                assert stdin is not None, 'a secret was passed as an argument rather than on standard input'
                self.config[key] = stdin
                self.secrets.add(key)
                return ''
            case ['config', 'set', key, '--stack', _]:
                assert stdin is not None, 'a value named no argument and came on no standard input'
                self.config[key] = stdin
                return ''
            case ['config', 'set', key, value, '--stack', _]:
                if value.startswith('-'):
                    # What the pinned CLI does with an argument that opens
                    # with a dash: its flag parser takes it for a flag.
                    raise SlotRefused(f'`pulumi config set` failed: bad flag syntax: {value}')
                self.config[key] = value
                return ''
            case ['config', '--json', '--stack', name] if name not in self.stacks:
                # What the pinned CLI does for a stack the backend does not
                # hold: exit 6, naming the stack.
                raise SlotRefused(f"`pulumi config --json` failed: error: no stack named '{name}' found")
            case ['config', '--json', '--stack', _]:
                return json.dumps(self._listing(cwd))
            case ['config', 'get', key, '--stack', name]:
                if key in self.undecryptable:
                    raise SlotRefused(f'`pulumi config get {key}` failed: error: incorrect passphrase')
                if key not in self.config:
                    # What the real CLI does with a key the stack has no value
                    # for: exit non-zero, which `run_pulumi` turns into this.
                    # A `KeyError` here instead would make a caller's own
                    # refusal untestable.
                    raise SlotRefused(f'`pulumi config get {key}` failed: missing required configuration variable')
                return ('tampered' if self.corrupts else self.config[key]) + '\n'
            case unknown:  # pragma: no cover - an invocation the slot is not meant to make
                raise AssertionError(f'unexpected pulumi invocation {unknown}')

    def _listing(self, cwd: Path) -> dict[str, dict[str, object]]:
        """Every key set on the stack, as `pulumi config --json` prints them without `--show-secrets`.

        Each key in full: one set with no namespace is listed under the
        project's, the `name:` of the `Pulumi.yaml` the run starts beside. A
        secret is listed as `{"secret": true}` and nothing else, under any
        passphrase; a plain value with it. The real listing also carries the
        project-level `config:` block of `Pulumi.yaml`, with values; this one
        lists only what was set on the stack.
        """
        found = re.search(r'^name:\s*(\S+)\s*$', (cwd / 'Pulumi.yaml').read_text(), re.MULTILINE)
        assert found is not None, f'the Pulumi.yaml in {cwd} names no project'
        listing: dict[str, dict[str, object]] = {}
        for key in [*self.config, *self.undecryptable]:
            secret = key in self.secrets or key in self.undecryptable
            entry: dict[str, object] = {'secret': True} if secret else {'value': self.config[key], 'secret': False}
            listing[key if ':' in key else f'{found.group(1)}:{key}'] = entry
        return listing


#: What the pinned `pulumi` prints, as its last line, for every command run
#: against a `postgres://` state backend whose host name resolves nowhere, the
#: resolver's own address left out. A refused connection ends the same line
#: with `connect: connection refused` instead.
UNANSWERED = (
    'error: unable to open state database "postgres://operator@backend.invalid:5432/pulumi_state'
    '?sslmode=verify-full": failed to ping PostgreSQL: dial tcp: lookup backend.invalid: no such host'
)


@dataclass
class Unanswering:
    """A `pulumi` whose state backend does not answer: every run is refused, as `run_pulumi` refuses one."""

    refusals: list[SlotRefused] = field(default_factory=list[SlotRefused])

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        refusal = SlotRefused(f'`pulumi {" ".join(args)}` failed: {UNANSWERED}')
        self.refusals.append(refusal)
        raise refusal
