"""Access to the KeePassXC databases the credential scripts read and write.

The seed kit is the canonical offline store (docs/credentials.md §2.1): §2's
rows live in it and nowhere else, and a rotation playbook's "update the offline
store" step writes it. Scripting that step is what keeps the store fresh
without upkeep — the write events *are* the rotation events.

Credentials are also *read* from here, so minting a key never needs its parent
secret in an environment variable or a shell history: the operator types the
master password once and the script takes it from there.

The database is manipulated in-process through `pykeepass` rather than by
shelling out to `keepassxc-cli`. Two reasons: `bootstrap` and `rotate` each
have to *create* a database (§4), which the CLI can do only interactively; and
a library call cannot leak a secret into an argv another process can read.
`pykeepass` ships no type information, so this module is the whole of its
untyped surface -- everything it exports is annotated.

A master password is taken from the desktop secret store (the freedesktop
Secret Service, or the platform equivalent) before anyone is prompted for it.
Bring-up opens two databases -- the kit and the personal estate (§2) -- and
`bootstrap` was going to mean typing two passwords into a script that then
runs for minutes. Nothing is ever written to the store implicitly: `kdbx
remember` is the only thing that puts one there, so a machine that has not
opted in behaves exactly as before.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import getpass
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pykeepass import PyKeePass, create_database
from pykeepass.exceptions import CredentialsError

if TYPE_CHECKING:
    from pykeepass.entry import Entry
    from pykeepass.group import Group

log = logging.getLogger(__name__)

#: The seed kit (credentials.md §2.1). Named by environment variable so the
#: path is configurable and never hard-coded to one machine's layout.
PATH_ENV = 'KLUSTER_KDBX'

#: Secret Service collection entries are keyed by (service, account); the
#: database's path is the account, so the kit and the estate never collide and
#: a database moved to a new path simply stops matching.
KEYRING_SERVICE = 'kluster-credentials'

#: The operator's personal estate, which holds the account roots. Read at
#: bring-up and at re-seeding, never otherwise: the two databases are separate
#: so that everything in the kit is rotatable (§2).
MASTER_PATH_ENV = 'KLUSTER_MASTER_KDBX'

#: The attributes an entry carries natively; anything else is a custom
#: property, which is how a seed records what it is without spending a field.
_NATIVE = ('Title', 'UserName', 'Password', 'URL', 'Notes')


class KdbxError(RuntimeError):
    pass


def _path(entry: str) -> list[str]:
    """An entry path as pykeepass addresses it: `'seeds/B2 seed key'` -> `['seeds', 'B2 seed key']`."""
    return [part for part in entry.strip('/').split('/') if part]


@dataclass
class KdbxStore:
    """One KeePassXC database, unlocked at most once per process.

    The master password is prompted for on first use and kept only in memory
    for the lifetime of the run, so a bring-up that touches the kit five times
    still asks once (§4.1).
    """

    path: Path
    _db: PyKeePass | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, path: Path | None = None, *, env: str = PATH_ENV, flag: str = '--kdbx') -> KdbxStore:
        if path is None:
            raw = os.environ.get(env)
            if not raw:
                raise KdbxError(f'pass {flag} or set {env} to the KeePassXC database')
            path = Path(raw).expanduser()
        if not path.is_file():
            raise KdbxError(f'no database at {path}')
        return cls(path=path)

    @classmethod
    def create(cls, path: Path, password: str) -> KdbxStore:
        """A new, empty database — the output of `bootstrap` and of `rotate`.

        Refuses an existing file: rotation writes a *new* database and the old
        one stays until its last derived secret has expired (§2.2), so
        overwriting is never the intent.
        """
        if path.exists():
            raise KdbxError(f'{path} already exists; rotation writes a new file rather than replacing one')
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(path=path, _db=create_database(str(path), password=password))

    def unlock(self) -> None:
        """Open the database, asking the secret store before asking the operator.

        Opening up front means a wrong password fails before a rotation has
        minted anything, not halfway through one. A stored password that no
        longer opens the database falls through to the prompt rather than
        failing: a stale entry should cost one typed password, not a run.
        """
        if self._db is not None:
            return
        stored = self._remembered()
        if stored is not None:
            try:
                self.unlock_with(stored)
            except KdbxError:
                log.warning('the stored password for %s no longer opens it', self.path.name)
            else:
                return
        self.unlock_with(getpass.getpass(f'master password for {self.path.name}: '))

    def _remembered(self) -> str | None:
        """The stored master password, or None if there is no store at all.

        A headless machine has no Secret Service, and that is not an error --
        it is the case this falls back from.
        """
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, str(self.path))
        except Exception as exc:  # noqa: BLE001 - any backend failure is a miss
            log.debug('no secret store for %s: %s', self.path, exc)
            return None

    def remember(self, password: str) -> None:
        """Put the master password in the desktop secret store.

        Explicit by design: nothing else in this module writes to the store,
        so a password is only there because someone asked for it to be.
        """
        import keyring

        keyring.set_password(KEYRING_SERVICE, str(self.path), password)
        log.info('stored the master password for %s in %s', self.path.name, keyring.get_keyring().name)

    def forget(self) -> None:
        """Remove it again."""
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(KEYRING_SERVICE, str(self.path))
        except keyring.errors.PasswordDeleteError as exc:
            raise KdbxError(f'no stored password for {self.path}') from exc
        log.info('removed the stored password for %s', self.path.name)

    def unlock_with(self, password: str) -> None:
        """Open with a password already in hand.

        Bring-up holds two databases open at once (the kit and the estate,
        §2), and a caller that has prompted for itself should not be made to
        prompt again through this class.
        """
        if self._db is not None:
            return
        try:
            self._db = PyKeePass(str(self.path), password=password)
        except CredentialsError as exc:
            raise KdbxError(f'could not unlock {self.path}') from exc

    @property
    def _open(self) -> PyKeePass:
        self.unlock()
        assert self._db is not None
        return self._db

    def _entry(self, entry: str) -> Entry:
        # A path lookup matches at most one entry, but the signature admits a
        # list; collapse both shapes here so no caller has to.
        found = cast('Entry | list[Entry] | None', self._open.find_entries(path=_path(entry)))
        if isinstance(found, list):
            found = found[0] if found else None
        if found is None:
            raise KdbxError(f'no entry {entry!r} in {self.path} (try: credentials kdbx ls)')
        return found

    def entries(self, group: str = '/') -> list[str]:
        """Entry paths under `group`, so a caller can be told what exists."""
        prefix = _path(group)
        found = cast('list[Entry]', self._open.entries or [])
        paths = ('/'.join(part for part in entry.path if part) for entry in found if entry.path)
        return sorted(path for path in paths if _path(path)[: len(prefix)] == prefix)

    def describe(self, entry: str) -> dict[str, str]:
        """The entry's non-secret attributes — enough to diagnose a wrong field."""
        found = self._entry(entry)
        return {
            'Title': str(found.title or ''),
            'UserName': str(found.username or ''),
            'URL': str(found.url or ''),
            'Notes': str(found.notes or ''),
        }

    def get(self, entry: str, attribute: str = 'Password') -> str:
        found = self._entry(entry)
        match attribute:
            case 'Password':
                value = found.password
            case 'UserName':
                value = found.username
            case 'Title':
                value = found.title
            case 'URL':
                value = found.url
            case 'Notes':
                value = found.notes
            case custom:
                value = found.get_custom_property(custom)
        if value is None:
            raise KdbxError(f'entry {entry!r} has no {attribute}')
        return str(value)

    def _group(self, parts: list[str]) -> Group:
        """The group at `parts`, creating every level that is missing."""
        group = cast('Group', self._open.root_group)
        for depth in range(1, len(parts) + 1):
            existing = cast('Group | list[Group] | None', self._open.find_groups(path=parts[:depth]))
            if isinstance(existing, list):
                existing = existing[0] if existing else None
            group = existing if existing is not None else self._open.add_group(group, parts[depth - 1])
        return group

    def put(self, entry: str, username: str, secret: str) -> None:
        """Create `entry`, or replace the password of an existing one.

        Idempotent by design: a rotation playbook re-runs the same call. The
        database is saved on every write, so an interrupted run leaves the
        writes that already succeeded on disk rather than in memory.
        """
        parts = _path(entry)
        existing = cast('Entry | list[Entry] | None', self._open.find_entries(path=parts))
        if isinstance(existing, list):
            existing = existing[0] if existing else None
        if existing is None:
            _ = self._open.add_entry(self._group(parts[:-1]), parts[-1], username, secret)
            verb = 'added'
        else:
            existing.username = username
            existing.password = secret
            verb = 'edited'
        self._open.save()
        log.info('kdbx: %s %s', verb, entry)
