"""A brief's front matter: what `dispatch` reads from a brief (dispatch.md §1.1).

A brief is a markdown file that opens with a YAML block between two `---`
lines; the prose below it is the brief as an agent reads it. The block is read
here once, at the boundary, and every field wrong with it is refused by name.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.tokens import CommentToken

from .paths import OwnedPath, PathRefused


class Role(enum.StrEnum):
    """What the dispatched agent does (dispatch.md §1.3)."""

    BUILDER = 'builder'
    DESIGN = 'design'
    REVIEW = 'review'


#: An RFC slice's name, as it leads the slice issue's title (rfc.md §3.4).
RFC_SLICE = re.compile(r'rfc-\d{3} slice \S+')

#: The block's delimiters: a `---` line opening the file, and the next one.
_FRONT_MATTER = re.compile(r'\A---\n(?P<block>.*?)\n---\n', re.S)

#: The fields whose value is prose. In an unquoted YAML value, whitespace and a
#: `#` open a comment, so prose holding ` #` — a `#938` after a space — is cut
#: there without a word of warning. The other fields hold names and numbers,
#: which a comment after them annotates, as §1.1's example does.
_PROSE = frozenset({'done'})


class BriefRefused(ValueError):
    """The brief's front matter is missing, or a field in it is wrong."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class FrontMatter:
    """The fields of a brief's front matter."""

    #: The ledger issue the dispatch is for.
    issue: int
    role: Role
    #: What "done" means, in one sentence.
    done: str
    #: The paths the work owns, exhaustively. Empty only for a role that
    #: writes nothing of its own: a review, or a design on its issue.
    owned: tuple[OwnedPath, ...]
    #: Whether the pull request closes the issue; None where no pull request
    #: of the dispatch's own will exist, which only a builder's must.
    closes: bool | None
    #: The RFC slice, when the issue is one (`RFC_SLICE`).
    rfc: str | None
    workspace: str | None
    branch: str | None
    #: The pull request a review reads.
    pr: int | None

    def as_json(self) -> dict[str, object]:
        """The fields as the dispatch record carries them."""
        return {
            'issue': self.issue,
            'role': self.role.value,
            'done': self.done,
            'owned': [str(path) for path in self.owned],
            'closes': self.closes,
            'rfc': self.rfc,
            'workspace': self.workspace,
            'branch': self.branch,
            'pr': self.pr,
        }


@dataclass(frozen=True)
class Brief:
    """A brief file: its front matter and the hash of all of it."""

    front: FrontMatter
    #: The hex sha256 of the file's bytes, which the record names it by.
    sha256: str


def _optional_str(raw: dict[str, Any], key: str, problems: list[str]) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(f'`{key}` is not a non-empty string')
        return None
    return value


def _optional_int(raw: dict[str, Any], key: str, problems: list[str]) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f'`{key}` is not a number')
        return None
    return value


def _owned(raw: dict[str, Any], role: Role | None, problems: list[str]) -> tuple[OwnedPath, ...]:
    if 'owned' not in raw:
        problems.append('`owned` is missing')
        return ()
    value = raw['owned']
    if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
        problems.append('`owned` is not a list of paths')
        return ()
    owned: list[OwnedPath] = []
    for item in cast(list[str], value):
        try:
            owned.append(OwnedPath.parse(item))
        except PathRefused as exc:
            problems.append(f'`owned`: {exc}')
    if not owned and role is Role.BUILDER:
        problems.append('`owned` is empty, and a builder owns the paths it writes')
    return tuple(owned)


def front_matter(raw: object) -> FrontMatter:
    """The front matter in `raw`, the block as YAML loaded it; every problem refused at once."""
    if not isinstance(raw, dict):
        raise BriefRefused(['the front matter is not a mapping'])
    fields = cast(dict[str, Any], raw)
    problems: list[str] = []
    unknown = sorted(set(fields) - set(FrontMatter.__dataclass_fields__))
    if unknown:
        problems.append(f'unknown fields {", ".join(unknown)}')
    issue = _optional_int(fields, 'issue', problems)
    if issue is None and 'issue' not in fields:
        problems.append('`issue` is missing')
    role: Role | None = None
    try:
        role = Role(fields.get('role'))
    except ValueError:
        problems.append(f'`role` is not one of {", ".join(member.value for member in Role)}')
    done = _optional_str(fields, 'done', problems)
    if done is None and 'done' not in fields:
        problems.append('`done` is missing')
    owned = _owned(fields, role, problems)
    closes = fields.get('closes')
    if closes is not None and not isinstance(closes, bool):
        problems.append('`closes` is not true or false')
    if closes is None and role is Role.BUILDER:
        problems.append("`closes` is missing, and it decides a builder's issue line")
    rfc = _optional_str(fields, 'rfc', problems)
    if rfc is not None and not RFC_SLICE.fullmatch(rfc):
        problems.append(f'`rfc` is not of the form `rfc-NNN slice N`: {rfc!r}')
    pr = _optional_int(fields, 'pr', problems)
    if pr is None and role is Role.REVIEW:
        problems.append('`pr` is missing, and a review names the pull request it reads')
    workspace = _optional_str(fields, 'workspace', problems)
    branch = _optional_str(fields, 'branch', problems)
    if problems or issue is None or role is None or done is None:
        raise BriefRefused(problems)
    return FrontMatter(
        issue=issue,
        role=role,
        done=done,
        owned=owned,
        closes=cast(bool | None, closes),
        rfc=rfc,
        workspace=workspace,
        branch=branch,
        pr=pr,
    )


def _cut_by_comment(block: str, key: str) -> bool:
    """Whether a comment begins on a line of the value of `key`, the top-level field of `block`.

    Asked of ruamel's round-trip loader, which keeps each comment beside the
    key it follows, so a value wrapped onto lines of its own is read the way
    YAML reads it. A comment on a line of the value opens with its `#`; a
    full-line comment below the value opens with the newline that ends the
    value's last line, and leaves the value whole.
    """
    try:
        loaded: object = YAML().load(block)  # pyright: ignore[reportUnknownMemberType] -- ruamel's `load` is untyped
    except YAMLError:
        return False
    if not isinstance(loaded, CommentedMap):
        return False
    comments = cast(Any, loaded.ca)  # ruamel's comment attribute is untyped
    attached = cast(dict[str, list[object]], comments.items).get(key, [])
    token = attached[2] if len(attached) > 2 else None
    return isinstance(token, CommentToken) and token.value.startswith('#')


def read(content: bytes) -> Brief:
    """The brief whose file holds `content`."""
    try:
        text = content.decode()
    except UnicodeDecodeError as exc:
        raise BriefRefused([f'the brief is not UTF-8: {exc}']) from exc
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise BriefRefused(['the brief opens with no front matter between two `---` lines'])
    try:
        raw: object = YAML(typ='safe', pure=True).load(match['block'])  # pyright: ignore[reportUnknownMemberType] -- ruamel's `load` is untyped
    except YAMLError as exc:
        raise BriefRefused([f'the front matter is not YAML: {exc}']) from exc
    cut = [
        f'`{key}` is cut short at its ` #`, which YAML reads as a comment: quote the value'
        for key in sorted(_PROSE)
        if _cut_by_comment(match['block'], key)
    ]
    try:
        front = front_matter(raw)
    except BriefRefused as exc:
        raise BriefRefused([*cut, *exc.problems]) from None
    if cut:
        raise BriefRefused(cut)
    return Brief(front=front, sha256=hashlib.sha256(content).hexdigest())
