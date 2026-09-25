"""The three things done to a seed kit: build one, replace one, spend one.

`kit bootstrap` builds a kit and `kit rotate` replaces one (`docs/credentials.md`
§4); a `derived` command that writes a stack's config spends one, recovering
from it what opening that stack takes (`environment`). The first two share a
shape: walk §2's table in order, and for each row either call the platform
that can mint it or stop and print what a human must do in a console.

Two properties are the point:

-   **Resumable by probing, not by bookkeeping.** A stage asks whether its
    output exists and skips if it does -- and what it asks about is the row
    in the kit, not the credential behind it at a platform. A checkpoint file
    would record "this ran", which stops being true the moment a row is
    deleted out of the kit -- and the run after that would skip the repair.
    `bootstrap` probes the kit it fills. `rotate` probes the successor it
    writes, and where a row is there it finishes that row's retirement rather
    than minting again; the one thing it records is the successor's lineage
    (`KdbxStore.mark_successor_of`), which says what the file is and not what
    ran.
-   **One password, and only for the kit.** The kit is unlocked once and
    passed down, so a bootstrap that pauses for two console visits does not
    ask again on the way back. The account roots a mint needs are not in the
    kit at all: each is looked up through the chain `masters.py` sets out,
    which ends in a prompt.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import b2, cloudflare, entries, escrow, masters, oci_iam, pulumi_config
from .kdbx import KdbxError, KdbxStore
from .masters import CredentialRejected, Prompt

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


def _ask_token(seed: entries.Seed) -> str:
    """One hidden paste of a console credential's secret; empty is a refusal to give one."""
    secret = getpass.getpass(f'{seed.title} — the token: ').strip()
    if not secret:
        raise KdbxError(f'{seed.title}: the token is required')
    return secret


def _read_console_token(seed: entries.Seed) -> str:
    """A console credential that is one secret and nothing else.

    The row's identifier is not asked for: where a platform can be asked who
    a token is, asking the operator instead only adds a way for the two to
    disagree.
    """
    _announce(seed)
    return _ask_token(seed)


def _left_behind(seed: entries.Seed, rotated: Sequence[str], into: KdbxStore) -> str:
    """What stopping a rotation at `seed`'s row leaves in the two kits, and how it goes on.

    Said by the refusal that stops there, because nothing else says it: the
    rows already in the successor are the ones whose predecessors the retired
    kit can no longer use, and the run that finishes the rest is the same
    command with the same `--into` (`rotate`).
    """
    held = (
        f'holds {", ".join(rotated)}, whose predecessors in the retired kit no longer work'
        if rotated
        else 'holds no row'
    )
    # What this run knows of the console row is that it wrote nothing for it
    # and retired nothing of it; whether the retired kit's token still works
    # is the dashboard's to say, and the console steps invite deleting it.
    return (
        f'the successor kit {into.path} {held}; it does not hold {seed.member}, whose row in the retired kit '
        'this run did not touch, and no row after it was rotated; re-run the same command with the same '
        '`--into` to resume at this row'
    )


def _adopt_pasted_seed(seed: entries.Seed, *, into: KdbxStore, rotated: Sequence[str]) -> str:
    """The console-made row of a rotation: a refused paste is asked again.

    Every refusal `cloudflare.adopt_seed` raises -- the value is not a token,
    the wrong template, no zone visible, the wrong account -- is one the
    operator fixes on the dashboard page they are standing on, so asking
    again costs one paste where stopping costs a second run of `rotate` and a
    second console visit. The line between what is asked again and what is
    raised is `CredentialRejected`'s own -- the API said no -- so the network,
    which no page fixes, is raised as it is. Nothing is stored by a refused
    paste; `adopt_seed` writes the row after its last check.

    Stopping is Ctrl-C or end of input, and the error that stops the run says
    what state the two kits are left in -- as a `KdbxError`, so it ends the
    command the way every other refusal does, with that message and no
    traceback. An empty paste is asked again like a refused one: a copy that
    did not take is the very class of slip the re-ask exists to make cheap,
    and a token is never empty, so nothing is lost by not treating it as the
    stop. That leaves the two keystrokes an operator reaches for to stop as
    the only ones that do.
    """
    _announce(seed)
    while True:
        try:
            token = getpass.getpass(f'{seed.title} — the token (Ctrl-C to stop): ').strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise KdbxError(f'{seed.title}: stopped at the paste; {_left_behind(seed, rotated, into)}') from exc
        if not token:
            log.warning('nothing pasted; paste the token, or Ctrl-C to stop')
            continue
        try:
            return cloudflare.adopt_seed(token=token, seeds=into, seed_entry=seed.entry)
        except CredentialRejected as exc:
            log.error('%s: refused, and nothing was stored: %s', seed.title, exc)
            # The paste at this prompt is the only command the operator is
            # given: no refusal names one (`require_zone_visibility` says why),
            # and `seed cloudflare create` run while `rotate` waits here would
            # record the token in the retired kit.
            log.warning('fix it on the dashboard page and paste the token again at this prompt, or Ctrl-C to stop')


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

    into.put(entry, identifier, _ask_token(seed))


def create_seed(
    seed: entries.Seed,
    *,
    kit: KdbxStore,
    prompt: Prompt,
    entry: str | None = None,
    registry: escrow.Registry | None = None,
) -> None:
    """Create one §2 row in the kit, over one already there -- except recovery.

    Nothing here probes the kit. Every provider row is written through
    `kdbx.put`, which replaces an existing entry's identifier and its secret
    both, and the OCI key file through `kdbx.attach`, which deletes an
    attachment of the same name first; B2 then retires the seed's other keys
    at the platform and OCI the user's, so a run against a present row leaves
    one credential standing, the new one (a superseded Cloudflare token stays,
    for the dashboard to delete). That is what makes `seed <member> create`
    the repair for a row that is present in the kit but dead at its platform
    -- the state `bootstrap`'s probe skips. The recovery row is the exception:
    `escrow.init` refuses a present row and a present `escrow/RECIPIENTS`,
    because every ciphertext opens with that one key and nothing else, and
    replacing it deliberately is `kit rotate`.

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
    """The bundle's URL file, or None when `bundle_dir` holds no bundle.

    The directory given is the only one looked in: the default is the
    workstation slot (`workstation.py`), and no location outside the checkout
    stands in for it.
    """
    current = bundle_dir / URL_FILE
    return current if current.is_file() else None


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
        apart=_apart(vault),
    )


def _apart(vault: escrow.Vault) -> dict[str, str]:
    """The passphrase of every stack that is not on the estate's, by stack name.

    **Walked from `pulumi_config.APART`, not from a list beside it.** That
    census already answers "which stacks are encrypted apart, and from which
    register row", and its values are row names, which `escrow.rows()` turns
    into the labels this recovers — so the two cannot disagree about which
    stacks there are. A second list here would fail *closed* rather than open,
    since a stack it forgot would refuse by name rather than fall back to the
    estate passphrase, but it would refuse telling an operator to run a
    `generate` they have already run, which is a bad half hour.

    An escrow with no generation yet is left out rather than raised on, which
    is the state a machine is in between the row being declared and the
    operator running its `generate`. Leaving it out is what makes the refusal
    the one `BackendEnvironment.variables` gives — which names the stack and
    the command — instead of an escrow error naming a label, raised here while
    building an environment most commands never point at that stack anyway.

    A row the escrow register does not carry is the one thing raised on: that
    is not a machine missing a value, it is `APART` naming a row that does not
    exist, and it would otherwise read as the absent-generation case forever.
    """
    labels = escrow.rows()
    found: dict[str, str] = {}
    for stack, row in pulumi_config.APART.items():
        label = labels[row].name
        try:
            found[stack] = vault.recover(label)
        except escrow.EscrowError as exc:
            log.debug('no %s in the escrow yet (%s); the %s stack will refuse by name', label, exc, stack)
    return found


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
    interrupted bootstrap is resumed by re-running it, and `--only <member>`
    is the walk confined to one row -- the repair for a row the kit has lost.
    The probe is `kit.has` and nothing else: a row the kit still holds is
    skipped whatever has become of the credential behind it at its platform,
    and that state is `seed <member> create`'s (`create_seed`, which
    overwrites a provider row) -- for every row but the recovery key, which
    `seed recovery create` refuses and `kit rotate` replaces.

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


def prove_account(kit: KdbxStore, seed: entries.Seed) -> None:
    """Hold one row's account against what `conventions` records, changing nothing.

    **The account check each self-reproducing row's `rotate_seed` makes before
    its own first write, and nothing else**: reachable on its own so that
    `rotate` can make every row's before any row's first write. What it takes
    to answer is the platform's business -- the OCI tenancy is stored on the
    row, and the B2 account is knowable only by authorizing as the seed -- so
    what a check happens to catch on the way (a B2 key that no longer
    authenticates, the network) is incidental, and what falls outside the
    definition lands at the row as it always did: a dead OCI key is met by the
    OCI row's own listing, once the recovery row has re-wrapped -- its repair
    is `credentials --kdbx <successor> seed oci create`, after which the same
    `kit rotate --into` resumes past the row. The recovery key has no account,
    and a console-made row has no credential until the walk reaches it, so
    neither has an account check.

    The row handed in is the live one (`live`): the successor's where it
    already holds a complete row, the kit's otherwise. A resumed run's B2 row
    is one the account no longer accepts once the first run retired it, and
    the check authorizes as the row it is handed.

    A seed family that mints its own successor adds its account check here as
    well as to its own rotation -- here is what keeps the refusal ahead of
    every other row's retirement, there is what keeps it ahead of its own when
    the row is rotated alone -- and until it does, its rotation is refused by
    name rather than walked past.
    """
    match seed.member:
        case entries.OCI:
            oci_iam.verify_tenancy(oci_iam.load_seed(kit, seed.entry).tenancy)
        case entries.B2:
            b2.verify_account(b2.Session.from_entry(kit, seed.entry).account_id)
        case _ if seed.mints_own_successor:
            raise KdbxError(
                f'rotating {seed.member} is in the register (§2) but its account check is not in the pre-flight'
            )
        case _:
            pass


@dataclass(frozen=True)
class Successor:
    """Where a rotation writes: the file at `path`, opened when it exists and created when it does not.

    Two callables rather than a store, because which of them runs is
    `rotate`'s to decide and when is too: an existing file is opened before
    the pre-flight, so that its rows are what the pre-flight checks, and a
    new one is created after it, so that a refusal leaves no file behind.
    """

    path: Path
    #: Unlocks an existing file.
    open: Callable[[Path], KdbxStore]
    #: Makes a new one. Called only once every refusal the pre-flight can
    #: raise has passed, and not at all where the file exists.
    create: Callable[[Path], KdbxStore]


def holds(store: KdbxStore, seed: entries.Seed) -> bool:
    """Whether `store` holds a complete row for `seed`: its own reader would succeed on it.

    Each family that has a reader answers for itself (`escrow.holds_recovery`,
    `oci_iam.holds_seed`, `b2.holds_seed`); a console-made row is complete
    when both halves `put` writes are there. A row that fails this is treated
    as absent and written over, which every writer does on its own.
    """
    match seed.member:
        case entries.RECOVERY:
            return escrow.holds_recovery(store, entry=seed.entry)
        case entries.OCI:
            return oci_iam.holds_seed(store, seed.entry)
        case entries.B2:
            return b2.holds_seed(store, seed.entry)
        case _:
            return (
                store.has(seed.entry)
                and bool(store.get(seed.entry, attribute='UserName'))
                and bool(store.get(seed.entry))
            )


def live(kit: KdbxStore, into: KdbxStore | None, seed: entries.Seed) -> KdbxStore:
    """The kit whose row for `seed` is the live credential: the successor where it holds one, else the kit.

    Once a row has rotated its predecessor is retired at the platform, so a
    resumed run that read the kit's row would be refused by the platform for
    a key it no longer has (`prove_account`), or -- worse -- pass and mint a
    second successor over the only private half of the first.
    """
    if into is not None and holds(into, seed):
        return into
    return kit


def require_successor_of(kit: KdbxStore, into: KdbxStore) -> None:
    """Refuse an existing `--into` that is not `kit`'s own successor (§4.2).

    The successor's lineage marker names the predecessor it was written from,
    and that is what is checked -- not the rows, which cannot tell an older
    retired kit of this estate from a successor that died before its first
    row, and not the path, which cannot see a copy. What fails here: the kit
    itself or a copy of it (one database identity), a kit `bootstrap` wrote
    (no marker), and a successor of some other kit -- an older retired kit
    among them, whose recovery row the re-wrap would otherwise reuse as the
    new one.
    """
    if into.uuid == kit.uuid:
        raise KdbxError(
            f'{into.path} is the kit being rotated, or a copy of it (the same database identity as '
            f'{kit.path}); a rotation writes a new file, and resumes only into the one it was writing'
        )
    predecessor = into.predecessor_uuid()
    if predecessor is None:
        raise KdbxError(
            f'{into.path} exists and carries no lineage marker, so it is not the successor of {kit.path}: '
            f'if `credentials --kdbx {into.path} kit ls` shows no row it is a successor that died before its '
            'marker was written, and is deleted by hand; if it shows rows it is some other kit, and a rotation '
            'writes a new file'
        )
    if predecessor != kit.uuid:
        raise KdbxError(
            f'{into.path} is the successor of another kit ({predecessor}), not of {kit.path} ({kit.uuid}): a '
            'retired kit of this estate, or a successor written from a different one; a rotation resumes only '
            'into the successor it was writing'
        )


def rotate(
    kit: KdbxStore,
    successor: Successor,
    *,
    prompt: Prompt,
    only: str | None = None,
    registry: escrow.Registry | None = None,
) -> list[str]:
    """Write a new kit in which every seed has been replaced. Returns every member the walk completed.

    A *new* database file, per §4.2. The recovery key is the row that makes
    the retired file destroyable: rotating it re-wraps the escrow, so once the
    run is done the old kit opens nothing. Provider seeds behave as they
    always did -- the minted credentials keep working, and each is replaced by
    re-running its own command.

    A seed whose platform can mint its successor does so; the rest stop and
    print their console steps, as at bootstrap -- with one difference: a
    paste the dashboard made wrong is asked for again rather than raised
    (`_adopt_pasted_seed`), because here the refusal would land after the
    rows before it have retired their predecessors.

    **An interrupted run is resumed by running the same command again.** The
    successor is opened where it exists and created where it does not
    (`Successor`); an existing file is accepted only as this kit's own
    successor (`require_successor_of`). Every arm then converges on a
    successor that already holds its row: it mints nothing and asks for
    nothing, and finishes the retirement it owes -- the recovery arm re-wraps
    to the key the successor holds, opening every ciphertext with that key or
    the retired one; the OCI and B2 arms authorize as the successor's key and
    retire every other; the Cloudflare arm re-verifies the stored token
    without a paste; a manual row is skipped. A completed rotation re-run is
    therefore a no-op at every platform, and reports every row. The retired
    kit is read and never written.

    **Every account refusal is raised before the walk starts** (`prove_account`,
    which says what that is and is not), against the live row of each seed
    (`live`): a refusal there costs nothing -- no predecessor retired, no row
    written, no successor file made, no console visit asked for, no second
    run. Ordering the walk would not do instead: each row's own check sits
    directly above its own retirement, so whichever row went first would
    still have retired before the next row's check ran. The successor is
    created after those checks for the same reason, one step earlier:
    creating a KeePass file writes it, and a file a refusal left behind would
    be an empty successor to open on the re-run. Its lineage marker is
    written at creation, before any row, so that the re-run finds it.
    """
    require_member(only)
    walk = [(member, seed) for member, seed in entries.SEEDS.items() if only is None or member == only]
    into: KdbxStore | None = None
    if successor.path.exists():
        into = successor.open(successor.path)
        require_successor_of(kit, into)
        log.info('resuming into %s: a row it already holds is finished, not rotated again', successor.path)
    log.info('holding every seed to be rotated against the account `conventions` records, before any row rotates')
    for _, seed in walk:
        prove_account(live(kit, into, seed), seed)
    if into is None:
        into = successor.create(successor.path)
        into.mark_successor_of(kit.uuid)
    rotated: list[str] = []
    for member, seed in walk:
        match member:
            case entries.RECOVERY:
                # Pure re-encryption: a successor key, and every ciphertext in
                # the registry re-wrapped to it. No production secret changes
                # value, which is why the two rotations are separable.
                escrow.rotate_recovery(kit, into, registry or escrow.Registry.open(), entry=seed.entry)
            case entries.OCI:
                # Reads the live row -- the successor's where it holds one,
                # the retired kit's otherwise -- writes the successor into the
                # new kit, and leaves the retired file untouched.
                _ = oci_iam.rotate_seed(kit, seed_entry=seed.entry, into=into)
            case entries.CLOUDFLARE:
                # The platform allows no minted successor, so rotating is the
                # same console visit bring-up made, written into the new kit
                # -- and a paste the dashboard made wrong is asked for again
                # rather than ending a run that has already retired the rows
                # before it. A token the successor already holds is verified
                # again rather than asked for: the same checks a paste gets,
                # and the same row written back. Nothing at the platform is
                # retired by this program either way.
                if holds(into, seed):
                    log.info('%s: the successor kit already holds a token; verifying it', seed.title)
                    _ = cloudflare.adopt_seed(token=into.get(seed.entry), seeds=into, seed_entry=seed.entry)
                else:
                    _ = _adopt_pasted_seed(seed, into=into, rotated=rotated)
            case entries.B2:
                _ = b2.rotate_seed(kit, seed_entry=seed.entry, into=into)
            case _ if seed.manual:
                # No verifier to run: a row the successor holds is what the
                # operator pasted, and asking again would replace it.
                if holds(into, seed):
                    log.info('%s: already in the successor kit', seed.title)
                else:
                    _record_console_seed(seed, prompt, into=into, entry=seed.entry)
            case _:
                raise KdbxError(f'rotating {member} is in the register (§2) but not yet implemented')
        rotated.append(member)
    return rotated
