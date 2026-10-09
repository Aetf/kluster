"""A desktop secret store that lives for the length of one test, and a store that is not there.

The acquisition chain's first layer (`kluster.lib.acquisition`) is the
desktop secret store, and `keyring` resolves the backend the same way whether
it is the operator's login keyring or one held in memory. `tests/conftest.py`
installs the store that is not there for the whole session, fixtures of every
scope included, so the operator's own is never read or written; a case that
needs a store installs a `MemoryKeyring` over it for its own length. A named module rather than part of `conftest`,
because test modules import from it and `conftest` is not a name an import can
aim at.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import keyring
import keyring.backend
import keyring.backends.fail
import keyring.errors

if TYPE_CHECKING:
    from collections.abc import Generator


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A Secret Service that lives for the length of one test."""

    # `keyring`'s own backends declare this the same way; the base class makes
    # it a class property, which a plain value cannot match by type.
    priority: float = 1  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError(username)
        del self.items[(service, username)]


#: The backends `installed` has put in place, innermost last. What a block
#: restores is read from here rather than asked of `keyring`, which resolves the
#: machine's own backend wherever nothing has been set yet.
_INSTALLED: list[keyring.backend.KeyringBackend] = []


@contextmanager
def installed(backend: keyring.backend.KeyringBackend) -> Generator[keyring.backend.KeyringBackend]:
    """`backend` as the process's secret store for the block, and the previous one back after it.

    The previous one is the one an enclosing block installed. Outside every
    block it is a store that refuses every call: the machine's own backend is
    never resolved, so the end of no block can hand it back.
    """
    previous = _INSTALLED[-1] if _INSTALLED else keyring.backends.fail.Keyring()
    _INSTALLED.append(backend)
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        # Blocks end innermost first, as context managers and the fixtures
        # holding them do; one that did not would restore a stale store. The
        # store goes back before the check, so a refusal leaves no inner one.
        ended = _INSTALLED.pop()
        keyring.set_keyring(previous)
        assert ended is backend, 'a secret store block ended out of order'
