"""`state-backend` — provision, inspect, re-provision, dump and restore the appliance.

`provision` is idempotent end to end: it is equally the bring-up command and
the re-provision command, which is what keeps the rebuild path warm. `dump`
and `restore` are the other half of that path — every playbook that replaces
the box is a dump, a provision and a restore (physical/state-backend.md §7),
and each of them verifies rather than reports.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from kluster.scripts.credentials import b2, entries, escrow, pki, workstation
from kluster.scripts.credentials.age import AgeError
from kluster.scripts.credentials.escrow import EscrowError
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.workstation import WorkstationError

from . import config, provision, settings, state
from .state import StateError

log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
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

    provision_cmd = actions.add_parser('provision', help='create the appliance, or converge everything around it')
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


def _drift(
    roots: config.Roots,
    session: b2.Session,
    existing: object | None,
    *,
    address: str,
    bucket_id: str,
    replace: bool,
) -> list[str]:
    """Why the running box is not the box this commit describes.

    An empty list is the skip condition, and it is the *only* one: provision
    applies the current commit, so anything the repository changed -- the
    Butane file, an operator key, a pinned image, the address the server
    certificate is issued for -- makes the box stale in exactly the same way,
    and the dump key is one component among them rather than a special case.

    The comparison is per component (config.digests), so the reason a box is
    being replaced names what changed instead of asserting that something did.
    """
    if existing is None:
        return []
    if replace:
        return ['--replace was asked for']

    recorded, dump_key_id = provision.instance_config(existing)
    reasons: list[str] = []
    if not b2.dump_key_is_current(
        session, dump_key_id, bucket_id=bucket_id, prefix=settings.B2_PREFIX, name=settings.B2_DUMP_KEY_NAME
    ):
        # The box cannot be handed a new key without being rebuilt: the
        # secret only exists inside the Ignition it booted with.
        reasons.append(f'the dump key the box holds ({dump_key_id or "none recorded"}) is not the intended one')

    intended = config.digests(roots, address=address, dump_key_id=dump_key_id, bucket_id=bucket_id)
    changed = config.drift(intended, recorded)
    if changed and not recorded:
        reasons.append('the box predates this bookkeeping, so what it was built from cannot be compared')
    elif changed:
        reasons.append(f'the machine definition changed: {", ".join(changed)}')
    return reasons


def _provision(
    store: KdbxStore,
    *,
    seed_entry: str,
    compartment: str | None,
    replace: bool,
    registry: escrow.Registry,
) -> int:
    # Each stage says what it is starting, not only what it finished: the
    # image import and the first boot are minutes-long, and a log that only
    # speaks on success is indistinguishable from a hang while they run.
    log.info('[1/6] opening the escrow with the kit, and reading the B2 seed key out of it')
    roots = config.Roots.ensure(escrow.Vault.open(store, registry))

    log.info('[2/6] authorizing with B2, then converging bucket %s', settings.B2_BUCKET)
    session = b2.Session.from_entry(store, seed_entry)
    bucket_id = b2.ensure_bucket(
        session,
        settings.B2_BUCKET,
        prefix=settings.B2_PREFIX,
        retention_days=settings.B2_RETENTION_DAYS,
    )

    log.info('[3/6] converging the OCI network: VCN, subnet, gateway, security group, reserved address')
    client = provision.Oci.load(compartment)
    vcn_id, subnet_id = provision.ensure_network(client)
    nsg_id = provision.ensure_security_group(client, vcn_id)
    public_ip_id, address = provision.ensure_reserved_ip(client)
    log.info('appliance address: %s', address)

    log.info('[4/6] comparing the running box against this commit')
    existing = provision.find_instance(client)
    reasons = _drift(roots, session, existing, address=address, bucket_id=bucket_id, replace=replace)
    if existing is not None and not reasons:
        log.info('appliance %s matches the repository; nothing to rebuild', existing.id)
        instance_id = str(existing.id)
    else:
        if existing is not None:
            for reason in reasons:
                log.warning('%s', reason)
            log.warning('replacing %s — 5432 goes away until the new box answers', existing.id)
            provision.terminate_instance(client, str(existing.id))
            provision.forget_host_key(address)
        # Minting is deliberately on this side of the branch. B2 returns an
        # application key's secret once, so the box's copy cannot be read back
        # and re-used, and minting a replacement revokes what the box is
        # holding: on a run that then leaves the instance alone that breaks
        # the nightly dump silently, until it next fires. The key's lifetime
        # is the instance's.
        log.info('minting the dump key the new box will hold')
        dump_key_id, dump_key = b2.mint_dump_key(
            session, bucket_id=bucket_id, prefix=settings.B2_PREFIX, name=settings.B2_DUMP_KEY_NAME
        )
        log.info('rendering the Ignition config for %s', address)
        ignition = config.render_ignition(
            roots, address=address, dump_key_id=dump_key_id, dump_key=dump_key, bucket_id=bucket_id
        )
        log.info('[5/6] converging the custom image — a release not imported yet takes the better part of an hour')
        image_id = provision.ensure_image(client)
        log.info('[6/6] launching the instance')
        instance_id = provision.ensure_instance(
            client,
            subnet_id=subnet_id,
            nsg_id=nsg_id,
            image_id=image_id,
            ignition=ignition,
            digests=config.digests(roots, address=address, dump_key_id=dump_key_id, bucket_id=bucket_id),
            dump_key_id=dump_key_id,
        )
    provision.attach_reserved_ip(client, instance_id=instance_id, public_ip_id=public_ip_id)

    bundle_dir = workstation.bundle_dir()
    config.write_client_bundle(config.client_bundle(roots.ca, name='operator', address=address), bundle_dir)
    log.info('operator certificate bundle written to %s', bundle_dir)

    if not provision.wait_for_backend(address):
        log.error('the backend did not answer on %s:%d — ssh core@%s to look', address, settings.PORT, address)
        return 1
    log.info('backend answering on %s:%d', address, settings.PORT)
    return 0


def _dump(store: KdbxStore, *, registry: escrow.Registry, bundle_dir: Path, output: Path | None) -> int:
    """A dump on demand, in the form the appliance's own timer writes.

    Encrypted to the escrow's recipients rather than left in plain text, and
    verified before it is called a dump: the operator taking one is usually
    about to destroy the box it came from (§7.2), which is the worst moment
    to learn that a file of the right size has no readable contents.
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    if destination.exists():
        raise StateError(f'{destination} already exists; a dump never overwrites one')
    target = state.connection(bundle_dir)

    log.info('[1/4] opening the escrow with the kit, for the recipients the appliance encrypts its dumps to')
    recipients = config.age_recipients(escrow.Vault.open(store, registry))

    # The plaintext archive never lands beside the encrypted one: it is the
    # whole state in the clear, and it exists only for as long as the two
    # steps that read it. `TemporaryDirectory` makes it 0700.
    with tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-') as tmp:
        archive = Path(tmp) / 'state.dump'
        log.info('[2/4] dumping the live state over the client bundle in %s', bundle_dir)
        state.pg_dump(target, archive)
        log.info('[3/4] verifying the archive before calling it a dump')
        _ = state.verify_dump(archive)
        log.info('[4/4] encrypting the dump')
        state.encrypt(archive, destination, recipients)
    log.info(
        '%s holds %.1f MiB, readable by the %d escrowed recipient(s) the appliance encrypts to',
        destination,
        destination.stat().st_size / 2**20,
        len(recipients),
    )
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
    args = _parser().parse_args(argv)

    try:
        if args.action == 'pins':
            return 0 if provision.verify_pins() else 1
        if args.action == 'ssh':
            return provision.ssh(provision.Oci.load(args.compartment), args.command)
        if args.action == 'restore' and args.identity_file is not None:
            # The drill machine holds one age key and no kit; asking for a
            # database it does not have would fail before the restore starts.
            return _restore(
                None,
                registry=escrow.Registry.open(args.escrow),
                bundle_dir=args.bundle,
                source=args.dump,
                identity=args.identity_file,
                force=args.force,
            )

        store = KdbxStore.from_env(args.kdbx)
        registry = escrow.Registry.open(args.escrow)
        match args.action:
            case 'render':
                print(
                    config.render_ignition(
                        config.Roots.recover(escrow.Vault.open(store, registry)),
                        address=args.address,
                        dump_key_id='rendered-without-a-key',
                        dump_key='rendered-without-a-key',
                        bucket_id='rendered-without-a-bucket',
                    )
                )
            case 'provision':
                return _provision(
                    store,
                    seed_entry=args.seed_entry,
                    compartment=args.compartment,
                    replace=args.replace,
                    registry=registry,
                )
            case 'bundle':
                config.write_client_bundle(
                    config.client_bundle(
                        pki.Authority.from_pem(escrow.Vault.open(store, registry).recover(escrow.CA)),
                        name=args.name,
                        address=args.address,
                    ),
                    args.directory,
                )
            case 'dump':
                return _dump(store, registry=registry, bundle_dir=args.bundle, output=args.output)
            case 'restore':
                return _restore(
                    store,
                    registry=registry,
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
