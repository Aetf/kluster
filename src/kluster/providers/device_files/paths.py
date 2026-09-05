"""What a declared path is on the device, and what its parent is.

One grammar, asked by everything that judges a path or takes one apart: `check`
refuses what `refusal` names a reason for, and the transport builds its
`mkdir -p` from the `parent` of a path that passed. A second derivation of
either would agree with this one until the day one of them changed, and nothing
would fail when they diverged.

`parent` therefore refuses what `check` refuses rather than answering anyway: a
path this module does not call canonical names no place on the device, so the
directory it sits in is not a question with an answer.
"""

from __future__ import annotations

import posixpath

__all__ = ('ROOT', 'canonical', 'parent', 'refusal')

#: The device root: the parent of everything and a declaration of nothing.
ROOT = '/'


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


def refusal(path: str) -> str | None:
    """Why `path` is not a declared path, in a caller's words, or `None` if it is.

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
        return f'must be an absolute path on the device, got {path!r}'
    spelling = canonical(path)
    if spelling == ROOT:
        # Asked before the spelling is compared, so that `//` and `/.` are
        # refused as what they name rather than told to declare `/` instead.
        return 'must name a place on the device rather than the device root itself'
    if path != spelling:
        return f'{_departure(path)}, so declare it as {spelling!r}, not {path!r}'
    return None


def parent(path: str) -> str:
    """The directory a declared path sits in.

    The answer is absolute and canonical, and it may be the root -- `/data` sits
    in one, and `mkdir -p /` is a no-op rather than an error. That the root is
    an answer here and not a declaration is the difference between a place a
    write has to reach through and a place a resource may name.
    """
    reason = refusal(path)
    if reason is not None:
        raise ValueError(f'{path!r} is no declared path, so it has no parent: {reason}')
    return posixpath.dirname(path)


def _departure(path: str) -> str:
    """Which departure from the canonical spelling this path took, in its own words."""
    if path != ROOT and path.endswith(ROOT):
        # Named on its own because it is the one that reads as harmless: the
        # place is the same one, and every test of it answers differently.
        return 'a trailing slash is a path resolved through to a directory, not the path itself'
    return 'a path names its place once, without a doubled slash, a "." or a ".."'
