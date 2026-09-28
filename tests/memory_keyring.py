"""A desktop secret store that lives for the length of one test, and a store that is not there.

The acquisition chain's first layer (`kluster.lib.acquisition`) is the
desktop secret store, and `keyring` resolves the backend the same way whether
it is the operator's login keyring or one held in memory. `tests/conftest.py`
installs the store that is not there for every case, so the operator's own is
never read or written; a case that needs a store installs a `MemoryKeyring`
over it for its own length. A named module rather than part of `conftest`,
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


def _current() -> keyring.backend.KeyringBackend:
    """Whatever backend this machine resolves to, or none at all.

    Resolution itself raises where a Secret Service is configured but not
    running, which is the state a test runner is usually in.
    """
    try:
        return keyring.get_keyring()
    except Exception:  # noqa: BLE001 -- an unresolvable backend is "no backend"
        return keyring.backends.fail.Keyring()


@contextmanager
def installed(backend: keyring.backend.KeyringBackend) -> Generator[keyring.backend.KeyringBackend]:
    """`backend` as the process's secret store for the block, and the previous one back after it."""
    previous = _current()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
