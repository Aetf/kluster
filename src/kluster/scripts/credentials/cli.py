"""`credentials` — the executable form of the credential register.

One subcommand per credential family (docs/credentials.md §4): mint, push to
every slot, verify. The same command is what a rotation playbook runs, so
rotation is a re-run rather than a second procedure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import b2, seeds
from .kdbx import PATH_ENV, KdbxError, KdbxStore

log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='credentials', description=__doc__)
    _ = parser.add_argument(
        '--kdbx',
        type=Path,
        default=None,
        help=f'the cluster KeePassXC database (default: ${PATH_ENV})',
    )
    families = parser.add_subparsers(dest='family', required=True)

    kdbx_cmd = families.add_parser('kdbx', help='inspect the offline store')
    kdbx_actions = kdbx_cmd.add_subparsers(dest='action', required=True)
    ls = kdbx_actions.add_parser('ls', help='list entry paths')
    _ = ls.add_argument('group', nargs='?', default='/')
    show = kdbx_actions.add_parser('show', help="an entry's non-secret attributes")
    _ = show.add_argument('entry')

    seed_cmd = families.add_parser('seed', help='the root seed behind every derived secret')
    _ = seed_cmd.add_argument('--entry', default=seeds.ROOT_ENTRY, help='entry holding the root seed')
    seed_actions = seed_cmd.add_subparsers(dest='action', required=True)
    _ = seed_actions.add_parser('init', help='create the root seed (bring-up, once)')

    b2_cmd = families.add_parser('b2', help='the B2 seed key')
    _ = b2_cmd.add_argument('--seed-entry', default='seeds/B2 seed key', help='entry holding the seed key')
    b2_actions = b2_cmd.add_subparsers(dest='action', required=True)
    create = b2_actions.add_parser(
        'create-seed',
        help='create the seed from the account master key (bring-up, or seed loss)',
    )
    _ = create.add_argument(
        '--master-entry',
        required=True,
        help='entry holding the account master key (id as username, key as password)',
    )
    _ = b2_actions.add_parser('rotate-seed', help='have the seed mint and install its successor')

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = _parser().parse_args(argv)

    try:
        store = KdbxStore.from_env(args.kdbx)

        match (args.family, args.action):
            case ('kdbx', 'ls'):
                for entry in store.entries(args.group):
                    print(entry)
            case ('kdbx', 'show'):
                for name, value in store.describe(args.entry).items():
                    print(f'{name}: {value}')
            case ('seed', 'init'):
                seeds.init_root(store, args.entry)
            case ('b2', 'create-seed'):
                _ = b2.create_seed(store, master_entry=args.master_entry, seed_entry=args.seed_entry)
            case ('b2', 'rotate-seed'):
                _ = b2.rotate_seed(store, seed_entry=args.seed_entry)
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.family} {args.action}')
    except (KdbxError, b2.CredentialRejected) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
