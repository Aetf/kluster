"""The three things done to a seed kit: build one, replace one, spend one.

`docs/credentials.md` §4 describes them as `bootstrap`, `rotate` and the
derivations `bringup` pushes. They are one module because they share a shape:
walk §2's table in order, and for each row either call the platform that can
mint it or stop and print what a human must do in a console.

Two properties are the point:

-   **Resumable by probing, not by bookkeeping.** A stage asks whether its
    output exists and skips if it does. A checkpoint file would record "this
    ran", which stops being true the moment someone deletes a key in a
    console -- and the run after that would skip the repair.
-   **One master password.** The kit and the personal estate are opened once
    and passed down, so a bootstrap that pauses for two console visits does
    not ask again on the way back.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import b2, entries, seeds
from .kdbx import KdbxError, KdbxStore

log = logging.getLogger(__name__)

#: Reading a secret from the operator. Injected so tests do not need a
#: terminal, and so a future non-interactive mode has one place to change.
Prompt = Callable[[str], str]


@dataclass
class Estate:
    """Where the account roots live (§2), opened only if something needs one.

    Account roots are not in the kit, so minting a seed from one crosses two
    databases. Bootstrap is the only operation that does.
    """

    store: KdbxStore | None
    entries_by_member: dict[str, str]

    def master_entry(self, member: str) -> str:
        entry = self.entries_by_member.get(member)
        if entry is None:
            raise KdbxError(
                f'no account-root entry given for {member}; pass --master-entry {member}=<entry in the estate>'
            )
        return entry

    def opened(self) -> KdbxStore:
        if self.store is None:
            raise KdbxError('no personal estate given; pass --master-kdbx or set KLUSTER_MASTER_KDBX')
        return self.store


def _read_console_seed(seed: entries.Seed, prompt: Prompt) -> tuple[str, str, bytes | None]:
    """Walk the operator through a credential no API can create."""
    log.warning('%s cannot be minted; it has to be created in a console:', seed.title)
    for line in seed.console.splitlines():
        log.warning('  %s', line)

    identifier = prompt(f'{seed.title} — {seed.identifier}: ').strip()
    if not identifier:
        raise KdbxError(f'{seed.title}: {seed.identifier} is required')

    payload: bytes | None = None
    if seed.attachment:
        path = Path(prompt(f'{seed.title} — path to {seed.attachment}: ').strip()).expanduser()
        payload = path.read_bytes()
        secret = ''
    else:
        secret = getpass.getpass(f'{seed.title} — the token: ').strip()
        if not secret:
            raise KdbxError(f'{seed.title}: the token is required')
    return identifier, secret, payload


def create_seed(seed: entries.Seed, *, kit: KdbxStore, estate: Estate, prompt: Prompt) -> None:
    """Create one §2 row in the kit. Assumes it is not there yet."""
    if seed.member == 'derivation':
        seeds.init_seed(kit, seed.entry)
        return
    if seed.member == 'b2':
        _ = b2.create_seed(
            master=estate.opened(),
            seeds=kit,
            master_entry=estate.master_entry('b2'),
            seed_entry=seed.entry,
        )
        return
    if seed.manual:
        identifier, secret, payload = _read_console_seed(seed, prompt)
        kit.put(seed.entry, identifier, secret)
        if payload is not None and seed.attachment:
            kit.attach(seed.entry, seed.attachment, payload)
        return
    raise KdbxError(f'minting {seed.member} is in the register (§2) but not yet implemented')


def bootstrap(kit: KdbxStore, *, estate: Estate, prompt: Prompt, only: str | None = None) -> list[str]:
    """Fill the kit with every §2 row. Returns the members it created.

    Idempotent by probing: a row already in the kit is left alone, so an
    interrupted bootstrap is resumed by re-running it. That also makes this
    the repair path when one seed is lost -- `--only <member>`.
    """
    created: list[str] = []
    for member, seed in entries.SEEDS.items():
        if only is not None and member != only:
            continue
        if kit.has(seed.entry):
            log.info('%s: already in the kit', seed.title)
            continue
        create_seed(seed, kit=kit, estate=estate, prompt=prompt)
        created.append(member)
    if only is not None and only not in entries.SEEDS:
        raise KdbxError(f'no seed named {only!r}; expected one of {", ".join(entries.SEEDS)}')
    return created


def rotate(kit: KdbxStore, successor: KdbxStore, *, prompt: Prompt, only: str | None = None) -> list[str]:
    """Write a new kit in which every seed has been replaced.

    A *new* database file, per §4.2: the retired one stays until the last
    secret derived from it has expired, which for a backup key means until
    the last dump under it is out of retention (§2.2).

    A seed whose platform can mint its successor does so; the rest stop and
    print their console steps, exactly as at bootstrap.
    """
    rotated: list[str] = []
    for member, seed in entries.SEEDS.items():
        if only is not None and member != only:
            continue
        if member == 'derivation':
            # Generated, not minted: the successor is fresh random bytes.
            seeds.store_seed(successor, seeds.generate_seed(), seed.entry)
        elif member == 'b2':
            # Reads the predecessor from the retired kit, writes the successor
            # into the new one, and leaves the retired file untouched.
            _ = b2.rotate_seed(kit, seed_entry=seed.entry, into=successor)
        elif seed.manual:
            identifier, secret, payload = _read_console_seed(seed, prompt)
            successor.put(seed.entry, identifier, secret)
            if payload is not None and seed.attachment:
                successor.attach(seed.entry, seed.attachment, payload)
        else:
            raise KdbxError(f'rotating {member} is in the register (§2) but not yet implemented')
        rotated.append(member)
    return rotated
