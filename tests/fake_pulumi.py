"""A `pulumi` CLI that runs nothing and remembers everything.

Its own named module because more than one suite pushes into a config slot,
and because `conftest` is not a name an import can aim at (see `memory_kit`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RecordedPulumi:
    """A `pulumi` that runs nothing and remembers everything."""

    stacks: list[str] = field(default_factory=list[str])
    config: dict[str, str] = field(default_factory=dict[str, str])
    invocations: list[list[str]] = field(default_factory=list[list[str]])
    #: What `config get` returns instead of what was set, for the one case
    #: that matters: a slot that took the value and did not keep it.
    corrupts: bool = False

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
                return ''
            case ['config', 'set', key, value, '--stack', _]:
                self.config[key] = value
                return ''
            case ['config', 'get', key, '--stack', _]:
                return ('tampered' if self.corrupts else self.config[key]) + '\n'
            case unknown:  # pragma: no cover - an invocation the slot is not meant to make
                raise AssertionError(f'unexpected pulumi invocation {unknown}')
