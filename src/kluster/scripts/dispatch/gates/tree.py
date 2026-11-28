"""What a gate reads of the repository beyond what it is shown: three read-only tools, answered from the store at a commit.

A gate is shown the parts of the change (`view`). Some rules turn on what
lies outside them: whether a definition of the set exists elsewhere, whether
the other side of an assertion is computed by the program, whether a
declaration already implies a constraint, whether the members a count counts
are in view. So every call carries tools the model may call before it
answers (`Tree.tools`): read a file whole or a run of its lines
(`read_file`), search the tree for a regular expression, optionally under a
path (`search`), and list a directory (`list_directory`).

Each is answered from the local store at a fixed commit of the call's
`Tree`, with `git cat-file`, `git grep` and `git ls-tree`: no checkout is
made or written, a historical head in `calibrate` reads as a live one does,
and nothing outside the repository is reachable. A tool reads the change's
head unless it names the base. A claim reads a brief with both at the
commit the dispatcher's checkout is at (`checkout`), since a brief writes no
file of the repository.

A path that is absolute, climbs out of the tree, or starts with `:`, as
the pseudo-paths a commit's message and a brief are shown under do
(`change`), or that names no file or directory at the commit, is answered with an error
result the model reads, and the call goes on. Every answer is held to
`TOOL_BYTES`, and says where it was cut.

What the tools return is context. A finding still names a part the call
showed, and stands only on the lines the change writes (`ask.written`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from anthropic.lib.tools import BetaFunctionTool, ToolError, beta_tool

from ..forge import ForgeError
from ..prcheck import Git

#: The most one tool's answer holds, in bytes of UTF-8: about 6,700 tokens at
#: the three bytes a token `view.Part.size` estimates, a few hundred lines.
#: An answer stays in the conversation, and each later request of the call
#: sends it again, so it is paid for once more each round after it
#: (`model.ROUNDS`); a file longer than this is read a run of its lines at a
#: time, which is as much of it as a rule's context needs at once.
TOOL_BYTES = 20_000

#: The sides of a change a tool reads, by the name the model gives: as the change leaves the tree, and as it stood.
HEAD, BASE = 'head', 'base'

#: What each tool says it does, and the input it takes: the definitions the API is sent, beside the functions
#: that answer them (`Tree.tools`). Each input is held to its schema (`strict`).
DEFINITIONS: Mapping[str, Mapping[str, Any]] = {
    'read_file': {
        'description': (
            'Read a file of the repository, whole or from its line `first_line` to its line `last_line`, each line '
            'under its number. It reads the tree at the head, as the change leaves it, unless `side` is `base`, as '
            'it stood before the change. An answer that would run past its bound is cut, and says at which line.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'The path from the root of the repository.'},
                'side': {'type': 'string', 'enum': [HEAD, BASE]},
                'first_line': {'type': 'integer'},
                'last_line': {'type': 'integer'},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
    'search': {
        'description': (
            'Search the text files of the repository for an extended regular expression (POSIX ERE, as '
            '`git grep -E` reads it), in any case, under `path` where it is given: every line that matches, as '
            '`<path>:<line>:<text>`. It reads the tree at the head unless `side` is `base`.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'pattern': {'type': 'string'},
                'path': {'type': 'string', 'description': 'A directory or file to search under.'},
                'side': {'type': 'string', 'enum': [HEAD, BASE]},
            },
            'required': ['pattern'],
            'additionalProperties': False,
        },
    },
    'list_directory': {
        'description': (
            'List a directory of the repository, the root where `path` is empty: each entry by name, a directory '
            'with `/` after it. It reads the tree at the head unless `side` is `base`.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'side': {'type': 'string', 'enum': [HEAD, BASE]},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
}


class TreeGit(Git):
    """The local store, searched and listed at a commit."""

    def grep(self, commit: str, pattern: str, under: str) -> tuple[bool, str]:
        """Each line of a text file at `commit` under `under`, the whole tree where empty, that `pattern` matches.

        The lines as `<path>:<line>:<text>`, and whether git read the pattern;
        where it did not, git's reason in their place.
        """
        spec = [f':(literal){under}'] if under else []
        done = self._run('grep', '-n', '-I', '-i', '-E', '-e', pattern, commit, '--', *spec, ok=(0, 1, 128))
        if done.returncode == 128:
            return False, done.stderr.decode(errors='replace').strip()
        prefix = f'{commit}:'
        out = done.stdout.decode(errors='replace')
        return True, ''.join(line.removeprefix(prefix) for line in out.splitlines(keepends=True))

    def listing(self, commit: str, path: str) -> list[str] | None:
        """The entries of the directory `path` at `commit`, a directory's name ending in `/`; None where it is none."""
        done = self._run('ls-tree', '-z', f'{commit}:{path}', ok=(0, 128))
        if done.returncode != 0:
            return None
        entries: list[str] = []
        for entry in done.stdout.decode(errors='replace').split('\0'):
            if entry:
                meta, _, name = entry.partition('\t')
                entries.append(f'{name}/' if meta.split()[1] == 'tree' else name)
        return entries

    def commit(self, revision: str) -> str:
        """The full SHA of the commit `revision` names."""
        return self._run('rev-parse', '--verify', f'{revision}^{{commit}}').stdout.decode().strip()


def _bounded(lines: Sequence[str], more: Callable[[int], str]) -> str:
    """`lines` joined, as many as fit `TOOL_BYTES`; where some do not, `more` of how many are left closes the answer."""
    kept: list[str] = []
    size = 0
    for line in lines:
        size += len(line.encode()) + 1
        if size > TOOL_BYTES:
            return '\n'.join([*kept, more(len(lines) - len(kept))])
        kept.append(line)
    return '\n'.join(kept)


def _place(path: str) -> str:
    """`path` as the tree names it; `ToolError` where it is no path inside the tree."""
    if path.startswith(':'):
        raise ToolError(f'{path} is a part the change is shown under, not a path of the repository')
    if '\0' in path or '\n' in path:
        raise ToolError('a path holds no NUL or newline')
    pure = PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts:
        raise ToolError(f'{path} is outside the repository: a path runs from its root and never climbs out of it')
    named = str(pure)
    return '' if named == '.' else named


@dataclass(frozen=True)
class Tree:
    """The repository a call's tools read: the store, and the commits of the change's head and base."""

    store: TreeGit
    head: str
    base: str

    def _at(self, side: str) -> str:
        if side not in (HEAD, BASE):
            raise ToolError(f'side is {HEAD} or {BASE}, not {side}')
        return self.head if side == HEAD else self.base

    def read_file(
        self, path: str, side: str = HEAD, first_line: int | None = None, last_line: int | None = None
    ) -> str:
        """The answer of `read_file`."""
        place, commit = _place(path), self._at(side)
        text = self._git(lambda: self.store.text(commit, place)) if place else None
        if text is None:
            raise ToolError(f'no file at {path} at the {side}; list_directory shows what is there')
        if '\0' in text:
            raise ToolError(f'{path} at the {side} is not text')
        lines = text.split('\n')
        lines = lines[:-1] if lines and lines[-1] == '' else lines
        first = first_line if first_line is not None else 1
        last = min(last_line if last_line is not None else len(lines), len(lines))
        if not 1 <= first <= max(last, 1):
            raise ToolError(f'{path} at the {side} holds lines 1–{len(lines)}; there is no line {first}–{last}')
        rows = [f'{n:>6} {lines[n - 1]}' for n in range(first, last + 1)]
        head = f'## {path} at the {side} ({len(lines)} lines, lines {first}–{last})'

        def more(left: int) -> str:
            stop = last - left + 1
            return f'[cut at line {stop}, at the bound on an answer: read from line {stop} on for the rest]'

        return _bounded([head, *rows], more)

    def search(self, pattern: str, path: str = '', side: str = HEAD) -> str:
        """The answer of `search`."""
        place, commit = _place(path), self._at(side)
        read, out = self._git(lambda: self.store.grep(commit, pattern, place))
        if not read:
            raise ToolError(f'git did not read the pattern: {out}')
        if not out:
            return f'no line at the {side} matches {pattern}' + (f' under {path}' if path else '')
        return _bounded(out.splitlines(), lambda left: f'[{left} more matching lines; narrow the pattern or the path]')

    def list_directory(self, path: str, side: str = HEAD) -> str:
        """The answer of `list_directory`."""
        place, commit = _place(path), self._at(side)
        entries = self._git(lambda: self.store.listing(commit, place))
        if entries is None:
            raise ToolError(f'no directory at {path} at the {side}')
        return _bounded(entries, lambda left: f'[{left} more entries]')

    @staticmethod
    def _git[T](read: Callable[[], T]) -> T:
        try:
            return read()
        except ForgeError as exc:
            raise ToolError(str(exc)) from exc

    def tools(self) -> list[BetaFunctionTool[Any]]:
        """The tools a call carries, each answering from this tree as `DEFINITIONS` declares it."""
        answers: dict[str, Callable[..., str]] = {
            'read_file': self.read_file,
            'search': self.search,
            'list_directory': self.list_directory,
        }
        return [
            beta_tool(
                answers[name],
                name=name,
                description=str(definition['description']),
                input_schema=dict(definition['input_schema']),
                strict=True,
            )
            for name, definition in DEFINITIONS.items()
        ]


def checkout(root: Path) -> Tree:
    """The tree of the checkout at `root`, at the commit it is at, as both the head and the base."""
    store = TreeGit(root)
    commit = store.commit('HEAD')
    return Tree(store, commit, commit)
