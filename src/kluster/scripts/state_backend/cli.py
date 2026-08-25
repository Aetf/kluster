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

    _ = actions.add_parser('provision', help='create or converge the appliance')
    _ = actions.add_parser('pins', help='check the pinned artefacts against their digests')

    bundle = actions.add_parser('bundle', help='write a client bundle (ca/cert/key/url)')
    _ = bundle.add_argument('name', choices=['ci', 'operator'])
    _ = bundle.add_argument('--address', required=True)
    _ = bundle.add_argument('--directory', type=Path, default=DEFAULT_BUNDLE_DIR)

    return parser


def _provision(store: KdbxStore, *, seed_entry: str, compartment: str | None) -> int:
    # Each stage says what it is starting, not only what it finished: the
    # image import and the first boot are minutes-long, and a log that only
    # speaks on success is indistinguishable from a hang while they run.
    log.info('[1/6] reading the derivation seed and the B2 seed key')
    seed = seeds.load_seed(store)

    session = b2.Session.from_entry(store, seed_entry)
    log.info('[2/6] bucket %s and its dump key', settings.B2_BUCKET)
    bucket_id = b2.ensure_bucket(
        session,
        settings.B2_BUCKET,
        prefix=settings.B2_PREFIX,
        retention_days=settings.B2_RETENTION_DAYS,
    )
    dump_key_id, dump_key = b2.mint_dump_key(
        session, bucket_id=bucket_id, prefix=settings.B2_PREFIX, name=settings.B2_DUMP_KEY_NAME
    )

    client = provision.Oci.load(compartment)
    log.info('[3/6] OCI network: VCN, subnet, gateway, security group, reserved address')
    vcn_id, subnet_id = provision.ensure_network(client)
    nsg_id = provision.ensure_security_group(client, vcn_id)
    public_ip_id, address = provision.ensure_reserved_ip(client)
    log.info('appliance address: %s', address)

    log.info('[4/6] rendering Ignition for %s', address)
    ignition = config.render_ignition(
        seed, address=address, dump_key_id=dump_key_id, dump_key=dump_key, bucket_id=bucket_id
    )
    log.info('[5/6] custom image (imports on first run; several minutes)')
    image_id = provision.ensure_image(client)
    log.info('[6/6] instance')
    instance_id = provision.ensure_instance(
        client, subnet_id=subnet_id, nsg_id=nsg_id, image_id=image_id, ignition=ignition
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
                return _provision(store, seed_entry=args.seed_entry, compartment=args.compartment)
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
