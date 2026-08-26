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
-   **One password, and only for the kit.** The kit is unlocked once and
    passed down, so a bootstrap that pauses for two console visits does not
    ask again on the way back. The account roots a mint needs are not in a
    database at all: they come from the desktop secret store, or from a
    prompt when there is none (`masters.py`).
"""

from __future__ import annotations

import getpass
import logging
from pathlib import Path

from . import b2, cloudflare, entries, masters, oci_iam, seeds
from .kdbx import KdbxError, KdbxStore
from .masters import Prompt

log = logging.getLogger(__name__)


def root(member: str, prompt: Prompt) -> masters.Credential:
    """The account root a mint needs, read at the moment it is needed.

    Late rather than up front: a bootstrap that has to stop for a console
    visit should not have asked for credentials it never reached.
    """
    return masters.load(masters.ROOTS[member], prompt)


def _announce(seed: entries.Seed) -> None:
    """Print what a human has to do in a console, at the moment they must."""
    log.warning('%s cannot be minted; it has to be created in a console:', seed.title)
    for line in seed.console.splitlines():
        log.warning('  %s', line)


def _read_console_token(seed: entries.Seed) -> str:
    """A console credential that is one secret and nothing else.

    The row's identifier is not asked for: where a platform can be asked who
    a token is, asking the operator instead only adds a way for the two to
    disagree.
    """
    _announce(seed)
    secret = getpass.getpass(f'{seed.title} — the token: ').strip()
    if not secret:
        raise KdbxError(f'{seed.title}: the token is required')
    return secret


def _read_console_seed(seed: entries.Seed, prompt: Prompt) -> tuple[str, str, bytes | None]:
    """Walk the operator through a credential no API can create."""
    _announce(seed)

    # The secret comes second and is asked hidden; saying so here is what
    # keeps a token value from being typed into the identifier, echoed.
    identifier = prompt(f'{seed.title} — {seed.identifier} (the secret itself is asked next, hidden): ').strip()
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


def create_seed(seed: entries.Seed, *, kit: KdbxStore, prompt: Prompt, entry: str | None = None) -> None:
    """Create one §2 row in the kit. Assumes it is not there yet.

    `entry` overrides where the row is written, which is what `seed <member>
    create --entry` passes; the register's own path is the default.
    """
    where = entry or seed.entry
    match seed.member:
        case 'derivation':
            seeds.init_seed(kit, where)
        case 'oci':
            _ = oci_iam.create_seed(root=root('oci', prompt), seeds=kit, seed_entry=where)
        case 'cloudflare':
            # Console-made, like the rows below, but its identifier is read
            # off the token and its template is checked before it is stored.
            _ = cloudflare.adopt_seed(token=_read_console_token(seed), seeds=kit, seed_entry=where)
        case 'b2':
            _ = b2.create_seed(root=root('b2', prompt), seeds=kit, seed_entry=where)
        case _ if seed.manual:
            identifier, secret, payload = _read_console_seed(seed, prompt)
            kit.put(where, identifier, secret)
            if payload is not None and seed.attachment:
                kit.attach(where, seed.attachment, payload)
        case _:  # pragma: no cover - every §2 row is one of the above
            raise KdbxError(f'minting {seed.member} is in the register (§2) but not yet implemented')


def environment(kit: KdbxStore, bundle_dir: Path) -> dict[str, str]:
    """The variables a Pulumi run needs, derived and read rather than stored.

    `PULUMI_CONFIG_PASSPHRASE` is a derivation of the seed (§2.2) and lives in
    no slot an operator can read, so before this there was no way to obtain it
    -- the state-backend README told the reader to export it without saying
    where from. `PULUMI_BACKEND_URL` is read from the bundle the appliance's
    provisioner writes, so the two halves of "log in to the backend" come from
    one command.
    """
    values = {'PULUMI_CONFIG_PASSPHRASE': seeds.pulumi_passphrase(seeds.load_seed(kit))}
    url = bundle_dir / 'backend-url'
    if url.is_file():
        values['PULUMI_BACKEND_URL'] = url.read_text().strip()
    else:
        log.warning('no %s; run `state-backend provision` (or `state-backend bundle operator`) first', url)
    return values


def bootstrap(kit: KdbxStore, *, prompt: Prompt, only: str | None = None) -> list[str]:
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
        log.info('%s: creating', seed.title)
        create_seed(seed, kit=kit, prompt=prompt)
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
        elif member == 'oci':
            # Reads the predecessor from the retired kit, writes the successor
            # into the new one, and leaves the retired file untouched.
            _ = oci_iam.rotate_seed(kit, seed_entry=seed.entry, into=successor)
        elif member == 'cloudflare':
            # The platform allows no minted successor, so rotating is the
            # same console visit bring-up made, written into the new kit.
            _ = cloudflare.adopt_seed(token=_read_console_token(seed), seeds=successor, seed_entry=seed.entry)
        elif member == 'b2':
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
