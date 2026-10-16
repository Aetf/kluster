"""What a declared path is on the device, and what its parent is.

One grammar, asked by everything that judges a path or takes one apart: `check`
refuses what `refusal` names a departure for, and the transport builds its
`mkdir -p` from the `parent` of a path that passed. A second derivation of
either would agree with this one until the day one of them changed, and nothing
would fail when they diverged.

`parent` therefore refuses what `check` refuses rather than answering anyway: a
path this module does not call canonical names no place on the device, so the
directory it sits in is not a question with an answer.

A refusal is a `Departure`, which `explain` puts into words for the caller: a
case or a caller that has to know which rule a path broke reads the member, and
the sentence stays free to change.
"""

from __future__ import annotations

import enum
import posixpath

__all__ = ('ROOT', 'Departure', 'UndeclaredPath', 'canonical', 'explain', 'parent', 'refusal')

#: The device root: the parent of everything and a declaration of nothing.
ROOT = '/'


class Departure(enum.Enum):
    """Which rule of a declared path a spelling breaks."""

    #: Not absolute: it would be resolved against wherever a session starts.
    RELATIVE = enum.auto()
    #: The device root itself, in any spelling.
    ROOT = enum.auto()
    #: A trailing slash, which every test resolves through to a directory.
    TRAILING_SLASH = enum.auto()
    #: Another spelling of a place: a doubled slash, a `.` or a `..`.
    NOT_CANONICAL = enum.auto()


class UndeclaredPath(ValueError):
    """A path asked for its parent that is no declared path; `departure` says which rule it breaks."""

    def __init__(self, path: str, departure: Departure) -> None:
        super().__init__(f'{path!r} is no declared path, so it has no parent: {explain(path, departure)}')
        self.path: str = path
        self.departure: Departure = departure


def canonical(path: str) -> str:
    """The device's own single spelling of the place `path` names.

    `posixpath.normpath` preserves exactly two leading slashes, because POSIX
    leaves a doubled leading slash to the implementation to read as it likes.
    They are stripped to one here, which is how Linux reads them and therefore
    how every device this installation dials reads them. That is the line to
    revisit if a declared path ever has to survive a system where `//` is a
    distinct root.
    """
    collapsed = posixpath.normpath(path)
    return collapsed[1:] if collapsed.startswith('//') else collapsed


def refusal(path: str) -> Departure | None:
    """Which rule `path` breaks as a declared path, or `None` if it is one.

    **Absolute**, because a relative path would be resolved against whatever
    directory a session happens to start in, and no declaration here chose one.

    **Below the root**, because the root itself is the device rather than
    something on it: every operation these resources have is destructive or
    meaningless there -- a mode set on `/`, a staged file moved over it, a
    delete of it -- and no declaration wants any of them.

    **Canonical**, because a place with two spellings is a place the comparison
    and the operations can disagree about. A trailing slash is the one that
    bites: a path ending in one is resolved through to a directory by every
    shell test, by `stat` and by `mv`, so the spelling names the same place and
    answers a different question about it.
    """
    if not path.startswith(ROOT):
        return Departure.RELATIVE
    spelling = canonical(path)
    if spelling == ROOT:
        # Asked before the spelling is compared, so that `//` and `/.` are
        # refused as what they name rather than told to declare `/` instead.
        return Departure.ROOT
    if path != spelling:
        # Named on its own because it is the one that reads as harmless: the
        # place is the same one, and every test of it answers differently.
        return Departure.TRAILING_SLASH if path.endswith(ROOT) else Departure.NOT_CANONICAL
    return None


def explain(path: str, departure: Departure) -> str:
    """`departure` in a caller's words, about `path`: what the rule is, and how to spell the path instead."""
    match departure:
        case Departure.RELATIVE:
            return f'must be an absolute path on the device, got {path!r}'
        case Departure.ROOT:
            return 'must name a place on the device rather than the device root itself'
        case Departure.TRAILING_SLASH:
            why = 'a trailing slash is a path resolved through to a directory, not the path itself'
        case Departure.NOT_CANONICAL:
            why = 'a path names its place once, without a doubled slash, a "." or a ".."'
    return f'{why}, so declare it as {canonical(path)!r}, not {path!r}'


def parent(path: str) -> str:
    """The directory a declared path sits in.

    The answer is absolute and canonical, and it may be the root -- `/data` sits
    in one, and `mkdir -p /` is a no-op rather than an error. That the root is
    an answer here and not a declaration is the difference between a place a
    write has to reach through and a place a resource may name.
    """
    departure = refusal(path)
    if departure is not None:
        raise UndeclaredPath(path, departure)
    return posixpath.dirname(path)
