"""`state-backend` — provision, inspect, and re-provision the appliance.

`provision` is idempotent end to end: it is equally the bring-up command and
the re-provision command, which is what keeps the rebuild path warm.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from kluster.scripts.credentials import b2, entries, seeds
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore

from . import config, provision, settings

log = logging.getLogger(__name__)

DEFAULT_BUNDLE_DIR = Path.home() / '.config' / 'kluster' / 'state-backend'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='state-backend', description=__doc__)
    _ = parser.add_argument('--kdbx', type=Path, default=None, help='the cluster KeePassXC database')
    _ = parser.add_argument(
        '--seed-entry',
        default=entries.SEEDS['b2'].entry,
        help='entry holding the B2 seed key',
    )
    _ = parser.add_argument('--compartment', default=None, help='OCI compartment (default: ~/.oci/config)')
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
    _ = bundle.add_argument('--directory', type=Path, default=DEFAULT_BUNDLE_DIR)

    return parser


def _drift(
    seed: bytes,
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

    intended = config.digests(seed, address=address, dump_key_id=dump_key_id, bucket_id=bucket_id)
    changed = config.drift(intended, recorded)
    if changed and not recorded:
        reasons.append('the box predates this bookkeeping, so what it was built from cannot be compared')
    elif changed:
        reasons.append(f'the machine definition changed: {", ".join(changed)}')
    return reasons


def _provision(store: KdbxStore, *, seed_entry: str, compartment: str | None, replace: bool) -> int:
    # Each stage says what it is starting, not only what it finished: the
    # image import and the first boot are minutes-long, and a log that only
    # speaks on success is indistinguishable from a hang while they run.
    log.info('[1/6] reading the derivation seed and the B2 seed key')
    seed = seeds.load_seed(store)

    session = b2.Session.from_entry(store, seed_entry)
    log.info('[2/6] bucket %s', settings.B2_BUCKET)
    bucket_id = b2.ensure_bucket(
        session,
        settings.B2_BUCKET,
        prefix=settings.B2_PREFIX,
        retention_days=settings.B2_RETENTION_DAYS,
    )

    client = provision.Oci.load(compartment)
    log.info('[3/6] OCI network: VCN, subnet, gateway, security group, reserved address')
    vcn_id, subnet_id = provision.ensure_network(client)
    nsg_id = provision.ensure_security_group(client, vcn_id)
    public_ip_id, address = provision.ensure_reserved_ip(client)
    log.info('appliance address: %s', address)

    existing = provision.find_instance(client)
    reasons = _drift(seed, session, existing, address=address, bucket_id=bucket_id, replace=replace)
    if existing is not None and not reasons:
        log.info('[4/6] appliance %s matches the repository; nothing to do', existing.id)
        instance_id = str(existing.id)
    else:
        if existing is not None:
            for reason in reasons:
                log.warning('[4/6] %s', reason)
            log.warning('[4/6] replacing %s — 5432 goes away until the new box answers', existing.id)
            provision.terminate_instance(client, str(existing.id))
            provision.forget_host_key(address)
        # Minting is deliberately on this side of the branch. B2 returns an
        # application key's secret once, so the box's copy cannot be read back
        # and re-used, and minting a replacement revokes what the box is
        # holding: on a run that then leaves the instance alone that breaks
        # the nightly dump silently, until it next fires. The key's lifetime
        # is the instance's.
        log.info('[4/6] minting the dump key and rendering Ignition for %s', address)
        dump_key_id, dump_key = b2.mint_dump_key(
            session, bucket_id=bucket_id, prefix=settings.B2_PREFIX, name=settings.B2_DUMP_KEY_NAME
        )
        ignition = config.render_ignition(
            seed, address=address, dump_key_id=dump_key_id, dump_key=dump_key, bucket_id=bucket_id
        )
        log.info('[5/6] custom image (imports on first run; several minutes)')
        image_id = provision.ensure_image(client)
        log.info('[6/6] instance')
        instance_id = provision.ensure_instance(
            client,
            subnet_id=subnet_id,
            nsg_id=nsg_id,
            image_id=image_id,
            ignition=ignition,
            digests=config.digests(seed, address=address, dump_key_id=dump_key_id, bucket_id=bucket_id),
            dump_key_id=dump_key_id,
        )
    provision.attach_reserved_ip(client, instance_id=instance_id, public_ip_id=public_ip_id)

    config.write_client_bundle(config.client_bundle(seed, name='operator', address=address), DEFAULT_BUNDLE_DIR)
    log.info('operator certificate bundle written to %s', DEFAULT_BUNDLE_DIR)

    if not provision.wait_for_backend(address):
        log.error('the backend did not answer on %s:%d — ssh core@%s to look', address, settings.PORT, address)
        return 1
    log.info('backend answering on %s:%d', address, settings.PORT)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = _parser().parse_args(argv)

    try:
        if args.action == 'pins':
            return 0 if provision.verify_pins() else 1
        if args.action == 'ssh':
            return provision.ssh(provision.Oci.load(args.compartment), args.command)

        store = KdbxStore.from_env(args.kdbx)
        match args.action:
            case 'render':
                print(
                    config.render_ignition(
                        seeds.load_seed(store),
                        address=args.address,
                        dump_key_id='rendered-without-a-key',
                        dump_key='rendered-without-a-key',
                        bucket_id='rendered-without-a-bucket',
                    )
                )
            case 'provision':
                return _provision(store, seed_entry=args.seed_entry, compartment=args.compartment, replace=args.replace)
            case 'bundle':
                config.write_client_bundle(
                    config.client_bundle(seeds.load_seed(store), name=args.name, address=args.address),
                    args.directory,
                )
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled action {args.action}')
    except KdbxError as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
