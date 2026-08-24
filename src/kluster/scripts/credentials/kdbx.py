"""Access to the cluster's dedicated KeePassXC database.

The database is the canonical offline store (docs/credentials.md §2.1): §2's
rows live in it and nowhere else, and a rotation playbook's "update the offline
store" step writes it. Scripting that step is what keeps the store fresh
without upkeep — the write events *are* the rotation events.

Credentials are also *read* from here, so minting a key never needs its parent
secret in an environment variable or a shell history: the operator types the
master password once and the script takes it from there.

Runs on the machine holding the database (`keepassxc-cli` required); the two
USB copies of the kit are refreshed from it at rotation.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess as sp
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Environment variable naming the database, so the path is configurable and
#: never hard-coded to one machine's layout.
PATH_ENV = 'KLUSTER_KDBX'


class KdbxError(RuntimeError):
    pass


@dataclass
class KdbxStore:
    """One unlocked KeePassXC database.

    The master password is prompted for once per process and kept only in
    memory for the lifetime of the run.
    """

    path: Path
    _password: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, path: Path | None = None) -> KdbxStore:
        if path is None:
            raw = os.environ.get(PATH_ENV)
            if not raw:
                raise KdbxError(f'pass --kdbx or set {PATH_ENV} to the cluster KeePassXC database')
            path = Path(raw).expanduser()
        if not path.is_file():
            raise KdbxError(f'no database at {path}')
        if shutil.which('keepassxc-cli') is None:
            raise KdbxError('keepassxc-cli not found — run this on the machine holding the database')
        return cls(path=path)

    def unlock(self) -> None:
        """Ask for the master password and prove it opens the database.

        Verifying up front means a wrong password fails before a rotation has
        minted anything, not halfway through one.
        """
        if self._password is not None:
            return
        password = getpass.getpass(f'master password for {self.path.name}: ')
        proc = self._run(['ls', '-q', str(self.path)], password=password, check=False)
        if proc.returncode != 0:
            raise KdbxError(f'could not unlock {self.path}')
        self._password = password

    def _run(self, args: list[str], *, password: str | None = None, stdin: str = '', check: bool = True) -> sp.CompletedProcess[str]:
        if password is None:
            self.unlock()
            password = self._password
        assert password is not None
        return sp.run(
            ['keepassxc-cli', *args],
            input=f'{password}\n{stdin}',
            capture_output=True,
            text=True,
            check=check,
        )

    def entries(self, group: str = '/') -> list[str]:
        """Entry paths under `group`, so a caller can be told what exists."""
        proc = self._run(['ls', '-q', '-R', '-f', str(self.path), group])
        return [line for line in proc.stdout.splitlines() if line and not line.endswith('/')]

    def get(self, entry: str, attribute: str = 'Password') -> str:
        # -s: without it a protected attribute prints as 'PROTECTED'.
        proc = self._run(['show', '-q', '-s', '-a', attribute, str(self.path), entry], check=False)
        if proc.returncode != 0:
            raise KdbxError(f'no entry {entry!r} in {self.path} (try: credentials kdbx ls)')
        return proc.stdout.strip()

    def _ensure_group(self, entry: str) -> None:
        """Create the entry's parent groups; `add` will not create them."""
        parts = [part for part in entry.strip('/').split('/')[:-1] if part]
        for depth in range(1, len(parts) + 1):
            group = '/'.join(parts[:depth])
            # mkdir fails when the group already exists, which is not an error
            # here — every call before the last one is expected to.
            _ = self._run(['mkdir', '-q', str(self.path), group], check=False)

    def put(self, entry: str, username: str, secret: str) -> None:
        """Create `entry`, or replace the password of an existing one.

        Idempotent by design: a rotation playbook re-runs the same call.
        """
        self._ensure_group(entry)
        exists = self._run(['show', '-q', str(self.path), entry], check=False).returncode == 0
        verb = 'edit' if exists else 'add'
        # keepassxc-cli consumes the database password first, then the entry's.
        _ = self._run(
            [verb, '-q', str(self.path), entry, '--username', username, '--password-prompt'],
            stdin=f'{secret}\n',
        )
        log.info('kdbx: %sed %s', verb, entry)
