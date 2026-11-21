"""What a rebuild keeps while it runs: `run.json`, and a transcript of each step.

Both live in the run's directory, `.claude/drill/` in the checkout that holds
`.credentials/` (operations.md §3.5), which `.gitignore` covers and the
pass readings delete once the run closes.

-   **`run.json`** holds each step's verdict with its start and end, the parts
    of a step that passed, the baselines step 0 took, and the facts a later
    step reads back: the capacity reading, the runs the dispatch created,
    which commands of the sitting ran. It is rewritten whole after every
    change, through a file beside it renamed over it, so a run stopped at any
    point leaves the last change or the one before, never half of one.
-   **`step-<key>.log`** is the transcript of one step: each line stamped in
    UTC, each command with its exit and what it printed. A step run again
    appends to its transcript.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

#: The run's directory, relative to the checkout.
DIRECTORY = Path('.claude') / 'drill'
RUN = 'run.json'


class Verdict(enum.StrEnum):
    """How a step, or a part of one, ended."""

    PASS = 'pass'
    FAIL = 'fail'
    #: A reading that is not whole: what it read is no evidence either way,
    #: and the step is run again (operations.md §3.5).
    NO_READING = 'no reading'


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def stamp(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


@dataclass
class Record:
    """`run.json`, read once and written after every change."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    #: Set once the run's directory is deleted, after which nothing is written.
    closed: bool = False

    @classmethod
    def open(cls, directory: Path) -> Record:
        path = directory / RUN
        data = cast('dict[str, Any]', json.loads(path.read_text())) if path.exists() else {}
        return cls(path=path, data=data)

    def save(self) -> None:
        if self.closed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.path.with_name(f'.{self.path.name}.new')
        _ = staged.write_text(json.dumps(self.data, indent=2, sort_keys=True) + '\n')
        os.replace(staged, self.path)

    # -- steps --------------------------------------------------------------

    def _step(self, key: str) -> dict[str, Any]:
        steps = cast('dict[str, dict[str, Any]]', self.data.setdefault('steps', {}))
        return steps.setdefault(key, {})

    def verdict(self, key: str) -> Verdict | None:
        recorded = cast('dict[str, dict[str, Any]]', self.data.get('steps', {})).get(key, {}).get('verdict')
        return Verdict(recorded) if isinstance(recorded, str) else None

    def started(self, key: str, at: dt.datetime) -> None:
        step = self._step(key)
        _ = step.setdefault('started', stamp(at))
        step.pop('ended', None)
        step.pop('verdict', None)
        self.save()

    def ended(self, key: str, verdict: Verdict, at: dt.datetime) -> None:
        step = self._step(key)
        step['ended'] = stamp(at)
        step['verdict'] = str(verdict)
        self.save()

    def passed_parts(self, key: str) -> tuple[str, ...]:
        return tuple(cast('list[str]', self._step(key).get('parts', [])))

    def part_passed(self, key: str, part: str) -> None:
        parts = cast('list[str]', self._step(key).setdefault('parts', []))
        if part not in parts:
            parts.append(part)
        self.save()

    def progress(self, key: str) -> tuple[str, ...]:
        """What a part of step `key` that runs several commands finished, which the step's record holds."""
        return tuple(cast('list[str]', self._step(key).get('progress', [])))

    def keep_progress(self, key: str, done: Sequence[str]) -> None:
        self._step(key)['progress'] = list(done)
        self.save()

    def forget(self, key: str) -> None:
        """Drop what is recorded of step `key`, so it runs from its first part."""
        cast('dict[str, Any]', self.data.get('steps', {})).pop(key, None)
        self.save()

    def times(self, key: str) -> tuple[str | None, str | None]:
        step = cast('dict[str, dict[str, Any]]', self.data.get('steps', {})).get(key, {})
        return cast('str | None', step.get('started')), cast('str | None', step.get('ended'))

    # -- baselines and facts ------------------------------------------------

    def baseline(self, name: str) -> object:
        return cast('dict[str, object]', self.data.get('baselines', {})).get(name)

    def keep_baseline(self, name: str, value: object) -> None:
        cast('dict[str, object]', self.data.setdefault('baselines', {}))[name] = value
        self.save()

    def fact(self, name: str) -> object:
        return cast('dict[str, object]', self.data.get('facts', {})).get(name)

    def keep_fact(self, name: str, value: object) -> None:
        cast('dict[str, object]', self.data.setdefault('facts', {}))[name] = value
        self.save()


#: What a transcript records in place of output it does not keep.
WITHHELD = '(output withheld: it carries secrets)'
ATTACHED = '(attached to the terminal; output not kept)'


@dataclass
class Transcript:
    """`step-<key>.log`: what one step did, line by line, each line stamped in UTC."""

    path: Path
    clock: Callable[[], dt.datetime] = now
    _out: TextIO | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> Transcript:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._out = self.path.open('a')
        return self

    def __exit__(self, *_: object) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None

    def line(self, text: str) -> None:
        assert self._out is not None, 'a transcript is written inside its `with` block'
        at = stamp(self.clock())
        for each in text.splitlines() or ['']:
            _ = self._out.write(f'{at} {each}\n')
        self._out.flush()

    def command(
        self,
        argv: Sequence[str],
        *,
        started: dt.datetime,
        exit: int,  # noqa: A002 -- the field is a process's exit status, named as the transcript prints it
        printed: str,
    ) -> None:
        """One command: when it started, its arguments, its exit, and what it printed (`WITHHELD`, `ATTACHED`)."""
        self.line(f'$ {shlex.join(argv)}  (started {stamp(started)})')
        if printed.strip():
            self.line(printed.rstrip('\n'))
        self.line(f'exit {exit}')


def as_mapping(value: object) -> Mapping[str, object]:
    """`value` as a JSON object, or an empty one: what `run.json` holds where nothing was kept."""
    return cast('Mapping[str, object]', value) if isinstance(value, dict) else {}
