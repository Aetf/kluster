"""A `gh` that runs nothing and remembers every secret it was handed.

Its own named module for the reason `fake_pulumi` is one: both the suite that
covers the slot itself and the suite that covers the map pushing into it need
the same stand-in, and `conftest` is not a name an import can aim at.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class RecordedGh:
    """Every `gh secret` invocation, and the collections they add up to."""

    #: `(repository, environment)` -> name -> the timestamp the listing shows.
    collections: dict[tuple[str, str | None], dict[str, str]] = field(
        default_factory=dict[tuple[str, str | None], dict[str, str]]
    )
    invocations: list[list[str]] = field(default_factory=list[list[str]])
    #: What was written, which the real API would never disclose again. Here so
    #: a test can assert the value that reached the slot is the one intended.
    values: dict[tuple[str, str | None, str], str] = field(default_factory=dict[tuple[str, str | None, str], str])
    #: What a fresh write claims as its timestamp. Moved by a test to stand in
    #: for time passing between two runs.
    now: str = '2026-08-26T12:00:00Z'
    #: A listing that never shows what was just written, which is what a push
    #: into a collection that silently dropped it looks like.
    forgets: bool = False

    def _where(self, args: Sequence[str]) -> tuple[str, str | None]:
        rest = list(args)
        repository = rest[rest.index('--repo') + 1]
        environment = rest[rest.index('--env') + 1] if '--env' in rest else None
        return repository, environment

    def __call__(self, args: Sequence[str], *, token: str, stdin: str | None) -> str:
        assert token, 'the pusher must authenticate as the account root'
        self.invocations.append(list(args))
        where = self._where(args)
        match list(args):
            case ['secret', 'list', *_]:
                return json.dumps(
                    [{'name': name, 'updatedAt': at} for name, at in self.collections.get(where, {}).items()]
                )
            case ['secret', 'set', name, *_]:
                assert stdin is not None, 'a secret was passed as an argument rather than on standard input'
                self.values[(*where, name)] = stdin
                if not self.forgets:
                    self.collections.setdefault(where, {})[name] = self.now
                return ''
            case unknown:  # pragma: no cover - an invocation the slot is not meant to make
                raise AssertionError(f'unexpected gh invocation {unknown}')
