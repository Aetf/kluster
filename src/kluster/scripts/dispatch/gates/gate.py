"""A gate: its declaration, its directory, and the set of them.

`Spec` is what a gate's package declares as `GATE`; `Gate` is that
declaration with the directory beside it, where its prompt, its record and
its cases are files. `discover` finds every gate this package holds.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..forge import CODE
from ..paths import OwnedPath
from ..prcheck import section_lines
from . import calibration
from .ask import harness
from .cases import Case
from .cases import load as load_cases
from .extract import Change, Item

#: The model a gate is asked on unless it names another: the cheapest that
#: meets a gate's calibration bar is the one it stays on (dispatch.md §3).
DEFAULT_MODEL = 'claude-haiku-5-5'

#: The files of a gate's directory. The prompt is what makes a directory a gate.
PROMPT = 'prompt.md'
RECORD = 'calibration.json'
CASES = 'cases.jsonl'

#: Where the prompt takes the canonical section's text.
PLACEHOLDER = '{canon}'

#: The attribute of a gate's package that holds its `Spec`.
DECLARATION = 'GATE'

#: The directory every gate is a subdirectory of.
PACKAGE = Path(__file__).parent

Extractor = Callable[[Change], Sequence[Item]]


class GateUnreadable(ValueError):
    """A gate whose directory, declaration or canonical section does not read."""


@dataclass(frozen=True)
class Canon:
    """The canonical text a gate's prompt quotes: a section of a document of this repository."""

    document: str
    #: The section, by its number (`§3`) or a heading's whole text (`prcheck.section_lines`).
    section: str


@dataclass(frozen=True)
class Section:
    """A canonical section as one tree holds it."""

    text: str
    first: int
    last: int


@dataclass(frozen=True)
class Spec:
    """What a gate's package declares: its canon, its trigger, its extractor and its model."""

    canon: Canon
    #: The path patterns a change must touch for the gate to run, in the owned paths' forms (`paths.OwnedPath`).
    trigger: tuple[str, ...]
    extract: Extractor
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class Gate:
    """A gate: its name, its directory and its declaration."""

    name: str
    directory: Path
    spec: Spec

    @classmethod
    def load(cls, directory: Path, spec: Spec) -> Gate:
        """The gate of `directory`, refused unless its prompt takes the canon in exactly one place."""
        prompt = directory / PROMPT
        if not prompt.is_file():
            raise GateUnreadable(f'{directory} holds no {PROMPT}')
        if prompt.read_text().count(PLACEHOLDER) != 1:
            raise GateUnreadable(f'{prompt} must hold {PLACEHOLDER} exactly once, where the canonical section goes')
        for pattern in spec.trigger:
            _ = OwnedPath.parse(pattern)
        return cls(name=directory.name.replace('_', '-'), directory=directory, spec=spec)

    @property
    def prompt(self) -> str:
        return (self.directory / PROMPT).read_text()

    def system(self, canon: str) -> str:
        """The prompt with the canonical section's text in its place."""
        return self.prompt.replace(PLACEHOLDER, canon)

    def hashes(self, canon: str) -> calibration.Hashes:
        """What a calibration of this gate is a measurement of, with `canon` as the canonical text.

        The prompt is `prompt.md` with the harness's own fixed text after it
        (`ask.harness`): both are what the model is asked.
        """
        return calibration.Hashes.of(prompt=f'{self.prompt}\n{harness()}', canon=canon, model=self.spec.model)

    def triggered(self, paths: Collection[str]) -> bool:
        owned = [OwnedPath.parse(pattern) for pattern in self.spec.trigger]
        return any(o.owns(path) for o in owned for path in paths)

    def canon(self, text_of: Callable[[str], str | None]) -> Section:
        """The canonical section in the tree `text_of` reads a path of. Raises `GateUnreadable`."""
        canon = self.spec.canon
        text = text_of(canon.document)
        if text is None:
            raise GateUnreadable(f'{self.name}: {canon.document} is not in the tree')
        found = section_lines(text, canon.section)
        if isinstance(found, str):
            raise GateUnreadable(f'{self.name}: {canon.document}#{canon.section}: {found}')
        first, last = found
        return Section('\n'.join(text.split('\n')[first - 1 : last]), first, last)

    def rule(self, head: str, section: Section) -> str:
        """The link to the canonical section at commit `head`."""
        return f'https://github.com/{CODE}/blob/{head}/{self.spec.canon.document}#L{section.first}-L{section.last}'

    def stale(self, text_of: Callable[[str], str | None]) -> list[str]:
        """Each part of the gate its record no longer measures, the canon read by `text_of`; empty without a record."""
        record = self.record()
        return [] if record is None else record.hashes.changed(self.hashes(self.canon(text_of).text))

    def record(self) -> calibration.Calibration | None:
        """The gate's calibration record; None where it has none."""
        path = self.directory / RECORD
        return calibration.load(path) if path.is_file() else None

    def cases(self) -> list[Case]:
        path = self.directory / CASES
        return load_cases(path) if path.is_file() else []


def discover(package: Path = PACKAGE, module: str = __name__.rpartition('.')[0]) -> list[Gate]:
    """Every gate of `package`, whose module name is `module`: each subdirectory with a prompt, declaring a `Spec`.

    The definition of the set, so a gate is added by adding its directory: a
    directory holding a prompt is a gate, and one whose package declares no
    `Spec` is refused by name rather than passed over.
    """
    found: list[Gate] = []
    for directory in sorted(p for p in package.iterdir() if (p / PROMPT).is_file()):
        declared = importlib.import_module(f'{module}.{directory.name}')
        spec: object = getattr(declared, DECLARATION, None)
        if not isinstance(spec, Spec):
            raise GateUnreadable(f'{declared.__name__} declares no {DECLARATION} of type Spec')
        found.append(Gate.load(directory, spec))
    return found


def named(name: str) -> Gate:
    """The gate of this package called `name`. Raises `GateUnreadable` naming the gates there are."""
    gates = discover()
    match = next((g for g in gates if g.name == name), None)
    if match is None:
        raise GateUnreadable(f'no gate is named {name}; the gates are {", ".join(g.name for g in gates) or "none"}')
    return match
