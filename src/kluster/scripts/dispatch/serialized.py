"""The serialized files, as AGENTS.md names them (dispatch.md §2 rule 3).

AGENTS.md is the set's one statement: a bullet that names the files in code
spans and then says they `are **serialized**`. The set is read from that
bullet every time it is needed, and a text in which the bullet names no file
is refused, since an empty set would serialize nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from kluster.lib import workstation

#: The bullet, from its marker to the word that makes it the set's statement.
_BULLET = re.compile(r'^\* (?P<names>(?:(?!^\* ).)*?)\bare \*\*serialized\*\*', re.S | re.M)
#: A file the bullet names.
_NAME = re.compile(r'`(?P<name>[^`]+)`')


class SetUnreadable(ValueError):
    """AGENTS.md no longer states the serialized set in a form this module reads."""


def parse(agents: str) -> frozenset[str]:
    """The serialized files AGENTS.md's text `agents` names."""
    match = _BULLET.search(agents)
    names = frozenset(_NAME.findall(match['names'])) if match else frozenset[str]()
    if not names:
        raise SetUnreadable('AGENTS.md names no file as **serialized**: no claim can be checked against the set')
    return names


def read() -> frozenset[str]:
    """The serialized files of the checkout this package runs from."""
    return parse(Path(workstation.repo_root(), 'AGENTS.md').read_text())
