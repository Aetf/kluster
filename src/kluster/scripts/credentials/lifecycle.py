"""The three things done to a seed kit: build one, replace one, spend one.

`docs/credentials.md` §4 describes them as `bootstrap`, `rotate` and the
credentials `bringup` pushes. They are one module because they share a shape:
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

from . import b2, cloudflare, entries, escrow, masters, oci_iam, pulumi_config, workstation
from .kdbx import KdbxError, KdbxStore
from .masters import Prompt

log = logging.getLogger(__name__)

#: The file inside a client bundle that names the backend. Duplicated from
#: `state_backend.config` rather than imported: that package depends on this
#: one, and one string is a cheaper price than the cycle.
URL_FILE = 'backend-url'


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


def _record_console_seed(seed: entries.Seed, prompt: Prompt, *, into: KdbxStore, entry: str) -> None:
    """Walk the operator through a credential no API can create, and write it.

    Reading and writing are one function because they are one act: a row's
    public identifier and its secret are asked for together and written
    together, and a caller holding one without the other has nothing to do
    with it.
    """
    _announce(seed)

    # The secret comes second and is asked hidden; saying so here is what
    # keeps a token value from being typed into the identifier, echoed.
    identifier = prompt(f'{seed.title} — {seed.identifier} (the secret itself is asked next, hidden): ').strip()
    if not identifier:
        raise KdbxError(f'{seed.title}: {seed.identifier} is required')

    secret = getpass.getpass(f'{seed.title} — the token: ').strip()
    if not secret:
        raise KdbxError(f'{seed.title}: the token is required')
    into.put(entry, identifier, secret)


def create_seed(
    seed: entries.Seed,
    *,
    kit: KdbxStore,
    prompt: Prompt,
    entry: str | None = None,
    registry: escrow.Registry | None = None,
) -> None:
    """Create one §2 row in the kit. Assumes it is not there yet.

    `entry` overrides where the row is written, which is what `seed <member>
    create --entry` passes; the register's own path is the default.
    """
    where = entry or seed.entry
    match seed.member:
        case entries.RECOVERY:
            # The one row with a half that leaves the kit: the recipient is
            # committed, so creating the key and writing `escrow/RECIPIENTS`
            # is a single act rather than a step someone can forget.
            _ = escrow.init(kit, registry or escrow.Registry.open(), entry=where)
        case entries.OCI:
            _ = oci_iam.create_seed(root=root(masters.OCI, prompt), seeds=kit, seed_entry=where)
        case entries.CLOUDFLARE:
            # The one console-made row in the kit, and it is not pasted in
            # blind: its identifier is read off the token and its template is
            # checked before it is stored.
            _ = cloudflare.adopt_seed(token=_read_console_token(seed), seeds=kit, seed_entry=where)
        case entries.B2:
            _ = b2.create_seed(root=root(masters.B2, prompt), seeds=kit, seed_entry=where)
        case _ if seed.manual:
            _record_console_seed(seed, prompt, into=kit, entry=where)
        case _:  # pragma: no cover - every §2 row is one of the above
            raise KdbxError(f'minting {seed.member} is in the register (§2) but not yet implemented')


def backend_url_file(bundle_dir: Path) -> Path | None:
    """The bundle's URL file, or None when this machine has no bundle at all.

    The bundle is a workstation slot (`workstation.py`). A machine that still
    has one where it used to live keeps working, once and loudly: the URL
    written there names the certificates beside it by absolute path, so the
    older bundle is a complete, working answer.

    TODO(kluster-ops#34): delete the fallback once every workstation has
    re-run `state-backend bundle operator`.
    """
    current = bundle_dir / URL_FILE
    if current.is_file():
        return current
    legacy = workstation.LEGACY_BUNDLE_DIR / URL_FILE
    if bundle_dir == workstation.bundle_dir() and legacy.is_file():
        log.warning(
            'using the client bundle in %s: it now belongs in %s, which `state-backend bundle operator` writes',
            workstation.LEGACY_BUNDLE_DIR,
            bundle_dir,
        )
        return legacy
    return None


def environment(
    kit: KdbxStore, bundle_dir: Path, registry: escrow.Registry | None = None
) -> pulumi_config.BackendEnvironment:
    """What a Pulumi run needs here, recovered and read rather than stored.

    The passphrase is recovered from the escrow with the kit's recovery key
    (§2.2), so the one place it exists outside its consumers is a committed
    ciphertext nobody can open without the kit. The URL is read from the bundle
    the appliance's provisioner writes, so the two halves of "log in to the
    backend" come from one command — and a machine with no bundle yet answers
    with no URL, which its caller can see rather than discover inside a
    subprocess.
    """
    vault = escrow.Vault.open(kit, registry)
    url = backend_url_file(bundle_dir)
    if url is None:
        log.warning(
            'no %s; run `state-backend provision` (or `state-backend bundle operator`) first',
            bundle_dir / URL_FILE,
        )
    return pulumi_config.BackendEnvironment(
        passphrase=vault.recover(escrow.PASSPHRASE),
        url=url.read_text().strip() if url is not None else None,
    )


def require_member(only: str | None) -> None:
    """Refuse an `--only` that names no §2 row.

    Checked before either walk starts, and before `rotate` opens its
    successor: a member that matches nothing otherwise walks past every row,
    touches none of them, and reports the empty result as a finished run.
    """
    if only is not None and only not in entries.SEEDS:
        raise KdbxError(f'no seed named {only!r}; expected one of {", ".join(entries.SEEDS)}')


def bootstrap(
    kit: KdbxStore, *, prompt: Prompt, only: str | None = None, registry: escrow.Registry | None = None
) -> list[str]:
    """Fill the kit with every §2 row. Returns the members it created.

    Idempotent by probing: a row already in the kit is left alone, so an
    interrupted bootstrap is resumed by re-running it. That also makes this
    the repair path when one seed is lost -- `--only <member>`.

    It fills the kit and stops there. The escrow's own labels are minted one
    command at a time (`credentials derived <row> generate`), because
    generating the state passphrase is a decision with consequences for every
    stack, not a step a fill-everything command should take on its own.
    """
    require_member(only)
    created: list[str] = []
    for member, seed in entries.SEEDS.items():
        if only is not None and member != only:
            continue
        if kit.has(seed.entry):
            log.info('%s: already in the kit', seed.title)
            continue
        log.info('%s: creating', seed.title)
        create_seed(seed, kit=kit, prompt=prompt, registry=registry)
        created.append(member)
    return created


def rotate(
    kit: KdbxStore,
    successor: KdbxStore,
    *,
    prompt: Prompt,
    only: str | None = None,
    registry: escrow.Registry | None = None,
) -> list[str]:
    """Write a new kit in which every seed has been replaced.

    A *new* database file, per §4.2. The recovery key is the row that makes
    the retired file destroyable: rotating it re-wraps the escrow, so once the
    run is done the old kit opens nothing. Provider seeds behave as they
    always did -- the minted credentials keep working, and each is replaced by
    re-running its own command.

    A seed whose platform can mint its successor does so; the rest stop and
    print their console steps, exactly as at bootstrap.
    """
    require_member(only)
    rotated: list[str] = []
    for member, seed in entries.SEEDS.items():
        if only is not None and member != only:
            continue
        match member:
            case entries.RECOVERY:
                # Pure re-encryption: a successor key, and every ciphertext in
                # the registry re-wrapped to it. No production secret changes
                # value, which is why the two rotations are separable.
                escrow.rotate_recovery(kit, successor, registry or escrow.Registry.open(), entry=seed.entry)
            case entries.OCI:
                # Reads the predecessor from the retired kit, writes the
                # successor into the new one, and leaves the retired file
                # untouched.
                _ = oci_iam.rotate_seed(kit, seed_entry=seed.entry, into=successor)
            case entries.CLOUDFLARE:
                # The platform allows no minted successor, so rotating is the
                # same console visit bring-up made, written into the new kit.
                _ = cloudflare.adopt_seed(token=_read_console_token(seed), seeds=successor, seed_entry=seed.entry)
            case entries.B2:
                _ = b2.rotate_seed(kit, seed_entry=seed.entry, into=successor)
            case _ if seed.manual:
                _record_console_seed(seed, prompt, into=successor, entry=seed.entry)
            case _:
                raise KdbxError(f'rotating {member} is in the register (§2) but not yet implemented')
        rotated.append(member)
    return rotated
