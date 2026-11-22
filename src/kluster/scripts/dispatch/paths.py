"""Owned paths, and whether two of them can touch the same file (dispatch.md §2 rule 2).

An owned path takes one of these forms:

-   a file, `tests/test_apps_blog.py`, which owns everything under it too
    should it be a directory, since the path alone cannot say which it is;
-   a directory, written with its trailing `/`, which owns everything under it;
-   a pattern, in which `*` and `?` match within one path segment and `**`
    across segments: `tests/test_dispatch_*.py`;
-   a file narrowed to a section, `docs/operations.md#§3.5`, which owns that
    section of the file and leaves the rest of it to others.

The comparison errs one way only: two paths it calls disjoint can never touch
the same file, while two patterns it cannot tell apart are taken to overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import PurePosixPath

#: The characters that make an owned path a pattern.
_GLOB = re.compile(r'[*?]')


class PathRefused(ValueError):
    """An owned path that is not one of the module's forms."""


@dataclass(frozen=True)
class OwnedPath:
    """One owned path. `path` is the file, directory or pattern; `section` the narrowing."""

    path: str
    section: str | None = None

    @classmethod
    def parse(cls, text: str) -> OwnedPath:
        path, hash_, section = text.partition('#')
        if hash_ and not section:
            raise PathRefused(f'{text!r} names an empty section')
        if not path or path.startswith('/') or '..' in PurePosixPath(path).parts or '\\' in path or '[' in path:
            raise PathRefused(f'{text!r} is not a path relative to the repository root')
        owned = cls(path=path, section=section or None)
        if owned.section is not None and (owned.directory or owned.pattern):
            raise PathRefused(f'{text!r} narrows a directory or a pattern to a section; only a file has sections')
        return owned

    def __str__(self) -> str:
        return self.path if self.section is None else f'{self.path}#{self.section}'

    @property
    def directory(self) -> bool:
        return self.path.endswith('/')

    @property
    def pattern(self) -> bool:
        return _GLOB.search(self.path) is not None

    @property
    def prefix(self) -> str:
        """The literal part every file it owns starts with."""
        found = _GLOB.search(self.path)
        return self.path if found is None else self.path[: found.start()]

    @cached_property
    def _regex(self) -> re.Pattern[str]:
        out: list[str] = []
        for token in re.split(r'(\*\*|\*|\?)', self.path):
            out.append({'**': '.*', '*': '[^/]*', '?': '[^/]'}.get(token, re.escape(token)))
        return re.compile(''.join(out))

    @property
    def plain(self) -> bool:
        """Neither a directory nor a pattern: a file, or a directory written without its `/`."""
        return not (self.directory or self.pattern)

    def owns(self, file: str) -> bool:
        """Whether `file`, a path some change touches, is one this path owns some of."""
        if self.directory:
            return file.startswith(self.path)
        if self.pattern:
            return self._regex.fullmatch(file) is not None
        return file == self.path or file.startswith(f'{self.path}/')

    def overlaps(self, other: OwnedPath) -> bool:
        """Whether this path and `other` can own part of the same file."""
        if self.plain and other.plain:
            if self.path == other.path:
                return self.section is None or other.section is None or self.section == other.section
            return self.owns(other.path) or other.owns(self.path)
        if self.plain:
            return other.overlaps(self)
        if other.plain:
            return self.owns(other.path) or self._prefixes_nest(OwnedPath(f'{other.path}/'))
        return self._prefixes_nest(other)

    def _depths(self) -> tuple[int, int | None]:
        """The fewest and the most segments of a file this directory or pattern owns; None for no most."""
        segments = self.path.rstrip('/').count('/') + 1
        if self.directory:
            return segments + 1, None
        return (segments, None) if '**' in self.path else (segments, segments)

    def _prefixes_nest(self, other: OwnedPath) -> bool:
        """Whether two directories or patterns can own one file, judged by depth and literal prefix."""
        (low, high), (other_low, other_high) = self._depths(), other._depths()
        if (high is not None and high < other_low) or (other_high is not None and other_high < low):
            return False
        return self.prefix.startswith(other.prefix) or other.prefix.startswith(self.prefix)
