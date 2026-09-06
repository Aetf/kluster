"""`state-backend` — provision, inspect, re-provision, dump and restore the appliance.

`provision` is idempotent end to end: it is equally the bring-up command and
the re-provision command, which is what keeps the rebuild path warm. It is not
therefore harmless — replacing the box destroys every stack's state that is
not in a dump — so a run that finds drift on a box that exists reports it and
stops, and `--force` is how a replacement is asked for. `dump` and `restore`
are the other half of that path: every playbook that replaces the box is a
dump, a provision and a restore (physical/state-backend.md §7), with the dump
taken by the converge itself, and each of them verifies rather than reports.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from kluster.scripts.credentials import b2, entries, escrow, pki, workstation
from kluster.scripts.credentials.age import AgeError
from kluster.scripts.credentials.escrow import EscrowError
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.workstation import WorkstationError

from . import config, provision, settings, state
from .state import StateError

log = logging.getLogger(__name__)

#: What `provision` exits with when it replaced the box and the state is not
#: back in it. Neither 0 — 5432 answering over an empty database is not the end
#: of the operation, and a caller that stops reading at the exit code would
#: otherwise be told the appliance is ready — nor 1, which says that the run
#: failed and nothing more: a run can fail before it touches anything, or
#: after it has already destroyed the box, and which it was is in the run's
#: last words rather than in its status. This one always means the same thing,
#: which is what a caller can branch on. A wrapper reads it, so it is
#: published rather than internal: `provision --help` and
#: deploy/state-backend/README.md both name it.
RESTORE_PENDING = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='state-backend', description=__doc__)
    _ = parser.add_argument('--kdbx', type=Path, default=None, help='the cluster KeePassXC database')
    _ = parser.add_argument(
        '--seed-entry',
        default=entries.SEEDS['b2'].entry,
        help='entry holding the B2 seed key',
    )
    _ = parser.add_argument(
        '--escrow',
        type=Path,
        default=None,
        help=f'the escrow registry (default: the {escrow.DIRECTORY}/ directory of this checkout)',
    )
    _ = parser.add_argument(
        '--compartment',
        default=None,
        help='OCI compartment (default: the one `conventions` names for the appliance)',
    )
    actions = parser.add_subparsers(dest='action', required=True)

    render = actions.add_parser('render', help='render the Ignition config without touching the cloud')
    _ = render.add_argument('--address', default='192.0.2.10', help='address to issue the server certificate for')

    provision_cmd = actions.add_parser(
        'provision',
        help='create the appliance, or converge everything around it',
        epilog=(
            'exit status: 0 the appliance is current and holds its state; '
            f'{RESTORE_PENDING} the box was replaced and the state is not back in it yet, so run '
            '`state-backend restore` with the dump this printed; '
            '1 the run failed — read its last words for whether it had already replaced the box, '
            'which is where the dump to restore is named.'
        ),
    )
    # Re-provision is the appliance's only apply path (state-backend.md §1),
    # and it is destructive by construction: a new box, a new dump key, and
    # whatever state was not in the last dump. `provision` decides for itself
    # whether that is needed by comparing the box to the commit; this flag is
    # for the case with no diff to find -- rotating the dump key, or replacing
    # a box that is broken in some way the metadata cannot show.
    _ = provision_cmd.add_argument(
        '--replace',
        action='store_true',
        help='rebuild even if the box already matches (rotates the dump key with it)',
    )
    # Drift is a reason to replace, not permission to, so the plain converge
    # reports what it found and stops and this is how an operator asks for
    # exactly that replacement. `--force` rather than `--yes` because `restore`
    # spells the same concept that way already, and because `--yes` elsewhere
    # -- `pulumi up --yes` -- promises to skip a prompt, which is a promise
    # this cannot keep: there is no prompt to skip.
    _ = provision_cmd.add_argument(
        '--force',
        action='store_true',
        help='replace the box that is running (the plain converge reports and stops)',
    )
    # The escape for a box that cannot answer at all: an unreachable machine or
    # a Postgres that will not start is the case the rebuild path is the
    # diagnosis for (state-backend.md §6), where refusing to replace without a
    # dump would leave nothing to do.
    _ = provision_cmd.add_argument(
        '--no-dump',
        action='store_true',
        help='replace without dumping first, accepting the loss of everything since the last nightly dump',
    )
    # The dump lands wherever the command was run unless this says otherwise,
    # and `provision` is run from the checkout: `.gitignore` covers the default
    # name so that a dump of every stack's state cannot be committed to a
    # public repository by the next `jj` command.
    _ = provision_cmd.add_argument(
        '--dump-output',
        type=Path,
        default=None,
        help=f'where to write that dump (default: ./{settings.NAME}-<UTC stamp>.dump.age)',
    )
    _ = provision_cmd.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle to take that dump over',
    )

    # Diagnosis only: the box is never configured by hand (state-backend.md
    # §1). Needs no offline database, so it does not ask for one.
    ssh_cmd = actions.add_parser('ssh', help='log in to the appliance for diagnosis')
    _ = ssh_cmd.add_argument('command', nargs='*', help='run this instead of a login shell')
    _ = actions.add_parser('pins', help='check the pinned artefacts against their digests')

    bundle = actions.add_parser('bundle', help='write a client bundle (ca/cert/key/url)')
    _ = bundle.add_argument('name', choices=['ci', 'operator'])
    _ = bundle.add_argument('--address', required=True)
    _ = bundle.add_argument('--directory', type=Path, default=workstation.bundle_dir())

    dump = actions.add_parser('dump', help='take a dump of the live state, encrypted like the nightly one')
    _ = dump.add_argument(
        '--output',
        type=Path,
        default=None,
        help=f'where to write it (default: ./{settings.NAME}-<UTC stamp>.dump.age)',
    )
    _ = dump.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to dump over',
    )

    restore = actions.add_parser('restore', help='feed a dump into a provisioned box')
    _ = restore.add_argument('dump', type=Path, help='an age-encrypted dump, or a raw pg_dump custom-format archive')
    # The unattended drill (§7.3) runs in the ops repo with one age key in a
    # repository secret and no kit at all, so naming a key here has to be
    # enough on its own -- including for opening the kit this run then never
    # touches.
    _ = restore.add_argument(
        '--identity-file',
        type=Path,
        default=None,
        help='decrypt with the age identity in this file instead of opening the escrow',
    )
    _ = restore.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to restore over',
    )
    _ = restore.add_argument(
        '--force',
        action='store_true',
        help='restore even though the target backend already serves stacks',
    )

    return parser


def _rebuild_reasons(
    roots: config.Roots,
    session: b2.Session,
    existing: object,
    *,
    address: str,
    bucket_id: str,
    replace: bool,
) -> list[str]:
    """Why the running box is not the box this commit describes.

    Asked only of a box that exists, so an empty list means one thing: this
    one matches and nothing has to happen. Provision applies the current
    commit, so anything the repository changed -- the Butane file, an operator
    key, a pinned image, the address the server certificate is issued for --
    makes the box stale in exactly the same way, and the dump key is one
    component among them rather than a special case.

    The comparison is per component (config.digests), so the reason a box is
    being replaced names what changed instead of asserting that something did.

    One reason is not a comparison against the repository at all: the server
    certificate's remaining life. Every digested component is re-derived here
    and is therefore always young, so an expiry the box is walking towards is
    invisible to equality; what the box records is when its own certificate
    dies, and `config.renewal_due` reads that against the clock.
    """
    if replace:
        return ['--replace was asked for']

    recorded = provision.instance_config(existing)
    reasons: list[str] = []
    if not b2.dump_key_is_current(session, recorded.dump_key_id, bucket_id=bucket_id):
        # The box cannot be handed a new key without being rebuilt: the
        # secret only exists inside the Ignition it booted with.
        held = recorded.dump_key_id or 'none recorded'
        reasons.append(f'the dump key the box holds ({held}) is not the intended one')

    intended = config.digests(roots, address=address, dump_key_id=recorded.dump_key_id, bucket_id=bucket_id)
    changed = config.drift(intended, recorded.digests)
    if changed and not recorded.digests:
        reasons.append('the box predates this bookkeeping, so what it was built from cannot be compared')
    elif changed:
        reasons.append(f'the machine definition changed: {", ".join(changed)}')
    expiring = config.renewal_due(recorded.server_cert_expiry)
    if expiring is not None:
        reasons.append(expiring)
    return reasons


def _launch_box(
    clients: provision.OciClients,
    roots: config.Roots,
    *,
    dump_key: b2.AppKey,
    placement: provision.Placement,
    nsg_id: str,
    reserved: provision.ReservedAddress,
    bucket_id: str,
) -> str:
    """Render this commit's machine around `dump_key` and launch it. Returns the instance id.

    The push half of the dump key's delivery: the key's only consumer is the
    box, and the box comes into being holding it, because B2 discloses an
    application key's secret once and a box launched without it can never be
    handed one afterwards.
    """
    log.info('rendering the Ignition config for %s', reserved.address)
    # One machine, rendered once: the Ignition the box boots with and the
    # expiry recorded beside it have to describe the same certificate, and
    # a second `config.machine` call would issue a second one.
    built = config.machine(
        roots,
        address=reserved.address,
        dump_key_id=dump_key.key_id,
        dump_key=dump_key.key,
        bucket_id=bucket_id,
    )
    ignition = config.render_ignition(built)
    log.info('[6/7] converging the custom image — a release not imported yet takes the better part of an hour')
    image_id = provision.ensure_image(clients)
    log.info('[7/7] launching the instance')
    return provision.ensure_instance(
        clients,
        subnet_id=placement.subnet_id,
        nsg_id=nsg_id,
        image_id=image_id,
        ignition=ignition,
        digests=config.digests(roots, address=reserved.address, dump_key_id=dump_key.key_id, bucket_id=bucket_id),
        dump_key_id=dump_key.key_id,
        server_cert_expiry=config.expires_at(built),
    )


def _provision(
    store: KdbxStore,
    *,
    seed_entry: str,
    compartment: str | None,
    replace: bool,
    force: bool,
    dump: bool,
    dump_output: Path | None,
    bundle_dir: Path,
    registry: escrow.Registry,
) -> int:
    # Each stage says what it is starting, not only what it finished: the
    # image import and the first boot are minutes-long, and a log that only
    # speaks on success is indistinguishable from a hang while they run.
    log.info('[1/7] authorizing with OCI, and looking for a box that already exists')
    clients = provision.OciClients.load(compartment)
    existing = provision.find_instance(clients)

    # Ahead of everything else because whether a box is running is what
    # decides whether this run may generate roots at all: on a live appliance,
    # a label the escrow cannot answer for means the wrong escrow rather than
    # a first run (config.Roots.ensure).
    log.info('[2/7] opening the escrow with the kit')
    roots = config.Roots.ensure(escrow.Vault.open(store, registry), appliance_exists=existing is not None)

    log.info('[3/7] authorizing with B2, then converging bucket %s', settings.B2_BUCKET)
    session = b2.Session.from_entry(store, seed_entry)
    bucket_id = b2.ensure_bucket(
        session,
        settings.B2_BUCKET,
        prefix=settings.B2_PREFIX,
        retention_days=settings.B2_RETENTION_DAYS,
    )

    log.info('[4/7] converging the OCI network: VCN, subnet, gateway, security group, reserved address')
    placement = provision.ensure_network(clients)
    nsg_id = provision.ensure_security_group(clients, placement.vcn_id)
    reserved = provision.ensure_reserved_ip(clients)
    log.info('appliance address: %s', reserved.address)

    log.info('[5/7] comparing the running box against this commit')
    reasons = (
        []
        if existing is None
        else _rebuild_reasons(roots, session, existing, address=reserved.address, bucket_id=bucket_id, replace=replace)
    )
    # The dump this run took of the box it destroyed, for the closing
    # instruction. `None` covers both the run that destroyed nothing and the
    # `--no-dump` run, which have different last words.
    taken: Path | None = None
    # Set the moment the old box starts going away, not when the decision is
    # made: everything after that point owes the operator the closing
    # instruction, including the paths that raise. The `finally` below is what
    # makes that true of every exit, which is what lets the README promise
    # that silence means the old box is still serving.
    destroyed = False
    announced = False
    try:
        if existing is not None and not reasons:
            log.info('appliance %s matches the repository; nothing to rebuild', existing.id)
            instance_id = str(existing.id)
        else:
            if existing is not None:
                for reason in reasons:
                    log.warning('%s', reason)
                if not (replace or force):
                    log.error('%s would be replaced, and nothing has been changed', existing.id)
                    log.error(
                        "a replacement destroys the box holding every stack's state, and its boot volume with it: "
                        're-run with --force to replace this one, or --replace to rebuild a box that matches'
                    )
                    return 1
                if dump:
                    taken = _dump_before_replacing(roots, output=dump_output, bundle_dir=bundle_dir)
                    if taken is None:
                        return 1
                else:
                    log.warning('--no-dump: replacing without a dump, so everything since the nightly one is lost')
                log.warning('replacing %s — 5432 goes away until the new box answers', existing.id)
                destroyed = True
                provision.terminate_instance(clients, str(existing.id))
                provision.forget_host_key(reserved.address)
            # Minting is deliberately on this side of the branch. B2 returns an
            # application key's secret once, so the box's copy cannot be read back
            # and re-used, and minting a replacement revokes what the box is
            # holding: on a run that then leaves the instance alone that breaks
            # the nightly dump silently, until it next fires. The key's lifetime
            # is the instance's.
            log.info('minting the dump key the new box will hold')
            pending = b2.mint_dump_key(session, bucket_id=bucket_id)
            # Launching the box is this credential's push, so it runs through
            # `deliver` and the predecessor is retired only once the box holding
            # the successor exists -- the order every mint in that package has
            # (`credentials/delivery.py`).
            _, instance_id = pending.deliver(
                lambda dump_key: _launch_box(
                    clients,
                    roots,
                    dump_key=dump_key,
                    placement=placement,
                    nsg_id=nsg_id,
                    reserved=reserved,
                    bucket_id=bucket_id,
                )
            )
        provision.attach_reserved_ip(clients, instance_id=instance_id, public_ip_id=reserved.id)

        slot = workstation.bundle_dir()
        config.write_client_bundle(config.client_bundle(roots.ca, name='operator', address=reserved.address), slot)
        log.info('operator certificate bundle written to %s', slot)

        if not provision.wait_for_backend(reserved.address):
            log.error(
                'the backend did not answer on %s:%d — ssh core@%s to look',
                reserved.address,
                settings.PORT,
                reserved.address,
            )
            return 1
        log.info('backend answering on %s:%d', reserved.address, settings.PORT)
        if destroyed:
            announced = True
            return _restore_pending(taken)
        return 0
    finally:
        # The stretch above is minutes to an hour long — an image import, a
        # first boot — and every step of it can raise: B2 refusing the mint,
        # OCI refusing the launch, the import timing out. The box is already
        # gone by then and the state is in one file, so a run that leaves this
        # way says so before the traceback does.
        if destroyed and not announced:
            _ = _restore_pending(taken)


def _restore_pending(taken: Path | None) -> int:
    """The last word of a run that replaced the box: the state is not back yet.

    The new box initdb'd an empty data directory, so a `pulumi` run against it
    reads a backend serving nothing and acts on that. Naming the command is
    the point — the operator has just watched an image import and a first boot,
    and what they are holding is half of an operation.
    """
    if taken is None:
        log.warning('the box was replaced without a dump; the state comes from the newest object in B2:')
        log.warning('    state-backend restore <that object>')
    else:
        log.warning('the new box serves an empty database until the dump this run took goes back into it:')
        log.warning('    state-backend restore %s', taken)
    return RESTORE_PENDING


def _dump_before_replacing(roots: config.Roots, *, output: Path | None, bundle_dir: Path) -> Path | None:
    """Dump the box about to be destroyed, or answer None having destroyed nothing.

    The nightly timer leaves a window of up to a day, and a rebuild throws
    away whatever is in it; this closes that window without the operator
    having to remember to. It is a precondition rather than best effort — a
    replacement whose dump did not happen is the loss the whole step exists to
    prevent — so a failure here stops the run with the box still standing.
    `--no-dump` is how an operator says the box cannot be dumped at all and
    the loss is accepted.

    The dump goes over a client bundle that authenticates against the box
    being replaced — by default the workstation slot, which holds exactly
    that: same CA, same address.
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    log.info('dumping the running box before it is replaced, into %s', destination)
    try:
        _write_dump(destination, bundle_dir=bundle_dir, recipients=roots.age_recipients)
    except StateError as exc:
        log.error('the dump of the running box failed: %s', exc)
        log.error('nothing has been destroyed; the box is still serving')
        log.error(
            'a missing or stale bundle is `state-backend bundle operator --address <ip>`; '
            '--no-dump replaces a box that cannot be dumped at all, and loses what is not in the nightly object'
        )
        return None
    return destination


def _refuse_to_overwrite(destination: Path) -> None:
    """A dump never lands on top of one.

    Asked twice: by `_dump` before it opens the escrow, so a name clash costs
    no kit password, and by the writer itself, which has a second caller.
    """
    if destination.exists():
        raise StateError(f'{destination} already exists; a dump never overwrites one')


def _write_dump(destination: Path, *, bundle_dir: Path, recipients: Sequence[str]) -> None:
    """`pg_dump -Fc` under age, verified before the file is called a dump.

    The single writer of the operator-side artefact, behind both commands that
    produce one — `state-backend dump`, and the converge dumping a box it is
    about to destroy — so the two cannot drift into producing different files.
    """
    _refuse_to_overwrite(destination)
    target = state.connection(bundle_dir)
    # The plaintext archive never lands beside the encrypted one: it is the
    # whole state in the clear, and it exists only for as long as the two
    # steps that read it. `TemporaryDirectory` makes it 0700.
    with tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-') as tmp:
        archive = Path(tmp) / 'state.dump'
        log.info('dumping the live state over the client bundle in %s', bundle_dir)
        state.pg_dump(target, archive)
        log.info('verifying the archive before calling it a dump')
        _ = state.verify_dump(archive)
        log.info('encrypting the dump')
        state.encrypt(archive, destination, recipients)
    log.info(
        '%s holds %.1f MiB, readable by the %d escrowed recipient(s) the appliance encrypts to',
        destination,
        destination.stat().st_size / 2**20,
        len(recipients),
    )


def _dump(store: KdbxStore, *, registry: escrow.Registry, bundle_dir: Path, output: Path | None) -> int:
    """A dump on demand, in the form the appliance's own timer writes.

    Encrypted to the escrow's recipients rather than left in plain text, and
    listed before it is called a dump: an archive naming no table is a dump of
    a database that has lost its state — what a replaced box holds until its
    restore — and the operator taking one is usually about to destroy the box
    it came from (§7.2).
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    _refuse_to_overwrite(destination)

    log.info('[1/2] opening the escrow with the kit, for the recipients the appliance encrypts its dumps to')
    recipients = config.age_recipients(escrow.Vault.open(store, registry))
    log.info('[2/2] taking the dump')
    _write_dump(destination, bundle_dir=bundle_dir, recipients=recipients)
    return 0


def _served(target: state.Connection) -> list[str]:
    """The stacks the backend serves, or nothing if it cannot answer at all.

    Used before a restore, where a backend that refuses the question is the
    ordinary case: a box provisioned minutes ago has an empty database that
    no `pulumi` has ever written a layout into. So this only ever reports a
    positive answer, and the caller's guard only ever fires on one.
    """
    try:
        return state.stacks(target)
    except StateError as exc:
        log.info('the backend cannot list stacks yet (%s); a box provisioned minutes ago cannot either', exc)
        return []


def _restore(
    store: KdbxStore | None,
    *,
    registry: escrow.Registry,
    bundle_dir: Path,
    source: Path,
    identity: Path | None,
    force: bool,
) -> int:
    """Feed a dump into a provisioned box, and prove afterwards that it took.

    The order is the one that makes each failure cheap: refuse to overwrite a
    populated backend before anything is decrypted, verify the archive before
    it touches the database, restore in one transaction, and only then
    report — by asking `pulumi` what the backend now serves.
    """
    target = state.connection(bundle_dir)
    log.info('[1/5] asking the target backend what it already holds')
    occupied = _served(target)
    if occupied and not force:
        log.error('%s already serves %d stack(s): %s', state.endpoint(target.url), len(occupied), ', '.join(occupied))
        log.error('restoring over live state is `--force`; a rebuild restores into a box that has none')
        return 1
    if occupied:
        log.warning(
            '--force: restoring over the %d stack(s) %s already serves', len(occupied), state.endpoint(target.url)
        )

    with tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-') as tmp:
        archive = source
        if state.encrypted(source):
            archive = Path(tmp) / 'state.dump'
            if identity is not None:
                log.info('[2/5] decrypting with the identity in %s', identity)
                identities = state.identity_file(identity)
            else:
                if store is None:  # pragma: no cover - main opens a kit whenever there is no identity file
                    raise StateError('no kit and no --identity-file: nothing can open this dump')
                log.info('[2/5] opening the escrow with the kit, for the identities the dump may be under')
                identities = config.backup_identities(escrow.Vault.open(store, registry))
            state.decrypt(source, archive, identities)
        else:
            log.info('[2/5] %s is a plain archive; nothing to decrypt', source)
        log.info('[3/5] verifying the archive before it touches the database')
        _ = state.verify_dump(archive)
        log.info('[4/5] restoring over the client bundle in %s', bundle_dir)
        state.pg_restore(target, archive)

    log.info('[5/5] verifying: a restore is done when pulumi can log in to what it restored')
    restored = state.stacks(target)
    if not restored:
        log.error('%s serves no stacks after the restore, so the state did not arrive', state.endpoint(target.url))
        return 1
    log.info('the restored backend serves %d stack(s): %s', len(restored), ', '.join(restored))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)

    # One dispatch, and the kit is opened by the arms that need it rather than
    # before them: `pins`, `ssh` and a drill's `restore --identity-file` run on
    # machines that have no offline database, and asking for one would fail
    # before the command started.
    def kit() -> KdbxStore:
        return KdbxStore.from_env(args.kdbx)

    def registry() -> escrow.Registry:
        return escrow.Registry.open(args.escrow)

    try:
        match args.action:
            case 'pins':
                return 0 if provision.verify_pins() else 1
            case 'ssh':
                provision.ssh(provision.OciClients.load(args.compartment), args.command)
            case 'restore' if args.identity_file is not None:
                return _restore(
                    None,
                    registry=registry(),
                    bundle_dir=args.bundle,
                    source=args.dump,
                    identity=args.identity_file,
                    force=args.force,
                )
            case 'render':
                store = kit()
                print(
                    config.render_ignition(
                        config.machine(
                            config.Roots.recover(escrow.Vault.open(store, registry())),
                            address=args.address,
                            dump_key_id='rendered-without-a-key',
                            dump_key='rendered-without-a-key',
                            bucket_id='rendered-without-a-bucket',
                        )
                    )
                )
                return 0
            case 'provision':
                return _provision(
                    kit(),
                    seed_entry=args.seed_entry,
                    compartment=args.compartment,
                    replace=args.replace,
                    force=args.force,
                    dump=not args.no_dump,
                    dump_output=args.dump_output,
                    bundle_dir=args.bundle,
                    registry=registry(),
                )
            case 'bundle':
                store = kit()
                config.write_client_bundle(
                    config.client_bundle(
                        pki.Authority.from_pem(escrow.Vault.open(store, registry()).recover(escrow.CA)),
                        name=args.name,
                        address=args.address,
                    ),
                    args.directory,
                )
                return 0
            case 'dump':
                return _dump(kit(), registry=registry(), bundle_dir=args.bundle, output=args.output)
            case 'restore':
                return _restore(
                    kit(),
                    registry=registry(),
                    bundle_dir=args.bundle,
                    source=args.dump,
                    identity=None,
                    force=args.force,
                )
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled action {args.action}')
    except (KdbxError, EscrowError, AgeError, StateError, WorkstationError) as exc:
        log.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
