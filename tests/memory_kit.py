"""A kit that is not a file.

A real KeePass database is the right thing to test the row shape against
(§2), and the wrong thing to run a fault sweep against: every open and every
write re-derives the key, which is the whole cost of a suite that runs one
operation a few dozen times over.

Its own module, and a named one, because test modules import it: `conftest`
is not a unique module name — every directory with tests may have one, and
pytest puts each of those directories on `sys.path`, so which `conftest` an
`import conftest` finds depends on collection order.
"""

from __future__ import annotations

from pathlib import Path

from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore


class MemoryKit(KdbxStore):
    """The kit's interface over a dictionary rather than a database file.

    A subclass rather than a look-alike so that everything typed against the
    store keeps type-checking, and so the parts a caller does not touch
    (unlocking, the secret store) stay exactly the ones it does not touch: an
    override that drifts from `KdbxStore` is a type error rather than a
    surprise.
    """

    def __init__(self) -> None:
        super().__init__(path=Path('memory.kdbx'))
        self.rows: dict[str, dict[str, str]] = {}
        self.files: dict[str, dict[str, bytes]] = {}

    def _row(self, entry: str) -> dict[str, str]:
        if entry not in self.rows:
            raise KdbxError(f'no entry at {entry}')
        return self.rows[entry]

    def has(self, entry: str) -> bool:
        return entry in self.rows

    def put(self, entry: str, username: str, secret: str) -> None:
        self.rows.setdefault(entry, {}).update({'UserName': username, 'Password': secret})

    def get(self, entry: str, attribute: str = 'Password') -> str:
        return self._row(entry)[attribute]

    def describe(self, entry: str) -> dict[str, str]:
        return {name: value for name, value in self._row(entry).items() if name != 'Password'}

    def entries(self, group: str = '/') -> list[str]:
        return sorted(entry for entry in self.rows if entry.startswith(group.strip('/')))

    def set_attribute(self, entry: str, name: str, value: str, *, protect: bool = True) -> None:
        self._row(entry)[name] = value

    def attribute(self, entry: str, name: str) -> str:
        return self._row(entry)[name]

    def attributes(self, entry: str) -> list[str]:
        return [name for name in self._row(entry) if name not in ('UserName', 'Password')]

    def attach(self, entry: str, filename: str, data: bytes) -> None:
        _ = self._row(entry)
        self.files.setdefault(entry, {})[filename] = data

    def attachment(self, entry: str, filename: str) -> bytes:
        return self.files.get(entry, {})[filename]

    def attachments(self, entry: str) -> list[str]:
        return sorted(self.files.get(entry, {}))
